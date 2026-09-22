"""AX사업기획실 공고목록 — Streamlit 대시보드.

data/notifier.db(매일 GitHub Actions가 커밋)를 읽기 전용으로 열어서, 로컬 HTML 대시보드
(g2b_notifier/preview.py)와 같은 정보를 정렬/필터/검색 가능한 형태로 보여준다.
Streamlit Community Cloud에 이 저장소를 연결하면(entry point: streamlit_app.py) 그대로 배포된다.

로컬 실행: streamlit run streamlit_app.py
"""

from datetime import date, datetime

import pandas as pd
import streamlit as st

from g2b_notifier import db
from g2b_notifier.slack import deadline_date, format_money

st.set_page_config(page_title="AX사업기획실 공고목록", page_icon="📋", layout="wide")

_SOURCE_LABELS = {"g2b_bid": "📋 입찰공고", "g2b_prespec": "📐 사전규격", "bizinfo": "🏢 기업마당"}
_DEADLINE_LABELS = {"g2b_bid": "마감", "g2b_prespec": "의견마감", "bizinfo": "신청마감"}


@st.cache_data(ttl=300)
def load_postings() -> pd.DataFrame:
    """DB에서 전체 공고를 읽어 DataFrame으로 변환한다. 5분 캐시 — 새로고침해도 매번 DB를 다시
    열지 않는다(Streamlit Cloud는 파일시스템이 읽기전용에 가까워 잦은 접근을 줄이는 게 안전하다)."""
    conn = db.get_connection()
    rows = db.get_all_postings(conn)
    conn.close()
    if not rows:
        return pd.DataFrame()

    records = []
    for row in rows:
        classification = row.get("classification") or {}
        d = deadline_date(row)
        star = classification.get("star")
        records.append(
            {
                "source": row["source"],
                "구분": _SOURCE_LABELS.get(row["source"], row["source"]),
                "발주기관": row.get("org") or "",
                "용역명": row.get("title") or "",
                "링크": row.get("url") or "",
                "예산": format_money(row.get("budget")),
                "예산_raw": int(row.get("budget")) if str(row.get("budget") or "").isdigit() else 0,
                "계약방법": row.get("method") or "-",
                "업종코드확인": ", ".join(classification.get("industry_codes") or []) or "-",
                "게시일": (row.get("posted_at") or "")[:10],
                "마감라벨": _DEADLINE_LABELS.get(row["source"], "마감"),
                "마감일": d.isoformat() if d else "",
                "마감정렬": d or date.max,
                "적합성": (
                    "⭐ 확실후보" if star else ("⚠️ 확인필요" if classification.get("tier") == "review" else "✅ 확실포함")
                ),
                "확실후보근거": f"{star['org']} / {star['project']}" if star else "",
                "확인사유": " / ".join(classification.get("reasons") or []),
                "발송여부": "발송됨" if row.get("notified") else "미발송",
            }
        )
    return pd.DataFrame(records)


df = load_postings()

st.title("📋 AX사업기획실 공고목록")
st.caption("나라장터 입찰공고 · 사전규격 · 기업마당 매칭 공고 — data/notifier.db 기준, 매일 자동 갱신")

if df.empty:
    st.info("아직 저장된 공고가 없습니다. 알림봇이 최소 한 번은 실행돼야 데이터가 쌓입니다.")
    st.stop()

col1, col2, col3, col4 = st.columns(4)
col1.metric("전체", len(df))
col2.metric("✅ 확실포함 + ⭐확실후보", int((df["적합성"] != "⚠️ 확인필요").sum()))
col3.metric("⚠️ 확인필요", int((df["적합성"] == "⚠️ 확인필요").sum()))
today = datetime.now().date()
urgent = df[(df["마감정렬"] != date.max) & (df["마감정렬"] >= today) & (df["마감정렬"] <= today.fromordinal(today.toordinal() + 7))]
col4.metric("🔥 7일 내 마감", len(urgent))

st.divider()

f1, f2, f3 = st.columns([1, 1, 2])
with f1:
    sources = st.multiselect("구분", options=sorted(df["구분"].unique()), default=sorted(df["구분"].unique()))
with f2:
    fits = st.multiselect("적합성", options=sorted(df["적합성"].unique()), default=sorted(df["적합성"].unique()))
with f3:
    query = st.text_input("검색 (발주기관/용역명)", "")

filtered = df[df["구분"].isin(sources) & df["적합성"].isin(fits)]
if query:
    mask = filtered["발주기관"].str.contains(query, case=False, na=False) | filtered["용역명"].str.contains(
        query, case=False, na=False
    )
    filtered = filtered[mask]

filtered = filtered.sort_values("마감정렬")

st.dataframe(
    filtered[
        [
            "구분", "발주기관", "용역명", "예산", "계약방법", "업종코드확인",
            "게시일", "마감일", "적합성", "확실후보근거", "확인사유", "링크",
        ]
    ],
    column_config={
        "링크": st.column_config.LinkColumn("링크", display_text="바로가기"),
        "마감일": st.column_config.TextColumn("마감"),
    },
    hide_index=True,
    use_container_width=True,
    height=600,
)

st.caption(f"총 {len(filtered)}건 표시 중 (전체 {len(df)}건)")
