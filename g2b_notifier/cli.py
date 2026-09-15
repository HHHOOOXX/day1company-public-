"""CLI 진입점.

서브커맨드:
  notify [카테고리1,카테고리2,...]   - 실제 Slack 발송 (+ SQLite에 upsert, 중복 발송 방지)
  daily [카테고리1,카테고리2,...]    - 발송 전 미리보기 (DB에 손대지 않음)
  classify [카테고리] [일수]          - 업종분류/키워드 교차 집계
  prespec [일수]                      - 사전규격(용역) 원본 필드 탐색
  bizinfo-discover                    - 기업마당 원본 필드 탐색 (BIZINFO_SERVICE_KEY 필요)
  alio-discover                       - ALIO 공공기관 정보 원본 필드 탐색 (ALIO_SERVICE_KEY 필요)
  (인자 없음 / 카테고리만)             - 최근 입찰공고 미리보기
"""

import json
import sys
from datetime import datetime

from . import db
from .alio import fetch_alio_preview
from .bizinfo import fetch_bizinfo_preview
from .classify import tag_business_area
from .g2b import (
    analyze_classifications,
    fetch_pre_spec_preview,
    fetch_recent_bids,
    get_daily_relevant_bids,
    get_daily_relevant_pre_specs,
    get_lookback_range,
)
from .slack import format_pre_spec_message, format_slack_message, print_daily_digest, send_to_slack


def _posting_id(source: str, item: dict) -> str:
    if source == "g2b_prespec":
        return f"{source}:{item.get('bidNtceNo', '')}"
    return f"{source}:{item.get('bidNtceNo', '')}:{item.get('bidNtceOrd', '0')}"


def _filter_unnotified(conn, source: str, items: list, today_str: str) -> list:
    """items를 SQLite에 upsert하고, (posting_id, item) 쌍 중 예전에 이미 슬랙 발송된 적 없는 것만 반환한다.
    같은 공고가 다음 lookback 구간에 다시 걸려도(예: 월요일이 직전 금요일을 재조회) 중복 알림을 막아준다."""
    fresh = []
    new_count = 0
    for item in items:
        posting_id = _posting_id(source, item)
        tags = tag_business_area(item.get("bidNtceNm", ""))
        result = db.upsert_posting(
            conn,
            posting_id=posting_id,
            source=source,
            title=item.get("bidNtceNm", ""),
            org=item.get("ntceInsttNm", ""),
            budget=item.get("asignBdgtAmt", ""),
            posted_at=item.get("bidNtceDt", ""),
            close_at=item.get("bidClseDt", ""),
            url=item.get("bidNtceDtlUrl", ""),
            category_tags=tags,
            today_str=today_str,
        )
        if result["is_new"]:
            new_count += 1
        if not result["already_notified"]:
            fresh.append((posting_id, item))

    print(f"[DB] {source}: {len(items)}건 중 신규 {new_count}건, 발송대상(미발송) {len(fresh)}건")
    return fresh


def run_daily_notification(categories=("용역",), include_pre_spec: bool = True):
    """기준 기간(월요일은 직전 금요일 하루, 그 외엔 어제 하루) 관심 공고 + 사전규격(용역)을 수집해 Slack으로 발송한다.
    발송 전 SQLite(data/notifier.db)에 upsert하고, 예전에 이미 발송된 공고는 다시 보내지 않는다.
    (스케줄러가 매일 호출할 진입점)"""
    start_date, end_date = get_lookback_range()
    today_str = datetime.now().date().isoformat()
    conn = db.get_connection()

    bids = get_daily_relevant_bids(categories=categories, start_date=start_date, end_date=end_date)
    bid_pairs = _filter_unnotified(conn, "g2b_bid", bids, today_str)
    message = format_slack_message([item for _, item in bid_pairs], start_date, end_date, categories=categories)

    pre_spec_pairs = []
    if include_pre_spec:
        pre_specs = get_daily_relevant_pre_specs(start_date=start_date, end_date=end_date)
        pre_spec_pairs = _filter_unnotified(conn, "g2b_prespec", pre_specs, today_str)
        message += "\n\n" + format_pre_spec_message([item for _, item in pre_spec_pairs], start_date, end_date)

    print("\n----- 발송할 메시지 미리보기 -----")
    print(message)

    sent = send_to_slack(message)
    if sent:
        all_ids = [pid for pid, _ in bid_pairs] + [pid for pid, _ in pre_spec_pairs]
        db.mark_notified(conn, all_ids)

    conn.close()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "notify":
        categories = sys.argv[2].split(",") if len(sys.argv) > 2 else ["용역"]
        run_daily_notification(categories=categories)
        return

    if len(sys.argv) > 1 and sys.argv[1] == "classify":
        category = sys.argv[2] if len(sys.argv) > 2 else "용역"
        days = int(sys.argv[3]) if len(sys.argv) > 3 else 30
        analyze_classifications(category=category, days=days)
        return

    if len(sys.argv) > 1 and sys.argv[1] == "prespec":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
        items = fetch_pre_spec_preview(days=days)
        if not items:
            print("가져온 사전규격이 없습니다. (기간을 늘리거나 활용신청 승인 여부를 확인해보세요)")
            return
        print("\n===== 첫 번째 사전규격 원본 (전체 필드 확인용) =====")
        print(json.dumps(items[0], ensure_ascii=False, indent=2))
        return

    if len(sys.argv) > 1 and sys.argv[1] == "bizinfo-discover":
        data = fetch_bizinfo_preview()
        if data is None:
            return
        print("\n===== 기업마당 원본 응답 (전체 필드 확인용) =====")
        print(json.dumps(data, ensure_ascii=False, indent=2)[:4000])
        return

    if len(sys.argv) > 1 and sys.argv[1] == "alio-discover":
        data = fetch_alio_preview()
        if data is None:
            return
        print("\n===== ALIO 원본 응답 (전체 필드 확인용) =====")
        print(json.dumps(data, ensure_ascii=False, indent=2)[:4000])
        return

    if len(sys.argv) > 1 and sys.argv[1] == "daily":
        categories = sys.argv[2].split(",") if len(sys.argv) > 2 else ["용역"]
        items = get_daily_relevant_bids(categories=categories)
        print_daily_digest(items)

        print("\n\n===== 사전규격(용역) =====")
        pre_spec_items = get_daily_relevant_pre_specs()
        print_daily_digest(pre_spec_items)
        return

    category = sys.argv[1] if len(sys.argv) > 1 else "용역"
    items = fetch_recent_bids(category=category, days=2, num_of_rows=20)

    if not items:
        print("가져온 공고가 없습니다. (기간을 늘리거나 카테고리를 바꿔서 다시 시도해보세요)")
        return

    print("\n===== 첫 번째 공고 원본 (전체 필드 확인용) =====")
    print(json.dumps(items[0], ensure_ascii=False, indent=2))

    print("\n===== 전체 공고 제목 목록 =====")
    for idx, item in enumerate(items, 1):
        title = item.get("bidNtceNm", "(제목 필드 확인 필요)")
        org = item.get("ntceInsttNm", "(기관 필드 확인 필요)")
        print(f"{idx:2d}. [{org}] {title}")
