"""공고 필터링(관심 공고 판별) + 사업영역 태깅."""

from .config import BUSINESS_AREA_KEYWORDS, EDU_KEYWORDS, EXCLUDE_KEYWORDS, ORG_KEYWORDS


def _matches_any(text: str, keywords: list) -> bool:
    text = (text or "").upper()
    return any(kw.upper() in text for kw in keywords)


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
    """우리팀(교육회사, 대학교/지자체/공공기관 대상) 기준 관심 공고 여부.
    키워드만으로는 노이즈가 많아서, 발주기관 매칭과 결합될 때만 인정한다.
    단, 제목에 축제/행사 대행성 키워드가 있으면 다른 조건과 무관하게 제외한다."""
    title = item.get("bidNtceNm", "")
    org = item.get("ntceInsttNm", "")
    if _matches_any(title, EXCLUDE_KEYWORDS):
        return False
    return _matches_any(title, EDU_KEYWORDS) and _matches_any(org, ORG_KEYWORDS)


def tag_business_area(title: str) -> list:
    """제목 키워드로 사업영역 5축(교육운영/콘텐츠/연구/전략/컨설팅) 중 해당하는 것을 태깅한다.
    GO/NO-GO 판단용이 아니라 참고용 라벨이며, 여러 축에 동시에 걸릴 수 있다."""
    return [axis for axis, keywords in BUSINESS_AREA_KEYWORDS.items() if _matches_any(title, keywords)]
