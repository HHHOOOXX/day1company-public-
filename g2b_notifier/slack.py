"""콘솔 미리보기 출력 + Slack 메시지 포맷팅/발송."""

import os
import time
from datetime import date, datetime

import requests


def print_daily_digest(items: list):
    """슬랙 발송 전 미리보기용 콘솔 출력."""
    if not items:
        print("\n오늘 알림 보낼 신규 공고가 없습니다.")
        return

    print(f"\n===== 슬랙 발송 대상 공고 {len(items)}건 =====")
    for idx, item in enumerate(items, 1):
        print(f"{idx:2d}. [{item.get('ntceInsttNm', '')}] {item.get('bidNtceNm', '')}")
        print(f"     공고일시: {item.get('bidNtceDt', '')}  |  마감: {item.get('bidClseDt', '')}")
        print(f"     링크: {item.get('bidNtceDtlUrl', '')}")


def deadline_date(item: dict):
    """item의 마감/의견마감/신청기간 필드("YYYY-MM-DD HH:MM:SS" 단일값 또는
    "YYYY-MM-DD ~ YYYY-MM-DD" 범위값)에서 마감 판단 기준일(date)을 뽑는다.
    범위값이면 종료일 기준. 파싱 불가/미정이면 None."""
    raw = (item.get("bidClseDt") or "").strip()
    if not raw or raw == "미정":
        return None
    last_part = raw.split("~")[-1].strip()
    try:
        return datetime.strptime(last_part[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def dday_parts(item: dict, today):
    """(date_str, days_left, is_passed) 반환.
    date_str: 'M/D' 형식(파싱 안 되면 원문/'미정'). days_left: 오늘 기준 D-day 정수, 파싱 불가면 None.
    is_passed: 이미 마감 지났는지."""
    d = deadline_date(item)
    if d is None:
        raw = (item.get("bidClseDt") or "").strip() or "미정"
        return raw, None, False
    delta = (d - today).days
    return f"{d.month}/{d.day}", delta, delta < 0


def format_money(raw) -> str:
    """원화 정수(문자열)를 '9,972만원'/'4.2억원' 형태로. 값이 없거나 0 이하면 '-'."""
    try:
        won = int(str(raw).strip())
    except (TypeError, ValueError):
        return "-"
    if won <= 0:
        return "-"
    if won >= 100_000_000:
        eok = won / 100_000_000
        eok_str = f"{eok:.1f}".rstrip("0").rstrip(".")
        return f"{eok_str}억원"
    man = won // 10_000
    if man <= 0:
        return f"{won:,}원"
    return f"{man:,}만원"


def format_star_reminder(star_postings: list, today) -> str:
    """이미 발송됐지만 마감이 안 지난, 예전에 함께 일한 기관의 공고를 매일 다시 안내하는 섹션.
    star_postings: db.get_active_star_postings()가 반환하는 postings 테이블 row dict 리스트.
    2026-09-22 피드백: "확실후보" 같은 내부 용어 없이, 팀원이 바로 이해할 수 있는 문구로 정리."""
    if not star_postings:
        return ""

    def _row_deadline(row):
        raw = (row.get("close_at") or "").strip()
        if not raw or raw == "미정":
            return None
        last_part = raw.split("~")[-1].strip()
        try:
            return datetime.strptime(last_part[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None

    rows = [r for r in star_postings if _row_deadline(r) is None or _row_deadline(r) >= today]
    rows.sort(key=lambda r: (_row_deadline(r) is None, _row_deadline(r) or date.max))
    if not rows:
        return ""

    lines = [f"*\U0001F4CC 예전에 함께 일한 기관에서 새 공고가 떴어요 ({len(rows)}건)*", ""]
    for idx, row in enumerate(rows, 1):
        org = row.get("org", "")
        title = row.get("title", "")
        url = row.get("url", "")
        title_part = f"<{url}|{title}>" if url else title
        d = _row_deadline(row)
        if d is None:
            dday_text = f"`{row.get('close_at') or '미정'}`"
        else:
            delta = (d - today).days
            dday_text = f"{_urgency_emoji(delta)} `{d.month}/{d.day} (D-{delta})`" if delta > 0 else f"{_urgency_emoji(delta)} `{d.month}/{d.day} (D-day)`"
        lines.append(f"`#{idx}` *[{org}]* {title_part} — 마감 {dday_text}")

    return "\n".join(lines)


def _pick_top(entries: list, today, top_n: int) -> list:
    """카드/요약 섹션에 올릴 상위 top_n건을 [(deadline, tag, label, item), ...]로 고른다.
    마감이 지난 건만 빼고, 마감일이 있는 건을 급한 순으로 앞에, 마감일이 비어 있거나 파싱 안 되는
    건은 입력 순서 그대로 뒤에 붙인다.
    2026-09-29: 예전엔 마감일 없는 건을 통째로 버렸는데, 호출부(cli.py)가 이미 10건으로 잘라 넘긴
    뒤라 카드가 10건 -> 7건으로 줄고, 제안 이력 기관이라 1순위로 뽑힌 가천대 공고까지 사라졌다.
    협상계약 등은 bidClseDt가 비어 오는 경우가 있어 마감일 유무로 관심도를 판단할 수 없다."""
    return _rank_entries(entries, today)[:top_n]


# 사전규격 entries의 source_tag(cli.py). 사전규격 날짜는 입찰 마감이 아니라 규격서 의견등록 마감이다.
PRESPEC_TAG = "사전규격"


def _rank_entries(entries: list, today) -> list:
    """_pick_top의 정렬 규칙으로 전체를 줄 세운다(마감 지난 건 제외).
    순서: 입찰공고·기업마당·기관공고 → 사전규격, 각 묶음 안에서는 우선검토 → 마감 빠른 순 → 마감일 없는 건.
    2026-10-06: 우선검토(_priority_review) 건을 앞에 둔다 — 마감순으로만 다시 정렬하면 호출부에서 앞에 모아 둔
    우선검토 건이 뒤로 밀려 카드에서 빠진다.
    같은 날 요청: 사전규격을 입찰공고보다 뒤에 둔다. 사전규격의 의견마감은 거의 항상 7일 미만이라(9/22 확인 91%)
    마감순으로 섞으면 카드 맨 앞을 다 차지하고 메시지가 임박한 공고투성이로 보였다(10/2 재조회: 33건 중 18건)."""
    dated, undated = [], []
    for tag, label, item in entries:
        d = deadline_date(item)
        if d is None:
            undated.append((None, tag, label, item))
        elif d >= today:
            dated.append((d, tag, label, item))
    dated.sort(key=lambda x: x[0])
    ranked = dated + undated
    return sorted(ranked, key=lambda x: (x[1] == PRESPEC_TAG, not x[3].get("_priority_review")))


def split_top(entries: list, today, top_n: int):
    """카드로 보여줄 상위 top_n건과 카드에 못 들어간 나머지를 (top, rest)로 나눈다. 둘 다 (tag, label, item) 목록."""
    ranked = [(tag, label, item) for _d, tag, label, item in _rank_entries(entries, today)]
    return ranked[:top_n], ranked[top_n:]


def format_overflow_text(sections: list, today) -> str:
    """카드에 못 넣은 공고 목록 텍스트(스레드 댓글 또는 본문 맨 아래용).
    sections: [(섹션 제목, [(tag, label, item), ...]), ...] — 빈 섹션은 건너뛴다."""
    parts = []
    for title, entries in sections:
        if not entries:
            continue
        lines = [f"*{title} — 카드에 못 넣은 {len(entries)}건*"]
        for idx, (tag, label, item) in enumerate(entries, 1):
            d = deadline_date(item)
            url = item.get("bidNtceDtlUrl", "")
            item_title = item.get("bidNtceNm", "")
            title_part = f"<{url}|{item_title}>" if url else item_title
            date_text = f"{d.month}/{d.day}" if d else "미정"
            priority = "  `우선검토`" if item.get("_priority_review") else ""
            lines.append(
                f"{idx}. *[{item.get('ntceInsttNm', '')}]* {title_part} — "
                f"{item.get('_deadline_basis') or label} {date_text}  `{tag}`{priority}"
            )
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def format_urgent_digest(entries: list, today, top_n: int = 8, title: str = "\U0001F525 마감임박 TOP") -> str:
    """세 소스를 합쳐 마감이 가장 급한 순으로 top_n개만 뽑은 요약 섹션.
    entries: [(source_tag, label, item), ...] — 소스마다 마감 필드 라벨(마감/의견마감/신청)이 다르므로 같이 받는다.
    title: 섹션 헤더 전체 텍스트 — 확실포함용/확인필요 참고용 섹션이 같은 카드 포맷을 재사용하되
    성격이 다르다는 걸 구분하기 위해 호출부에서 지정한다(2026-09-22, 내부 용어 없이 사람이 바로
    이해할 수 있는 문구로— "확인필요" 같은 태그는 더 이상 노출하지 않는다)."""
    top = _pick_top(entries, today, top_n)

    header = f"*{title} ({len(top)}건)*"
    if not top:
        return f"{header}\n마감 전인 공고가 없어 생략합니다."

    lines = [header, ""]
    for idx, (d, tag, label, item) in enumerate(top, 1):
        org = item.get("ntceInsttNm", "")
        item_title = item.get("bidNtceNm", "")
        url = item.get("bidNtceDtlUrl", "")
        title_part = f"<{url}|{item_title}>" if url else item_title
        date_text = f"{d.month}/{d.day}" if d else "미정"
        priority = "  `우선검토`" if item.get("_priority_review") else ""
        lines.append(f"{idx}. *[{org}]* {title_part} — {item.get('_deadline_basis') or label} {date_text}  `{tag}`{priority}")

    return "\n".join(lines)


def mrkdwn_section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def context_section(text: str) -> dict:
    """작은 회색 글씨(Block Kit context 블록)로 렌더링되는 텍스트.
    2026-09-23 피드백: 카드와 같은 굵기/크기로 떠서 카드 내용과 섞여 보이던 요약 줄을,
    카드와 시각적으로 구분되는 사이드노트 톤으로 빼기 위해 추가."""
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def divider() -> dict:
    return {"type": "divider"}


def chunk_mrkdwn_blocks(text: str, limit: int = 2900) -> list:
    """긴 mrkdwn 텍스트를 Slack section 블록의 글자수 제한(3000자) 아래로 줄바꿈 단위로 쪼갠다."""
    lines = text.split("\n")
    blocks = []
    buf = []
    length = 0
    for line in lines:
        add_len = len(line) + 1
        if buf and length + add_len > limit:
            blocks.append(mrkdwn_section("\n".join(buf)))
            buf = []
            length = 0
        buf.append(line)
        length += add_len
    if buf:
        blocks.append(mrkdwn_section("\n".join(buf)))
    return blocks


def _urgency_emoji(delta) -> str:
    """D-day 정수 기준 긴급도 신호등. delta가 None(마감일 파싱 불가)이면 회색."""
    if delta is None:
        return "⚪"
    if delta <= 1:
        return "\U0001F534"  # 🔴 오늘/내일
    if delta <= 3:
        return "\U0001F7E0"  # 🟠
    if delta <= 7:
        return "\U0001F7E1"  # 🟡
    return "\U0001F7E2"  # 🟢


def format_urgent_blocks(entries: list, today, top_n: int = 8, title: str = "\U0001F525 마감임박 TOP") -> list:
    """마감임박 TOP N을 Block Kit 카드로 만든다. 카드당 2줄(제목줄 + 기관·예산·마감 메타줄)로 압축한
    한 줄형 레이아웃 — 카드당 4줄(제목+5필드+구분선)이던 이전 버전은 TOP5+TOP5만으로도 메시지가
    너무 길어진다는 2026-09-23 피드백에 따라 축소했다. 계약방법 필드는 뺐다(대시보드에서 확인 가능).
    entries: [(source_tag, label, item), ...]. Block 개수가 한정적이어야 해서(Slack 메시지당 50개 제한)
    이 카드형 레이아웃은 TOP N에만 쓰고, 전체 리스트는 기존 압축 텍스트(chunk_mrkdwn_blocks)로 보낸다.
    title: 섹션 헤더 전체 텍스트(format_urgent_digest와 동일한 이유로 파라미터화).
    2026-09-23 피드백: 카드마다 이모지가 너무 많아 눈이 피로하다 — 구분(📋/📐/🏢) 아이콘과 필드
    라벨용 장식 아이콘(🏢/💰/⏰)을 다 빼고, 실제로 판단에 쓰는 신호인 순위 메달(TOP3)과 마감
    긴급도 신호등만 남겼다. 소스 구분은 어차피 label(마감/의견/신청)로도 드러난다."""
    top = _pick_top(entries, today, top_n)

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{title} ({len(top)}건)", "emoji": True},
        }
    ]

    if not top:
        blocks.append(mrkdwn_section("마감 전인 공고가 없어 생략합니다."))
        return blocks

    rank_emoji = ["\U0001F947", "\U0001F948", "\U0001F949"]  # 🥇🥈🥉 TOP3만 메달, 나머지는 번호

    for idx, (_, _tag, label, item) in enumerate(top, 1):
        item_title = item.get("bidNtceNm", "")
        url = item.get("bidNtceDtlUrl", "")
        org = item.get("ntceInsttNm", "")
        rank_marker = rank_emoji[idx - 1] if idx <= 3 else f"`#{idx}`"

        title_line = f"{rank_marker} *<{url}|{item_title}>*" if url else f"{rank_marker} *{item_title}*"

        date_str, delta, passed = dday_parts(item, today)
        if _tag == PRESPEC_TAG:
            # 2026-10-06 요청: 의견마감은 입찰 마감이 아니므로 D-day·긴급도 신호등 없이 날짜만 보여준다.
            dday_text = f"`{date_str}`"
        elif delta is None:
            dday_text = f"`{date_str}`"
        elif passed:
            dday_text = f"`{date_str} 마감`"
        elif delta == 0:
            dday_text = f"`{date_str} (D-day)`"
        else:
            dday_text = f"`{date_str} (D-{delta})`"
        if _tag != PRESPEC_TAG:
            dday_text = f"{_urgency_emoji(delta)} {dday_text}"

        budget_text = format_money(item.get("asignBdgtAmt", ""))
        meta_line = f"{org} · {budget_text} · {item.get('_deadline_basis') or label} {dday_text}"
        # 2026-10-06: 제외 키워드에 걸렸지만 교육·콘텐츠 신호가 강한 확인필요 건(classify.downgrade priority=True).
        if item.get("_priority_review"):
            title_line = f"{title_line}  `우선검토`"

        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"{title_line}\n{meta_line}"},
            }
        )

    return blocks


