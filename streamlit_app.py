"""AX사업기획실 공고목록 — Streamlit 대시보드.

data/notifier.db(매일 GitHub Actions가 커밋)를 읽기 전용으로 열어서, 입찰공고/사전규격/기업마당
매칭 공고를 카드형 리스트(크제비 등 전문 입찰정보 서비스 스타일 참고, 2026-09-22)로 보여준다.
Streamlit Community Cloud에 이 저장소를 연결하면(entry point: streamlit_app.py) 그대로 배포된다.

로컬 실행: streamlit run streamlit_app.py
"""

import html
from datetime import date, datetime

import pandas as pd
import streamlit as st

from g2b_notifier import db
from g2b_notifier.slack import deadline_date, format_money

st.set_page_config(page_title="AX사업기획실 공고목록", page_icon="📋", layout="wide")

_SOURCE_LABELS = {"g2b_bid": "📋 입찰공고", "g2b_prespec": "📐 사전규격", "bizinfo": "🏢 기업마당"}
_DEADLINE_LABELS = {"g2b_bid": "마감", "g2b_prespec": "의견마감", "bizinfo": "신청마감"}

# 적합성/마감임박 배지 색상 — (글자색, 배경색). Slack 메시지(slack.py의 🔴🟠🟡🟢 신호등)와
# 같은 긴급도 감각을 웹 배지로 옮긴 것.
_FIT_STYLE = {
    "⭐ 확실후보": ("#6d28d9", "#ede9fe"),
    "✅ 확실포함": ("#15803d", "#dcfce7"),
    "⚠️ 확인필요": ("#b45309", "#fef3c7"),
    "❔ 분류정보없음(구버전)": ("#4b5563", "#f3f4f6"),
}
_DDAY_STYLE_URGENT = ("#b91c1c", "#fee2e2")
_DDAY_STYLE_SOON = ("#c2410c", "#ffedd5")
_DDAY_STYLE_WEEK = ("#a16207", "#fef9c3")
_DDAY_STYLE_SAFE = ("#15803d", "#dcfce7")
_DDAY_STYLE_UNKNOWN = ("#6b7280", "#e5e7eb")


@st.cache_data(ttl=300)
def load_postings() -> pd.DataFrame:
    """DB에서 전체 공고를 읽어 DataFrame으로 변환한다. 5분 캐시 — 새로고침해도 매번 DB를 다시
    열지 않는다(Streamlit Cloud는 파일시스템이 읽기전용에 가까워 잦은 접근을 줄이는 게 안전하다)."""
    conn = db.get_connection()
    rows = db.get_all_postings(conn)
    conn.close()
    if not rows:
        return pd.DataFrame()

    today = datetime.now().date()
    records = []
    for row in rows:
        classification = row.get("classification") or {}
        d = deadline_date(row)
        star = classification.get("star")
        tier = classification.get("tier")
        # 2026-09-22 피드백: classification_json이 없는(확신도 분류 기능 이전에 저장된) 구버전
        # 행을 "✅확실포함"으로 잘못 기본 표시하던 버그를 고침 — tier가 아예 없는 것과 실제로
        # 'include' 판정을 받은 것은 구분해야 한다.
        if star:
            fit = "⭐ 확실후보"
        elif tier == "review":
            fit = "⚠️ 확인필요"
        elif tier == "include":
            fit = "✅ 확실포함"
        else:
            fit = "❔ 분류정보없음(구버전)"

        dday = (d - today).days if d else None
        records.append(
            {
                "source": row["source"],
                "구분": _SOURCE_LABELS.get(row["source"], row["source"]),
                "발주기관": row.get("org") or "",
                "용역명": row.get("title") or "",
                "링크": row.get("url") or "",
                "예산": format_money(row.get("budget")),
                "계약방법": row.get("method") or "-",
                "업종코드확인": ", ".join(classification.get("industry_codes") or []) or "-",
                "게시일": (row.get("posted_at") or "")[:10],
                "마감라벨": _DEADLINE_LABELS.get(row["source"], "마감"),
                "마감일": d.isoformat() if d else "",
                "마감정렬": d or date.max,
                "마감D": dday,
                "적합성": fit,
                "확실후보근거": f"{star['org']} / {star['project']}" if star else "",
                "확인사유": " / ".join(classification.get("reasons") or []),
                "오늘등록": (row.get("first_seen_date") or "") == today.isoformat(),
            }
        )
    return pd.DataFrame(records)


def _badge(text: str, fg: str, bg: str) -> str:
    return (
        f'<span style="background:{bg};color:{fg};padding:2px 9px;border-radius:11px;'
        f'font-size:12px;font-weight:600;white-space:nowrap;">{html.escape(text)}</span>'
    )


def _dday_badge(dday) -> str:
    if dday is None:
        return _badge("마감일 미정", *_DDAY_STYLE_UNKNOWN)
    if dday < 0:
        return _badge("마감", *_DDAY_STYLE_UNKNOWN)
    if dday == 0:
        return _badge("D-day", *_DDAY_STYLE_URGENT)
    if dday <= 3:
        return _badge(f"D-{dday}", *_DDAY_STYLE_URGENT)
    if dday <= 7:
        return _badge(f"D-{dday}", *_DDAY_STYLE_SOON)
    if dday <= 14:
        return _badge(f"D-{dday}", *_DDAY_STYLE_WEEK)
    return _badge(f"D-{dday}", *_DDAY_STYLE_SAFE)


