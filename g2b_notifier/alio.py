"""ALIO(공공기관 경영정보 공개시스템) 공공기관 마스터 리스트 - 탐색(discovery) 전용.

공기업/준정부기관/기타공공기관(약 370개) 리스트를 받아, 나라장터 필터링의 ntceInsttNm을
"정확 매칭"으로 한 번 더 검증하는 화이트리스트로 쓰기 위한 소스다. 자체적으로 공고를
긁어오는 소스는 아니다.

정확한 요청 파라미터/인증키 발급 절차를 아직 확정하지 못했으므로, 원본 응답을 눈으로
확인하는 탐색 모드만 우선 만들어둔다. (URL은 잠정치 — 실제 호출 결과를 보고 확정 필요)
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
