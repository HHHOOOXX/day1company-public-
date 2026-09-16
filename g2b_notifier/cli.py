"""CLI 진입점.

서브커맨드:
  notify [카테고리1,카테고리2,...] [--quiet-if-empty]
                                       - 실제 Slack 발송 (+ SQLite에 upsert, 중복 발송 방지)
                                         --quiet-if-empty: 새 공고/수집경고가 없으면 발송 자체를 생략
                                         (정시 실행이 지연될 때를 대비한 백업 스케줄용)
  daily [카테고리1,카테고리2,...]    - 발송 전 미리보기 (DB에 손대지 않음, 콘솔 텍스트)
  preview [카테고리1,카테고리2,...]  - 발송 전 미리보기 (DB에 손대지 않음, Slack UI처럼 생긴 로컬 HTML로 브라우저에 열림)
  classify [카테고리] [일수]          - 업종분류/키워드 교차 집계
  prespec [일수]                      - 사전규격(용역) 원본 필드 탐색
  bizinfo-discover                    - 기업마당 원본 필드 탐색 (BIZINFO_SERVICE_KEY 필요)
  alio-discover                       - ALIO 공공기관 정보 원본 필드 탐색 (ALIO_SERVICE_KEY 필요)
  (인자 없음 / 카테고리만)             - 최근 입찰공고 미리보기
"""

import json
import sys
import traceback
from datetime import datetime

from . import db
from .alio import fetch_alio_preview
from .bizinfo import fetch_bizinfo_preview, get_daily_relevant_bizinfo
from .classify import tag_business_area
from .config import BIZINFO_SERVICE_KEY
from .g2b import (
    COLLECTION_WARNINGS,
    analyze_classifications,
    fetch_pre_spec_preview,
    fetch_recent_bids,
    get_daily_relevant_bids,
    get_daily_relevant_pre_specs,
    get_lookback_range,
    set_collection_deadline,
)
from .preview import write_and_open_preview
from .slack import (
    format_bizinfo_message,
    format_pre_spec_message,
    format_slack_message,
    format_urgent_digest,
    print_daily_digest,
    send_to_slack,
)


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


