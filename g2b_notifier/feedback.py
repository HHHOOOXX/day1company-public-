"""Slack 반응(✅/❌)으로 공고별 '우리 팀 핏' 피드백 모으기.

2026-10-13 요청: 통계적 필터링 고도화의 1단계 — 우리 핏에 맞는/안 맞는 공고를 DB에 쌓는다. Slack 반응은 메시지 단위로만
달리고, 카드(Block Kit 섹션)마다 따로 달 수 없어서, 공고 알림 스레드에 카드에 올린 공고를 한 건씩 댓글로 단다
(post_feedback_items). 실무자가 그 댓글에 ✅(우리 핏) / ❌(아님)을 누르면, 다음 실행 때 반응을 읽어 공고별로 저장한다
(collect_feedback → DB posting_feedback). ✅가 더 많은 공고는 팀 맞춤 공고(team_fit_refs, source='slack')로도 넣어
비슷한 공고를 추천 공고 맨 앞에 올리는 데 바로 쓴다(team_fit.py). 반응을 바꾸거나 지우면 다음 실행 때 따라 바뀐다.

필요한 봇 권한: chat:write(이미 사용 중), reactions:read(반응 읽기). 봇 토큰(SLACK_BOT_TOKEN)이 없으면 이 기능은 꺼진다.
"""

import time
from datetime import timedelta

from . import db
from .slack import get_reactions, post_with_bot

FIT_REACTIONS = {"white_check_mark", "heavy_check_mark", "+1"}
NOT_FIT_REACTIONS = {"x", "negative_squared_cross_mark", "-1"}
# 반응은 보통 받은 날~다음 날 달린다. 하루 20건 안팎 x 7일이라 reactions.get 호출이 100건 남짓 — 분당 50회 제한에 맞춰 천천히 부른다.
FEEDBACK_LOOKBACK_DAYS = 7
_REACTIONS_CALL_INTERVAL = 1.2
_POST_INTERVAL = 1.1  # chat.postMessage 채널당 초당 1건 제한

GUIDE_TEXT = (
    "\U0001F447 *공고 피드백* — 아래 공고마다 :white_check_mark: (우리 팀 핏) 또는 :x: (아님)를 눌러주세요. "
    "다음 날 아침에 자동으로 모아서, ✅ 공고와 비슷한 공고를 추천 공고 맨 앞에 올리고 필터링 학습 데이터로 쌓아요."
)


def post_feedback_items(conn, channel: str, thread_ts: str, items: list, today_str: str) -> int:
    """공고 알림 스레드에 안내 댓글 + 공고마다 댓글 하나씩 달고 (채널, ts, posting_id)를 DB에 남긴다.
    items: [(posting_id, item), ...] — 카드에 올린 공고. 반환: 단 댓글 수."""
    if not items:
        return 0
    if post_with_bot(GUIDE_TEXT, thread_ts=thread_ts, channel=channel) is None:
        return 0
    saved = []
    for idx, (posting_id, item) in enumerate(items, 1):
        time.sleep(_POST_INTERVAL)
        url = item.get("bidNtceDtlUrl", "")
        title = item.get("bidNtceNm", "")
        title_part = f"<{url}|{title}>" if url else title
        text = f"{idx}. *[{item.get('ntceInsttNm', '')}]* {title_part}"
        ts, posted_channel = post_with_bot(text, thread_ts=thread_ts, channel=channel, want_channel=True)
        if ts:
            saved.append((posted_channel or channel, ts, posting_id))
    db.save_feedback_messages(conn, saved, today_str)
    print(f"[피드백] 공고 {len(saved)}건에 반응 받을 댓글을 달았어요")
    return len(saved)


def _label(reactions: list):
    """반응 목록 → (라벨, ✅ 수, ❌ 수, 반응한 사용자). 한 사람이 둘 다 누르면 둘 다 센다(드문 경우라 그대로 둔다)."""
    fit_users, not_fit_users = set(), set()
    for name, users in reactions:
        base = name.split("::")[0]  # 피부색 변형(+1::skin-tone-2)
        if base in FIT_REACTIONS:
            fit_users.update(users)
        elif base in NOT_FIT_REACTIONS:
            not_fit_users.update(users)
    fit, not_fit = len(fit_users), len(not_fit_users)
    if fit == not_fit:
        label = "none" if fit == 0 else "mixed"
    else:
        label = "fit" if fit > not_fit else "not_fit"
    return label, fit, not_fit, fit_users | not_fit_users


def collect_feedback(conn, today, warnings: list = None) -> dict:
    """최근 FEEDBACK_LOOKBACK_DAYS일 동안 단 피드백 댓글의 반응을 읽어 DB에 반영한다. 반환: 라벨별 건수."""
    since = (today - timedelta(days=FEEDBACK_LOOKBACK_DAYS)).isoformat()
    today_str = today.isoformat()
    messages = db.get_feedback_messages_since(conn, since)
    counts = {"fit": 0, "not_fit": 0, "mixed": 0, "none": 0}
    for idx, (channel, ts, posting_id) in enumerate(messages):
        if idx:
            time.sleep(_REACTIONS_CALL_INTERVAL)
        reactions, error = get_reactions(channel, ts)
        if reactions is None:
            if error in ("message_not_found", "channel_not_found"):
                continue
            note = " — Slack 앱에 reactions:read 권한을 추가해 주세요" if error == "missing_scope" else ""
            print(f"[피드백] 반응 조회 실패: {error}{note}")
            if warnings is not None:
                warnings.append(f"Slack 반응 조회 실패({error}){note}")
            return counts
        label, fit, not_fit, users = _label(reactions)
        counts[label] += 1
        db.save_posting_feedback(conn, posting_id, label, fit, not_fit, list(users), today_str)
        db.set_slack_team_fit_ref(conn, posting_id, label == "fit", today_str)
    if messages:
        print(f"[피드백] 최근 {FEEDBACK_LOOKBACK_DAYS}일 댓글 {len(messages)}건 반응 확인: "
              f"✅ {counts['fit']}건 / ❌ {counts['not_fit']}건 / 엇갈림 {counts['mixed']}건 / 반응 없음 {counts['none']}건")
    return counts
