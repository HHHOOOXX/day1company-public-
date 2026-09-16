"""나라장터(조달청) 입찰공고정보서비스 + 사전규격정보서비스 연동."""

import time
from collections import Counter
from datetime import datetime, timedelta

import requests

from .classify import _matches_any, dedupe_latest, is_relevant_bid
from .config import (
    BASE_URL,
    EDU_KEYWORDS,
    OPERATIONS,
    ORG_KEYWORDS,
    PRE_SPEC_BASE_URL,
    PRE_SPEC_LIST_OPERATION,
    SERVICE_KEY,
)


# 페이지 수집이 재시도 끝에도 실패한 경우 여기 쌓인다.
# run_daily_notification이 발송 직전에 확인해서, 비어있지 않으면 슬랙 메시지에 경고를 붙인다.
COLLECTION_WARNINGS = []


def get_with_retry(url: str, params: dict, max_retries: int = 4, label: str = ""):
    """GET 요청. 연결 타임아웃/거부 등 네트워크 레벨 예외가 나면 지수 백오프(1s, 2s, 4s, ...)로
    최대 max_retries번까지 재시도한다. 그래도 실패하면 COLLECTION_WARNINGS에 기록하고 None을 반환한다.
    (정부 공공 API가 간헐적으로 연결 자체가 안 되는 경우가 있어, 이걸 못 잡으면 예외가 그대로
    스크립트를 죽여 알림 발송 자체가 통째로 스킵된다.)"""
    for attempt in range(1, max_retries + 1):
        try:
            return requests.get(url, params=params, timeout=15)
        except requests.exceptions.RequestException as exc:
            print(f"[재시도] 연결 실패: {exc} (시도 {attempt}/{max_retries})")
            if attempt == max_retries:
                print(f"[에러] {max_retries}회 재시도 후에도 연결 실패, 건너뜁니다.")
                COLLECTION_WARNINGS.append(f"{label}: 연결 실패 ({exc.__class__.__name__}, {max_retries}회 재시도 소진)")
                return None
            time.sleep(2 ** (attempt - 1))
    return None


def _call_api(operation: str, params: dict, base_url: str = BASE_URL, max_retries: int = 4):
    """API 호출 + 공통 응답 파싱. 나라장터 API가 간헐적으로 빈 응답/오류를 주는 경우가 있어
    지수 백오프(1s, 2s, 4s, ...)로 최대 max_retries번까지 재시도한다.
    그래도 실패하면 COLLECTION_WARNINGS에 기록하고 None을 반환한다."""
    url = f"{base_url}/{operation}"

    for attempt in range(1, max_retries + 1):
        resp = get_with_retry(url, params, max_retries=max_retries, label=operation)
        if resp is None:
            return None

        content_type = resp.headers.get("Content-Type", "")
        if "json" not in content_type.lower():
            print(f"[경고] 응답이 JSON이 아닙니다. (시도 {attempt}/{max_retries})")
            if attempt == max_retries:
                print("------ 원문 응답 (앞 1000자) ------")
                print(resp.text[:1000])
                COLLECTION_WARNINGS.append(f"{operation}: 응답이 JSON이 아님 ({max_retries}회 재시도 소진)")
                return None
            time.sleep(2 ** (attempt - 1))
            continue

        try:
            data = resp.json()
        except ValueError as exc:
            print(f"[경고] JSON 파싱 실패: {exc} (시도 {attempt}/{max_retries})")
            if attempt == max_retries:
                print("------ 원문 응답 (앞 1000자) ------")
                print(resp.text[:1000])
                COLLECTION_WARNINGS.append(f"{operation}: JSON 파싱 실패 ({max_retries}회 재시도 소진)")
                return None
            time.sleep(2 ** (attempt - 1))
            continue

        header = data.get("response", {}).get("header", {})
        result_code = header.get("resultCode")
        result_msg = header.get("resultMsg")

        if result_code != "00":
            print(f"[재시도] 결과코드 {result_code} / {result_msg} (시도 {attempt}/{max_retries})")
            if attempt == max_retries:
                print(f"[에러] {max_retries}회 재시도 후에도 실패, 이 페이지는 건너뜁니다.")
                COLLECTION_WARNINGS.append(f"{operation}: 결과코드 {result_code}/{result_msg} ({max_retries}회 재시도 소진)")
                return None
            time.sleep(2 ** (attempt - 1))
            continue

        body = data.get("response", {}).get("body", {})
        items = body.get("items", [])
        if isinstance(items, str):
            items = []

        return {
            "totalCount": body.get("totalCount", 0),
            "items": items,
        }

    return None