def post_with_bot(text: str, blocks: list = None, thread_ts: str = None, max_retries: int = 3, channel: str = None):
    """봇 토큰(config.SLACK_BOT_TOKEN)으로 chat.postMessage를 보낸다. 성공하면 메시지 ts(스레드 댓글을 달 때
    thread_ts로 쓴다), 실패하면 None. 웹훅과 달리 HTTP 200이어도 응답 JSON의 ok가 false일 수 있다
    (예: 봇이 채널에 초대되지 않았으면 not_in_channel). channel을 주면 SLACK_CHANNEL_ID 대신 그곳으로 보낸다
    (사용자 ID를 주면 그 사람과 봇의 DM)."""
    from .config import SLACK_BOT_TOKEN, SLACK_CHANNEL_ID

    payload = {"channel": channel or SLACK_CHANNEL_ID, "text": text, "unfurl_links": False}
    if blocks:
        payload["blocks"] = blocks
    if thread_ts:
        payload["thread_ts"] = thread_ts
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post("https://slack.com/api/chat.postMessage", json=payload, headers=headers, timeout=10)
        except requests.exceptions.RequestException as exc:
            print(f"[슬랙] 연결 실패: {exc} (시도 {attempt}/{max_retries})")
            time.sleep(2 ** (attempt - 1))
            continue
        if resp.status_code == 429:
            time.sleep(int(resp.headers.get("Retry-After", "1")))
            continue
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if data.get("ok"):
            print("[슬랙] 발송 성공" + (" (스레드 댓글)" if thread_ts else ""))
            return data.get("ts")
        print(f"[슬랙] 발송 실패: HTTP {resp.status_code} / {data.get('error') or resp.text[:300]}")
        return None
    print(f"[슬랙] {max_retries}회 재시도 후에도 발송 실패")
    return None


