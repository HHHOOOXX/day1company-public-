"""공고 카드 버튼(✅ 적합함 / ❌ 아님) 피드백 — 버튼 만들기 + 클릭 기록을 DB에 넣기.

2026-10-13 요청: "공고 하나하나에 버튼을 만들어 누르게" — 통계적 필터링 고도화의 1단계로, 우리 핏에 맞는/안 맞는 공고를
건별로 DB에 쌓는다. Slack 버튼 클릭은 Slack이 앱의 Request URL로 바로 보내는데, 이 봇은 GitHub Actions에서 하루 한 번만
돌아서 받을 서버가 없다. 그래서 Google Apps Script 웹앱(apps_script/feedback.gs)이 클릭을 받아 Google 시트에 쌓고,
매일 실행 때 sync_feedback()이 시트 기록을 받아 DB(posting_feedback)에 넣는다.
  - 공고마다 사람별 마지막 클릭만 센다(눌렀다가 바꾸면 바뀐 쪽으로). ✅가 더 많으면 fit, ❌가 더 많으면 not_fit,
    같으면 mixed.
  - fit 공고는 팀 맞춤 공고(team_fit_refs, source='feedback')로 넣어 같은 실행에서 바로 비슷한 공고를 추천 맨 앞에 올린다.
"""

import json
from datetime import datetime

import requests

from . import db
from .config import FEEDBACK_EXPORT_TOKEN, FEEDBACK_LINK_TOKEN, FEEDBACK_WEBAPP_URL
from .g2b import COLLECTION_WARNINGS

LABELS = ("fit", "not_fit")


def enabled() -> bool:
    return bool(FEEDBACK_WEBAPP_URL)


def actions_block(item: dict):
    """카드 아래에 붙일 버튼 줄(Block Kit actions). 버튼을 안 붙이는 경우 None."""
    posting_id = item.get("_posting_id")
    if not enabled() or not posting_id:
        return None
    title = item.get("bidNtceNm", "")[:80]
    buttons = []
    for label, text in (("fit", "✅ 적합함"), ("not_fit", "❌ 아님")):
        button = {"type": "button", "text": {"type": "plain_text", "text": text, "emoji": True}, "action_id": label}
        if FEEDBACK_LINK_TOKEN:
            # 링크 방식: 버튼이 웹앱 주소를 브라우저로 연다(apps_script/README.md).
            req = requests.Request(
                "GET", FEEDBACK_WEBAPP_URL, params={"p": posting_id, "l": label, "k": FEEDBACK_LINK_TOKEN, "t": title}
            ).prepare()
            button["url"] = req.url
        else:
            button["value"] = json.dumps({"p": posting_id, "t": title}, ensure_ascii=False)
        buttons.append(button)
    # block_id는 메시지 안에서 겹치면 안 된다(같은 공고가 두 섹션에 나오는 일은 없지만 길이 제한 255자 안에서 공고 ID로 구분).
    return {"type": "actions", "block_id": f"fb:{posting_id}"[:255], "elements": buttons}


def fetch_feedback_rows():
    """웹앱에서 버튼 기록 전체를 받는다. [{"at", "posting_id", "label", "user_id", "user_name"}, ...] 실패하면 None."""
    try:
        resp = requests.get(
            FEEDBACK_WEBAPP_URL, params={"action": "export", "token": FEEDBACK_EXPORT_TOKEN}, timeout=30
        )
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"[피드백] 버튼 기록 가져오기 실패: {exc!r}")
        return None
    if not data.get("ok"):
        print(f"[피드백] 버튼 기록 가져오기 실패: {data.get('error')} (FEEDBACK_EXPORT_TOKEN이 스크립트 속성 EXPORT_TOKEN과 같은지 확인)")
        return None
    return data.get("rows") or []


def summarize(rows: list) -> list:
    """버튼 기록 → 공고별 [(posting_id, label, ✅ 수, ❌ 수, [사용자]), ...]. 사람별 마지막 클릭만 센다.
    링크 방식(user_id='link')은 누가 눌렀는지 모르므로 클릭 하나를 한 사람으로 센다."""
    latest = {}  # (posting_id, 사람) -> (시각, 라벨)
    for idx, row in enumerate(rows):
        posting_id, label = row.get("posting_id"), row.get("label")
        if not posting_id or label not in LABELS:
            continue
        who = row.get("user_id") or "unknown"
        if who == "link":
            who = f"link#{idx}"
        key = (posting_id, who)
        at = row.get("at", "")
        if key not in latest or at >= latest[key][0]:
            latest[key] = (at, label)
    per_posting = {}
    for (posting_id, who), (_at, label) in latest.items():
        per_posting.setdefault(posting_id, {"fit": set(), "not_fit": set()})[label].add(who)
    out = []
    for posting_id, users in per_posting.items():
        fit, not_fit = len(users["fit"]), len(users["not_fit"])
        label = "mixed" if fit == not_fit else ("fit" if fit > not_fit else "not_fit")
        out.append((posting_id, label, fit, not_fit, sorted(users["fit"] | users["not_fit"])))
    return out


def sync_feedback(conn) -> None:
    """버튼 기록을 받아 DB에 반영한다. 실행 시작 때 부르면 어제까지의 ✅가 오늘 팀 맞춤 판정에 바로 쓰인다."""
    if not enabled():
        return
    if not FEEDBACK_EXPORT_TOKEN:
        print("[피드백] FEEDBACK_EXPORT_TOKEN이 없어 버튼 기록을 가져오지 않음")
        return
    rows = fetch_feedback_rows()
    if rows is None:
        COLLECTION_WARNINGS.append("피드백 버튼 기록: 가져오기 실패")
        return
    summary = summarize(rows)
    today_str = datetime.now().date().isoformat()
    db.replace_posting_feedback(conn, summary, today_str)
    db.sync_feedback_team_fit_refs(conn, {s[0] for s in summary if s[1] == "fit"}, today_str)
    counts = {k: sum(1 for s in summary if s[1] == k) for k in ("fit", "not_fit", "mixed")}
    print(f"[피드백] 버튼 기록 {len(rows)}건 → 공고 {len(summary)}건: "
          f"✅ {counts['fit']}건 / ❌ {counts['not_fit']}건 / 엇갈림 {counts['mixed']}건")