def fetch_recent_bids(category: str = "용역", days: int = 2, num_of_rows: int = 20):
    """최근 N일간의 입찰공고 목록을 가져온다. (미리보기용, 1페이지만)"""
    if category not in OPERATIONS:
        raise ValueError(f"category는 {list(OPERATIONS.keys())} 중 하나여야 합니다.")

    operation = OPERATIONS[category]
    end_dt = datetime.now()
    begin_dt = end_dt - timedelta(days=days)

    params = {
        "ServiceKey": SERVICE_KEY,
        "inqryDiv": "1",  # 1: 공고게시일시 기준 조회
        "type": "json",
        "inqryBgnDt": begin_dt.strftime("%Y%m%d0000"),
        "inqryEndDt": end_dt.strftime("%Y%m%d2359"),
        "pageNo": "1",
        "numOfRows": str(num_of_rows),
    }

    print(f"[요청] {category} 카테고리 / {operation}")
    print(f"       기간: {params['inqryBgnDt']} ~ {params['inqryEndDt']}")

    result = _call_api(operation, params)
    if result is None:
        return None

    print(f"[결과] 총 {result['totalCount']}건 중 {len(result['items'])}건 수신")
    return result["items"]


def fetch_pre_spec_preview(days: int = 7, num_of_rows: int = 20):
    """나라장터 사전규격(용역) 목록을 미리보기용으로 가져온다. (탐색 전용, 1페이지만)"""
    end_dt = datetime.now()
    begin_dt = end_dt - timedelta(days=days)

    params = {
        "ServiceKey": SERVICE_KEY,
        "inqryDiv": "1",  # 1: 등록일시 기준 조회 (추정 - BidPublicInfoService와 동일 관례)
        "type": "json",
        "inqryBgnDt": begin_dt.strftime("%Y%m%d0000"),
        "inqryEndDt": end_dt.strftime("%Y%m%d2359"),
        "pageNo": "1",
        "numOfRows": str(num_of_rows),
    }

    print(f"[요청] 사전규격(용역) / {PRE_SPEC_LIST_OPERATION}")
    print(f"       기간: {params['inqryBgnDt']} ~ {params['inqryEndDt']}")

    result = _call_api(PRE_SPEC_LIST_OPERATION, params, base_url=PRE_SPEC_BASE_URL)
    if result is None:
        return None

    print(f"[결과] 총 {result['totalCount']}건 중 {len(result['items'])}건 수신")
    return result["items"]