def send_to_slack(text: str, blocks: list = None, webhook_url: str = None, max_retries: int = 3) -> bool:
    """Slack Incoming Webhook으로 메시지를 보낸다. blocks가 있으면 Block Kit 레이아웃으로,
    text는 어느 경우든 알림 미리보기/스크린리더용 폴백으로 항상 같이 보낸다.
    데이터 수집을 다 마친 뒤 마지막에 호출되는 만큼, 연결 타임아웃 등으로 여기서 죽으면
    수집한 내용이 통째로 날아가버린다 — 그래서 지수 백오프로 재시도한다."""
    webhook_url = webhook_url or os.getenv("SLACK_WEBHOOK_URL")
    if not webhook_url:
        print("[에러] SLACK_WEBHOOK_URL이 .env에 설정되어 있지 않습니다.")
        print("       .env에 아래 줄을 추가하세요:")
        print("       SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...")
        return False

    payload = {"text": text}
    if blocks:
        payload["blocks"] = blocks

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(webhook_url, json=payload, timeout=10)
        except requests.exceptions.RequestException as exc:
            print(f"[슬랙] 연결 실패: {exc} (시도 {attempt}/{max_retries})")
            if attempt == max_retries:
                print(f"[슬랙] {max_retries}회 재시도 후에도 발송 실패")
                return False
            time.sleep(2 ** (attempt - 1))
            continue

        if resp.status_code == 200 and resp.text.strip().lower() == "ok":
            print("[슬랙] 발송 성공")
            return True

        print(f"[슬랙] 발송 실패: HTTP {resp.status_code} / {resp.text[:300]}")
        return False

    return False
