"""콘솔 미리보기 출력 + Slack 메시지 포맷팅/발송."""

import os
import time
from datetime import datetime

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


def _short_date(item: dict) -> str:
    """마감 필드를 'M/D' 짧은 형태로. 파싱 안 되면 원문(미정 등) 그대로."""
    d = deadline_date(item)
    if d is not None:
        return f"{d.month}/{d.day}"
    return (item.get("bidClseDt") or "미정") or "미정"


def _compact_line(idx, item: dict, label: str) -> str:
    """`{idx}. *[기관]* <링크|제목> — 라벨 M/D` 한 줄짜리 항목 포맷."""
    org = item.get("ntceInsttNm", "")
    title = item.get("bidNtceNm", "")
    url = item.get("bidNtceDtlUrl", "")
    short_date = _short_date(item)
    title_part = f"<{url}|{title}>" if url else title
    return f"{idx}. *[{org}]* {title_part} — {label} {short_date}"


def format_urgent_digest(entries: list, today, top_n: int = 8) -> str:
    """세 소스를 합쳐 마감이 가장 급한 순으로 top_n개만 뽑은 요약 섹션.
    entries: [(source_tag, label, item), ...] — 소스마다 마감 필드 라벨(마감/의견마감/신청)이 다르므로 같이 받는다."""
    dated = [(deadline_date(item), tag, label, item) for tag, label, item in entries]
    dated = [d for d in dated if d[0] is not None and d[0] >= today]
    dated.sort(key=lambda d: d[0])
    top = dated[:top_n]

    header = f"*\U0001F525 마감임박 TOP {len(top)} (전체 {len(entries)}건 중 오늘 기준 가장 급한 것부터)*"
    if not top:
        return f"{header}\n마감일이 확인되는 공고가 없어 생략합니다."

    lines = [header, ""]
    for idx, (_, tag, label, item) in enumerate(top, 1):
        lines.append(f"{_compact_line(idx, item, label)}  `{tag}`")

    return "\n".join(lines)


def format_slack_message(items: list, start_date, end_date, categories=("용역",)) -> str:
    """Slack Incoming Webhook로 보낼 메시지 텍스트(mrkdwn)를 만든다."""
    category_label = "/".join(categories)
    period_label = start_date.isoformat() if start_date == end_date else f"{start_date.isoformat()}~{end_date.isoformat()}"
    header = f"*\U0001F4CB 나라장터 입찰공고 알림 — {period_label} 게시분 ({len(items)}건)*"

    if not items:
        return (
            f"{header}\n"
            f"{period_label}에 게시된 {category_label} 공고를 확인했지만, "
            f"교육/양성 키워드 + 대학교·지자체 발주기관 조건을 모두 만족하는 신규 공고가 없었습니다."
        )

    lines = [header, ""]
    for idx, item in enumerate(items, 1):
        lines.append(_compact_line(idx, item, "마감"))

    return "\n".join(lines)


def format_pre_spec_message(items: list, start_date, end_date) -> str:
    """사전규격(용역) 알림 섹션을 Slack mrkdwn 텍스트로 만든다. (입찰공고보다 먼저 뜨는 초기 정보이므로 별도 섹션으로 구성)"""
    period_label = start_date.isoformat() if start_date == end_date else f"{start_date.isoformat()}~{end_date.isoformat()}"
    header = f"*\U0001F4D0 나라장터 사전규격 알림 — {period_label} 등록분 ({len(items)}건)*"

    if not items:
        return (
            f"{header}\n"
            f"{period_label}에 등록된 용역 사전규격을 확인했지만, "
            f"교육/양성 키워드 + 대학교·지자체(수요기관) 조건을 모두 만족하는 신규 건이 없었습니다."
        )

    lines = [header, ""]
    for idx, item in enumerate(items, 1):
        lines.append(_compact_line(idx, item, "의견"))

    return "\n".join(lines)


def format_bizinfo_message(items: list, start_date, end_date) -> str:
    """기업마당(중소기업 지원사업) 알림 섹션을 Slack mrkdwn 텍스트로 만든다."""
    period_label = start_date.isoformat() if start_date == end_date else f"{start_date.isoformat()}~{end_date.isoformat()}"
    header = f"*\U0001F3E2 기업마당 지원사업 알림 — {period_label} 등록분 ({len(items)}건)*"

    if not items:
        return (
            f"{header}\n"
            f"{period_label}에 등록된 지원사업 공고를 확인했지만, "
            f"교육/양성 키워드 + 기관 조건을 모두 만족하는 신규 건이 없었습니다."
        )

    lines = [header, ""]
    for idx, item in enumerate(items, 1):
        lines.append(_compact_line(idx, item, "신청"))

    return "\n".join(lines)


def send_to_slack(text: str, webhook_url: str = None, max_retries: int = 3) -> bool:
    """Slack Incoming Webhook으로 텍스트 메시지를 보낸다.
    데이터 수집을 다 마친 뒤 마지막에 호출되는 만큼, 연결 타임아웃 등으로 여기서 죽으면
    수집한 내용이 통째로 날아가버린다 — 그래서 지수 백오프로 재시도한다."""
    webhook_url = webhook_url or os.getenv("SLACK_WEBHOOK_URL")
    if not webhook_url:
        print("[에러] SLACK_WEBHOOK_URL이 .env에 설정되어 있지 않습니다.")
        print("       .env에 아래 줄을 추가하세요:")
        print("       SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...")
        return False

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(webhook_url, json={"text": text}, timeout=10)
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
