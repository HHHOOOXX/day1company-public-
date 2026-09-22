"""공고 필터링(관심 공고 판별) + 확신도 판정 + 사업영역 태깅."""

import re

from .config import (
    BUSINESS_AREA_KEYWORDS,
    CENTRAL_GOV_ORGS,
    EDU_KEYWORDS,
    EXCLUDE_KEYWORDS,
    EXCLUDE_PROCUREMENT_CATEGORIES,
    ORG_KEYWORDS,
    ORG_KEYWORDS_BROAD,
    WEAK_EDU_KEYWORDS,
    WIN_HISTORY,
)
from .slack import deadline_date

# 2026-09-22 피드백: 마감까지 7일 미만으로 급박하게 남은 건은 실무자가 검토할 시간이 부족해
# 원칙적으로 보내지 않는다. 예외는 딱 하나 — 과거 실제 수주 이력과 발주기관이 겹치는 ⭐확실후보
# (is_confident_win)이면서, 업종/지역/제외키워드/확신도 판정까지 전부 깨끗하게 통과한
# (_tier == "include", 즉 review 사유가 하나도 없는) 90% 이상 확신할 수 있는 사업만 예외적으로 통과.
DEADLINE_GATE_DAYS = 7


def _matches_any(text: str, keywords) -> bool:
    text = (text or "").upper()
    return any(kw.upper() in text for kw in keywords)


def _matched_keywords(text: str, keywords) -> list:
    text = (text or "").upper()
    return [kw for kw in keywords if kw.upper() in text]


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


def _matches_alio_org(org: str, alio_orgs: set) -> bool:
    """발주기관명이 ALIO 공공기관 마스터 리스트(alio.fetch_alio_org_names)의 기관명과 겹치는지
    양방향 부분일치로 확인한다(WIN_HISTORY 매칭과 동일한 방식 — 나라장터 발주기관명은
    "OO공단 OO지사"처럼 소속이 덧붙는 경우가 많아서다). alio_orgs가 비어있으면(API 미사용/실패)
    항상 False — 화이트리스트가 없으면 이 신호 없이 기존 ORG_KEYWORDS 판정만 쓴다."""
    if not org or not alio_orgs:
        return False
    org_upper = org.upper()
    for name in alio_orgs:
        name_upper = name.upper()
        if name_upper and (name_upper in org_upper or org_upper in name_upper):
            return True
    return False


def _org_confidence(org: str, alio_orgs: set = None) -> str:
    """발주기관명 신뢰도.
    'core': 대학교/지자체/중앙부처 등 핵심 유형이거나, 과거 수주 실적으로 유효성이 확인된 유형(ORG_KEYWORDS),
    또는 ALIO 공공기관 마스터 리스트와 정확히 매칭되는 실제 지정 공공기관(2026-09-22 연동).
    'broad': 공공기관 성격은 있으나(ORG_KEYWORDS_BROAD 매칭) ALIO로도 확인되지 않는 유형.
    'none': 셋 다 아님."""
    if _matches_any(org, ORG_KEYWORDS) or _matches_any(org, CENTRAL_GOV_ORGS):
        return "core"
    if _matches_alio_org(org, alio_orgs):
        return "core"
    if _matches_any(org, ORG_KEYWORDS_BROAD):
        return "broad"
    return "none"


def _keyword_confidence(title: str) -> str:
    """제목 키워드 신뢰도.
    'strong': 구체적인 교육/양성/콘텐츠 신호 키워드가 매칭됨.
    'weak': '운영'/'위탁운영'/'교육'처럼 범용적인 키워드만 매칭됨(다른 구체 신호 없음).
    'none': 매칭 없음."""
    hits = _matched_keywords(title, EDU_KEYWORDS)
    if not hits:
        return "none"
    if all(hit in WEAK_EDU_KEYWORDS for hit in hits):
        return "weak"
    return "strong"


def is_relevant_bid(item: dict, alio_orgs: set = None) -> bool:
    """우리팀(교육회사, 대학교/지자체/공공기관 대상) 기준 관심 공고 여부.
    키워드만으로는 노이즈가 많아서, 발주기관 매칭과 결합될 때만 인정한다.
    단, 제목에 축제/행사 대행성 키워드가 있거나 공식 업종 대분류가 구조적으로 무관하면
    다른 조건과 무관하게 제외한다."""
    title = item.get("bidNtceNm", "")
    org = item.get("ntceInsttNm", "")
    if _matches_any(title, EXCLUDE_KEYWORDS):
        return False
    if item.get("pubPrcrmntLrgClsfcNm", "") in EXCLUDE_PROCUREMENT_CATEGORIES:
        return False
    return _keyword_confidence(title) != "none" and _org_confidence(org, alio_orgs) != "none"


