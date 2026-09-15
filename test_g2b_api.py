"""
나라장터(조달청) 입찰공고정보서비스 API 연결 테스트 스크립트
================================================================

목적:
  - 공공데이터포털에서 발급받은 서비스키로 API가 정상 호출되는지 확인
  - 실제 응답 구조(필드명)를 눈으로 확인해서, 이후 파이프라인에서
    어떤 필드를 쓸지 결정하기 위한 "탐색용" 스크립트
  - (확장) 최근 N일간 공고를 모아 실제로 어떤 업종분류값이 붙어있는지,
    교육/양성 키워드가 어떤 분류값과 함께 나타나는지 집계해서
    우리팀(교육회사, 대학교/지자체 사업) 필터링 기준을 잡는 용도

사용법:
  1) pip install requests python-dotenv
  2) 같은 폴더에 .env 파일을 만들고 아래처럼 작성:
       G2B_SERVICE_KEY=여기에_발급받은_서비스키
  3) 기본 미리보기(최근 2일, 20건):
       python test_g2b_api.py [용역|공사|물품|외자]
  4) 분류값/키워드 집계 (최근 N일 전체 수집):
       python test_g2b_api.py classify [용역|공사|물품|외자] [일수]
       예) python test_g2b_api.py classify 용역 30
  5) 일일 배치 미리보기 (전날 공고 중 중복제거 + 관심 공고만, 슬랙 발송 전 확인용):
       python test_g2b_api.py daily [카테고리1,카테고리2,...]
       예) python test_g2b_api.py daily 용역,물품
  6) 슬랙 발송 (전날 기준 관심 공고를 실제로 Slack Incoming Webhook으로 발송):
       .env에 SLACK_WEBHOOK_URL=https://hooks.slack.com/services/... 추가 후
       python test_g2b_api.py notify [카테고리1,카테고리2,...]
       예) python test_g2b_api.py notify 용역
  7) 사전규격(용역) 탐색 미리보기 (필드명 확인용, data.go.kr에서 별도 활용신청 필요):
       python test_g2b_api.py prespec [일수]
       예) python test_g2b_api.py prespec 7

※ 서비스키 형태(Encoding/Decoding) 신경 안 쓰셔도 됩니다.
   공공데이터포털 계정에 따라 키가 하나만 보이기도 하고 두 개(Encoding/Decoding)로
   보이기도 하는데, 이 스크립트는 어떤 걸 넣으시든 urllib.parse.unquote()로
   한 번 정규화한 뒤 requests가 다시 인코딩하도록 처리해서 두 형태 모두 그대로
   동작합니다. (이미 디코딩된 키에 unquote를 걸어도 '%' 문자가 없으면 아무 변화가
   없으므로 안전합니다.) 그래도 인증 에러가 나면 활용신청 승인 여부부터 확인하세요.
"""

import os
import sys
import json
import time
from collections import Counter
from datetime import datetime, timedelta
from urllib.parse import unquote

import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

_raw_key = os.getenv("G2B_SERVICE_KEY")

if not _raw_key:
    print("[에러] .env 파일에 G2B_SERVICE_KEY 가 설정되어 있지 않습니다.")
    print("       .env 파일에 아래 줄을 추가하세요:")
    print("       G2B_SERVICE_KEY=발급받은_서비스키")
    sys.exit(1)

# Encoding형/Decoding형 어느 쪽이 들어와도 동일하게 동작하도록 정규화
SERVICE_KEY = unquote(_raw_key)

# 업무구분별 오퍼레이션 (필요한 것부터 하나씩 테스트하세요)
OPERATIONS = {
    "용역": "getBidPblancListInfoServc",
    "공사": "getBidPblancListInfoCnstwk",
    "물품": "getBidPblancListInfoThng",
    "외자": "getBidPblancListInfoFrgcpt",
}

BASE_URL = "https://apis.data.go.kr/1230000/ad/BidPublicInfoService"

# 나라장터 사전규격정보서비스(용역) - data.go.kr 상품 15129437
# 입찰공고정보서비스(BidPublicInfoService)와 별개 상품이라 활용신청을 따로 해야 정상 호출된다.
# 응답 필드명이 아직 미확정이라 탐색 전용(prespec 모드)으로만 우선 연결해둔다.
PRE_SPEC_BASE_URL = "https://apis.data.go.kr/1230000/ao/HrcspSsstndrdInfoService"
PRE_SPEC_LIST_OPERATION = "getPublicPrcureThngInfoServc"  # 사전규격 용역 목록 조회

# 우리팀(교육회사, 대학교/지자체 대상 교육·양성사업) 필터링용 키워드 후보.
# classify 모드로 실제 분류값과의 교차 결과를 보고 다듬어 나가면 됩니다.
EDU_KEYWORDS = [
    "양성", "육성", "인재양성", "역량강화", "창작자", "크리에이터",
    "콘텐츠", "아카데미", "부트캠프", "멘토링", "교육과정", "직무교육",
    "위탁교육", "이러닝", "온라인교육", "운영", "위탁운영", "교육생",
    "강사", "AI", "인공지능", "디지털콘텐츠",
]

# 대학교/지자체(및 산하기관) 발주처 판별용 키워드
ORG_KEYWORDS = [
    "대학교", "대학원", "산학협력단",
    "시청", "군청", "구청", "도청", "교육청", "재단", "진흥원",
]

