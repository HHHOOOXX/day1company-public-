"""기관 홈페이지 게시판 공고의 2차 검토 — 본문 규칙 검사 + 과거 사례 검색(RAG). 외부 API·비용 없음.

2026-10-01 요청: 기관 게시판은 공식 API가 아니라 게시판 제목 키워드로만 1차로 거르기 때문에, 그동안
나라장터·기업마당 필터링에서 쌓아온 제외 기준으로 한 번 더 확실히 걸러낸다. 처음엔 Claude API로 판정하게
만들었다가, "API 키 등록 없이 비용 최소화" 요청으로 로컬 계산만 쓰는 이 방식으로 바꿨다.

키워드 필터를 통과한 후보(하루 0~5건 수준)마다 아래 순서로 본다:
  1) 상세페이지 본문 규칙 검사 — 제목엔 안 드러나는 제외 사유를 본문에서 찾는다. 나라장터·기업마당
     파이프라인이 첨부파일 본문에 쓰는 것과 같은 함수를 그대로 쓴다:
       - 제외 키워드(DOC_EXCLUDE_KEYWORDS) — doc_extract.matches_exclude_keyword → 확인필요로 낮춤(2026-10-06)
       - 서울 외 지역 소재 기업 한정 — bizinfo.is_region_restricted_by_attachment → 제외
       - 소상공인·중소기업 확인서 — doc_extract.certificate_requirement → 소지 요구면 제외, 언급만 있으면 확인필요
  2) 과거 사례 검색(RAG) — 후보 제목과 비슷한 과거 공고를 찾는다. 통과 사례는 DB postings(나라장터·
     사전규격·기업마당 필터를 통과해 발송된 공고), 제외 사례는 DB raw_titles 중 필터를 통과하지 못한 공고다.
     제외 키워드가 걸려서 빠졌던 과거 공고가 통과 사례보다 더 비슷하면 확인필요로 낮추고 그 사례를 사유로 보여준다.
  3) 본문을 못 읽은 공고(IITP·중진공·소상공인24는 본문을 자바스크립트로 그림)는 1)을 못 거쳤으므로 확인필요로 낮춘다.
판정은 걸러내는 방향으로만 반영한다(등급을 올리지는 않는다).
"""

import math
import re
from collections import Counter

from .bizinfo import is_region_restricted_by_attachment
from .classify import downgrade, downgrade_for_doc_exclude
from .config import EXCLUDE_KEYWORDS
from .doc_extract import certificate_requirement, matches_exclude_keyword

TOP_K_EXAMPLES = 5
# 가중 Jaccard 유사도 기준. 2026-10-01 기관 게시판 최근 공고 약 30건으로 실측: 가장 비슷한 과거 제외 사례의
# 유사도는 대부분 0.05~0.16이었고, 주제가 실제로 겹치는 쌍은 0.2 근처였다(서울신보 "골목형상점가 육성 지원
# 사업" ↔ 제외 키워드 '골목상권'으로 빠졌던 "골목형상점가 유망골목상권 육성사업 상인교육" 0.22). 0.15 근처는
# "소상공인"처럼 대상만 겹치는 경우라(제외 사유 '홍보'와 무관) 기준을 0.20으로 둔다. 사례가 쌓이면 다시 볼 것.
SIMILAR_REJECT_THRESHOLD = 0.20

_SOURCE_LABEL = {"g2b_bid": "나라장터 입찰", "g2b_prespec": "나라장터 사전규격", "bizinfo": "기업마당"}


# ---------------------------------------------------------------- 과거 사례 검색(RAG)

# 숫자(연도·차수)와 괄호 머리말은 비교에서 뺀다 — "2026년"끼리 겹쳐서 무관한 공고가 비슷하게 잡히는 걸 막는다.
_NON_HANGUL_ALPHA_RE = re.compile(r"[^A-Za-z가-힣]")
_BRACKET_RE = re.compile(r"\[[^\]]*\]|\([^)]*\)")


def bigrams(title: str) -> frozenset:
    """제목을 두 글자씩 자른 조각 집합. 숫자·기호·괄호 머리말은 뺀다."""
    s = _NON_HANGUL_ALPHA_RE.sub("", _BRACKET_RE.sub("", title or "")).upper()
    return frozenset(s[i:i + 2] for i in range(len(s) - 1))


class TitleSimilarity:
    """글자 2-gram에 IDF 가중치를 준 가중 Jaccard 유사도(0~1). "모집"·"사업"·"용역"처럼 거의 모든 공고에
    나오는 글자쌍은 가중치가 낮아져서, 주제가 실제로 겹치는 제목끼리 점수가 높게 나온다.
    corpus: IDF를 계산할 2-gram 집합들(비교 대상 전체)."""

    def __init__(self, corpus):
        corpus = list(corpus)
        df = Counter(g for grams in corpus for g in grams)
        n = max(1, len(corpus))
        self._idf = {g: math.log(n / c) for g, c in df.items()}
        self._default_idf = math.log(n)

    def _weight(self, grams) -> float:
        return sum(self._idf.get(g, self._default_idf) for g in grams)

    def score(self, a: frozenset, b: frozenset) -> float:
        union = self._weight(a | b)
        return self._weight(a & b) / union if union else 0.0


