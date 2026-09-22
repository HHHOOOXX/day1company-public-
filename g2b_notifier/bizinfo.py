"""기업마당(bizinfo.go.kr) 중소기업 지원사업 공고 API.

NIPA/NIA/IITP/중진공/소진공 등이 올리는 지원사업 공고가 상당수 여기 집계된다.
2026-09-15 실제 호출로 필드명 확정 완료:
  pblancNm(공고명), jrsdInsttNm(소관기관), excInsttNm(수행기관, "직접수행"이면 소관기관과 동일 취급),
  pblancId(공고ID), creatPnttm(등록일시), reqstBeginEndDe(신청기간 — "YYYY-MM-DD ~ YYYY-MM-DD"
  또는 "모집 완료시까지" 같은 자유 텍스트라 별도 파싱 없이 그대로 표시), pblancUrl(상세페이지),
  bsnsSumryCn(사업개요 HTML — 신청자격/지역 조건이 여기 자유 텍스트로 들어있음)
"""

import re
import time
from datetime import datetime
from urllib.parse import unquote

import requests

from .classify import _matches_any, attach_confidence, dedupe_latest, is_relevant_bid
from .config import BIZINFO_API_URL, BIZINFO_SERVICE_KEY, NATIONWIDE_OVERRIDE_KEYWORDS, NON_METRO_REGION_ORGS
from .doc_extract import extract_document_text
from .doc_extract import requires_ineligible_certificate as _doc_requires_ineligible_certificate
from .g2b import COLLECTION_WARNINGS, get_lookback_range, get_with_retry

_HTML_TAG_RE = re.compile(r"<[^>]+>")
# 2026-09-18 피드백: 데이원컴퍼니 본사 소재지는 서울이고, "지역제한이 서울이거나 없어야" 통과.
# 기존엔 경기/인천/수도권도 통과 처리했으나 문자 그대로 서울만 허용하도록 좁힌다.
_METRO_REGION_TOKENS = ["서울"]


_LOCATION_ANCHOR_WORDS = ["소재", "본사"]


def _location_clause_regions(text: str):
    """텍스트에서 '소재'/'본사' 앞뒤 문맥에 등장하는 지역 토큰을 (비수도권 집합, 수도권 집합)으로 나눈다.
    자격조건 문구가 두 가지 형태로 나온다: "본사·지사·공장 중 1개 이상이 OO에 소재한 기업"(소재 앵커),
    "OO시,OO군,OO군 내 제조기업(본사, 공장 등) 중"(본사 앵커, 실사례: PBLN_000000000126558 — 충북
    충주시/진천군/음성군 한정인데 '소재'라는 단어 자체가 없어서 소재 앵커만으로는 못 잡았음).
    (소관기관이 중앙부처라도 실제로는 지역 한정 사업인 경우가 있어, jrsdInsttNm만으로는 못 잡는다)."""
    non_metro, metro = set(), set()
    for anchor in _LOCATION_ANCHOR_WORDS:
        for m in re.finditer(anchor, text):
            window = text[max(0, m.start() - 40) : m.start() + 10]
            non_metro.update(token for token in NON_METRO_REGION_ORGS if token in window)
            metro.update(token for token in _METRO_REGION_TOKENS if token in window)
    return non_metro, metro


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

    # 공고문 PDF(printFlpthNm)를 최우선으로 본다 — "신청자격" 같은 실제 자격조건은 사업개요
    # 요약(bsnsSumryCn)엔 안 나오고 이 공고문 안에만 적혀있는 경우가 실제로 있다(붙임 첨부파일
    # flpthNm은 신청서식/샘플 등 잡다한 파일이 섞여있어 후순위로만 둔다).
    attachment_urls = [u for u in [item.get("printFlpthNm", "")] if u]
    attachment_urls += [u for u in (item.get("flpthNm", "") or "").split("@") if u]

    return {
        "bidNtceNm": item.get("pblancNm", ""),
        "ntceInsttNm": org,
        "bidNtceNo": item.get("pblancId", ""),
        "bidNtceOrd": "0",
        "bidNtceDt": item.get("creatPnttm", ""),
        "bidClseDt": item.get("reqstBeginEndDe", ""),
        "bidNtceDtlUrl": item.get("pblancUrl", ""),
        "_jrsdInsttNm": jrsd,
        "_bsnsSumryCn": item.get("bsnsSumryCn", ""),
        "_attachment_urls": attachment_urls,
    }