def _fetch_paginated(operation: str, begin_str: str, end_str: str, page_size: int = 100, max_pages: int = 20, base_url: str = BASE_URL):
    """지정한 기간(begin_str~end_str, YYYYMMDDHHMM 형식)의 공고를 페이지네이션으로 전부 수집한다.
    특정 페이지가 재시도 끝에도 실패하면(COLLECTION_WARNINGS에 기록됨), 그 페이지만 건너뛰고
    나머지 페이지는 계속 시도한다 — 한 페이지 실패로 뒤쪽 페이지까지 통째로 놓치지 않기 위함."""
    all_items = []
    total_count = None
    failed_pages = 0

    for page in range(1, max_pages + 1):
        params = {
            "ServiceKey": SERVICE_KEY,
            "inqryDiv": "1",
            "type": "json",
            "inqryBgnDt": begin_str,
            "inqryEndDt": end_str,
            "pageNo": str(page),
            "numOfRows": str(page_size),
        }

        result = _call_api(operation, params, base_url=base_url)
        if result is None:
            failed_pages += 1
            if total_count is not None and page * page_size >= total_count:
                break  # 마지막 페이지 근처였던 것으로 보이면 그냥 종료
            continue

        if total_count is None:
            total_count = result["totalCount"]
            print(f"       총 {total_count}건 확인됨. 수집 시작...")

        items = result["items"]
        if not items:
            break

        all_items.extend(items)
        print(f"  - {page}페이지 수집 ({len(all_items)}/{total_count})")

        if len(all_items) >= total_count:
            break

        time.sleep(0.2)  # 과도한 연속 호출 방지

    if failed_pages:
        print(f"[경고] {failed_pages}개 페이지는 재시도 후에도 실패해서 건너뛰었습니다 (일부 공고 누락 가능).")
    print(f"[완료] {len(all_items)}건 수집됨 (전체 {total_count}건 중, max_pages={max_pages} 제한)")
    return all_items


def fetch_all_bids(category: str = "용역", days: int = 30, page_size: int = 100, max_pages: int = 20):
    """최근 N일간의 입찰공고를 페이지네이션으로 전부(또는 max_pages 한도까지) 수집한다."""
    if category not in OPERATIONS:
        raise ValueError(f"category는 {list(OPERATIONS.keys())} 중 하나여야 합니다.")

    operation = OPERATIONS[category]
    end_dt = datetime.now()
    begin_dt = end_dt - timedelta(days=days)

    print(f"[요청] {category} / 최근 {days}일")
    return _fetch_paginated(
        operation,
        begin_dt.strftime("%Y%m%d0000"),
        end_dt.strftime("%Y%m%d2359"),
        page_size,
        max_pages,
    )


def get_lookback_range(today=None):
    """오늘 요일에 따라 확인할 날짜 범위(시작일, 종료일)를 정한다.
    주말에는 공고가 올라오지 않으므로, 월요일은 직전 금요일 하루만, 그 외 요일은 어제 하루만 확인한다."""
    if today is None:
        today = datetime.now().date()

    if today.weekday() == 0:  # 0 = 월요일
        start_date = today - timedelta(days=3)  # 금요일 (토/일은 공고가 없어 건너뜀)
    else:
        start_date = today - timedelta(days=1)  # 어제

    end_date = start_date
    return start_date, end_date


def fetch_bids_for_date_range(category: str = "용역", start_date=None, end_date=None, page_size: int = 100, max_pages: int = 20):
    """start_date~end_date(둘 다 date 객체, 포함) 기간 동안 게시된 공고를 수집한다. 기본값: get_lookback_range() 결과."""
    if category not in OPERATIONS:
        raise ValueError(f"category는 {list(OPERATIONS.keys())} 중 하나여야 합니다.")

    if start_date is None or end_date is None:
        start_date, end_date = get_lookback_range()

    operation = OPERATIONS[category]
    begin_str = start_date.strftime("%Y%m%d0000")
    end_str = end_date.strftime("%Y%m%d2359")

    if start_date == end_date:
        print(f"[요청] {category} / {start_date.isoformat()} 공고")
    else:
        print(f"[요청] {category} / {start_date.isoformat()} ~ {end_date.isoformat()} 공고 (주말 포함 구간)")

    return _fetch_paginated(operation, begin_str, end_str, page_size, max_pages)


