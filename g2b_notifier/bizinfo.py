"""기업마당(bizinfo.go.kr) 중소기업 지원사업 공고 API.

NIPA/NIA/IITP/중진공/소진공 등이 올리는 지원사업 공고가 상당수 여기 집계된다.
2026-09-15 실제 호출로 필드명 확정 완료:
  pblancNm(공고명), jrsdInsttNm(소관기관), excInsttNm(수행기관, "직접수행"이면 소관기관과 동일 취급),
  pblancId(공고ID), creatPnttm(등록일시), reqstBeginEndDe(신청기간 — "YYYY-MM-DD ~ YYYY-MM-DD"
  또는 "모집 완료시까지" 같은 자유 텍스트라 별도 파싱 없이 그대로 표시), pblancUrl(상세페이지)
"""

import time
from datetime import datetime

import requests

from .classify import dedupe_latest, is_relevant_bid
from .config import BIZINFO_API_URL, BIZINFO_SERVICE_KEY
from .g2b import COLLECTION_WARNINGS, get_lookback_range


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


def _normalize_bizinfo(item: dict) -> dict:
    """기업마당 원본 응답 필드를, 입찰공고 필터링/포맷 함수들이 쓰는 필드명으로 맞춰준다."""
    jrsd = item.get("jrsdInsttNm", "")
    exc = item.get("excInsttNm", "")
    org = f"{jrsd}/{exc}" if exc and exc != "직접수행" and exc != jrsd else jrsd

    return {
        "bidNtceNm": item.get("pblancNm", ""),
        "ntceInsttNm": org,
        "bidNtceNo": item.get("pblancId", ""),
        "bidNtceOrd": "0",
        "bidNtceDt": item.get("creatPnttm", ""),
        "bidClseDt": item.get("reqstBeginEndDe", ""),
        "bidNtceDtlUrl": item.get("pblancUrl", ""),
    }


def _creat_date(raw_item: dict):
    """creatPnttm("YYYY-MM-DD HH:MM:SS")에서 date만 뽑는다. 파싱 실패 시 None."""
    raw = raw_item.get("creatPnttm", "")
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def fetch_bizinfo_for_date_range(start_date=None, end_date=None, page_size: int = 100, max_pages: int = 10):
    """start_date~end_date(포함) 기간 동안 등록된 기업마당 지원사업 공고를 수집한다.
    이 API는 날짜범위 조회 파라미터가 확인되지 않아, creatPnttm 기준 최신순(관찰된 정렬)으로
    페이지를 넘기다가 start_date보다 오래된 항목이 나오면 중단하는 방식으로 클라이언트에서 필터링한다."""
    if not BIZINFO_SERVICE_KEY:
        print("[에러] BIZINFO_SERVICE_KEY가 없어 기업마당 조회를 건너뜁니다.")
        return []

    if start_date is None or end_date is None:
        start_date, end_date = get_lookback_range()

    if start_date == end_date:
        print(f"[요청] 기업마당 / {start_date.isoformat()} 등록분")
    else:
        print(f"[요청] 기업마당 / {start_date.isoformat()} ~ {end_date.isoformat()} 등록분")

    collected = []
    for page in range(1, max_pages + 1):
        params = {
            "crtfcKey": BIZINFO_SERVICE_KEY,
            "dataType": "json",
            "pageUnit": str(page_size),
            "pageIndex": str(page),
        }
        resp = requests.get(BIZINFO_API_URL, params=params, timeout=15)
        if "json" not in resp.headers.get("Content-Type", "").lower():
            print(f"[경고] 기업마당 응답이 JSON이 아닙니다 ({page}페이지). crtfcKey를 확인하세요.")
            COLLECTION_WARNINGS.append(f"기업마당 {page}페이지: 응답이 JSON이 아님")
            break

        items = resp.json().get("jsonArray", [])
        if not items:
            break

        reached_older = False
        for raw in items:
            item_date = _creat_date(raw)
            if item_date is None:
                continue
            if item_date < start_date:
                reached_older = True
                break
            if start_date <= item_date <= end_date:
                collected.append(_normalize_bizinfo(raw))

        print(f"  - {page}페이지 확인 (누적 {len(collected)}건)")
        if reached_older:
            break

        time.sleep(0.2)

    print(f"[완료] 기업마당 {len(collected)}건 수집됨")
    return collected


def get_daily_relevant_bizinfo(start_date=None, end_date=None):
    """지정 기간(기본값: get_lookback_range()) 동안 등록된 기업마당 지원사업 공고 중,
    중복 제거 + 우리팀 관심 조건(키워드∩기관)을 만족하는 건만 반환한다."""
    if start_date is None or end_date is None:
        start_date, end_date = get_lookback_range()

    raw_items = fetch_bizinfo_for_date_range(start_date=start_date, end_date=end_date)

    deduped = dedupe_latest(raw_items)
    print(f"[중복제거] {len(raw_items)}건 → {len(deduped)}건 (공고ID 기준)")

    relevant = [item for item in deduped if is_relevant_bid(item)]
    relevant.sort(key=lambda item: item.get("bidNtceDt", ""))
    print(f"[필터링] 키워드 + 기관 동시 매칭: {len(relevant)}건")

    return relevant