def is_confident_win(item: dict):
    """발주기관명이 과거 실제 수주 이력(WIN_HISTORY)의 기관명과 겹치면 '확실 후보'로 판정한다.
    양방향 부분일치(공고 발주기관명 안에 과거 기관명이 들어있거나, 그 반대)로 비교 — 나라장터
    발주기관명은 "OO대학교 산학협력단"처럼 소속기관명이 덧붙는 경우가 많아서다.
    반환값: (matched: bool, entry: dict|None) — entry는 매칭된 WIN_HISTORY 원본 항목."""
    org = (item.get("ntceInsttNm", "") or "").upper()
    if not org:
        return False, None
    for entry in WIN_HISTORY:
        ref_org = entry["org"].upper()
        if ref_org in org or org in ref_org:
            return True, entry
    return False, None


_TOKEN_SPLIT_RE = re.compile(r"[\[\]()_\-/,.·~&|\s]+")
_TOKEN_STOPWORDS = {
    "및", "등", "위한", "관련", "사업", "제공", "지원", "구축", "운영", "개발", "제작", "용역", "교육",
}


def _extract_tokens(text: str) -> set:
    """자유 텍스트(주로 과거 수주 사업명)에서 공백/괄호/특수문자 기준으로 의미있는 토큰만 뽑는다.
    2글자 미만이거나 너무 범용적인 단어(_TOKEN_STOPWORDS)는 제외한다."""
    raw = _TOKEN_SPLIT_RE.split(text or "")
    return {t for t in raw if len(t) >= 2 and t not in _TOKEN_STOPWORDS}


def build_learned_keywords(historical_bid_titles_orgs: list, alio_orgs: set = None) -> set:
    """사전규격 전용 '학습된 키워드' 집합을 만든다. 사전규격은 업종제한/지역제한 API 자체가 없어서
    (getBidPblancListInfoLicenseLimit/PrtcptPsblRgn이 사전규격 서비스엔 존재하지 않음 — 실제 호출로 확인)
    입찰공고보다 훨씬 좁고 확실한 키워드로만 걸러야 노이즈(전통시장/뷰티 등)가 안 섞인다.

    재료 두 가지:
      1) 과거에 실제로 '핵심 발주기관 + 구체적 키워드'로 확실하게 매칭됐던 나라장터 입찰공고 제목들
         (애매한 발주기관/범용 키워드로만 걸린 review 등급은 제외 — 노이즈를 학습하지 않기 위함)에서
         실제 매칭에 쓰인 EDU_KEYWORDS만 추출.
      2) 우리팀이 실제로 수주한 사업명(WIN_HISTORY)에서 추출한 의미있는 토큰.
    historical_bid_titles_orgs: [(title, org), ...] — db.get_historical_bid_titles() 결과."""
    learned = set()
    for title, org in historical_bid_titles_orgs:
        if _keyword_confidence(title) == "strong" and _org_confidence(org, alio_orgs) == "core":
            # WEAK_EDU_KEYWORDS('운영'/'교육' 등)는 다른 강한 신호와 같이 있어서 title 자체는
            # strong으로 판정됐더라도, 범용 단어 그 자체를 학습 키워드에 넣으면 사전규격 쪽에서
            # 다시 오탐을 일으키므로 제외한다.
            learned.update(kw for kw in _matched_keywords(title, EDU_KEYWORDS) if kw not in WEAK_EDU_KEYWORDS)
    for entry in WIN_HISTORY:
        learned.update(_extract_tokens(entry["project"]))
    return learned


def is_relevant_prespec(item: dict, learned_keywords: set, alio_orgs: set = None) -> bool:
    """사전규격 전용 관심 공고 판별. is_relevant_bid와 달리 EDU_KEYWORDS 전체가 아니라
    build_learned_keywords로 좁힌 키워드만 쓰고, 발주기관도 핵심군(core)만 인정한다 — 사전규격은
    업종/지역 API로 걸러낼 안전망이 없어서 애초에 더 엄격한 기준으로 들어오는 것 자체를 좁혀야 한다."""
    title = item.get("bidNtceNm", "")
    org = item.get("ntceInsttNm", "")
    if _matches_any(title, EXCLUDE_KEYWORDS):
        return False
    if not learned_keywords:
        return False
    return _matches_any(title, learned_keywords) and _org_confidence(org, alio_orgs) == "core"