def is_region_restricted(item: dict) -> bool:
    """사업개요(bsnsSumryCn)에 실제로 명시된 지역 제한이 서울을 포함하지 않을 때만 제외 대상으로
    판정한다. 2026-09-18 피드백: "지역제한이 실제로 있는 공고만 지역을 확인해서 서울이 아니면 제외"
    — 실제 제한 문구가 없는 공고를 소관기관(jrsdInsttNm) 소재지만으로 추정해서 제외하지 않는다
    (소관기관이 지방자치단체라도 사업 자체는 전국 대상인 경우가 흔하기 때문).
    "본사·지사·공장 중 1개 이상이 전남광주통합특별시 또는 충청남도에 소재한 기업"처럼 '소재' 문구
    근처에 비수도권 지역만 언급되면 확정 제외하고, 그 외(문구 자체가 없거나 서울이 포함되면)는 통과시킨다.
    "전국 소재/전국 기업" 같은 전국 대상 명시가 있으면 항상 예외적으로 통과시킨다."""
    summary = _HTML_TAG_RE.sub(" ", item.get("_bsnsSumryCn", "") or "")
    if _matches_any(summary, NATIONWIDE_OVERRIDE_KEYWORDS):
        return False

    non_metro, metro = _location_clause_regions(summary)
    if non_metro and not metro:
        return True

    return False


def _fetch_attachment_text(url: str) -> str:
    """공고 첨부파일을 내려받아 본문 텍스트를 추출한다. 실패/미지원 형식이면 빈 문자열."""
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    except requests.exceptions.RequestException:
        return ""
    match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", resp.headers.get("Content-Disposition", ""))
    filename = unquote(match.group(1)) if match else ""
    return extract_document_text(resp.content, filename)


def fetch_attachment_texts(item: dict) -> list:
    """공고 첨부파일들을 한 번씩만 내려받아 텍스트 목록을 만든다. 지역제한/제출서류 확인이
    같은 다운로드를 재사용하도록 묶어서, 첨부파일마다 두 번씩 내려받지 않게 한다."""
    return [t for t in (_fetch_attachment_text(u) for u in item.get("_attachment_urls", [])) if t]


def is_region_restricted_by_attachment(texts: list) -> bool:
    """2026-09-18 피드백: 사업개요(bsnsSumryCn)엔 지역 조건이 없지만 첨부 공고문(PDF/HWP 등) 안에만
    "신청자격: 강원특별자치도 소재(본사 또는 공장) 기업"처럼 명시된 경우가 실제로 있다
    (실제 사례로 검증: PBLN_000000000126590 — 강원특별자치도 소재 기업 한정). is_region_restricted가
    요약 텍스트로 통과시킨 건에 한해서만(키워드+기관 필터를 이미 통과한 소수 건) 첨부파일을 직접 열어
    같은 방식(_location_clause_regions)으로 재확인한다."""
    for text in texts:
        if _matches_any(text, NATIONWIDE_OVERRIDE_KEYWORDS):
            return False
        non_metro, metro = _location_clause_regions(text)
        if non_metro and not metro:
            print(f"  [지역제외] 첨부파일에서 '{non_metro}' 소재 조건 확인 -> 배제")
            return True
        if non_metro or metro:
            # 지역 조건 문구를 찾았고(서울 포함이든 전국이든) 판정이 끝났으면 나머지 첨부파일은
            # 더 열어볼 필요가 없다 — 보통 공고문 1개에 신청자격이 다 들어있다.
            break
    return False


