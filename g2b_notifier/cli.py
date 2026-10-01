"""CLI 진입점.

서브커맨드:
  notify [카테고리1,카테고리2,...] [--quiet-if-empty] [--dry-run]
                                       - 실제 Slack 발송 (+ SQLite에 upsert, 중복 발송 방지)
                                         --quiet-if-empty: 오늘 이미 정상 발송된 기록이 있으면 즉시 종료.
                                         그렇지 않을 때도 새 공고/확실후보 리마인드/수집경고가 다 없으면 생략
                                         (정시 실행이 지연될 때를 대비한 백업 스케줄용)
                                         --dry-run: 수집/필터링/메시지 구성까지만 하고 Slack 발송과
                                         발송 기록(notified/daily_sends)은 남기지 않는다(CI 동작 확인용)
  daily [카테고리1,카테고리2,...]    - 발송 전 미리보기 (DB에 손대지 않음, 콘솔 텍스트)
  preview [카테고리1,카테고리2,...]  - 발송 전 미리보기 (DB에 손대지 않음, Slack UI처럼 생긴 로컬 HTML로 브라우저에 열림)
  classify [카테고리] [일수]          - 업종분류/키워드 교차 집계
  prespec [일수]                      - 사전규격(용역) 원본 필드 탐색
  bizinfo-discover                    - 기업마당 원본 필드 탐색 (BIZINFO_SERVICE_KEY 필요)
  alio-discover                       - ALIO 공공기관 정보 원본 필드 탐색 (ALIO_SERVICE_KEY 필요)
  agency-discover                     - 기관 홈페이지 게시판(NIPA/NIA/중진공 등) 목록 파싱 결과 확인
  proposals                           - 제안 검토 이력(Drive [제안 및 검토] 폴더 ↔ 공고 연결) 현황
  proposals-sync                      - Drive 폴더를 지금 읽어 제안 이력만 갱신 (GOOGLE_SERVICE_ACCOUNT_JSON 필요)
  (인자 없음 / 카테고리만)             - 최근 입찰공고 미리보기
"""

import json
import sys
import time
import traceback
from datetime import datetime

import os

