"""CLI 진입점.

서브커맨드:
  notify [카테고리1,카테고리2,...] [--quiet-if-empty]
                                       - 실제 Slack 발송 (+ SQLite에 upsert, 중복 발송 방지)
                                         --quiet-if-empty: 오늘 이미 정상 발송된 기록이 있으면 즉시 종료.
                                         그렇지 않을 때도 새 공고/확실후보 리마인드/수집경고가 다 없으면 생략
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

import os

from . import db
from .alio import fetch_alio_org_names, fetch_alio_preview
from .bizinfo import fetch_bizinfo_preview, get_daily_relevant_bizinfo
from .classify import tag_business_area
from .config import BIZINFO_SERVICE_KEY, DASHBOARD_URL, REPO_ROOT, SERVICE_KEY
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
    format_star_reminder,
    format_urgent_blocks,
    format_urgent_digest,
    mrkdwn_section,
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
        classification = {
            "tier": item.get("_tier", "include"),
            "reasons": item.get("_reasons", []),
            "star": item.get("_star"),
            "industry_codes": sorted(item.get("_industry_codes") or []),
        }
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
            is_star=bool(item.get("_star")),
            classification=classification,
            method=item.get("cntrctCnclsMthdNm", ""),
        )
        if result["is_new"]:
            new_count += 1
        if not result["already_notified"]:
            fresh.append((posting_id, item))

    print(f"[DB] {source}: {len(items)}건 중 신규 {new_count}건, 발송대상(미발송) {len(fresh)}건")
    return fresh


def _fetch_alio_orgs() -> set:
    """ALIO 공공기관 화이트리스트(classify._org_confidence 보강용)를 가져온다.
    실패해도(서비스키 없음/API 오류) 예외를 전파하지 않고 빈 set을 반환한다 — 화이트리스트는
    판정을 보강하는 보조 신호일 뿐이라, 이게 없다고 전체 실행이 막히면 안 된다."""
    try:
        orgs = fetch_alio_org_names()
    except Exception as exc:
        print(f"[경고] ALIO 기관 화이트리스트 수집 중 예외 발생: {exc!r}")
        COLLECTION_WARNINGS.append(f"ALIO 기관정보: 예외 발생 ({exc.__class__.__name__})")
        return set()
    print(f"[ALIO] 화이트리스트 기관 {len(orgs)}개 로드")
    return orgs


def run_daily_notification(
    categories=("용역",), include_pre_spec: bool = True, include_bizinfo: bool = True, quiet_if_empty: bool = False
):
    """기준 기간(월요일은 직전 금요일 하루, 그 외엔 어제 하루) 관심 공고 + 사전규격(용역) + 기업마당
    지원사업을 수집해 Slack으로 발송한다.
    발송 전 SQLite(data/notifier.db)에 upsert하고, 예전에 이미 발송된 공고는 다시 보내지 않는다.
    (스케줄러가 매일 호출할 진입점)

    quiet_if_empty=True면, 오늘자 발송이 db.daily_sends에 이미 기록돼 있을 때(=정시 실행이 이미 정상
    발송했을 때) 수집을 시작하기도 전에 조용히 종료한다. — 정시 실행이 지연/스킵될 경우를 대비한
    "백업" 스케줄에서 쓴다."""
    start_date, end_date = get_lookback_range()
    today_str = datetime.now().date().isoformat()
    conn = db.get_connection()

    if quiet_if_empty and db.has_sent_today(conn, today_str):
        # 오늘자 발송이 이미 성공적으로 끝난 뒤 지연 실행된 백업 워크플로우다. 이 실행이 API 일시
        # 오류를 겪든 말든(COLLECTION_WARNINGS) 이미 정상 발송된 이상 무조건 조용히 종료한다.
        # 수집 자체를 시도하지 않아 불필요한 API 호출도 없다.
        print(f"[백업 실행] 오늘({today_str})은 이미 정상 발송된 기록이 있어 수집 없이 조용히 종료합니다.")
        conn.close()
        return

    # 나라장터/기업마당 API가 하루 종일 불안정한 날엔, 페이지별 재시도가 다 정상 동작해도
    # 수십 페이지를 순서대로 재시도하느라 실행 자체가 수십 분씩 걸릴 수 있다. 그러면 "정시 발송"이
    # 의미가 없어지므로, 전체 수집 단계에 3분 상한을 두고 넘기면 남은 건 포기하고 지금까지 모은 것만 보낸다.
    set_collection_deadline(180)
    all_pairs = []
    COLLECTION_WARNINGS.clear()

    alio_orgs = _fetch_alio_orgs()

    try:
        bids = get_daily_relevant_bids(
            categories=categories, start_date=start_date, end_date=end_date, alio_orgs=alio_orgs
        )
    except Exception as exc:
        print(f"[에러] 나라장터 입찰공고 수집 중 예외 발생: {exc!r}")
        traceback.print_exc()
        COLLECTION_WARNINGS.append(f"나라장터 입찰공고: 예외 발생 ({exc.__class__.__name__})")
        bids = []
    bid_pairs = _filter_unnotified(conn, "g2b_bid", bids, today_str)
    all_pairs += bid_pairs

    urgent_entries = [("입찰", "마감", item) for _, item in bid_pairs]
    pre_specs = []
    bizinfo_items = []

    if include_pre_spec:
        try:
            # 이 시점엔 위에서 이미 오늘자 입찰공고가 upsert돼 있어서, 학습 키워드에 오늘 매칭분까지 반영된다.
            historical_bid_titles_orgs = db.get_historical_bid_titles(conn)
            pre_specs = get_daily_relevant_pre_specs(
                start_date=start_date,
                end_date=end_date,
                historical_bid_titles_orgs=historical_bid_titles_orgs,
                alio_orgs=alio_orgs,
            )
        except Exception as exc:
            print(f"[에러] 나라장터 사전규격 수집 중 예외 발생: {exc!r}")
            traceback.print_exc()
            COLLECTION_WARNINGS.append(f"나라장터 사전규격: 예외 발생 ({exc.__class__.__name__})")
            pre_specs = []
        pre_spec_pairs = _filter_unnotified(conn, "g2b_prespec", pre_specs, today_str)
        all_pairs += pre_spec_pairs
        urgent_entries += [("사전규격", "의견", item) for _, item in pre_spec_pairs]

    if include_bizinfo and BIZINFO_SERVICE_KEY:
        try:
            bizinfo_items = get_daily_relevant_bizinfo(start_date=start_date, end_date=end_date, alio_orgs=alio_orgs)
        except Exception as exc:
            print(f"[에러] 기업마당 수집 중 예외 발생: {exc!r}")
            traceback.print_exc()
            COLLECTION_WARNINGS.append(f"기업마당: 예외 발생 ({exc.__class__.__name__})")
            bizinfo_items = []
        bizinfo_pairs = _filter_unnotified(conn, "bizinfo", bizinfo_items, today_str)
        all_pairs += bizinfo_pairs
        urgent_entries += [("기업마당", "신청", item) for _, item in bizinfo_pairs]

    # 대시보드(AX사업기획실 공고목록)는 오늘자 전체 관심 공고(신규/기존 발송 여부 무관)를 담아서
    # 매 실행마다 최신 상태로 갱신한다. docs/index.html에 고정 경로로 써서, 호스팅(GitHub Pages 등)이
    # 정해지면 바로 서빙될 수 있게 미리 준비해둔다. 로컬/CI 어디서든 브라우저는 띄우지 않는다.
    dashboard_path = os.path.join(REPO_ROOT, "docs", "index.html")
    os.makedirs(os.path.dirname(dashboard_path), exist_ok=True)
    write_and_open_preview(
        bids, pre_specs, bizinfo_items, start_date, end_date,
        warnings=list(COLLECTION_WARNINGS), path=dashboard_path, auto_open=False,
    )

    # 이미 발송됐지만 마감이 안 지난 확실후보(⭐)는 마감일까지 매일 다시 안내한다 — 오늘 새로
    # 발송할 게 없어도 이 섹션은 독립적으로 존재할 수 있다.
    star_postings = db.get_active_star_postings(conn, today_str)
    star_section = format_star_reminder(star_postings, today=datetime.now().date())

    # 2026-09-18 재설계: 메시지 한 번에 너무 많은 공고가 쏟아져서, 핵심(확인필요가 아닌) 공고만
    # 마감임박 상위 5건으로 추리고 나머지는 전부 대시보드로 넘긴다. ⚠️확인필요 등급은 이 5건 선정에서
    # 빠진다 — 애매한 건까지 채널에 바로 밀어붙이지 않기 위함(대시보드에서는 그대로 확인 가능).
    # 2026-09-22 피드백: 단, 확실포함 건이 하나도 없는 날 메시지가 텅 비어 보이는 문제가 있어 —
    # 그런 날에 한해서는 ⚠️확인필요 건이라도 카드에 보여준다(카드 자체에 "⚠️확인필요" 적합성 태그가
    # 이미 붙어 있어 확실포함과 혼동되지 않는다).
    core_entries = [e for e in urgent_entries if e[2].get("_tier") != "review"]
    review_count = sum(1 for _, item in all_pairs if item.get("_tier") == "review")
    digest_entries = core_entries or [e for e in urgent_entries if e[2].get("_tier") == "review"]

    summary_lines = [
        f"오늘 나라장터 입찰공고 {len(bid_pairs)}건",
    ]
    if include_pre_spec:
        summary_lines.append(f"사전규격 {len(pre_spec_pairs)}건")
    if include_bizinfo and BIZINFO_SERVICE_KEY:
        summary_lines.append(f"기업마당 {len(bizinfo_pairs)}건")
    summary_text = " / ".join(summary_lines) + f" 확인"
    if review_count:
        summary_text += f" (⚠️확인필요 {review_count}건 포함)"

    dashboard_text = ""
    if DASHBOARD_URL:
        dashboard_text = f"\U0001F4CA 전체 공고 목록: <{DASHBOARD_URL}|AX사업기획실 공고목록>"

    # text(폴백/콘솔 미리보기)
    message_parts = []
    if digest_entries:
        message_parts.append(format_urgent_digest(digest_entries, today=datetime.now().date(), top_n=5))
    message_parts.append(summary_text)
    if star_section:
        message_parts.append(star_section)
    if dashboard_text:
        message_parts.append(dashboard_text)
    message = "\n\n".join(message_parts)

    warning_text = ""
    if COLLECTION_WARNINGS:
        warning_lines = "\n".join(f"- {w}" for w in COLLECTION_WARNINGS)
        warning_text = (
            "*⚠️ 일부 데이터 수집에 실패했습니다 — 이 알림이 불완전할 수 있습니다.*\n"
            f"{warning_lines}\n나라장터/기업마당에서 직접 한 번 더 확인해주세요."
        )
        message = warning_text + "\n\n" + message

    if quiet_if_empty and not all_pairs and not star_postings and not COLLECTION_WARNINGS:
        print("[백업 실행] 새로 보낼 공고도, 확실후보 리마인드도, 수집 경고도 없어서 조용히 종료합니다.")
        conn.close()
        return

    message = "<!channel>\n" + message

    # blocks(실제 Slack 레이아웃): 채널 멘션 → 경고(있으면) → 핵심 공고 TOP 5 카드 → 확실후보
    # 리마인더 → 요약 카운트 → 대시보드 링크. 전체 리스트 나열은 더 이상 채널에 뿌리지 않는다.
    blocks = [mrkdwn_section("<!channel>")]
    if warning_text:
        blocks.append(mrkdwn_section(warning_text))
    if digest_entries:
        blocks.extend(format_urgent_blocks(digest_entries, today=datetime.now().date(), top_n=5))
    blocks.append(mrkdwn_section(summary_text))
    if star_section:
        blocks.append(mrkdwn_section(star_section))
    if dashboard_text:
        blocks.append(mrkdwn_section(dashboard_text))

    print("\n----- 발송할 메시지 미리보기 -----")
    print(message)

    sent = send_to_slack(message, blocks=blocks)
    if sent:
        db.mark_notified(conn, [pid for pid, _ in all_pairs])
        db.mark_sent_today(conn, today_str)

    conn.close()


def _require_g2b_key():
    """나라장터 API를 실제로 호출하는 서브커맨드 진입 시점에만 키 유무를 확인한다.
    (config.py는 더 이상 import 시점에 강제 종료하지 않는다 — streamlit_app.py처럼 API를
    전혀 안 쓰고 DB만 읽는 코드가 이 패키지를 import만 해도 죽는 문제가 있었다.)"""
    if not SERVICE_KEY:
        print("[에러] .env 파일에 G2B_SERVICE_KEY 가 설정되어 있지 않습니다.")
        print("       .env 파일에 아래 줄을 추가하세요:")
        print("       G2B_SERVICE_KEY=발급받은_서비스키")
        sys.exit(1)


def main():
    # 2026-09-22: Windows 콘솔/파일 리다이렉트 환경은 기본 인코딩이 cp949라, 슬랙 메시지 본문이나
    # 로그에 들어있는 이모지(⚠️ 등)를 print()하는 순간 UnicodeEncodeError로 전체 실행이 죽는다.
    # GitHub Actions(ubuntu-latest)는 기본이 UTF-8이라 원래 안 겪는 문제지만, 로컬에서 디버깅/테스트
    # 할 때마다 이 크래시를 만나 — stdout/stderr를 UTF-8로 강제 재설정해 근본적으로 막는다.
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    if len(sys.argv) > 1 and sys.argv[1] == "notify":
        _require_g2b_key()
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
        _require_g2b_key()
        category = sys.argv[2] if len(sys.argv) > 2 else "용역"
        days = int(sys.argv[3]) if len(sys.argv) > 3 else 30
        analyze_classifications(category=category, days=days)
        return

    if len(sys.argv) > 1 and sys.argv[1] == "prespec":
        _require_g2b_key()
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
        _require_g2b_key()
        categories = sys.argv[2].split(",") if len(sys.argv) > 2 else ["용역"]
        start_date, end_date = get_lookback_range()
        COLLECTION_WARNINGS.clear()

        preview_conn = db.get_connection()
        historical_bid_titles_orgs = db.get_historical_bid_titles(preview_conn)
        preview_conn.close()
        alio_orgs = _fetch_alio_orgs()

        bid_items = get_daily_relevant_bids(
            categories=categories, start_date=start_date, end_date=end_date, alio_orgs=alio_orgs
        )
        pre_spec_items = get_daily_relevant_pre_specs(
            start_date=start_date,
            end_date=end_date,
            historical_bid_titles_orgs=historical_bid_titles_orgs,
            alio_orgs=alio_orgs,
        )
        bizinfo_items = (
            get_daily_relevant_bizinfo(start_date=start_date, end_date=end_date, alio_orgs=alio_orgs)
            if BIZINFO_SERVICE_KEY
            else []
        )

        write_and_open_preview(
            bid_items, pre_spec_items, bizinfo_items, start_date, end_date, warnings=list(COLLECTION_WARNINGS)
        )
        return

    if len(sys.argv) > 1 and sys.argv[1] == "daily":
        _require_g2b_key()
        categories = sys.argv[2].split(",") if len(sys.argv) > 2 else ["용역"]
        alio_orgs = _fetch_alio_orgs()
        items = get_daily_relevant_bids(categories=categories, alio_orgs=alio_orgs)
        print_daily_digest(items)

        print("\n\n===== 사전규격(용역) =====")
        daily_conn = db.get_connection()
        historical_bid_titles_orgs = db.get_historical_bid_titles(daily_conn)
        daily_conn.close()
        pre_spec_items = get_daily_relevant_pre_specs(
            historical_bid_titles_orgs=historical_bid_titles_orgs, alio_orgs=alio_orgs
        )
        print_daily_digest(pre_spec_items)

        if BIZINFO_SERVICE_KEY:
            print("\n\n===== 기업마당 =====")
            bizinfo_items = get_daily_relevant_bizinfo(alio_orgs=alio_orgs)
            print_daily_digest(bizinfo_items)
        return

    _require_g2b_key()
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