def _normalize_pre_spec(item: dict) -> dict:
    """사전규격 원본 응답 필드를, 입찰공고 필터링/포맷 함수들이 쓰는 필드명으로 맞춰준다.
    - 제목: prdctClsfcNoNm(품명)
    - 발주처: rlDminsttNm(수요기관, 실제 사업 주체) 우선, 없으면 orderInsttNm(조달청 대행기관)
    - 마감: opninRgstClseDt(규격서 의견등록 마감)
    - 링크: specDocFileUrl1~5 중 첫 번째 비어있지 않은 규격서 첨부파일
    - 예산: asignBdgtAmt(배정예산금액)"""
    spec_url = next(
        (item.get(f"specDocFileUrl{i}") for i in range(1, 6) if item.get(f"specDocFileUrl{i}")),
        "",
    )
    return {
        "bidNtceNm": item.get("prdctClsfcNoNm", ""),
        "ntceInsttNm": item.get("rlDminsttNm", "") or item.get("orderInsttNm", ""),
        "bidNtceNo": item.get("bfSpecRgstNo", ""),
        "bidNtceOrd": "0",
        "bidNtceDt": item.get("rgstDt", ""),
        "bidClseDt": item.get("opninRgstClseDt", ""),
        "bidNtceDtlUrl": spec_url,
        "asignBdgtAmt": item.get("asignBdgtAmt", ""),
    }


def fetch_pre_specs_for_date_range(start_date=None, end_date=None, page_size: int = 100, max_pages: int = 20):
    """start_date~end_date(둘 다 date 객체, 포함) 기간 동안 등록된 사전규격(용역)을 수집한다.
    기본값: get_lookback_range() 결과. 반환값은 입찰공고와 동일한 필드명으로 정규화되어 있다."""
    if start_date is None or end_date is None:
        start_date, end_date = get_lookback_range()

    begin_str = start_date.strftime("%Y%m%d0000")
    end_str = end_date.strftime("%Y%m%d2359")

    if start_date == end_date:
        print(f"[요청] 사전규격(용역) / {start_date.isoformat()} 등록분")
    else:
        print(f"[요청] 사전규격(용역) / {start_date.isoformat()} ~ {end_date.isoformat()} 등록분 (주말 포함 구간)")

    raw_items = _fetch_paginated(
        PRE_SPEC_LIST_OPERATION, begin_str, end_str, page_size, max_pages, base_url=PRE_SPEC_BASE_URL
    )
    return [_normalize_pre_spec(item) for item in raw_items]


def analyze_classifications(category: str = "용역", days: int = 30):
    """수집한 공고들의 업종분류값 분포와, 교육 키워드/발주기관 키워드 매칭 현황을 집계한다."""
    items = fetch_all_bids(category=category, days=days)
    if not items:
        print("수집된 공고가 없습니다.")
        return

    # 1) 업종분류(대/중) 조합별 빈도
    clsfc_counter = Counter()
    clsfc_example = {}
    for item in items:
        large = item.get("pubPrcrmntLrgClsfcNm", "") or "(없음)"
        mid = item.get("pubPrcrmntMidClsfcNm", "") or "(없음)"
        key = (large, mid)
        clsfc_counter[key] += 1
        clsfc_example.setdefault(key, item.get("bidNtceNm", ""))

    print("\n===== 업종분류(대분류/중분류) 빈도 TOP 30 =====")
    for (large, mid), cnt in clsfc_counter.most_common(30):
        print(f"{cnt:4d}건  [{large}] > [{mid}]   예: {clsfc_example[(large, mid)]}")

    # 2) 제목에 교육 키워드가 포함된 공고 vs 분류값에 '교육'이 포함된 공고 교차 확인
    keyword_hits = []
    for item in items:
        title = item.get("bidNtceNm", "")
        if _matches_any(title, EDU_KEYWORDS):
            keyword_hits.append(item)

    clsfc_edu_hits = [
        item for item in items
        if "교육" in (item.get("pubPrcrmntMidClsfcNm", "") or "")
        or "교육" in (item.get("pubPrcrmntLrgClsfcNm", "") or "")
    ]

    keyword_only = [
        item for item in keyword_hits
        if item not in clsfc_edu_hits
    ]

    print(f"\n===== 키워드/분류값 교차 결과 (수집 {len(items)}건 기준) =====")
    print(f"제목 키워드 매칭:              {len(keyword_hits)}건")
    print(f"분류값에 '교육' 포함:          {len(clsfc_edu_hits)}건")
    print(f"키워드는 매칭되나 분류값엔 없음: {len(keyword_only)}건  (← 분류값만 쓰면 놓치는 공고)")

    if keyword_only:
        print("\n--- 키워드만 매칭된 공고 샘플 (최대 15건) ---")
        for item in keyword_only[:15]:
            large = item.get("pubPrcrmntLrgClsfcNm", "")
            mid = item.get("pubPrcrmntMidClsfcNm", "")
            print(f"  [{large}/{mid}] {item.get('bidNtceNm', '')}  ({item.get('ntceInsttNm', '')})")

    # 3) 발주기관명에 대학교/지자체 키워드가 포함된 공고 비율
    org_hits = [
        item for item in items
        if _matches_any(item.get("ntceInsttNm", ""), ORG_KEYWORDS)
    ]
    print(f"\n대학교/지자체(산하기관 포함) 발주처 매칭: {len(org_hits)}건 / 전체 {len(items)}건")

    # 4) 키워드 + 발주기관 둘 다 매칭되는 '우선순위 후보' 공고
    combined = [item for item in keyword_hits if item in org_hits]
    print(f"키워드 + 발주기관 둘 다 매칭 (우선 검토 대상): {len(combined)}건")
    if combined:
        print("\n--- 우선 검토 대상 샘플 (최대 15건) ---")
        for item in combined[:15]:
            print(f"  [{item.get('ntceInsttNm', '')}] {item.get('bidNtceNm', '')}")


