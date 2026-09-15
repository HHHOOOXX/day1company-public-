"""ALIO(공공기관 경영정보 공개시스템) 공공기관 마스터 리스트 - 보류 상태.

공기업/준정부기관/기타공공기관(약 370개) 리스트를 받아, 나라장터 필터링의 ntceInsttNm을
"정확 매칭"으로 한 번 더 검증하는 화이트리스트로 쓰려던 소스다. 자체적으로 공고를
긁어오는 소스는 아니다.

2026-09-15: 활용신청은 승인됐고 End Point(https://opendata.alio.go.kr/v1/publicinst/list.do)도
확인했지만, GET/POST + serviceKey/pageNo/numOfRows 조합을 여러 번 시도해도 계속
opendata.alio.go.kr/new 로 302 리다이렉트되어 실제 호출 방식을 확정하지 못했다.
opendata.alio.go.kr의 API 가이드 이미지가 안내하는 "OpenAPI 실행 준비" 테스트 버튼도
승인된 상세 페이지에서 찾지 못해 보류함. ORG_KEYWORDS 패턴 매칭만으로 우선 운영하고,
추후 ALIO 쪽에서 정확한 호출 예시(실제 작동하는 curl)를 확보하면 재시도한다.
"""

import requests

from .config import ALIO_PUBLIC_INST_URL, ALIO_SERVICE_KEY


def fetch_alio_preview(page_size: int = 10):
    """공공기관 정보 목록을 미리보기용으로 가져온다. (탐색 전용, 1페이지만)"""
    if not ALIO_SERVICE_KEY:
        print("[에러] .env 파일에 ALIO_SERVICE_KEY 가 설정되어 있지 않습니다.")
        print("       https://opendata.alio.go.kr 에서 공공기관 정보 오픈API 활용신청 후")
        print("       발급받은 서비스키를 .env에 추가하세요:")
        print("       ALIO_SERVICE_KEY=발급받은_서비스키")
        return None

    params = {
        "ServiceKey": ALIO_SERVICE_KEY,
        "pageNo": "1",
        "numOfRows": str(page_size),
        "type": "json",
    }

    print(f"[요청] ALIO 공공기관 정보 / {ALIO_PUBLIC_INST_URL}")
    resp = requests.get(ALIO_PUBLIC_INST_URL, params=params, timeout=15)

    content_type = resp.headers.get("Content-Type", "")
    if "json" not in content_type.lower():
        print("[경고] 응답이 JSON이 아닙니다. 서비스키·요청 URL·파라미터를 확인하세요.")
        print("       (이 URL은 아직 실제 호출로 검증되지 않은 잠정치입니다)")
        print("------ 원문 응답 (앞 1000자) ------")
        print(resp.text[:1000])
        return None

    return resp.json()