def run_daily_notification(
    categories=("용역",), include_pre_spec: bool = True, include_bizinfo: bool = True, quiet_if_empty: bool = False
):
    """기준 기간(월요일은 직전 금요일 하루, 그 외엔 어제 하루) 관심 공고 + 사전규격(용역) + 기업마당
    지원사업을 수집해 Slack으로 발송한다.
    발송 전 SQLite(data/notifier.db)에 upsert하고, 예전에 이미 발송된 공고는 다시 보내지 않는다.
    (스케줄러가 매일 호출할 진입점)

    quiet_if_empty=True면, 새로 보낼 공고도 없고 수집 경고도 없을 때 아무 메시지도 보내지 않고 조용히 종료한다.
    — 정시 실행이 지연/스킵될 경우를 대비한 "백업" 스케줄에서 쓴다. 정시 실행이 이미 정상적으로 보냈다면
    오늘 공고는 전부 notified 처리돼 있어서 백업 실행은 자연히 빈 결과가 되어 아무것도 다시 보내지 않는다."""
    # 나라장터/기업마당 API가 하루 종일 불안정한 날엔, 페이지별 재시도가 다 정상 동작해도
    # 수십 페이지를 순서대로 재시도하느라 실행 자체가 수십 분씩 걸릴 수 있다. 그러면 "정시 발송"이
    # 의미가 없어지므로, 전체 수집 단계에 3분 상한을 두고 넘기면 남은 건 포기하고 지금까지 모은 것만 보낸다.
    set_collection_deadline(180)
    start_date, end_date = get_lookback_range()
    today_str = datetime.now().date().isoformat()
    conn = db.get_connection()
    all_pairs = []
    COLLECTION_WARNINGS.clear()

    try:
        bids = get_daily_relevant_bids(categories=categories, start_date=start_date, end_date=end_date)
    except Exception as exc:
        print(f"[에러] 나라장터 입찰공고 수집 중 예외 발생: {exc!r}")
        traceback.print_exc()
        COLLECTION_WARNINGS.append(f"나라장터 입찰공고: 예외 발생 ({exc.__class__.__name__})")
        bids = []
    bid_pairs = _filter_unnotified(conn, "g2b_bid", bids, today_str)
    all_pairs += bid_pairs
    message = format_slack_message([item for _, item in bid_pairs], start_date, end_date, categories=categories)

    urgent_entries = [("입찰", "마감", item) for _, item in bid_pairs]

    if include_pre_spec:
        try:
            pre_specs = get_daily_relevant_pre_specs(start_date=start_date, end_date=end_date)
        except Exception as exc:
            print(f"[에러] 나라장터 사전규격 수집 중 예외 발생: {exc!r}")
            traceback.print_exc()
            COLLECTION_WARNINGS.append(f"나라장터 사전규격: 예외 발생 ({exc.__class__.__name__})")
            pre_specs = []
        pre_spec_pairs = _filter_unnotified(conn, "g2b_prespec", pre_specs, today_str)
        all_pairs += pre_spec_pairs
        message += "\n\n" + format_pre_spec_message([item for _, item in pre_spec_pairs], start_date, end_date)
        urgent_entries += [("사전규격", "의견", item) for _, item in pre_spec_pairs]

    if include_bizinfo and BIZINFO_SERVICE_KEY:
        try:
            bizinfo_items = get_daily_relevant_bizinfo(start_date=start_date, end_date=end_date)
        except Exception as exc:
            print(f"[에러] 기업마당 수집 중 예외 발생: {exc!r}")
            traceback.print_exc()
            COLLECTION_WARNINGS.append(f"기업마당: 예외 발생 ({exc.__class__.__name__})")
            bizinfo_items = []
        bizinfo_pairs = _filter_unnotified(conn, "bizinfo", bizinfo_items, today_str)
        all_pairs += bizinfo_pairs
        message += "\n\n" + format_bizinfo_message([item for _, item in bizinfo_pairs], start_date, end_date)
        urgent_entries += [("기업마당", "신청", item) for _, item in bizinfo_pairs]

    if urgent_entries:
        message = format_urgent_digest(urgent_entries, today=datetime.now().date()) + "\n\n" + message

    if COLLECTION_WARNINGS:
        warning_lines = "\n".join(f"- {w}" for w in COLLECTION_WARNINGS)
        message = (
            "*⚠️ 일부 데이터 수집에 실패했습니다 — 이 알림이 불완전할 수 있습니다.*\n"
            f"{warning_lines}\n나라장터/기업마당에서 직접 한 번 더 확인해주세요.\n\n"
        ) + message

    if quiet_if_empty and not all_pairs and not COLLECTION_WARNINGS:
        print("[백업 실행] 새로 보낼 공고도, 수집 경고도 없어서 조용히 종료합니다 (정시 실행이 이미 처리한 것으로 보임).")
        conn.close()
        return

    print("\n----- 발송할 메시지 미리보기 -----")
    print(message)

    sent = send_to_slack(message)
    if sent:
        db.mark_notified(conn, [pid for pid, _ in all_pairs])

    conn.close()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "notify":
        args = sys.argv[2:]
        quiet_if_empty = "--quiet-if-empty" in args
        positional = [a for a in args if not a.startswith("--")]
        categories = positional[0].split(",") if positional else ["용역"]
        try:
            run_daily_notification(categories=categories, quiet_if_empty=quiet_if_empty)
        except Exception as exc:
            # 소스별 try/except로도 못 막는, 완전히 예상 못한 버그용 마지막 안전망.
            # 팀 채널에는 정리된 공고문만 보여야 하므로 슬랙으로는 알리지 않는다 —
            # GitHub Actions 로그/실행 상태(빨간 X)로만 남기고 다시 raise한다.
            print(f"[치명적 에러] 자동 발송이 처리되지 않은 예외로 중단됐습니다: {exc!r}")
            traceback.print_exc()
            raise
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

    if len(sys.argv) > 1 and sys.argv[1] == "preview":
        categories = sys.argv[2].split(",") if len(sys.argv) > 2 else ["용역"]
        start_date, end_date = get_lookback_range()
        COLLECTION_WARNINGS.clear()

        bid_items = get_daily_relevant_bids(categories=categories, start_date=start_date, end_date=end_date)
        pre_spec_items = get_daily_relevant_pre_specs(start_date=start_date, end_date=end_date)
        bizinfo_items = (
            get_daily_relevant_bizinfo(start_date=start_date, end_date=end_date) if BIZINFO_SERVICE_KEY else []
        )

        write_and_open_preview(
            bid_items, pre_spec_items, bizinfo_items, start_date, end_date, warnings=list(COLLECTION_WARNINGS)
        )
        return

    if len(sys.argv) > 1 and sys.argv[1] == "daily":
        categories = sys.argv[2].split(",") if len(sys.argv) > 2 else ["용역"]
        items = get_daily_relevant_bids(categories=categories)
        print_daily_digest(items)

        print("\n\n===== 사전규격(용역) =====")
        pre_spec_items = get_daily_relevant_pre_specs()
        print_daily_digest(pre_spec_items)

        if BIZINFO_SERVICE_KEY:
            print("\n\n===== 기업마당 =====")
            bizinfo_items = get_daily_relevant_bizinfo()
            print_daily_digest(bizinfo_items)
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
