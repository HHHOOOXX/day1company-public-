"""카드 ✅/❌ 피드백으로 팀 맞춤 기준 다듬기 — 잘못 누른 ✅를 사람이 지우지 않아도 걸러지게 한다(2026-10-16 요청).

feedback.sync_feedback()이 ✅가 더 많은 공고를 팀 맞춤 공고(team_fit_refs, source='feedback')로 넣는다. 여기서는 그 기준을
모델에 넣기 전에 한 번 더 거른다(외부 API·비용 없음).
  1) 기존 로직의 거부권: 제목만으로 관심 공고에서 빠지는 공고(classify.is_title_excluded)는 ✅여도 기준으로 쓰지 않는다.
     첨부문서 제외 키워드는 보지 않는다 — "디지털역량교육"처럼 본문에 '박람회'가 한 번 나온 교육 사업도 많다.
  2) 자동 퇴출: 이 ✅ 기준 때문에 '팀 맞춤 공고와 비슷'으로 올라간 공고들이 실제로 ❌를 많이 받으면 기준에서 뺀다.
  3) ❌ 반영: ❌가 더 많은 공고로 모델을 따로 만들어, 새 공고가 ✅ 기준보다 ❌ 공고와 더 비슷하면 팀 맞춤으로 올리지 않는다
     (classify.team_fit_match).
  4) 비중 점진 조정: 판정이 config.FEEDBACK_FULL_WEIGHT_JUDGED건 쌓이기 전이거나 오래된 ✅ 기준은 엄격하게(단서 2개 이상) 쓴다.
"""

from datetime import date, timedelta

from .classify import is_title_excluded
from .config import (
    FEEDBACK_FULL_WEIGHT_JUDGED,
    FEEDBACK_RETIRE_MIN_JUDGED,
    FEEDBACK_RETIRE_NOT_FIT_RATIO,
    FEEDBACK_STALE_DAYS,
)


def _ref_outcomes(feedback_postings: list) -> dict:
    """팀 맞춤 기준별로, 그 기준 때문에 올라간 공고가 받은 판정 수 {기준 id 또는 제목: [✅ 수, ❌ 수]}."""
    out = {}
    for p in feedback_postings:
        tf = p.get("team_fit")
        if not tf or p["label"] not in ("fit", "not_fit"):
            continue
        # 2026-10-16 이전 기록은 기준 제목만 남아 있어 제목으로도 센다.
        key = tf.get("ref_id") or tf.get("ref")
        counts = out.setdefault(key, [0, 0])
        counts[0 if p["label"] == "fit" else 1] += 1
    return out


def build_refs(refs: list, feedback_postings: list, today: date):
    """팀 맞춤 기준(db.get_team_fit_refs)과 피드백 공고(db.get_feedback_postings)로
    (모델에 넣을 기준, ❌ 기준, 집계 dict)를 만든다."""
    judged = sum(1 for p in feedback_postings if p["label"] in ("fit", "not_fit"))
    full_weight = judged >= FEEDBACK_FULL_WEIGHT_JUDGED
    stale_before = (today - timedelta(days=FEEDBACK_STALE_DAYS)).isoformat()
    outcomes = _ref_outcomes(feedback_postings)
    kept, vetoed, retired = [], [], []
    for ref in refs:
        if ref.get("source") != "feedback":
            kept.append(ref)
            continue
        if is_title_excluded(ref["title"]):
            vetoed.append(ref)
            continue
        fit_n, not_fit_n = outcomes.get(ref["id"], outcomes.get(ref["title"], [0, 0]))
        if fit_n + not_fit_n >= FEEDBACK_RETIRE_MIN_JUDGED and not_fit_n / (fit_n + not_fit_n) >= FEEDBACK_RETIRE_NOT_FIT_RATIO:
            retired.append(ref)
            continue
        strict = not full_weight or (ref.get("added_date") or "") < stale_before
        kept.append({**ref, "strict": strict})
    not_fit_refs = [
        {"id": p["id"], "title": p["title"], "org": ""} for p in feedback_postings if p["label"] == "not_fit" and p["title"]
    ]
    stats = {
        "judged": judged, "full_weight": full_weight, "vetoed": vetoed, "retired": retired,
        "feedback_kept": sum(1 for r in kept if r.get("source") == "feedback"), "not_fit": len(not_fit_refs),
    }
    return kept, not_fit_refs, stats


def describe(stats: dict) -> str:
    mode = "기존 기준과 같은 비중" if stats["full_weight"] else f"엄격(단서 2개 이상, 판정 {FEEDBACK_FULL_WEIGHT_JUDGED}건부터 같은 비중)"
    parts = [f"✅ 기준 {stats['feedback_kept']}건 사용 — {mode}, 판정 누적 {stats['judged']}건", f"❌ 기준 {stats['not_fit']}건"]
    if stats["vetoed"]:
        parts.append("제외 키워드로 뺀 ✅ " + ", ".join(r["title"][:30] for r in stats["vetoed"]))
    if stats["retired"]:
        parts.append("추천 결과가 ❌라 퇴출한 ✅ " + ", ".join(r["title"][:30] for r in stats["retired"]))
    return "[학습] " + " / ".join(parts)
