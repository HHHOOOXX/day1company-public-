"""나라장터(조달청) 입찰공고정보서비스 + 사전규격정보서비스 연동."""

import re
import time
from collections import Counter
from datetime import datetime, timedelta
from urllib.parse import unquote

import requests

from .classify import (
    DEADLINE_GATE_DAYS,
    _matches_any,
    attach_confidence,
    build_learned_keywords,
    dedupe_latest,
    is_relevant_bid,
    is_relevant_prespec,
    passes_deadline_gate,
)
from .config import (
    BASE_URL,
    COMPANY_INDUSTRY_CODES,
    EDU_KEYWORDS,
    EXCLUDE_INDUSTRY_CODES,
    OPERATIONS,
    ORG_KEYWORDS,
    PRE_SPEC_BASE_URL,
    PRE_SPEC_LIST_OPERATION,
    SERVICE_KEY,
)
from .doc_extract import extract_document_text, find_industry_codes, requires_ineligible_certificate

LICENSE_LIMIT_OPERATION = "getBidPblancListInfoLicenseLimit"
PRTCPT_PSBL_RGN_OPERATION = "getBidPblancListInfoPrtcptPsblRgn"
HQ_REGION_TOKEN = "서울"


# 페이지 수집이 재시도 끝에도 실패한 경우 여기 쌓인다.
# run_daily_notification이 발송 직전에 확인해서, 비어있지 않으면 슬랙 메시지에 경고를 붙인다.
COLLECTION_WARNINGS = []

# 전체 수집 단계(나라장터 입찰+사전규격+기업마당 합산)의 시간 상한.
# run_daily_notification 시작 시 set_collection_deadline()으로 설정된다.
# 페이지별/호출별 재시도는 각자 정상 동작해도, API가 하루 종일 불안정하면 수십 페이지를
# 순서대로 재시도하느라 실행이 수십 분씩 걸릴 수 있다 — 그러면 "정시 발송"의 의미가 없어지므로,
# 이 상한을 넘기면 남은 페이지/소스는 그 자리에서 포기하고 지금까지 모은 것만으로 발송한다.
COLLECTION_DEADLINE = None


def set_collection_deadline(seconds: float):
    global COLLECTION_DEADLINE
    COLLECTION_DEADLINE = time.monotonic() + seconds


def _deadline_exceeded() -> bool:
    return COLLECTION_DEADLINE is not None and time.monotonic() > COLLECTION_DEADLINE


