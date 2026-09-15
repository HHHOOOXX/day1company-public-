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
"""


def get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


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
) -> dict:
    """공고 1건을 upsert한다.
    반환값: {"is_new": 처음 보는 공고인가, "already_notified": 예전에 이미 슬랙 발송됐는가}
    이미 발송된 공고는 오늘 조회 기간과 겹치더라도(예: 월요일이 직전 금요일을 다시 훑는 경우)
    already_notified=True로 나오므로, 호출부에서 중복 알림을 걸러낼 수 있다."""
    row = conn.execute("SELECT notified FROM postings WHERE id = ?", (posting_id,)).fetchone()
    tags_json = json.dumps(category_tags, ensure_ascii=False)

    if row is None:
        conn.execute(
            """
            INSERT INTO postings
                (id, source, title, org, budget, posted_at, close_at, url,
                 category_tags, relevant, first_seen_date, last_seen_date, notified, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 0, ?)
            """,
            (posting_id, source, title, org, budget, posted_at, close_at, url,
             tags_json, today_str, today_str, raw_json),
        )
        result = {"is_new": True, "already_notified": False}
    else:
        conn.execute(
            "UPDATE postings SET last_seen_date = ?, close_at = ?, title = ? WHERE id = ?",
            (today_str, close_at, title, posting_id),
        )
        result = {"is_new": False, "already_notified": bool(row[0])}

    conn.commit()
    return result


def mark_notified(conn: sqlite3.Connection, ids: list) -> None:
    if not ids:
        return
    conn.executemany("UPDATE postings SET notified = 1 WHERE id = ?", [(i,) for i in ids])
    conn.commit()


def upsert_institutions(conn: sqlite3.Connection, institutions: list) -> None:
    """[(name, category, source), ...] 형태의 기관 마스터 리스트를 저장한다."""
    conn.executemany(
        "INSERT INTO institutions (name, category, source) VALUES (?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET category = excluded.category, source = excluded.source",
        institutions,
    )
    conn.commit()
