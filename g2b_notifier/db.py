"""SQLite 기반 상태 저장소.

GitHub Actions엔 영구 서버가 없으므로, DB 파일(data/notifier.db) 자체를 repo에 커밋해서
"이 공고를 이전에 이미 봤는지"를 다음 실행에서도 알 수 있게 한다.
(워크플로우 마지막 단계에서 git commit/push — .github/workflows/notify.yml 참고)
"""

import json
import os
import sqlite3

from .config import REPO_ROOT

DB_PATH = os.path.join(REPO_ROOT, "data", "notifier.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS postings (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    org TEXT,
    budget TEXT,
    posted_at TEXT,
    close_at TEXT,
    url TEXT,
    category_tags TEXT,
    relevant INTEGER NOT NULL,
    first_seen_date TEXT NOT NULL,
    last_seen_date TEXT NOT NULL,
    notified INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS institutions (
    name TEXT PRIMARY KEY,
    category TEXT,
    source TEXT
);

-- 오늘 날짜에 "정상 발송"이 이미 있었는지만 기록한다. 정시 실행이 지연되어 여러 워크플로우(내부
-- schedule 백업, notify-backup.yml)가 같은 날 여러 번 돌아도, 이미 오늘자 발송 기록이 있으면
-- 그 워크플로우들이 자기 실행 중 겪은 일시적 API 오류와 무관하게 무조건 조용히 넘어가게 하기 위함.
-- (예전엔 quiet_if_empty가 "새 공고도 없고 수집경고도 없을 때만" 조용히 넘어가서, 이미 정상 발송된
-- 뒤에 지연 실행된 백업이 API 일시 오류만 만나도 빈 경고 메시지를 또 보내는 문제가 있었음)
CREATE TABLE IF NOT EXISTS daily_sends (
    date TEXT PRIMARY KEY,
    sent_at TEXT NOT NULL
);
"""


def get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(_SCHEMA)
    _ensure_column(conn, "postings", "is_star", "INTEGER NOT NULL DEFAULT 0")
    # 2026-09-18: Streamlit 대시보드가 DB만 보고도 오늘 만들었던 판정(확신도/사유/업종코드/확실후보
    # 근거)을 그대로 재현할 수 있도록, 그때그때 메모리에만 있던 _tier/_reasons/_star/_industry_codes를
    # JSON으로 같이 저장해둔다 (정적 HTML 대시보드는 실행 중 값을 바로 쓰지만, DB 기반 뷰어는 이게 없으면
    # 발주기관/제목/마감일 같은 뼈대 정보만 남고 판정 근거를 잃어버린다).
    _ensure_column(conn, "postings", "classification_json", "TEXT")
    _ensure_column(conn, "postings", "method", "TEXT")
    conn.commit()
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """SQLite는 'ADD COLUMN IF NOT EXISTS'가 없어서, 기존 DB에 컬럼이 없을 때만 수동으로 추가한다."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def has_sent_today(conn: sqlite3.Connection, date_str: str) -> bool:
    row = conn.execute("SELECT 1 FROM daily_sends WHERE date = ?", (date_str,)).fetchone()
    return row is not None


def mark_sent_today(conn: sqlite3.Connection, date_str: str) -> None:
    conn.execute(
        "INSERT INTO daily_sends (date, sent_at) VALUES (?, datetime('now')) "
        "ON CONFLICT(date) DO NOTHING",
        (date_str,),
    )
    conn.commit()


def upsert_posting(
    conn: sqlite3.Connection,
    posting_id: str,
    source: str,
    title: str,
    org: str,
    budget: str,
    posted_at: str,
    close_at: str,
    url: str,
    category_tags: list,
    today_str: str,
    raw_json: str = "",
    is_star: bool = False,
    classification: dict = None,
    method: str = "",
) -> dict:
    """공고 1건을 upsert한다.
    반환값: {"is_new": 처음 보는 공고인가, "already_notified": 예전에 이미 슬랙 발송됐는가}
    이미 발송된 공고는 오늘 조회 기간과 겹치더라도(예: 월요일이 직전 금요일을 다시 훑는 경우)
    already_notified=True로 나오므로, 호출부에서 중복 알림을 걸러낼 수 있다.
    classification: {"tier", "reasons", "star", "industry_codes"} — Streamlit 대시보드용으로
    그대로 저장해둔다(get_all_postings 참고)."""
    row = conn.execute("SELECT notified FROM postings WHERE id = ?", (posting_id,)).fetchone()
    tags_json = json.dumps(category_tags, ensure_ascii=False)
    classification_json = json.dumps(classification or {}, ensure_ascii=False)

    if row is None:
        conn.execute(
            """
            INSERT INTO postings
                (id, source, title, org, budget, posted_at, close_at, url,
                 category_tags, relevant, first_seen_date, last_seen_date, notified, raw_json, is_star,
                 classification_json, method)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 0, ?, ?, ?, ?)
            """,
            (posting_id, source, title, org, budget, posted_at, close_at, url,
             tags_json, today_str, today_str, raw_json, int(is_star), classification_json, method),
        )
        result = {"is_new": True, "already_notified": False}
    else:
        conn.execute(
            "UPDATE postings SET last_seen_date = ?, close_at = ?, title = ?, is_star = ?, "
            "classification_json = ?, method = ? WHERE id = ?",
            (today_str, close_at, title, int(is_star), classification_json, method, posting_id),
        )
        result = {"is_new": False, "already_notified": bool(row[0])}

    conn.commit()
    return result


def mark_notified(conn: sqlite3.Connection, ids: list) -> None:
    if not ids:
        return
    conn.executemany("UPDATE postings SET notified = 1 WHERE id = ?", [(i,) for i in ids])
    conn.commit()


def get_active_star_postings(conn: sqlite3.Connection, today_str: str) -> list:
    """이미 발송됐더라도(notified=1) 확실후보(is_star=1)면서 마감일이 아직 안 지난 공고를 반환한다.
    공고마감일까지 매일 다시 안내하는 리마인더 섹션용. 반환값은 postings 테이블 컬럼명 그대로의 dict 리스트."""
    rows = conn.execute(
        """
        SELECT id, source, title, org, budget, posted_at, close_at, url, category_tags
        FROM postings
        WHERE is_star = 1 AND notified = 1 AND (close_at IS NULL OR close_at = '' OR close_at >= ?)
        ORDER BY close_at ASC
        """,
        (today_str,),
    ).fetchall()
    columns = ["id", "source", "title", "org", "budget", "posted_at", "close_at", "url", "category_tags"]
    return [dict(zip(columns, row)) for row in rows]


def get_all_postings(conn: sqlite3.Connection) -> list:
    """postings 전체를 dict 리스트로 반환한다(Streamlit 대시보드 전용 읽기 전용 조회).
    category_tags/classification_json은 파싱해서 각각 tags/classification 키로 풀어 담는다."""
    rows = conn.execute(
        """
        SELECT id, source, title, org, budget, posted_at, close_at, url, category_tags,
               first_seen_date, last_seen_date, notified, is_star, classification_json, method
        FROM postings
        ORDER BY posted_at DESC
        """
    ).fetchall()
    columns = [
        "id", "source", "title", "org", "budget", "posted_at", "close_at", "url", "category_tags",
        "first_seen_date", "last_seen_date", "notified", "is_star", "classification_json", "method",
    ]
    results = []
    for row in rows:
        item = dict(zip(columns, row))
        try:
            item["tags"] = json.loads(item.pop("category_tags") or "[]")
        except (TypeError, ValueError):
            item["tags"] = []
        try:
            item["classification"] = json.loads(item.pop("classification_json") or "{}")
        except (TypeError, ValueError):
            item["classification"] = {}
        results.append(item)
    return results


def get_historical_bid_titles(conn: sqlite3.Connection) -> list:
    """지금까지 저장된 나라장터 입찰공고(source='g2b_bid') 전체의 (제목, 발주기관) 목록을 반환한다.
    사전규격 필터링용 '학습된 키워드'를 만드는 재료 — classify.build_learned_keywords 참고."""
    rows = conn.execute("SELECT title, org FROM postings WHERE source = 'g2b_bid'").fetchall()
    return [(row[0], row[1]) for row in rows]


def upsert_institutions(conn: sqlite3.Connection, institutions: list) -> None:
    """[(name, category, source), ...] 형태의 기관 마스터 리스트를 저장한다."""
    conn.executemany(
        "INSERT INTO institutions (name, category, source) VALUES (?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET category = excluded.category, source = excluded.source",
        institutions,
    )
    conn.commit()