def get_daily_relevant_bids(categories=("용역",), start_date=None, end_date=None):
    """지정 기간(기본값: get_lookback_range() — 월요일은 직전 금요일 하루, 그 외엔 어제 하루) 동안 게시된 공고 중,
    중복 제거 + 우리팀 관심 조건(키워드∩발주기관)을 만족하는 공고만 반환한다.
    (매일 지정 시각에 실행되는 슬랙 알림 배치에서 호출할 핵심 함수)"""
    if start_date is None or end_date is None:
        start_date, end_date = get_lookback_range()

    if start_date == end_date:
        print(f"[일일 배치] 기준일: {start_date.isoformat()}")
    else:
        print(f"[일일 배치] 기준 기간: {start_date.isoformat()} ~ {end_date.isoformat()}")

    raw_items = []
    for category in categories:
        raw_items.extend(fetch_bids_for_date_range(category=category, start_date=start_date, end_date=end_date))

    deduped = dedupe_latest(raw_items)
    print(f"[중복제거] {len(raw_items)}건 → {len(deduped)}건 (공고번호 기준 최신 차수만 유지)")

    relevant = [item for item in deduped if is_relevant_bid(item)]
    relevant.sort(key=lambda item: item.get("bidNtceDt", ""))
    print(f"[필터링] 키워드 + 발주기관 동시 매칭: {len(relevant)}건")

    return relevant


def get_daily_relevant_pre_specs(start_date=None, end_date=None):
    """지정 기간(기본값: get_lookback_range()) 동안 등록된 사전규격(용역) 중,
    중복 제거 + 우리팀 관심 조건(키워드∩수요기관)을 만족하는 건만 반환한다."""
    if start_date is None or end_date is None:
        start_date, end_date = get_lookback_range()

    raw_items = fetch_pre_specs_for_date_range(start_date=start_date, end_date=end_date)

    deduped = dedupe_latest(raw_items)
    print(f"[중복제거] {len(raw_items)}건 → {len(deduped)}건 (사전규격등록번호 기준)")

    relevant = [item for item in deduped if is_relevant_bid(item)]
    relevant.sort(key=lambda item: item.get("bidNtceDt", ""))
    print(f"[필터링] 키워드 + 수요기관 동시 매칭: {len(relevant)}건")

    return relevant