def get_with_retry(url: str, params: dict, max_retries: int = 4, label: str = ""):
    """GET 요청. 연결 타임아웃/거부 등 네트워크 레벨 예외가 나면 지수 백오프(1s, 2s, 4s, ...)로
    최대 max_retries번까지 재시도한다. 그래도 실패하면 COLLECTION_WARNINGS에 기록하고 None을 반환한다.
    (정부 공공 API가 간헐적으로 연결 자체가 안 되는 경우가 있어, 이걸 못 잡으면 예외가 그대로
    스크립트를 죽여 알림 발송 자체가 통째로 스킵된다.) 전체 수집 시간 상한을 넘겼으면 재시도 없이 바로 포기한다."""
    for attempt in range(1, max_retries + 1):
        if _deadline_exceeded():
            print(f"[경고] 전체 수집 시간 상한 초과 — {label} 재시도 중단")
            COLLECTION_WARNINGS.append(f"{label}: 시간 상한 초과로 재시도 중단")
            return None
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
        if _deadline_exceeded():
            print(f"[경고] 전체 수집 시간 상한 초과 — {page}페이지부터 중단합니다.")
            COLLECTION_WARNINGS.append(f"{operation}: 시간 상한 초과로 {page}페이지부터 중단 (일부 공고 누락 가능)")
            break

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
    - 링크: 일단 specDocFileUrl1~5 중 첫 번째로 채워두고, 최종 필터링을 통과한 건에 한해
      get_daily_relevant_pre_specs에서 resolve_task_order_url로 '과업지시서'류 파일로 교체한다
      (사전규격 API는 첨부파일명 필드를 안 줘서, 전체 원본 목록은 _spec_doc_urls에 남겨둔다).
    - 예산: asignBdgtAmt(배정예산금액)"""
    spec_urls = [item.get(f"specDocFileUrl{i}", "") for i in range(1, 6)]
    spec_url = next((u for u in spec_urls if u), "")
    return {
        "bidNtceNm": item.get("prdctClsfcNoNm", ""),
        "ntceInsttNm": item.get("rlDminsttNm", "") or item.get("orderInsttNm", ""),
        "bidNtceNo": item.get("bfSpecRgstNo", ""),
        "bidNtceOrd": "0",
        "bidNtceDt": item.get("rgstDt", ""),
        "bidClseDt": item.get("opninRgstClseDt", ""),
        "bidNtceDtlUrl": spec_url,
        "asignBdgtAmt": item.get("asignBdgtAmt", ""),
        "_spec_doc_urls": spec_urls,
    }


TASK_ORDER_FILENAME_HINTS = ["과업지시서", "과업내용서", "과업수행계획서", "과업지시"]


def _content_disposition_filename(url: str) -> str:
    """첨부파일 다운로드 URL에 요청을 보내 실제 파일명을 얻는다(stream=True로 헤더만 읽고 바로 닫아서
    본문 다운로드는 하지 않음). 사전규격 API 응답엔 파일명 필드가 아예 없어서 이 방법밖에 없다."""
    try:
        resp = requests.get(url, timeout=10, stream=True, headers={"User-Agent": "Mozilla/5.0"})
        content_disposition = resp.headers.get("Content-Disposition", "")
        resp.close()
    except requests.exceptions.RequestException:
        return ""

    match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", content_disposition)
    if not match:
        return ""
    return unquote(match.group(1))


def _fetch_attachment(url: str):
    """첨부파일을 통째로 내려받아 (파일명, 원문 바이트)를 반환한다. 실패 시 ("", b"")."""
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    except requests.exceptions.RequestException:
        return "", b""
    match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", resp.headers.get("Content-Disposition", ""))
    filename = unquote(match.group(1)) if match else ""
    return filename, resp.content


def resolve_prespec_attachments(spec_urls: list):
    """사전규격 첨부파일(specDocFileUrl1~5)을 한 번씩만 내려받아, (1) '과업지시서'류로 이어줄 링크,
    (2) 문서 본문에서 찾은 우리 보유 업종코드 교집합, (3) 소상공인/중소기업 확인서 요구 여부를 함께
    반환한다. 세 판정이 같은 다운로드를 재사용하도록 묶어서, 건당 최대 5번인 요청 횟수가 늘지 않게 한다.
    반환값: (best_url: str, matched_industry_codes: set, restricted: bool). 최종 필터를 통과한
    소수 건에만 호출한다.
    2026-09-22 피드백: 사전규격 R26BD00276775(한동대학교 산학협력단) 과업지시서에 "중·소기업·
    소상공인 확인서"를 소지한 자만 참가 가능하다고 명시돼 있었는데, 이런 기업규모 제한은 API 어디에도
    필드로 노출되지 않고 첨부문서 원문에만 있어 직접 열어봐야만 확인 가능하다."""
    candidates = [u for u in spec_urls if u]
    if not candidates:
        return "", set(), False

    fetched = [(url, *_fetch_attachment(url)) for url in candidates]

    best_url = candidates[0]
    for hint in TASK_ORDER_FILENAME_HINTS:
        for url, filename, _ in fetched:
            if hint in filename:
                best_url = url
                break
        else:
            continue
        break

    matched_codes = set()
    restricted = False
    for _, filename, raw in fetched:
        if not raw:
            continue
        text = extract_document_text(raw, filename)
        matched_codes |= find_industry_codes(text)
        if requires_ineligible_certificate(text):
            restricted = True

    return best_url, matched_codes & COMPANY_INDUSTRY_CODES, restricted


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


def _induty_code(lcns_lmt_nm: str) -> str:
    """면허제한정보 응답의 'lcnsLmtNm'("학술.연구용역/1169" 형식)에서 뒤의 업종코드만 뽑는다."""
    return lcns_lmt_nm.rsplit("/", 1)[-1].strip() if "/" in (lcns_lmt_nm or "") else ""


def fetch_license_limit(bid_ntce_no: str, bid_ntce_ord: str):
    """해당 공고의 업종제한(면허제한) 목록을 조회한다. API 실패 시 None(판정 불가)."""
    params = {
        "ServiceKey": SERVICE_KEY,
        "type": "json",
        "inqryDiv": "2",  # 2: 공고번호+차수 기준 조회
        "bidNtceNo": bid_ntce_no,
        "bidNtceOrd": bid_ntce_ord,
        "pageNo": "1",
        "numOfRows": "100",
    }
    result = _call_api(LICENSE_LIMIT_OPERATION, params)
    if result is None:
        return None
    return result["items"]


def check_induty_eligibility(item: dict):
    """업종제한이 걸린 공고를 그룹별로 엄격하게 판정한다.
    나라장터의 복수면허제한은 그룹(lmtGrpNo)별로 각각 최소 1개 업종을 보유해야 하는 구조라
    (그룹 내부는 OR, 그룹 간은 AND), 어느 한 그룹이라도 보유 업종코드와 전혀 겹치지 않으면
    우리 회사 단독으로는 참가 불가로 확정한다.
    단, 제한경쟁이라고만 표시되고 실제 업종코드가 기재되지 않은 그룹(또는 응답 전체)은
    판정 근거가 없으므로 통과로 간주해 보수적으로 포함시킨다.

    2026-09-18 수정: 목록 조회의 indstrytyLmtYn 플래그로 API 호출을 건너뛰던 걸 없앴다 — 같은
    문제의 지역제한 플래그(rgnLmtBidLocplcJdgmBssCd)가 실제로는 제한이 있는데도 빈 값으로 오는
    사례가 실증됐고(check_region_eligibility 참고), 업종제한 플래그도 같은 신뢰성 문제가 있을 수
    있어 플래그와 무관하게 항상 면허제한정보를 직접 조회해서 확정한다.

    반환값: (ok, certain, matched_codes)
      ok=False   -> 업종 불일치로 참가 불가 확정 (호출 측에서 제외).
      certain=False -> API 조회 실패로 판정을 확정하지 못함. ok는 일단 True(통과)로 두되,
                        호출 측에서 '확인 필요'로 표시해야 한다는 신호.
      matched_codes -> 실제 업종제한 목록에서 우리 보유 업종코드와 겹친 코드들(2026-09-18 추가).
                        대시보드 '업종코드 확인' 열에 사전규격의 문서 기반 확인과 동일하게 표시한다."""
    bid_ntce_no = item.get("bidNtceNo", "")
    bid_ntce_ord = item.get("bidNtceOrd", "0")
    limits = fetch_license_limit(bid_ntce_no, bid_ntce_ord)

    if limits is None:
        COLLECTION_WARNINGS.append(
            f"업종제한 조회 실패: {bid_ntce_no}-{bid_ntce_ord} (판정 보류, 일단 포함 — 직접 확인 필요)"
        )
        return True, False, set()

    if not limits:
        # 제한경쟁이지만 업종코드 자체가 기재되지 않은 경우 -> 포함
        return True, True, set()

    groups = {}
    for row in limits:
        grp = row.get("lmtGrpNo", "0")
        groups.setdefault(grp, set()).add(_induty_code(row.get("lcnsLmtNm", "")))

    matched_codes = set()
    for codes in groups.values():
        codes.discard("")
        if not codes:
            continue  # 이 그룹은 업종코드 미기재 -> 통과로 간주
        overlap = codes & COMPANY_INDUSTRY_CODES
        if not overlap:
            excluded_hit = codes & EXCLUDE_INDUSTRY_CODES
            if excluded_hit:
                print(f"  [업종제외] {excluded_hit} 코드가 제한 그룹에 있고 보유 업종과 안 겹침 -> 배제")
            return False, True, set()  # 이 그룹을 보유 업종으로 채울 수 없음 -> 참가 불가 확정
        matched_codes |= overlap

    return True, True, matched_codes


def fetch_participation_region(bid_ntce_no: str, bid_ntce_ord: str):
    """해당 공고의 참가가능지역(prtcptPsblRgnNm) 목록을 조회한다. API 실패 시 None(판정 불가)."""
    params = {
        "ServiceKey": SERVICE_KEY,
        "type": "json",
        "inqryDiv": "2",
        "bidNtceNo": bid_ntce_no,
        "bidNtceOrd": bid_ntce_ord,
        "pageNo": "1",
        "numOfRows": "100",
    }
    result = _call_api(PRTCPT_PSBL_RGN_OPERATION, params)
    if result is None:
        return None
    return result["items"]


def check_region_eligibility(item: dict):
    """공고의 참가가능지역에 우리 본사 소재지(서울)가 포함되는지 확인한다. 데이원컴퍼니 본사가
    서울에 있으므로, 참가가능지역 조회 결과가 비어있거나(=지역제한 없음) '서울'이 포함되면
    통과시키고, 그 외 지역으로만 한정되면 참가 불가로 확정한다.

    2026-09-18 수정: 목록 조회의 rgnLmtBidLocplcJdgmBssCd 플래그가 비어있는데도 실제로는 특정
    지역(예: 경상남도/부산광역시)으로 제한된 공고가 실제로 확인됐다(R26BK01735960, R26BK01734391 —
    둘 다 플래그는 빈 값인데 getBidPblancListInfoPrtcptPsblRgn 조회 시 지역이 나옴). 플래그를
    신뢰할 수 없으므로 값과 무관하게 항상 참가가능지역을 직접 조회해서 확정한다.
    반환값: (ok, certain) — check_induty_eligibility와 동일한 규약."""
    bid_ntce_no = item.get("bidNtceNo", "")
    bid_ntce_ord = item.get("bidNtceOrd", "0")
    regions = fetch_participation_region(bid_ntce_no, bid_ntce_ord)

    if regions is None:
        COLLECTION_WARNINGS.append(
            f"지역제한 조회 실패: {bid_ntce_no}-{bid_ntce_ord} (판정 보류, 일단 포함 — 직접 확인 필요)"
        )
        return True, False

    if not regions:
        return True, True  # 조회 결과가 비어있으면 지역제한이 없는 공고 -> 통과

    region_names = [r.get("prtcptPsblRgnNm", "") for r in regions]
    if any(HQ_REGION_TOKEN in name for name in region_names):
        return True, True

    print(f"  [지역제외] 참가가능지역 {region_names} 에 '{HQ_REGION_TOKEN}' 없음 -> 배제")
    return False, True


def is_negotiated_contract(item: dict) -> bool:
    """수의계약(경쟁입찰 없이 발주기관이 특정 업체와 바로 계약)은 완전히 제외한다.
    2026-09-18 재확인: 실제 API 데이터로 검증한 결과 '수의시담'/'다자간수의시담'은 낙찰방법
    (sucsfbidMthdNm)의 하위 항목일 뿐이고, 이런 건은 예외 없이 계약방법(cntrctCnclsMthdNm)이
    항상 '수의계약'으로 잡힌다(29일치 2100건 전수조사, 수의시담 344건 전부 확인) — 그래서
    cntrctCnclsMthdNm만 봐도 수의시담/다자간수의시담까지 전부 같이 제외된다."""
    return (item.get("cntrctCnclsMthdNm") or "") == "수의계약"


def bid_requires_ineligible_certificate(item: dict, max_files: int = 3) -> bool:
    """입찰공고 첨부파일(ntceSpecDocUrl1~10, 파일명은 ntceSpecFileNm1~10로 API가 바로 알려줘서
    사전규격과 달리 Content-Disposition 헤더 조회가 필요 없음)을 열어 소상공인/중소기업 확인서를
    참가자격으로 요구하는지 확인한다. 대부분 앞쪽 1~2개가 실제 공고문/제안요청서라 max_files개까지만
    시도해 다운로드 비용을 제한한다.

    업종제한/지역제한과 달리 이 조건을 나타내는 API 필드가 전혀 없어(2026-09-22 확인: 사전규격
    R26BD00276775 과업지시서에 "중·소기업·소상공인 확인서" 소지자만 참가 가능하다고 명시돼
    있었는데, 목록 API 어디에도 이를 알려주는 필드가 없었음) 첨부문서를 직접 열어봐야만 판정 가능하다."""
    checked = 0
    for i in range(1, 11):
        if checked >= max_files:
            break
        url = item.get(f"ntceSpecDocUrl{i}", "")
        if not url:
            continue
        filename, raw = _fetch_attachment(url)
        checked += 1
        if not raw:
            continue
        text = extract_document_text(raw, filename or item.get(f"ntceSpecFileNm{i}", ""))
        if requires_ineligible_certificate(text):
            return True
    return False


def get_daily_relevant_bids(categories=("용역",), start_date=None, end_date=None):
    """지정 기간(기본값: get_lookback_range() — 월요일은 직전 금요일 하루, 그 외엔 어제 하루) 동안 게시된 공고 중,
    중복 제거 + 우리팀 관심 조건(키워드∩발주기관) + 수의계약/수의시담 제외 + 업종제한(보유 업종코드) +
    지역제한(서울) 조건을 만족하는 공고를 반환한다. 애매하게 판정된 건('review' 등급)도 제외하지 않고
    ⚠️ 태그를 달아 같이 포함시킨다 — 업종/지역 필터가 엄격해질수록 애매한 진짜 기회를 조용히 놓칠 위험이
    커지기 때문. (매일 지정 시각에 실행되는 슬랙 알림 배치에서 호출할 핵심 함수)"""
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
    before_nego = len(relevant)
    relevant = [item for item in relevant if not is_negotiated_contract(item)]
    relevant.sort(key=lambda item: item.get("bidNtceDt", ""))
    print(f"[필터링] 키워드 + 발주기관 동시 매칭: {before_nego}건 (수의계약/수의시담 제외 후 {len(relevant)}건)")

    attach_confidence(relevant)

    eligible = []
    for item in relevant:
        ind_ok, ind_certain, matched_codes = check_induty_eligibility(item)
        if not ind_ok:
            continue
        rgn_ok, rgn_certain = check_region_eligibility(item)
        if not rgn_ok:
            continue
        # 2026-09-22 추가: 업종제한/지역제한 API 통과 후에도 첨부 공고문에만 소상공인/중소기업
        # 확인서 요구가 적혀있는 경우가 있어(사전규격과 동일 문제), 전체 수집 시간 상한에 걸리지
        # 않은 한 마지막으로 확인한다. 확인서 요구가 있으면 다른 조건과 무관하게 참가 불가로 제외.
        if not _deadline_exceeded() and bid_requires_ineligible_certificate(item):
            print(f"  [제외] {item.get('bidNtceNo', '')} 소상공인/중소기업 확인서 요구 확인 -> 배제")
            continue
        # 2026-09-18 추가: 업종제한사항에서 실제로 우리 보유 코드가 확인된 공고는 대시보드
        # '업종코드 확인' 열에도 사전규격과 동일하게 표시한다.
        item["_industry_codes"] = matched_codes
        if not ind_certain:
            item["_tier"] = "review"
            item["_reasons"].append("업종제한 조회 실패로 참가 가능 여부 판정 보류 — 직접 확인 필요")
        if not rgn_certain:
            item["_tier"] = "review"
            item["_reasons"].append("지역제한 조회 실패로 참가 가능 여부 판정 보류 — 직접 확인 필요")
        eligible.append(item)

    review_count = sum(1 for item in eligible if item.get("_tier") == "review")
    print(f"[업종/지역필터링] {len(relevant)}건 → {len(eligible)}건 (확인필요 {review_count}건 포함해서 전부 발송)")

    today = datetime.now().date()
    before_deadline_gate = len(eligible)
    eligible = [item for item in eligible if passes_deadline_gate(item, today)]
    print(
        f"[마감임박필터링] {before_deadline_gate}건 → {len(eligible)}건 "
        f"(마감 {DEADLINE_GATE_DAYS}일 미만이면서 확실후보(star)가 아닌 건 제외)"
    )

    return eligible


def get_daily_relevant_pre_specs(start_date=None, end_date=None, historical_bid_titles_orgs=None):
    """지정 기간(기본값: get_lookback_range()) 동안 등록된 사전규격(용역) 중,
    중복 제거 + '학습된 키워드' 조건(핵심 발주기관 + 좁혀진 키워드)을 만족하는 건만 반환한다.

    2026-09-18 재설계: 사전규격 API는 업종제한/지역제한 관련 필드/API가 아예 없어서(실제 호출로 확인
    — getBidPblancListInfoLicenseLimit/PrtcptPsblRgn 둘 다 "해당 오픈API 서비스가 없거나 폐기됨")
    입찰공고와 동일한 사후 검증을 할 수 없다. 대신 (1) 과거에 실제로 확실하게 매칭됐던 나라장터
    입찰공고 제목에서 학습한 키워드와 (2) 우리팀 실제 수주 이력(WIN_HISTORY)에서 뽑은 키워드로만
    사전에 좁혀서 필터링한다 (classify.build_learned_keywords 참고) — 애초에 전통시장/뷰티처럼
    명백히 무관한 건이 안 들어오게 하는 게 목표.

    통과한 소수의 건에 한해 링크를 규격서 첨부파일 중 '과업지시서'류로 교체한다(resolve_task_order_url).
    historical_bid_titles_orgs: db.get_historical_bid_titles() 결과. 없으면 WIN_HISTORY만으로 학습."""
    if start_date is None or end_date is None:
        start_date, end_date = get_lookback_range()

    raw_items = fetch_pre_specs_for_date_range(start_date=start_date, end_date=end_date)

    deduped = dedupe_latest(raw_items)
    print(f"[중복제거] {len(raw_items)}건 → {len(deduped)}건 (사전규격등록번호 기준)")

    learned_keywords = build_learned_keywords(historical_bid_titles_orgs or [])
    print(f"[학습] 사전규격 필터링용 키워드 {len(learned_keywords)}개 (과거 확실매칭 입찰공고 + 수주이력 기반)")

    relevant = [item for item in deduped if is_relevant_prespec(item, learned_keywords)]
    before_attach = len(relevant)
    # 2026-09-18 피드백: 첨부파일이 아예 없어서 과업지시서/규격서 중 어느 것도 링크로 못 거는 건은
    # 발송해도 실무자가 열어볼 자료가 없으므로 제외한다.
    relevant = [item for item in relevant if any(item.get("_spec_doc_urls", []))]
    relevant.sort(key=lambda item: item.get("bidNtceDt", ""))
    print(
        f"[필터링] 학습된 키워드 + 핵심 발주기관 동시 매칭: {before_attach}건 "
        f"(첨부파일 없는 건 제외 후 {len(relevant)}건)"
    )

    attach_confidence(relevant)

    final = []
    for item in relevant:
        spec_urls = item.pop("_spec_doc_urls", [])
        if _deadline_exceeded():
            # 전체 수집 시간 상한에 걸리면 남은 건은 첨부파일 본문 다운로드/탐색 없이
            # 기존 동작(첫 번째 첨부파일, 업종코드 미확인, 확인서 요구 미확인)으로 대체하고 넘어간다
            # — 정시 발송이 우선이다.
            item["bidNtceDtlUrl"] = next((u for u in spec_urls if u), "")
            item["_industry_codes"] = set()
            final.append(item)
            continue
        best_url, matched_codes, restricted = resolve_prespec_attachments(spec_urls)
        if restricted:
            # 2026-09-22 피드백: 과업지시서/제안요청서에 "중·소기업·소상공인 확인서" 등 우리가
            # 발급받을 수 없는 확인서를 참가자격으로 요구하면, 키워드/업종코드가 아무리 잘 맞아도
            # 애초에 참가 자체가 불가능하므로 완전히 제외한다(실사례: R26BD00276775).
            print(f"  [제외] {item.get('bidNtceNo', '')} 소상공인/중소기업 확인서 요구 확인 -> 배제")
            continue
        item["bidNtceDtlUrl"] = best_url
        item["_industry_codes"] = matched_codes
        if matched_codes:
            # 2026-09-18 피드백: 제안요청서 원문에서 우리 보유 업종코드가 실제로 확인되면(예:
            # "이러닝콘텐츠업 (업종코드: 6527)") 키워드 매칭보다 훨씬 강한 신호이므로 확실로 격상한다.
            item["_tier"] = "include"
            item["_reasons"] = []
        final.append(item)

    # 2026-09-22 피드백: 마감임박 게이트(passes_deadline_gate)는 여기 적용하지 않는다. 사전규격의
    # "마감"(opninRgstClseDt)은 실제 입찰 참가 마감이 아니라 규격서 의견등록 기간일 뿐이고, 실제
    # 검증해보니 사전규격 91%(209/230건, 중앙값 4일)가 애초에 7일 미만이라 그대로 적용하면 사전규격
    # 알림이 사실상 전부 막힌다. 진짜 입찰공고는 나중에 별도 마감으로 다시 뜨므로, 여기서는 게이트를
    # 건너뛴다 — 마감임박 필터는 입찰공고(get_daily_relevant_bids)/기업마당에서만 적용한다.

    return final
