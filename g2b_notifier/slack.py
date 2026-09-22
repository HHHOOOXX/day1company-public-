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


def format_urgent_digest(entries: list, today, top_n: int = 8, title: str = "\U0001F525 마감임박 TOP") -> str:
    """세 소스를 합쳐 마감이 가장 급한 순으로 top_n개만 뽑은 요약 섹션.
    entries: [(source_tag, label, item), ...] — 소스마다 마감 필드 라벨(마감/의견마감/신청)이 다르므로 같이 받는다.
    title: 섹션 헤더 전체 텍스트 — 확실포함용/확인필요 참고용 섹션이 같은 카드 포맷을 재사용하되
    성격이 다르다는 걸 구분하기 위해 호출부에서 지정한다(2026-09-22, 내부 용어 없이 사람이 바로
    이해할 수 있는 문구로— "확인필요" 같은 태그는 더 이상 노출하지 않는다)."""
    dated = [(deadline_date(item), tag, label, item) for tag, label, item in entries]
    dated = [d for d in dated if d[0] is not None and d[0] >= today]
    dated.sort(key=lambda d: d[0])
    top = dated[:top_n]

    header = f"*{title} ({len(top)}건)*"
    if not top:
        return f"{header}\n마감일이 확인되는 공고가 없어 생략합니다."

    lines = [header, ""]
    for idx, (d, tag, label, item) in enumerate(top, 1):
        org = item.get("ntceInsttNm", "")
        item_title = item.get("bidNtceNm", "")
        url = item.get("bidNtceDtlUrl", "")
        title_part = f"<{url}|{item_title}>" if url else item_title
        lines.append(f"{idx}. *[{org}]* {title_part} — {label} {d.month}/{d.day}  `{tag}`")

    return "\n".join(lines)


def mrkdwn_section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


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


_SOURCE_TAG_EMOJI = {"입찰": "\U0001F4CB", "사전규격": "\U0001F4D0", "기업마당": "\U0001F3E2"}


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
    """마감임박 TOP N을 Block Kit 카드(기관/구분/예산/계약방법/마감/적합성 필드)로 만든다.
    entries: [(source_tag, label, item), ...]. Block 개수가 한정적이어야 해서(Slack 메시지당 50개 제한)
    이 카드형 레이아웃은 TOP N에만 쓰고, 전체 리스트는 기존 압축 텍스트(chunk_mrkdwn_blocks)로 보낸다.
    2026-09-18 가독성 개선: 마감 임박도를 신호등 이모지로, 수치성 정보(예산/계약방법/마감)는 코드
    서식(`)으로 감싸서 한눈에 훑기 쉽게 만들었다.
    title: 섹션 헤더 전체 텍스트(format_urgent_digest와 동일한 이유로 파라미터화).
    2026-09-22 피드백: 판단 사유 인용문과 "적합성"(확실포함/확인필요 등 내부 용어) 필드를 카드에서
    뺐다 — 팀원은 우리가 내부적으로 어떤 기준으로 걸렀는지 몰라도 되고, 그냥 깔끔하게 공고 정보만
    보면 된다."""
    dated = [(deadline_date(item), tag, label, item) for tag, label, item in entries]
    dated = [d for d in dated if d[0] is not None and d[0] >= today]
    dated.sort(key=lambda d: d[0])
    top = dated[:top_n]

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{title} ({len(top)}건)", "emoji": True},
        }
    ]

    if not top:
        blocks.append(mrkdwn_section("마감일이 확인되는 공고가 없어 생략합니다."))
        return blocks

    rank_emoji = ["\U0001F947", "\U0001F948", "\U0001F949"]  # 🥇🥈🥉 TOP3만 메달, 나머지는 번호

    for idx, (_, tag, label, item) in enumerate(top, 1):
        item_title = item.get("bidNtceNm", "")
        url = item.get("bidNtceDtlUrl", "")
        source_emoji = _SOURCE_TAG_EMOJI.get(tag, "\U0001F4C4")
        rank_marker = rank_emoji[idx - 1] if idx <= 3 else f"`#{idx}`"

        title_line = f"{rank_marker} {source_emoji} *<{url}|{item_title}>*" if url else f"{rank_marker} {source_emoji} *{item_title}*"

        date_str, delta, passed = dday_parts(item, today)
        if delta is None:
            dday_text = f"`{date_str}`"
        elif passed:
            dday_text = f"`{date_str} 마감`"
        elif delta == 0:
            dday_text = f"`{date_str} (D-day)`"
        else:
            dday_text = f"`{date_str} (D-{delta})`"
        dday_text = f"{_urgency_emoji(delta)} {dday_text}"

        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": title_line},
                "fields": [
                    {"type": "mrkdwn", "text": f"\U0001F3E2 *발주기관*\n{item.get('ntceInsttNm', '')}"},
                    {"type": "mrkdwn", "text": f"{source_emoji} *구분*\n`{tag}`"},
                    {"type": "mrkdwn", "text": f"\U0001F4B0 *예산*\n`{format_money(item.get('asignBdgtAmt', ''))}`"},
                    {"type": "mrkdwn", "text": f"\U0001F4DD *계약방법*\n`{item.get('cntrctCnclsMthdNm') or '-'}`"},
                    {"type": "mrkdwn", "text": f"⏰ *{label}*\n{dday_text}"},
                ],
            }
        )
        blocks.append({"type": "divider"})

    return blocks


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