# 2026-09-18 피드백: "소상공인확인서"/"중소기업확인서"를 제출서류로 요구하는 지원사업은 대상이
# 소상공인/특정 규모 이하 중소기업으로 한정된 사업이라 데이원컴퍼니가 참가할 수 없다.
# 실제 사례로 검증: PBLN_000000000126566 — 제출서류에 "소상공인확인서" 명시(소상공인으로 명시된
# 업체만 인정). 판정 로직 자체는 doc_extract.requires_ineligible_certificate 공용 함수로 옮겨서
# 나라장터 사전규격/입찰공고 쪽(g2b.py)에서도 같이 쓴다(2026-09-22, R26BD00276775 사례로 확인).
def requires_ineligible_certificate(texts: list) -> bool:
    """첨부 공고문에 우리가 발급받을 수 없는 확인서(소상공인확인서/중소기업확인서)가 제출서류로
    명시돼 있으면 배제 대상으로 판정한다."""
    for text in texts:
        if _doc_requires_ineligible_certificate(text):
            print("  [제외] 첨부파일 제출서류에 소상공인/중소기업 확인서 요구 확인 -> 배제")
            return True
    return False


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
        resp = get_with_retry(BIZINFO_API_URL, params, label=f"기업마당 {page}페이지")
        if resp is None:
            break
        if "json" not in resp.headers.get("Content-Type", "").lower():
            print(f"[경고] 기업마당 응답이 JSON이 아닙니다 ({page}페이지). crtfcKey를 확인하세요.")
            COLLECTION_WARNINGS.append(f"기업마당 {page}페이지: 응답이 JSON이 아님")
            break

        try:
            items = resp.json().get("jsonArray", [])
        except ValueError as exc:
            print(f"[경고] 기업마당 JSON 파싱 실패 ({page}페이지): {exc}")
            COLLECTION_WARNINGS.append(f"기업마당 {page}페이지: JSON 파싱 실패")
            break
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
    중복 제거 + 우리팀 관심 조건(키워드∩기관) + 본사 지역 조건을 만족하는 건만 반환한다."""
    if start_date is None or end_date is None:
        start_date, end_date = get_lookback_range()

    raw_items = fetch_bizinfo_for_date_range(start_date=start_date, end_date=end_date)

    deduped = dedupe_latest(raw_items)
    print(f"[중복제거] {len(raw_items)}건 → {len(deduped)}건 (공고ID 기준)")

    relevant = [item for item in deduped if is_relevant_bid(item)]
    before_region = len(relevant)
    relevant = [item for item in relevant if not is_region_restricted(item)]
    after_summary_region = len(relevant)

    # 사업개요엔 없어도 첨부 공고문 안에만 있는 조건이 실제로 있어(지역제한 실사례: PBLN_000000000126590,
    # 제출서류 실사례: PBLN_000000000126566 — 소상공인확인서 요구), 이미 좁혀진 소수 건에 한해
    # 첨부파일을 직접 열어 지역제한 + 제출서류(소상공인/중소기업확인서)를 같이 확인한다.
    still_relevant = []
    for item in relevant:
        texts = fetch_attachment_texts(item)
        if is_region_restricted_by_attachment(texts):
            continue
        if requires_ineligible_certificate(texts):
            continue
        item.pop("_attachment_urls", None)
        still_relevant.append(item)
    relevant = still_relevant

    relevant.sort(key=lambda item: item.get("bidNtceDt", ""))
    print(
        f"[필터링] 키워드 + 기관 동시 매칭: {before_region}건 "
        f"(사업개요상 지역제한 제외 {after_summary_region}건 → 첨부파일 확인 후 {len(relevant)}건)"
    )

    attach_confidence(relevant)

    return relevant
