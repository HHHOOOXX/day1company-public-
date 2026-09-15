"""기업마당(bizinfo.go.kr) 중소기업 지원사업 공고 API - 탐색(discovery) 전용.

NIPA/NIA/IITP/중진공/소진공 등이 올리는 지원사업 공고가 상당수 여기 집계된다.
정확한 응답 필드명은 crtfcKey 발급 후 실제 호출로 확인해야 하므로,
아직 정식 수집/필터링 파이프라인에는 연결하지 않고 원본 JSON을 눈으로 확인하는 용도로만 둔다.
(나라장터 사전규격을 처음 붙일 때와 동일한 절차: 탐색 → 필드명 확정 → 정규화 함수 작성)
"""

import requests

from .config import BIZINFO_API_URL, BIZINFO_SERVICE_KEY


def fetch_bizinfo_preview(page_size: int = 10):
    """기업마당 지원사업 공고 목록을 미리보기용으로 가져온다. (탐색 전용, 1페이지만)"""
    if not BIZINFO_SERVICE_KEY:
        print("[에러] .env 파일에 BIZINFO_SERVICE_KEY 가 설정되어 있지 않습니다.")
        print("       https://www.bizinfo.go.kr 에서 회원가입 후 OpenAPI 메뉴에서 활용신청하면")
        print("       crtfcKey를 발급받을 수 있습니다. .env에 아래 줄을 추가하세요:")
        print("       BIZINFO_SERVICE_KEY=발급받은_crtfcKey")
        return None

    params = {
        "crtfcKey": BIZINFO_SERVICE_KEY,
        "dataType": "json",
        "pageUnit": str(page_size),
        "pageIndex": "1",
    }

    print(f"[요청] 기업마당 지원사업 공고 / {BIZINFO_API_URL}")
    resp = requests.get(BIZINFO_API_URL, params=params, timeout=15)

    content_type = resp.headers.get("Content-Type", "")
    if "json" not in content_type.lower():
        print("[경고] 응답이 JSON이 아닙니다. crtfcKey나 파라미터를 확인하세요.")
        print("------ 원문 응답 (앞 1000자) ------")
        print(resp.text[:1000])
        return None

    return resp.json()
