"""SQLite 기반 상태 저장소.

GitHub Actions엔 영구 서버가 없으므로, DB 파일(data/notifier.db) 자체를 repo에 커밋해서
"이 공고를 이전에 이미 봤는지"를 다음 실행에서도 알 수 있게 한다.
(워크플로우 마지막 단계에서 git commit/push — .github/workflows/notify.yml 참고)
"""

import json
import os
import re
import sqlite3
from datetime import datetime

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

-- 2026-10-01: 나라장터 입찰공고/사전규격 + 기업마당 원본 공고명(관심 공고 필터로 걸러진 것 포함).
-- 두 군데에 쓴다: (1) 기관 홈페이지 게시판에 며칠 늦게 다시 올라온 같은 공고를 중복으로 뺀다(실사례: IITP
-- "[자체조달 사전규격공개] 양자클러스터..."는 나라장터 등록 며칠 뒤 9/27에 IITP 게시판에 올라옴).
-- (2) 기관 게시판 2차 검토(rag_screen.py)의 과거 제외 사례 — postings엔 관심 공고만 저장돼서, 나라장터 필터에서 제외된
-- 공고 제목은 여기에만 남는다. RAW_TITLE_RETENTION_DAYS 지나면 지운다.
CREATE TABLE IF NOT EXISTS raw_titles (
    norm_title TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    last_seen_date TEXT NOT NULL
);

-- 2026-10-01: 제안 검토 이력(proposals.py). Google Drive "[제안 및 검토]" 폴더 하나 = 한 행. 팀이 폴더를 만들면
-- 매일 실행 때 자동으로 읽어 DB 공고와 짝짓는다(match_status 값의 뜻은 proposals.py 참고).
CREATE TABLE IF NOT EXISTS proposals (
    folder_id TEXT PRIMARY KEY,
    folder_name TEXT NOT NULL,
    label TEXT,
    folder_month TEXT,
    outcome TEXT,
    created TEXT,
    owner TEXT,
    match_status TEXT NOT NULL,
    posting_id TEXT,
    posting_source TEXT,
    posting_title TEXT,
    posting_org TEXT,
    notified INTEGER NOT NULL DEFAULT 0,
    score REAL,
    evidence TEXT,
    updated_date TEXT NOT NULL
);
"""

RAW_TITLE_RETENTION_DAYS = 60


def get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(_SCHEMA)
    _ensure_column(conn, "postings", "is_star", "INTEGER NOT NULL DEFAULT 0")
    # 2026-09-18: DB만 보고도 오늘 만들었던 판정(확신도/사유/업종코드/확실후보 근거)을 그대로
    # 되짚어볼 수 있도록, 그때그때 메모리에만 있던 _tier/_reasons/_star/_industry_codes를 JSON으로
    # 같이 저장해둔다 — 이게 없으면 발주기관/제목/마감일 같은 뼈대 정보만 남고 판정 근거를 잃어버린다.
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
    classification: {"tier", "reasons", "star", "industry_codes"} — 판정 근거를 그대로 저장해서,
    그날 실행이 끝난 뒤에도 왜 이렇게 분류됐는지 DB만 보고 되짚어볼 수 있게 남겨둔다."""
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


def get_historical_bid_titles(conn: sqlite3.Connection) -> list:
    """지금까지 저장된 나라장터 입찰공고(source='g2b_bid') 전체의 (제목, 발주기관) 목록을 반환한다.
    사전규격 필터링용 '학습된 키워드'를 만드는 재료 — classify.build_learned_keywords 참고."""
    rows = conn.execute("SELECT title, org FROM postings WHERE source = 'g2b_bid'").fetchall()
    return [(row[0], row[1]) for row in rows]


def get_known_titles(conn: sqlite3.Connection) -> list:
    """기관 게시판(source='agency')이 아닌 다른 소스로 이미 저장된 공고 제목 전체.
    기관 사이트에 다시 올라온 같은 공고를 중복으로 걸러내는 데 쓴다(agencies.normalize_title로 비교)."""
    rows = conn.execute("SELECT title FROM postings WHERE source != 'agency'").fetchall()
    return [row[0] for row in rows]


def save_raw_titles(conn: sqlite3.Connection, titles_by_norm: dict, today_str: str) -> None:
    """오늘 받은 원본 공고명을 저장하고, 보관기간이 지난 것은 지운다.
    titles_by_norm: {정규화 제목: (원래 제목, 출처)}."""
    conn.executemany(
        "INSERT INTO raw_titles (norm_title, title, source, last_seen_date) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(norm_title) DO UPDATE SET last_seen_date = excluded.last_seen_date",
        [(norm, title, source, today_str) for norm, (title, source) in titles_by_norm.items() if norm],
    )
    conn.execute(
        "DELETE FROM raw_titles WHERE last_seen_date < date(?, ?)",
        (today_str, f"-{RAW_TITLE_RETENTION_DAYS} days"),
    )
    conn.commit()


def get_raw_titles(conn: sqlite3.Connection) -> list:
    """[(정규화 제목, 원래 제목, 출처), ...]"""
    return conn.execute("SELECT norm_title, title, source FROM raw_titles").fetchall()


