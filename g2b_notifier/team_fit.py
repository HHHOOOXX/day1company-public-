"""팀 맞춤 공고와 비슷한 공고 찾기 — 실무자가 직접 골라준 "우리 팀에 맞는 공고"(config.TEAM_FIT_SEEDS → DB team_fit_refs)를
기준으로, 매일 들어오는 공고 중 주제가 겹치는 공고를 찾는다. 외부 API·비용 없음.

2026-10-12 요청: "이걸 우선적으로 학습해서 유사한 공고들을 추천공고에 띄울 수 있도록". 기준 공고가 3건뿐이라 제목 전체의
글자 유사도(rag_screen.TitleSimilarity)로는 구분이 안 됐다 — 최근 60일 공고 4,841건으로 재보니 "구급차 위탁운영 용역"(0.10)이
"2026년 UST 이러닝 콘텐츠 개발"보다 SNUH e-Learning과 더 비슷하게 나왔다. 그래서 '무엇에 관한 공고인가'를 단서로 본다:
  1) 주제 묶음(config.TEAM_FIT_CONCEPTS): "이러닝"/"E러닝"/"e-Learning"처럼 표기가 달라도 같은 주제로 본다.
  2) 기준 공고 제목에서 자동으로 뽑은 단어: 최근 공고 제목에 2번 이상, AUTO_TOKEN_MAX_DF번 이하로 나오는 단어만 쓴다 —
     한 번만 나오는 말("SNUH", "SCOUT" 같은 기관 약칭·사업 코드)은 그 공고에만 맞고, 너무 흔한 말("용역", "개발")은 단서가 안 된다.
판정: 기준 공고 하나와 strong 주제가 1개 이상 겹치거나, weak 주제·자동 단어가 합쳐 2개 이상 겹치면 비슷한 공고다.
기준 공고에 "strict": True가 붙어 있으면(카드 ✅로 들어온 기준이 아직 적을 때·오래된 ✅, learning.py) strong 주제 하나로는
부족하고 단서가 2개 이상 겹쳐야 한다.
기준 공고가 늘면 주제와 단어가 자동으로 넓어진다(TEAM_FIT_SEEDS에 추가).
"""

import re

from .config import TEAM_FIT_CONCEPTS

AUTO_TOKEN_MAX_DF = 30
# 자동 단어에서 빼는 말: 공고 절차·형식 용어와 거의 모든 교육 공고에 나오는 말.
_AUTO_STOPWORDS = {
    "용역", "사업", "운영", "개발", "제작", "구축", "지원", "교육", "프로그램", "과정", "관련", "위한", "및",
    "등", "년", "년도", "학년도", "입찰", "공고", "재공고", "긴급", "선정", "모집", "대행", "혁신", "AI",
}
_BRACKET_RE = re.compile(r"\[[^\]]*\]")
_NON_WORD_RE = re.compile(r"[^0-9A-Za-z가-힣]")
_WORD_RE = re.compile(r"[A-Za-z]+(?:-[A-Za-z]+)*|[가-힣]+")


def _norm(title: str) -> str:
    """대괄호 머리말([52891] 같은 사업 코드)을 떼고 공백·기호를 지운 대문자 문자열."""
    return _NON_WORD_RE.sub("", _BRACKET_RE.sub("", title or "")).upper()


def concepts_in(title: str) -> set:
    norm = _norm(title)
    return {name for name, c in TEAM_FIT_CONCEPTS.items() if any(t.upper() in norm for t in c["terms"])}


_CONCEPT_TERMS = {t.upper() for c in TEAM_FIT_CONCEPTS.values() for t in c["terms"]}
_STRONG = {name for name, c in TEAM_FIT_CONCEPTS.items() if c["strength"] == "strong"}


class TeamFitModel:
    """refs: [{"id", "title", "org"}, ...] (db.get_team_fit_refs). corpus_titles: 단어 빈도를 셀 최근 공고 제목들."""

    def __init__(self, refs: list, corpus_titles):
        corpus = [_norm(t) for t in set(corpus_titles) if t]
        self.refs = []
        for ref in refs:
            words = set()
            for w in _WORD_RE.findall(_BRACKET_RE.sub("", ref["title"])):
                w = w.replace("-", "").upper()
                if len(w) < 2 or w in _AUTO_STOPWORDS or w in _CONCEPT_TERMS:
                    continue
                if 2 <= sum(1 for t in corpus if w in t) <= AUTO_TOKEN_MAX_DF:
                    words.add(w)
            self.refs.append((ref, concepts_in(ref["title"]), words))

    def describe(self) -> list:
        """[(기준 공고 제목, 주제 묶음, 자동 단어), ...] — 학습 결과 확인용."""
        return [(ref["title"], sorted(concepts), sorted(words)) for ref, concepts, words in self.refs]

    def match(self, title: str):
        """비슷한 기준 공고가 있으면 (기준 공고 dict, 겹친 단서 목록), 없으면 None. 여러 개면 단서가 가장 많이 겹친 것."""
        norm = _norm(title)
        concepts = concepts_in(title)
        best = None
        for ref, ref_concepts, ref_words in self.refs:
            shared = concepts & ref_concepts
            shared_words = {w for w in ref_words if w in norm}
            if len(shared) + len(shared_words) < 2 and (ref.get("strict") or not (shared & _STRONG)):
                continue
            cues = sorted(shared) + sorted(shared_words)
            if best is None or len(cues) > len(best[1]):
                best = (ref, cues)
        return best