# 교육 키워드+발주기관 조건은 만족하지만 우리팀 사업과 무관한 공고(축제/행사 대행성) 제외용 키워드.
# 예: "OO축제 무대설치 및 운영 용역"처럼 "운영"이 걸려 오탐되는 케이스를 걸러낸다.
# (피드백: 2026-09-14 발송분 중 축제 무대설치 공고가 섞여있었음)
EXCLUDE_KEYWORDS = [
    "축제", "축전", "페스티벌", "페스타", "FESTIVAL",
    "무대설치", "무대 설치", "무대제작", "무대설비",
    "음향", "조명설치", "행사대행", "행사 대행", "행사운영", "행사진행",
    "불꽃놀이", "체육대회", "박람회", "전시부스", "부스설치", "부스운영",
    "야시장", "플리마켓", "공연", "이벤트대행", "축제운영", "해맞이",
]


def _call_api(operation: str, params: dict, base_url: str = BASE_URL):
    """API 호출 + 공통 응답 파싱. 실패 시 None 반환."""
    url = f"{base_url}/{operation}"
    resp = requests.get(url, params=params, timeout=15)

    content_type = resp.headers.get("Content-Type", "")
    if "json" not in content_type.lower():
        print("[경고] 응답이 JSON이 아닙니다. 서비스키나 파라미터를 확인하세요.")
        print("------ 원문 응답 (앞 1000자) ------")
        print(resp.text[:1000])
        return None

    data = resp.json()
    header = data.get("response", {}).get("header", {})
    result_code = header.get("resultCode")
    result_msg = header.get("resultMsg")

    if result_code != "00":
        print(f"[에러] 결과코드 {result_code} / {result_msg}")
        return None

    body = data.get("response", {}).get("body", {})
    items = body.get("items", [])
    if isinstance(items, str):
        items = []

    return {
        "totalCount": body.get("totalCount", 0),
        "items": items,
    }


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
    """나라장터 사전규격(용역) 목록을 미리보기용으로 가져온다. (탐색 전용, 1페이지만)
    응답 필드명이 아직 확정되지 않았으므로, 첫 건의 원본 JSON을 그대로 출력해서
    품명/발주기관/규격서URL 등 실제 필드명을 확인하는 용도로만 쓴다."""
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
    """지정한 기간(begin_str~end_str, YYYYMMDDHHMM 형식)의 공고를 페이지네이션으로 전부 수집한다."""
    all_items = []
    total_count = None

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
            break

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
    - 링크: specDocFileUrl1~5 중 첫 번째 비어있지 않은 규격서 첨부파일"""
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


def _matches_any(text: str, keywords: list) -> bool:
    text = text.upper()
    return any(kw.upper() in text for kw in keywords)


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


def dedupe_latest(items: list) -> list:
    """같은 공고번호(bidNtceNo)가 정정/재공고로 여러 차수(bidNtceOrd)로 올라온 경우,
    가장 최신 차수 한 건만 남긴다. (같은 공고문이 하루 안에 중복으로 잡히는 것 방지)"""
    latest_by_no = {}
    for item in items:
        notice_no = item.get("bidNtceNo")
        if not notice_no:
            continue
        try:
            order = int(item.get("bidNtceOrd", "0"))
        except (TypeError, ValueError):
            order = 0

        existing = latest_by_no.get(notice_no)
        if existing is None or order >= existing[0]:
            latest_by_no[notice_no] = (order, item)

    return [entry[1] for entry in latest_by_no.values()]


def is_relevant_bid(item: dict) -> bool:
    """우리팀(교육회사, 대학교/지자체 대상) 기준 관심 공고 여부.
    키워드만으로는 노이즈가 많아서, 발주기관 매칭과 결합될 때만 인정한다.
    단, 제목에 축제/행사 대행성 키워드가 있으면 다른 조건과 무관하게 제외한다."""
    title = item.get("bidNtceNm", "")
    org = item.get("ntceInsttNm", "")
    if _matches_any(title, EXCLUDE_KEYWORDS):
        return False
    return _matches_any(title, EDU_KEYWORDS) and _matches_any(org, ORG_KEYWORDS)


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


def run_daily_notification(categories=("용역",), include_pre_spec: bool = True):
    """기준 기간(월요일은 직전 금요일 하루, 그 외엔 어제 하루) 관심 공고 + 사전규격(용역)을 수집해 Slack으로 발송한다.
    (스케줄러가 매일 호출할 진입점)"""
    start_date, end_date = get_lookback_range()
    items = get_daily_relevant_bids(categories=categories, start_date=start_date, end_date=end_date)
    message = format_slack_message(items, start_date, end_date, categories=categories)

    if include_pre_spec:
        pre_spec_items = get_daily_relevant_pre_specs(start_date=start_date, end_date=end_date)
        message += "\n\n" + format_pre_spec_message(pre_spec_items, start_date, end_date)

    print("\n----- 발송할 메시지 미리보기 -----")
    print(message)
    send_to_slack(message)


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
        # bidNtceNm(공고명)이 실제 필드명이 맞는지 위 원본 출력으로 확인해보세요.
        title = item.get("bidNtceNm", "(제목 필드 확인 필요)")
        org = item.get("ntceInsttNm", "(기관 필드 확인 필요)")
        print(f"{idx:2d}. [{org}] {title}")


if __name__ == "__main__":
    main()