def classify_confidence(item: dict, alio_orgs: set = None) -> dict:
    """키워드/발주기관 신뢰도만으로 1차 확신도를 매긴다({"tier": "include"|"review", "reasons": [...]}).
    입찰공고의 경우 g2b.get_daily_relevant_bids에서 업종제한 판정 결과가 추가로 반영되어 최종 확정된다.

    판단 기준(2026-09-16 데이원 B2G 수주 실적 목록을 근거로 수립):
      - 발주기관이 대학교/지자체/재단/진흥원 등 핵심 유형이거나, 실제 수주 이력이 있는 유형
        (전문대/고등학교/기술원/진흥센터/거래소 등 ORG_KEYWORDS에 편입된 유형)이거나, ALIO 공공기관
        마스터 리스트와 정확히 매칭되는 실제 지정 공공기관(2026-09-22 연동)이면 'core' → include.
      - '공사/공단/협회/학원/연구원' 등 공공기관 성격은 있으나 수주 이력으로도 ALIO로도 아직 확인되지
        않은 유형이면 'broad' → review (실제 사업 대상인지 직접 확인 필요).
      - 제목이 '운영'/'위탁운영'/'교육'처럼 범용 동사성 단어 하나로만 매칭되고 다른 구체적 신호(양성/부트캠프/
        콘텐츠/AI 등)가 없으면 'weak' → review (예: 단순 "OO 시스템 운영 용역"은 교육 사업이 아닐 수 있음).
    """
    title = item.get("bidNtceNm", "")
    org = item.get("ntceInsttNm", "")
    org_conf = _org_confidence(org, alio_orgs)
    kw_conf = _keyword_confidence(title)

    reasons = []
    if org_conf == "broad":
        reasons.append("발주기관 유형이 대학교/지자체 핵심군이 아님(공사/공단/협회/연구원 등) — 실제 사업 대상인지 확인 필요")
    if kw_conf == "weak":
        reasons.append("제목이 '운영/교육/AI' 같은 범용 단어로만 매칭됨 — 교육·콘텐츠 사업이 맞는지 확인 필요")
    elif kw_conf == "none":
        # 사전규격은 EDU_KEYWORDS가 아니라 더 넓은 learned_keywords(예: WIN_HISTORY 사업명에서 뽑은
        # '연수' 같은 범용 토큰)로 1차 관심 공고 판정을 통과했을 수 있다. 이런 건은 구체적인 교육/양성
        # 신호가 전혀 없으므로 확실포함이 아니라 확인 필요로 낮춘다.
        # (예: "숭실대 차세대반도체학과 국제 반도체 연수" — '연수'는 학습된 키워드일 뿐 EDU_KEYWORDS는 아님)
        reasons.append("제목에 구체적인 교육/양성 신호 키워드가 없음(학습된 키워드로만 매칭) — 실제 사업 내용 확인 필요")

    tier = "review" if reasons else "include"
    return {"tier": tier, "reasons": reasons}


def attach_confidence(items: list, alio_orgs: set = None) -> list:
    """items 각각에 확신도 판정 결과를 _tier/_reasons/_star 필드로 붙인다(제자리 수정, 리스트 그대로 반환).
    과거 수주 이력(WIN_HISTORY)과 발주기관이 겹치면, 키워드/발주기관 판정이 'review'였더라도
    강제로 'include'로 올리고 _star에 매칭된 과거 프로젝트를 남긴다 — 실제로 우리와 거래한 기관이
    보낸 공고는 확인 필요 등급으로 묻히면 안 되기 때문."""
    for item in items:
        result = classify_confidence(item, alio_orgs)
        won, matched = is_confident_win(item)
        item["_star"] = matched
        if won:
            item["_tier"] = "include"
            item["_reasons"] = []  # review 사유가 있었더라도 star로 override되면 더 이상 안 보여줌
        else:
            item["_tier"] = result["tier"]
            item["_reasons"] = result["reasons"]
    return items


def passes_deadline_gate(item: dict, today) -> bool:
    """마감(입찰마감/의견마감/신청마감)까지 DEADLINE_GATE_DAYS일 미만으로 남은 공고는 원칙적으로
    걸러낸다. attach_confidence가 먼저 _star/_tier를 채워둔 뒤에 호출해야 한다.
    예외: ⭐확실후보(_star, 과거 실제 수주 기관과 겹침)이면서 _tier == 'include'(업종/지역/제외키워드/
    확신도 판정까지 전부 깨끗하게 통과해 review 사유가 하나도 없는) 90% 이상 확신할 수 있는 건만
    마감 임박에도 통과시킨다. 마감일을 파싱할 수 없는 항목(미정 등)은 판단 근거가 없어 차단하지 않는다."""
    d = deadline_date(item)
    if d is None:
        return True
    days_left = (d - today).days
    if days_left >= DEADLINE_GATE_DAYS:
        return True
    return bool(item.get("_star")) and item.get("_tier") == "include"


def tag_business_area(title: str) -> list:
    """제목 키워드로 사업영역 5축(교육운영/콘텐츠/연구/전략/컨설팅) 중 해당하는 것을 태깅한다.
    GO/NO-GO 판단용이 아니라 참고용 라벨이며, 여러 축에 동시에 걸릴 수 있다."""
    return [axis for axis, keywords in BUSINESS_AREA_KEYWORDS.items() if _matches_any(title, keywords)]