def _exclude_hits(title: str) -> list:
    return [kw for kw in EXCLUDE_KEYWORDS if kw.upper() in (title or "").upper()]


class ExampleIndex:
    """과거 처리 사례 검색용 인덱스(유사도는 TitleSimilarity). 하루 후보가 몇 건뿐이라 전수 비교(수천 건 x
    수 건)로 충분하다."""

    def __init__(self, postings: list, raw_titles: list):
        """postings: db.get_postings_for_rag(), raw_titles: db.get_raw_titles()."""
        passed, passed_grams = [], set()
        for source, title, org, tier, _reasons, _notified in postings:
            grams = bigrams(title)
            passed_grams.add(grams)
            passed.append((grams, f"[{_SOURCE_LABEL.get(source, source)}] {title} ({org}) — 관심 공고로 통과"))
        # 제외 사례 중 '제외 키워드에 걸려서' 빠진 것만 쓴다. 그 외(교육 키워드 없음, 업종제한 등)는 제목만으로
        # 실제 이유를 알 수 없어서, 비슷하다는 이유로 기관 공고를 낮추는 근거로 쓰기엔 약하다.
        rejected = []
        for _norm, title, source in raw_titles:
            grams = bigrams(title)
            hits = _exclude_hits(title)
            if grams in passed_grams or not hits:
                continue
            rejected.append((grams, f"[{_SOURCE_LABEL.get(source, source)}] {title} — 제외 키워드 {hits}로 제외"))
        self.passed, self.rejected = passed, rejected
        self._sim = TitleSimilarity(g for g, _ in passed + rejected)

    def _top(self, pool, grams, k):
        scored = sorted(((self._sim.score(grams, g), text) for g, text in pool), reverse=True)
        return scored[:k]

    def retrieve(self, title: str, k: int = TOP_K_EXAMPLES):
        """([(유사도, 통과 사례 설명), ...], [(유사도, 제외 사례 설명), ...]) — 유사도 내림차순."""
        grams = bigrams(title)
        return self._top(self.passed, grams, k), self._top(self.rejected, grams, k)


# ---------------------------------------------------------------- 2차 검토

def _body_exclusion(body: str):
    """본문에서 확정 제외 사유를 찾으면 사유 문자열, 없으면 None."""
    if is_region_restricted_by_attachment([body]):
        return "본문 신청자격이 서울 외 지역 소재 기업 한정"
    if certificate_requirement(body) == "restricted":
        return "본문에 소상공인·중소기업 확인서 소지 요구"
    return None


def _body_doubts(item: dict, body: str) -> None:
    """확정 제외까지는 아닌 본문 의심 사유를 확인필요로 반영한다(2026-10-06: 예전엔 제외 키워드도 제외했음)."""
    hits = matches_exclude_keyword(body)
    if hits:
        downgrade_for_doc_exclude(item, hits, "상세페이지 본문")
    if certificate_requirement(body) == "mention":
        downgrade(item, "본문에 소상공인·중소기업 확인서가 언급됨(신청자격 제한인지 불확실) — 직접 확인 필요")


def screen_items(items: list, detail_texts: dict, postings: list, raw_titles: list) -> list:
    """키워드 필터를 통과한 기관 게시판 공고를 본문 규칙 + 과거 사례로 한 번 더 걸러 남은 목록을 반환한다.
    detail_texts: {bidNtceNo: 상세페이지 본문 텍스트}."""
    if not items:
        return items
    index = ExampleIndex(postings, raw_titles)
    print(f"[2차 검토] {len(items)}건 / 참고 사례: 통과 {len(index.passed)}건, 제외 키워드로 빠진 공고 {len(index.rejected)}건")

    kept = []
    for item in items:
        title = item.get("bidNtceNm", "")
        body = detail_texts.get(item.get("bidNtceNo"), "")

        if body:
            reason = _body_exclusion(body)
            if reason:
                print(f"  [제외] {title[:50]} — {reason}")
                continue
            _body_doubts(item, body)
        else:
            downgrade(item, "상세페이지 본문을 읽지 못해 본문 기준 제외 규칙을 확인하지 못함 — 직접 확인 필요")

        passed, rejected = index.retrieve(title)
        best_pass = passed[0][0] if passed else 0.0
        if rejected and rejected[0][0] >= SIMILAR_REJECT_THRESHOLD and rejected[0][0] > best_pass:
            score, example = rejected[0]
            downgrade(item, f"예전에 제외됐던 비슷한 공고가 있음: {example} (유사도 {score:.2f})")
            print(f"  [확인필요] {title[:50]} — 유사 제외 사례({score:.2f}): {example[:60]}")
        else:
            print(f"  [통과] {title[:50]}" + (f" — 유사 통과 사례({best_pass:.2f}): {passed[0][1][:60]}" if passed else ""))
        kept.append(item)
    return kept