def get_postings_for_rag(conn: sqlite3.Connection) -> list:
    """기관 게시판 2차 검토(rag_screen.py)의 과거 통과 사례용: 지금까지 관심 공고로 판정돼 저장된 공고(기관 게시판 제외) 전체.
    [(source, title, org, tier, reasons(list), notified(bool)), ...]"""
    rows = conn.execute(
        "SELECT source, title, org, classification_json, notified FROM postings WHERE source != 'agency'"
    ).fetchall()
    out = []
    for source, title, org, cls_json, notified in rows:
        try:
            cls = json.loads(cls_json or "{}")
        except ValueError:
            cls = {}
        out.append((source, title, org or "", cls.get("tier", "include"), cls.get("reasons") or [], bool(notified)))
    return out


def get_postings_for_matching(conn: sqlite3.Connection) -> list:
    """제안 이력 짝짓기용 공고 목록 [(id, source, title, org, 게시일, notified)]. 기관 게시판 공고도 포함한다.
    게시일 필드 형식이 소스마다 달라 날짜로 못 읽으면 처음 수집한 날로 대신한다."""
    rows = conn.execute(
        "SELECT id, source, title, org, posted_at, first_seen_date, notified FROM postings"
    ).fetchall()
    out = []
    for pid, source, title, org, posted_at, first_seen, notified in rows:
        posted = (posted_at or "")[:10]
        if not re.match(r"\d{4}-\d{2}-\d{2}$", posted):
            posted = first_seen
        out.append((pid, source, title, org or "", posted, notified))
    return out


def get_raw_titles_dated(conn: sqlite3.Connection) -> list:
    """[(정규화 제목, 원래 제목, 출처, 마지막으로 본 날)]"""
    return conn.execute("SELECT norm_title, title, source, last_seen_date FROM raw_titles").fetchall()


def get_db_start_date(conn: sqlite3.Connection):
    """DB에 공고가 쌓이기 시작한 날(date). 비어 있으면 None."""
    row = conn.execute("SELECT MIN(first_seen_date) FROM postings").fetchone()
    try:
        return datetime.strptime(row[0], "%Y-%m-%d").date() if row and row[0] else None
    except ValueError:
        return None


def get_settled_proposal_ids(conn: sqlite3.Connection) -> set:
    """확정된 제안 폴더 — 다시 계산하지 않는다. matched/missed는 짝이 확정됐고, before_db는 DB 수집 시작 전에
    만든 폴더라 앞으로도 짝지을 공고가 생기지 않는다."""
    rows = conn.execute(
        "SELECT folder_id FROM proposals WHERE match_status IN ('matched', 'missed', 'before_db')"
    ).fetchall()
    return {r[0] for r in rows}


def upsert_proposals(conn: sqlite3.Connection, records: list, today_str: str) -> None:
    conn.executemany(
        """
        INSERT INTO proposals (folder_id, folder_name, label, folder_month, outcome, created, owner, match_status,
                               posting_id, posting_source, posting_title, posting_org, notified, score, evidence,
                               updated_date)
        VALUES (:folder_id, :folder_name, :label, :folder_month, :outcome, :created, :owner, :match_status,
                :posting_id, :posting_source, :posting_title, :posting_org, :notified, :score, :evidence, :today)
        ON CONFLICT(folder_id) DO UPDATE SET
            folder_name = excluded.folder_name, label = excluded.label, outcome = excluded.outcome,
            match_status = excluded.match_status, posting_id = excluded.posting_id,
            posting_source = excluded.posting_source, posting_title = excluded.posting_title,
            posting_org = excluded.posting_org, notified = excluded.notified, score = excluded.score,
            evidence = excluded.evidence, updated_date = excluded.updated_date
        """,
        [{**r, "notified": int(r["notified"]), "today": today_str} for r in records],
    )
    conn.commit()


def count_proposals_by_status(conn: sqlite3.Connection) -> dict:
    return dict(conn.execute("SELECT match_status, COUNT(*) FROM proposals GROUP BY match_status").fetchall())


def get_proposals(conn: sqlite3.Connection) -> list:
    conn_rows = conn.execute(
        "SELECT folder_name, created, outcome, match_status, posting_title, posting_org, posting_source, notified, "
        "score, evidence FROM proposals ORDER BY created DESC"
    ).fetchall()
    keys = ["folder_name", "created", "outcome", "match_status", "posting_title", "posting_org", "posting_source",
            "notified", "score", "evidence"]
    return [dict(zip(keys, r)) for r in conn_rows]


def get_proposal_orgs(conn: sqlite3.Connection) -> list:
    """제안 검토 폴더와 확실히 짝지어진 공고의 발주기관 — classify의 '과거 제안 기관' 판정에 더한다."""
    rows = conn.execute(
        "SELECT DISTINCT posting_org FROM proposals WHERE match_status IN ('matched', 'missed') AND posting_org != ''"
    ).fetchall()
    return [r[0] for r in rows]


def upsert_institutions(conn: sqlite3.Connection, institutions: list) -> None:
    """[(name, category, source), ...] 형태의 기관 마스터 리스트를 저장한다."""
    conn.executemany(
        "INSERT INTO institutions (name, category, source) VALUES (?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET category = excluded.category, source = excluded.source",
        institutions,
    )
    conn.commit()
