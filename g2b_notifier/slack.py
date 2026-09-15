"""콘솔 미리보기 출력 + Slack 메시지 포맷팅/발송."""

import os

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
        org = item.get("ntceInsttNm", "")
        title = item.get("bidNtceNm", "")
        close = item.get("bidClseDt") or "미정"
        url = item.get("bidNtceDtlUrl", "")
        if url:
            lines.append(f"{idx}. *[{org}]* <{url}|{title}>")
        else:
            lines.append(f"{idx}. *[{org}]* {title}")
        lines.append(f"     마감: {close}")

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
        org = item.get("ntceInsttNm", "")
        title = item.get("bidNtceNm", "")
        close = item.get("bidClseDt") or "미정"
        url = item.get("bidNtceDtlUrl", "")
        if url:
            lines.append(f"{idx}. *[{org}]* <{url}|{title}>")
        else:
            lines.append(f"{idx}. *[{org}]* {title}")
        lines.append(f"     의견마감: {close}")

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
        org = item.get("ntceInsttNm", "")
        title = item.get("bidNtceNm", "")
        period = item.get("bidClseDt") or "미정"
        url = item.get("bidNtceDtlUrl", "")
        if url:
            lines.append(f"{idx}. *[{org}]* <{url}|{title}>")
        else:
            lines.append(f"{idx}. *[{org}]* {title}")
        lines.append(f"     신청기간: {period}")

    return "\n".join(lines)


def send_to_slack(text: str, webhook_url: str = None) -> bool:
    """Slack Incoming Webhook으로 텍스트 메시지를 보낸다."""
    webhook_url = webhook_url or os.getenv("SLACK_WEBHOOK_URL")
    if not webhook_url:
        print("[에러] SLACK_WEBHOOK_URL이 .env에 설정되어 있지 않습니다.")
        print("       .env에 아래 줄을 추가하세요:")
        print("       SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...")
        return False

    resp = requests.post(webhook_url, json={"text": text}, timeout=10)
    if resp.status_code == 200 and resp.text.strip().lower() == "ok":
        print("[슬랙] 발송 성공")
        return True

    print(f"[슬랙] 발송 실패: HTTP {resp.status_code} / {resp.text[:300]}")
    return False