def render_row(row) -> None:
    fit_fg, fit_bg = _FIT_STYLE.get(row["적합성"], _DDAY_STYLE_UNKNOWN)
    badges = _badge(row["적합성"], fit_fg, fit_bg) + " " + _dday_badge(row["마감D"])
    if row["오늘등록"]:
        badges += " " + _badge("NEW", "#1d4ed8", "#dbeafe")

    title = html.escape(row["용역명"]) or "(제목 없음)"
    title_html = (
        f'<a href="{html.escape(row["링크"])}" target="_blank" rel="noopener" '
        f'style="color:#111827;text-decoration:none;font-weight:600;font-size:15px;">{title}</a>'
        if row["링크"]
        else f'<span style="font-weight:600;font-size:15px;">{title}</span>'
    )

    meta_bits = [
        f'🏢 {html.escape(row["발주기관"]) or "-"}',
        f'💰 {html.escape(row["예산"])}',
        f'📝 {html.escape(row["계약방법"])}',
        f'📅 게시 {html.escape(row["게시일"]) or "-"}',
        f'⏰ {html.escape(row["마감라벨"])} {html.escape(row["마감일"]) or "미정"}',
    ]
    if row["업종코드확인"] != "-":
        meta_bits.append(f'📄 업종코드 {html.escape(row["업종코드확인"])}')
    meta_line = " · ".join(meta_bits)

    extra = ""
    if row["확실후보근거"]:
        extra = f'<div style="color:#6d28d9;font-size:13px;margin-top:6px;">⭐ 과거 수주: {html.escape(row["확실후보근거"])}</div>'
    elif row["확인사유"]:
        extra = f'<div style="color:#b45309;font-size:13px;margin-top:6px;">⚠️ {html.escape(row["확인사유"])}</div>'

    st.markdown(
        f'''
        <div style="border:1px solid #e5e7eb;border-radius:10px;padding:14px 16px;margin-bottom:10px;">
          <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">
            {badges}
            <span style="color:#9ca3af;font-size:12px;">{row["구분"]}</span>
          </div>
          <div style="margin-top:8px;">{title_html}</div>
          <div style="margin-top:6px;color:#6b7280;font-size:13px;">{meta_line}</div>
          {extra}
        </div>
        ''',
        unsafe_allow_html=True,
    )


df = load_postings()

st.title("📋 AX사업기획실 공고목록")
st.caption("나라장터 입찰공고 · 사전규격 · 기업마당 매칭 공고 — data/notifier.db 기준, 매일 자동 갱신")

if df.empty:
    st.info("아직 저장된 공고가 없습니다. 알림봇이 최소 한 번은 실행돼야 데이터가 쌓입니다.")
    st.stop()

today = datetime.now().date()
urgent = df[(df["마감정렬"] != date.max) & (df["마감정렬"] >= today) & (df["마감정렬"] <= today.fromordinal(today.toordinal() + 7))]

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("전체", len(df))
m2.metric("✅ 확실포함 + ⭐확실후보", int(df["적합성"].isin(["✅ 확실포함", "⭐ 확실후보"]).sum()))
m3.metric("⚠️ 확인필요", int((df["적합성"] == "⚠️ 확인필요").sum()))
m4.metric("🔥 7일 내 마감", len(urgent))
m5.metric("🆕 오늘 등록", int(df["오늘등록"].sum()))

s1, s2, s3 = st.columns(3)
s1.metric("📋 입찰공고", int((df["source"] == "g2b_bid").sum()))
s2.metric("📐 사전규격", int((df["source"] == "g2b_prespec").sum()))
s3.metric("🏢 기업마당", int((df["source"] == "bizinfo").sum()))

st.divider()

f1, f2, f3 = st.columns([1.2, 2, 1])
with f1:
    fit_options = sorted(df["적합성"].unique())
    # ⚠️확인필요는 완전히 숨기지 않고 옵션으로는 남겨두되(백엔드가 이 등급을 제외하지 않고 태그만
    # 달아 보내는 원칙과 동일), 대시보드를 처음 열었을 때는 기본으로 꺼서 확실한 건부터 보이게 한다.
    fit_default = [f for f in fit_options if f != "⚠️ 확인필요"]
    fits = st.multiselect("적합성", options=fit_options, default=fit_default)
with f2:
    query = st.text_input("검색 (발주기관/용역명)", "")
with f3:
    show_n = st.selectbox("표시 개수", [20, 50, 100, "전체"], index=0)

filtered = df[df["적합성"].isin(fits)]
if query:
    mask = filtered["발주기관"].str.contains(query, case=False, na=False) | filtered["용역명"].str.contains(
        query, case=False, na=False
    )
    filtered = filtered[mask]
filtered = filtered.sort_values("마감정렬")

tab_all, tab_bid, tab_prespec, tab_biz = st.tabs(
    [f"🔎 전체 ({len(filtered)})", "📋 입찰공고", "📐 사전규격", "🏢 기업마당"]
)


def _render_tab(subset: pd.DataFrame) -> None:
    total = len(subset)
    limit = total if show_n == "전체" else show_n
    shown = subset.head(limit)
    if shown.empty:
        st.info("조건에 맞는 공고가 없습니다.")
        return
    for _, row in shown.iterrows():
        render_row(row)
    st.caption(f"총 {total}건 중 {len(shown)}건 표시")


with tab_all:
    _render_tab(filtered)
with tab_bid:
    _render_tab(filtered[filtered["source"] == "g2b_bid"])
with tab_prespec:
    _render_tab(filtered[filtered["source"] == "g2b_prespec"])
with tab_biz:
    _render_tab(filtered[filtered["source"] == "bizinfo"])