from . import db
from .proposals import print_report as print_proposal_report, sync_proposals
from .agencies import AGENCY_UNSUPPORTED, fetch_all_agency_rows, get_daily_relevant_agency_notices, normalize_title
from .alio import fetch_alio_org_names, fetch_alio_preview
from .bizinfo import fetch_bizinfo_preview, get_daily_relevant_bizinfo
from .classify import is_prior_proposal, set_learned_proposal_orgs, tag_business_area
from .config import BIZINFO_SERVICE_KEY, DASHBOARD_URL, REPO_ROOT, SERVICE_KEY, SLACK_MENTION, is_kr_holiday
from .g2b import (
    COLLECTION_WARNINGS,
    RAW_SOURCE_TITLES,
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
    context_section,
    divider,
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


COLLECTION_TIME_LIMIT_SECONDS = 240


def _collect_agency_notices(conn, start_date, end_date, alio_orgs, other_items) -> list:
    """기관 홈페이지 게시판 공고 수집(나라장터·기업마당에 없는 공고만, 본문·과거사례 2차 검토 포함). 실패해도 빈 목록으로
    계속 진행한다(다른 소스 발송을 막지 않음)."""
    try:
        # 오늘 받은 나라장터·사전규격·기업마당 원본 공고명(필터로 걸러진 것 포함)을 쌓아둔다. 기관 게시판이
        # 며칠 늦게 올리는 같은 공고를 빼는 데(최근 60일치 비교)와 2차 검토의 과거 사례(RAG)로 쓴다.
        db.save_raw_titles(
            conn,
            {normalize_title(t): (t, source) for t, source in RAW_SOURCE_TITLES.items() if t},
            datetime.now().date().isoformat(),
        )
        known_titles = db.get_known_titles(conn) + [item.get("bidNtceNm", "") for item in other_items]
        return get_daily_relevant_agency_notices(
            start_date=start_date, end_date=end_date, alio_orgs=alio_orgs, known_titles=known_titles,
            rag_postings=db.get_postings_for_rag(conn), rag_raw_titles=db.get_raw_titles(conn),
        )
    except Exception as exc:
        print(f"[에러] 기관 게시판 수집 중 예외 발생: {exc!r}")
        traceback.print_exc()
        COLLECTION_WARNINGS.append(f"기관 게시판: 예외 발생 ({exc.__class__.__name__})")
        return []


def run_daily_notification(
    categories=("용역",), include_pre_spec: bool = True, include_bizinfo: bool = True, quiet_if_empty: bool = False,
    dry_run: bool = False, include_agency: bool = True,
):
    """기준 기간(월요일은 직전 금요일 하루, 그 외엔 어제 하루) 관심 공고 + 사전규격(용역) + 기업마당
    지원사업을 수집해 Slack으로 발송한다.
    발송 전 SQLite(data/notifier.db)에 upsert하고, 예전에 이미 발송된 공고는 다시 보내지 않는다.
    (스케줄러가 매일 호출할 진입점)

    quiet_if_empty=True면, 오늘자 발송이 db.daily_sends에 이미 기록돼 있을 때(=정시 실행이 이미 정상
    발송했을 때) 수집을 시작하기도 전에 조용히 종료한다. — 정시 실행이 지연/스킵될 경우를 대비한
    "백업" 스케줄에서 쓴다.

    2026-09-23 피드백: 공휴일·연휴(설/추석 등)엔 API 호출/DB 기록도 없이 아예 조용히 종료한다 —
    발주기관이 쉬는 날이라 신규 공고 자체가 없고, 있어도 팀원이 확인할 상황이 아니다."""
    today_for_holiday = datetime.now().date()
    is_holiday, holiday_name = is_kr_holiday(today_for_holiday)
    if is_holiday:
        print(f"[공휴일] 오늘({today_for_holiday.isoformat()})은 '{holiday_name}'이라 수집/발송 없이 종료합니다.")
        return

    start_date, end_date = get_lookback_range()
    today_str = datetime.now().date().isoformat()
    conn = db.get_connection()
    # 2026-10-01: Drive 제안 검토 폴더와 짝지어진 공고의 발주기관을 '과거 제안 기관' 판정에 더한다(proposals.py).
    set_learned_proposal_orgs(db.get_proposal_orgs(conn))

    if quiet_if_empty and db.has_sent_today(conn, today_str):
        # 오늘자 발송이 이미 성공적으로 끝난 뒤 지연 실행된 백업 워크플로우다. 이 실행이 API 일시
        # 오류를 겪든 말든(COLLECTION_WARNINGS) 이미 정상 발송된 이상 무조건 조용히 종료한다.
        # 수집 자체를 시도하지 않아 불필요한 API 호출도 없다.
        print(f"[백업 실행] 오늘({today_str})은 이미 정상 발송된 기록이 있어 수집 없이 조용히 종료합니다.")
        conn.close()
        return

    # 나라장터/기업마당 API가 하루 종일 불안정한 날엔, 페이지별 재시도가 다 정상 동작해도
    # 수십 페이지를 순서대로 재시도하느라 실행 자체가 수십 분씩 걸릴 수 있다. 그러면 "정시 발송"이
    # 의미가 없어지므로, 전체 수집 단계에 상한을 두고 넘기면 남은 건 포기하고 지금까지 모은 것만 보낸다.
    # 2026-09-29: 180초 -> 240초. 세 소스를 끝까지 검사한 CI 실측이 160초로 여유가 20초뿐이었다.
    # 더 늘리면 안 된다 — 10:01 정시 실행이 10:07 백업(schedule, --quiet-if-empty) 전에 발송을 끝내야
    # 백업이 has_sent_today로 조용히 종료한다(늦어지면 중복 발송).
    set_collection_deadline(COLLECTION_TIME_LIMIT_SECONDS)
    collection_started = time.monotonic()
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
    agency_items = []

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

    # 2026-10-01: NIPA/NIA/IITP/과학창의재단/소진공/중진공/서울신보/창업진흥원 게시판(agencies.py). 나라장터
    # 대행 공고나 이미 다른 소스로 본 공고는 agencies 쪽에서 빼므로, 위 소스들을 다 모은 뒤에 돌린다.
    if include_agency:
        agency_items = _collect_agency_notices(conn, start_date, end_date, alio_orgs, bids + pre_specs + bizinfo_items)
        agency_pairs = _filter_unnotified(conn, "agency", agency_items, today_str)
        all_pairs += agency_pairs
        urgent_entries += [("기관공고", "마감", item) for _, item in agency_pairs]

    # 2026-10-01: Drive "[제안 및 검토]" 폴더를 읽어 제안 이력을 갱신한다. 오늘 수집한 원본 공고명(raw_titles)까지
    # 저장된 뒤에 돌려야 "필터가 놓친 공고"를 잡을 수 있어서 수집이 다 끝난 이 자리에서 한다.
    sync_proposals(conn, COLLECTION_WARNINGS)

    print(f"[수집 완료] 소요 {time.monotonic() - collection_started:.0f}초 (상한 {COLLECTION_TIME_LIMIT_SECONDS}초)")

    # 대시보드(AX사업기획실 공고목록)는 오늘자 전체 관심 공고(신규/기존 발송 여부 무관)를 담아서
    # 매 실행마다 최신 상태로 갱신한다. docs/index.html에 고정 경로로 써서, 호스팅(GitHub Pages 등)이
    # 정해지면 바로 서빙될 수 있게 미리 준비해둔다. 로컬/CI 어디서든 브라우저는 띄우지 않는다.
    dashboard_path = os.path.join(REPO_ROOT, "docs", "index.html")
    os.makedirs(os.path.dirname(dashboard_path), exist_ok=True)
    write_and_open_preview(
        bids, pre_specs, bizinfo_items, start_date, end_date,
        warnings=list(COLLECTION_WARNINGS), path=dashboard_path, auto_open=False, agency_items=agency_items,
    )

    # 이미 발송됐지만 마감이 안 지난 확실후보(⭐)는 마감일까지 매일 다시 안내한다 — 오늘 새로
    # 발송할 게 없어도 이 섹션은 독립적으로 존재할 수 있다.
    star_postings = db.get_active_star_postings(conn, today_str)
    star_section = format_star_reminder(star_postings, today=datetime.now().date())

    # 2026-09-18 재설계: 메시지 한 번에 너무 많은 공고가 쏟아져서, 핵심(확인필요가 아닌) 공고만
    # 마감임박 상위 5건으로 추리고 나머지는 전부 대시보드로 넘긴다.
    # 2026-09-22 피드백: ⚠️확인필요 등급을 완전히 숨기지 않고, "🔍 확인해볼 만한 공고"라는 별도
    # 섹션으로 항상 최대 5건씩 같이 보여준다 — AI가 새로 판단하는 게 아니라 기존 필터링 파이프라인이
    # 이미 계산해둔 확인필요 등급을 그대로 재사용하는 것뿐이라 추가 비용/의존성이 없다. 확실포함
    # 카드와 헤더 문구로 명확히 구분되므로 서로 혼동되지 않는다.
    core_entries = [e for e in urgent_entries if e[2].get("_tier") != "review"]
    review_entries = [e for e in urgent_entries if e[2].get("_tier") == "review"]

    # 2026-09-23 피드백: 확실포함 공고가 하나도 없는 날엔 "확인해볼 만한 공고"를 5건 -> 10건으로
    # 늘리고, 그중에서도 우리 공공사업그룹이 최근 제안서를 넣었던 기관(PROPOSAL_HISTORY, Google Drive
    # "[제안 및 검토]" 폴더 기준)의 공고를 우선 채운다 — 이미 관계가 있는 기관의 후속사업일 가능성이
    # 높아 다른 review 사유(발주기관 유형 애매함 등)보다 확인해볼 가치가 크다고 보기 때문.
    # 업종제한/지역제한은 여기서 새로 확인할 필요가 없다 — get_daily_relevant_bids가 이미 공식 API로
    # 걸러서(check_induty_eligibility/check_region_eligibility) 참가 불가로 확정된 건은 review_entries에
    # 아예 들어오지 못한다(판정 보류 건만 사유와 함께 남아 있고, 이는 대시보드/카드에서 확인 가능).
    review_top_n = 5
    if not core_entries and review_entries:
        review_top_n = 10
        proposal_matches = [e for e in review_entries if is_prior_proposal(e[2])[0]]
        other_review = [e for e in review_entries if not is_prior_proposal(e[2])[0]]
        review_entries = (proposal_matches + other_review)[:review_top_n]

    # 2026-09-22 피드백: "⚠️확인필요 N건 포함" 같은 내부 등급 문구 없이, 그냥 오늘 몇 건씩 확인했는지만
    # 담백하게 알려준다.
    summary_lines = [
        f"오늘 나라장터 입찰공고 {len(bid_pairs)}건",
    ]
    if include_pre_spec:
        summary_lines.append(f"사전규격 {len(pre_spec_pairs)}건")
    if include_bizinfo and BIZINFO_SERVICE_KEY:
        summary_lines.append(f"기업마당 {len(bizinfo_pairs)}건")
    if include_agency:
        summary_lines.append(f"기관 홈페이지 {len(agency_pairs)}건")
    summary_text = " / ".join(summary_lines) + " 확인했어요"

    dashboard_text = ""
    if DASHBOARD_URL:
        dashboard_text = f"\U0001F4CA 전체 공고 목록: <{DASHBOARD_URL}|AX사업기획실 공고목록>"

    # 2026-09-22: 두 카드 섹션 헤더는 내부 등급명("확실포함"/"확인필요") 없이, 팀원이 바로 이해할 수
    # 있는 문구로 통일한다. 확인해볼 만한 공고 섹션은 review_count와 무관하게 review_entries가 있는
    # 한 항상 노출된다(요청: "굳이 AI 연결하지 말고 기존 필터링 결과에서 3~5개 추려서 같이 보내자").
    core_title = "\U0001F4CB 오늘의 추천 공고"
    review_title = "\U0001F50D 이런 공고도 살펴보세요"

    # text(폴백/콘솔 미리보기). 2026-09-23 피드백: 카드 다음에 바로 "확인했어요" 요약줄이 붙어
    # 카드 내용과 뒤섞여 보였다 — blocks와 순서를 맞춰 요약은 맨 마지막으로 옮긴다.
    message_parts = []
    if core_entries:
        message_parts.append(format_urgent_digest(core_entries, today=datetime.now().date(), top_n=5, title=core_title))
    if review_entries:
        message_parts.append(
            format_urgent_digest(review_entries, today=datetime.now().date(), top_n=review_top_n, title=review_title)
        )
    if star_section:
        message_parts.append(star_section)
    if dashboard_text:
        message_parts.append(dashboard_text)
    message_parts.append(summary_text)
    message = "\n\n".join(message_parts)

    # 2026-09-22 피드백: API 호출 실패("일부 데이터 수집에 실패했습니다" 등) 관련 경고는 더 이상
    # 슬랙으로 보내지 않는다 — 공고 건수만큼 개별 경고 줄이 쌓여 메시지가 읽기 힘들 정도로 길어지고,
    # 대부분 일시적 API 지연/재시도 소진이라 팀 채널에서 조치할 수 있는 내용도 아니다. 콘솔 로그와
    # 대시보드(docs/index.html)에는 그대로 남겨서 운영자가 필요할 때 확인할 수 있게 한다.
    if COLLECTION_WARNINGS:
        warning_lines = "\n".join(f"- {w}" for w in COLLECTION_WARNINGS)
        print(f"[경고] 일부 데이터 수집 실패(슬랙에는 미포함):\n{warning_lines}")

    if quiet_if_empty and not all_pairs and not star_postings:
        print("[백업 실행] 새로 보낼 공고도, 확실후보 리마인드도 없어서 조용히 종료합니다.")
        conn.close()
        return

    message = f"{SLACK_MENTION}\n" + message

    # blocks(실제 Slack 레이아웃): 멘션 → 오늘의 추천 공고 → 이런 공고도 살펴보세요 →
    # 예전 협업 기관 리마인더 → 대시보드 링크 → 요약 카운트. 전체 리스트 나열은 더 이상 채널에 뿌리지 않는다.
    # 2026-09-23 피드백: 섹션들이 구분 없이 붙어 있어 카드가 서로 뒤섞여 보인다는 지적 — 섹션 사이에
    # 구분선(divider)을 넣고, 카드와 같은 굵기로 떠 있던 요약줄은 작은 회색 글씨(context)로 바꿔 맨
    # 아래로 뺐다(카드 내용과 헷갈리지 않게).
    blocks = [mrkdwn_section(SLACK_MENTION)]
    has_section = False
    if core_entries:
        blocks.extend(format_urgent_blocks(core_entries, today=datetime.now().date(), top_n=5, title=core_title))
        has_section = True
    if review_entries:
        if has_section:
            blocks.append(divider())
        blocks.extend(
            format_urgent_blocks(review_entries, today=datetime.now().date(), top_n=review_top_n, title=review_title)
        )
        has_section = True
    if star_section:
        if has_section:
            blocks.append(divider())
        blocks.append(mrkdwn_section(star_section))
        has_section = True
    if dashboard_text:
        if has_section:
            blocks.append(divider())
        blocks.append(mrkdwn_section(dashboard_text))
        has_section = True
    if has_section:
        blocks.append(divider())
    blocks.append(context_section(summary_text))

    print("\n----- 발송할 메시지 미리보기 -----")
    print(message)

    if dry_run:
        print("[dry-run] Slack 발송과 발송 기록(notified/daily_sends)을 생략합니다.")
        conn.close()
        return

    sent = send_to_slack(message, blocks=blocks)
    if sent:
        db.mark_notified(conn, [pid for pid, _ in all_pairs])
        db.mark_sent_today(conn, today_str)

    conn.close()


def _require_g2b_key():
    """나라장터 API를 실제로 호출하는 서브커맨드 진입 시점에만 키 유무를 확인한다.
    (config.py는 더 이상 import 시점에 강제 종료하지 않는다 — API를 전혀 안 쓰고 DB만 읽는
    별도 도구가 이 패키지를 import만 해도 죽는 문제가 있었다.)"""
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
        dry_run = "--dry-run" in args
        positional = [a for a in args if not a.startswith("--")]
        categories = positional[0].split(",") if positional else ["용역"]
        try:
            run_daily_notification(categories=categories, quiet_if_empty=quiet_if_empty, dry_run=dry_run)
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

    if len(sys.argv) > 1 and sys.argv[1] in ("proposals", "proposals-sync"):
        report_conn = db.get_connection()
        if sys.argv[1] == "proposals-sync":
            sync_proposals(report_conn, [])
        print_proposal_report(report_conn)
        report_conn.close()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "agency-discover":
        for name, rows in fetch_all_agency_rows().items():
            print(f"\n===== {name}: {len(rows)}건 =====")
            for row in rows:
                print(f"  {row['bidNtceDt'][:10]} | 마감 {row['bidClseDt'] or '-':10} | {row['bidNtceNm'][:60]}")
                print(f"    {row['bidNtceDtlUrl']}")
        print("\n[미지원] " + " / ".join(f"{k}: {v}" for k, v in AGENCY_UNSUPPORTED.items()))
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
        preview_conn = db.get_connection()
        agency_items = _collect_agency_notices(
            preview_conn, start_date, end_date, alio_orgs, bid_items + pre_spec_items + bizinfo_items
        )
        preview_conn.close()

        write_and_open_preview(
            bid_items, pre_spec_items, bizinfo_items, start_date, end_date, warnings=list(COLLECTION_WARNINGS),
            agency_items=agency_items,
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

        print("\n\n===== 기관 홈페이지 게시판 =====")
        daily_conn = db.get_connection()
        start_date, end_date = get_lookback_range()
        print_daily_digest(_collect_agency_notices(daily_conn, start_date, end_date, alio_orgs, items + pre_spec_items))
        daily_conn.close()
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
