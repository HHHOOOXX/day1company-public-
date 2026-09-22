"""ALIO(공공기관 경영정보 공개시스템) 공공기관 마스터 리스트.

공기업/준정부기관/기타공공기관(약 355개) 리스트를 받아, 나라장터 필터링의 ntceInsttNm을
"정확 매칭"으로 한 번 더 검증하는 화이트리스트로 쓴다(classify.py에 연동, 2026-09-22). 자체적으로
공고를 긁어오는 소스는 아니다.

2026-09-15: 활용신청은 승인됐지만, GET + ServiceKey/pageNo/numOfRows(data.go.kr 관례) 조합으로
호출하면 계속 opendata.alio.go.kr/new 로 302 리다이렉트돼 실제 호출 방식을 확정하지 못했었다.

2026-09-22 재조사 후 해결: 이 API의 진짜 스펙은 opendata.alio.go.kr에 로그인한 뒤 "오픈API
활용신청 > 기관정보/사업정보" 상세페이지의 Swagger 문서("오픈API 활용명세")에서만 볼 수 있는데
(로그인 없이는 이 상세페이지 자체가 로그인 화면으로 리다이렉트됨 — GET 실패의 원인과는 별개로
문서 접근 자체가 막혀있었음), 확인해보니 data.go.kr 계열 다른 서비스들과 완전히 다른 관례를 쓴다:
  - 메서드가 GET이 아니라 POST (파라미터는 body가 아니라 그대로 쿼리스트링).
  - 파라미터명이 대문자 ServiceKey가 아니라 소문자 시작 camelCase(serviceKey), 페이지 크기 등은
    동일하게 numOfRows/pageNo, JSON 응답 형식은 type이 아니라 resultType.
  - 응답 구조도 response.header/body.items가 아니라 최상위 result(배열)/resultCode/resultMsg/
    totalCount. resultCode는 200이 정상(사이트 문서엔 0=정상이라고도 나와 있어 둘 다 허용).
실제 호출로 검증 완료(POST /new/v1/publicinst/list.do, totalCount 355, 정상 응답).
"""

import requests

from .config import ALIO_PUBLIC_INST_URL, ALIO_SERVICE_KEY

# 사이트 문서 기준 정상 코드. 한 곳은 200(실제 응답으로 확인), Swagger 예시엔 0도 정상으로 표기돼
# 있어 둘 다 허용한다. 그 외 코드(1~11)는 전부 에러(코드 정의서 참고 — 8: 활용신청 승인 확인 실패,
# 11: 필수 파라미터 없음 등).
_SUCCESS_CODES = {0, 200}


def _fetch_alio_list(url: str, params: dict):
    """ALIO 목록 API 공통 호출. 성공 시 result(list)를 반환, 실패 시 None.
    이 API는 GET이 아니라 POST이고 파라미터도 쿼리스트링으로 붙는다(2026-09-22 확인)."""
    if not ALIO_SERVICE_KEY:
        print("[에러] .env 파일에 ALIO_SERVICE_KEY 가 설정되어 있지 않습니다.")
        print("       https://opendata.alio.go.kr 에서 공공기관 정보 오픈API 활용신청 후")
        print("       발급받은 서비스키를 .env에 추가하세요:")
        print("       ALIO_SERVICE_KEY=발급받은_서비스키")
        return None

    print(f"[요청] ALIO / {url}")
    resp = requests.post(url, params={"serviceKey": ALIO_SERVICE_KEY, **params}, timeout=15)

    content_type = resp.headers.get("Content-Type", "")
    if "json" not in content_type.lower():
        print("[경고] 응답이 JSON이 아닙니다. 서비스키·요청 URL·파라미터를 확인하세요.")
        print("------ 원문 응답 (앞 1000자) ------")
        print(resp.text[:1000])
        return None

    data = resp.json()
    result_code = data.get("resultCode")
    if result_code not in _SUCCESS_CODES:
        print(f"[에러] ALIO 응답 코드 {result_code}: {data.get('resultMsg', '')}")
        return None

    return data.get("result", [])


def fetch_alio_preview(page_size: int = 10):
    """공공기관 정보 목록을 미리보기용으로 가져온다. (탐색 전용, 1페이지만)"""
    result = _fetch_alio_list(
        ALIO_PUBLIC_INST_URL, {"pageNo": "1", "numOfRows": str(page_size), "resultType": "json"}
    )
    return {"result": result} if result is not None else None


def fetch_alio_org_names(page_size: int = 100, max_pages: int = 10) -> set:
    """전체 공공기관 명칭(instNm) 집합을 페이지네이션으로 모아서 반환한다.
    classify.py의 발주기관 화이트리스트 검증(_org_confidence)에 쓰인다.
    ALIO_SERVICE_KEY가 없거나 API 호출이 실패하면 빈 set을 반환한다 — 화이트리스트는 판정을
    보강하는 보조 신호일 뿐이라, 이게 비어있다고 전체 수집이 막히면 안 된다(호출부에서 빈 set을
    "ALIO 미사용"으로 취급해 기존 ORG_KEYWORDS 판정만으로 계속 동작한다)."""
    names = set()
    for page in range(1, max_pages + 1):
        items = _fetch_alio_list(
            ALIO_PUBLIC_INST_URL, {"pageNo": str(page), "numOfRows": str(page_size), "resultType": "json"}
        )
        if not items:
            break
        names.update(item.get("instNm", "") for item in items)
        if len(items) < page_size:
            break
    names.discard("")
    return names
