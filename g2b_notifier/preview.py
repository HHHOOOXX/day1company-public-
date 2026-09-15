"""로컬 브라우저에서 열어보는 Slack 메시지 UI 미리보기.

`notify`가 실제로 보낼 내용과 동일한 데이터(관심 공고 목록)를 받아서,
Slack 채널에 뜬 것처럼 생긴 정적 HTML 파일을 만든다. Slack 발송도, DB 기록도 하지 않는다.
"""

import html
import os
import webbrowser
from datetime import date

from .slack import _short_date, deadline_date

PREVIEW_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "preview.html")

_SECTION_META = {
    "bid": ("\U0001F4CB 나라장터 입찰공고 알림", "게시분", "입찰공고 · bid.g2b.go.kr", "마감"),
    "pre_spec": ("\U0001F4D0 나라장터 사전규격 알림", "등록분", "사전규격 · 규격서 의견 접수 중", "의견"),
    "bizinfo": ("\U0001F3E2 기업마당 지원사업 알림", "등록분", "중소기업 지원사업 · bizinfo.go.kr", "신청"),
}
_SOURCE_TAG = {"bid": "입찰", "pre_spec": "사전규격", "bizinfo": "기업마당"}


def _esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def _row_html(idx: int, item: dict, label: str, tag: str = None) -> str:
    org = _esc(item.get("ntceInsttNm", ""))
    title = _esc(item.get("bidNtceNm", ""))
    url = item.get("bidNtceDtlUrl", "")
    title_html = f'<a href="{_esc(url)}" target="_blank" rel="noopener">{title}</a>' if url else title
    tag_html = f'<span class="tag-chip">{_esc(tag)}</span>' if tag else ""
    return f'''<div class="row">
      <span class="row-idx">{idx}</span>
      <div class="row-body">
        <span class="row-org">[{org}]</span> {title_html}
        <span class="row-meta"><span class="meta-label">{label}</span> {_esc(_short_date(item))}</span>{tag_html}
      </div>
    </div>'''


def _section_html(source: str, items: list, period_label: str) -> str:
    title, suffix, sub, label = _SECTION_META[source]
    rows = "\n".join(_row_html(i, item, label) for i, item in enumerate(items, 1)) or "<div class=\"empty-row\">신규 공고 없음</div>"
    return f'''
<div class="section">
  <div class="section-head">
    <span class="section-title">{_esc(title)}</span>
    <span class="section-count">{len(items)}건</span>
  </div>
  <div class="section-sub">{_esc(period_label)} {_esc(suffix)} &middot; {_esc(sub)}</div>
  <div class="section-items">{rows}</div>
</div>'''


def _urgent_html(bid_items, pre_spec_items, bizinfo_items, today: date, top_n: int = 8) -> str:
    pool = (
        [("bid", item) for item in bid_items]
        + [("pre_spec", item) for item in pre_spec_items]
        + [("bizinfo", item) for item in bizinfo_items]
    )
    dated = [(deadline_date(item), source, item) for source, item in pool]
    dated = [d for d in dated if d[0] is not None and d[0] >= today]
    dated.sort(key=lambda d: d[0])
    top = dated[:top_n]

    if not top:
        return ""

    rows = "\n".join(
        _row_html(i, item, _SECTION_META[source][3], _SOURCE_TAG[source])
        for i, (_, source, item) in enumerate(top, 1)
    )
    return f'''
<div class="urgent-block">
  <div class="urgent-head"><span class="urgent-title">\U0001F525 마감임박 TOP {len(top)}</span></div>
  <div class="urgent-sub">전체 {len(pool)}건 중 오늘 기준 가장 급한 것부터</div>
  <div class="urgent-rows">{rows}</div>
</div>'''


_PAGE_TEMPLATE = """<!doctype html>
<title>G2B 알림봇 미리보기</title>
<style>
  :root {{
    --sidebar-bg: #19171D; --sidebar-active-bg: #1164A3;
    --sidebar-text: #C3B8D1; --sidebar-text-bright: #FFFFFF;
    --header-bg: #FFFFFF; --header-border: #DEDEDE;
    --bg-main: #FFFFFF; --bg-msg-hover: #F8F8F8;
    --text-primary: #1D1C1D; --text-secondary: #616061; --text-tertiary: #8A8A8A;
    --link-color: #1264A3; --bot-badge-bg: #ECECEC; --bot-avatar-bg: #4A154B;
    --count-pill-bg: #F0F0F0; --count-pill-text: #4A4A4A;
    --banner-bg: #FFF8E1; --banner-border: #F0DFA0; --banner-text: #6B5A17;
    --urgent-bg: #FFF4EC; --urgent-border: #F3C9A6; --urgent-title: #B5450A;
    --tag-bg: #ECECEC; --tag-text: #4A4A4A;
    --warn-bg: #FDECEC; --warn-border: #F3B4B4; --warn-text: #9B2C2C;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{
    margin: 0; background: var(--bg-main); color: var(--text-primary);
    font-family: 'Lato', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
  }}
  a {{ color: var(--link-color); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .preview-banner {{
    background: var(--banner-bg); border-bottom: 1px solid var(--banner-border); color: var(--banner-text);
    font-size: 13px; padding: 9px 20px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
  }}
  .preview-banner b {{ font-weight: 700; }}
  .preview-banner .dot {{ opacity: .5; }}
  .warn-note {{
    background: var(--warn-bg); border-bottom: 1px solid var(--warn-border); color: var(--warn-text);
    font-size: 13px; padding: 8px 20px; font-weight: 700;
  }}
  .app {{ display: flex; min-height: 100vh; align-items: stretch; }}
  .sidebar {{ width: 260px; flex: 0 0 260px; background: var(--sidebar-bg); color: var(--sidebar-text); padding: 16px 0; display: flex; flex-direction: column; gap: 18px; }}
  .workspace-name {{ padding: 0 16px 12px; border-bottom: 1px solid rgba(255,255,255,.08); font-size: 16px; font-weight: 900; color: var(--sidebar-text-bright); }}
  .side-group {{ padding: 0 8px; display: flex; flex-direction: column; gap: 1px; }}
  .side-label {{ padding: 6px 8px 4px; font-size: 13px; color: var(--sidebar-text); opacity: .75; font-weight: 700; }}
  .side-item {{ display: flex; align-items: center; gap: 7px; padding: 5px 8px; border-radius: 6px; font-size: 15px; color: var(--sidebar-text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .side-item .hash {{ opacity: .55; }}
  .side-item.active {{ background: var(--sidebar-active-bg); color: var(--sidebar-text-bright); font-weight: 700; }}
  .side-item.unread {{ color: var(--sidebar-text-bright); font-weight: 700; }}
  .side-item .badge {{ margin-left: auto; background: #CD2553; color: #fff; font-size: 11px; font-weight: 800; border-radius: 9px; padding: 1px 6px; }}
  .main {{ flex: 1 1 auto; display: flex; flex-direction: column; min-width: 0; }}
  .channel-header {{ position: sticky; top: 0; z-index: 2; background: var(--header-bg); border-bottom: 1px solid var(--header-border); padding: 12px 24px; display: flex; align-items: baseline; gap: 10px; }}
  .channel-header .hash {{ color: var(--text-tertiary); font-weight: 400; font-size: 19px; }}
  .channel-header h1 {{ font-size: 17px; margin: 0; font-weight: 900; }}
  .channel-header .members {{ color: var(--text-secondary); font-size: 13px; }}
  .message-list {{ padding: 18px 24px 60px; }}
  .msg {{ display: grid; grid-template-columns: 40px 1fr; gap: 12px; padding: 10px 8px; border-radius: 8px; }}
  .msg:hover {{ background: var(--bg-msg-hover); }}
  .avatar {{ width: 40px; height: 40px; border-radius: 8px; background: var(--bot-avatar-bg); color: #fff; display: flex; align-items: center; justify-content: center; font-size: 20px; }}
  .msg-head {{ display: flex; align-items: baseline; gap: 7px; flex-wrap: wrap; }}
  .bot-name {{ font-weight: 900; font-size: 15px; }}
  .bot-badge {{ background: var(--bot-badge-bg); color: var(--text-secondary); font-size: 10px; font-weight: 800; padding: 1px 5px; border-radius: 3px; }}
  .msg-time {{ color: var(--text-tertiary); font-size: 12px; }}
  .section-list {{ display: flex; flex-direction: column; gap: 22px; margin-top: 6px; }}
  .urgent-block {{ background: var(--urgent-bg); border: 1px solid var(--urgent-border); border-radius: 10px; padding: 12px 14px 8px; }}
  .urgent-head {{ display: flex; align-items: center; gap: 8px; }}
  .urgent-title {{ font-weight: 900; font-size: 15px; color: var(--urgent-title); }}
  .urgent-sub {{ color: var(--text-secondary); font-size: 12.5px; margin: 2px 0 10px; }}
  .urgent-rows {{ display: flex; flex-direction: column; gap: 4px; }}
  .section-head {{ display: flex; align-items: center; gap: 8px; margin-bottom: 2px; }}
  .section-title {{ font-weight: 900; font-size: 15px; }}
  .section-count {{ background: var(--count-pill-bg); color: var(--count-pill-text); font-size: 12px; font-weight: 800; padding: 1px 8px; border-radius: 10px; }}
  .section-sub {{ color: var(--text-secondary); font-size: 13px; margin-bottom: 10px; }}
  .section-items {{ display: flex; flex-direction: column; gap: 4px; }}
  .empty-row {{ color: var(--text-tertiary); font-size: 13.5px; padding: 4px 8px; font-style: italic; }}
  .row {{ display: grid; grid-template-columns: 22px 1fr; gap: 8px; padding: 5px 8px; border-radius: 6px; font-size: 14px; line-height: 1.55; }}
  .row:hover {{ background: var(--bg-msg-hover); }}
  .row-idx {{ color: var(--text-tertiary); font-size: 12.5px; font-variant-numeric: tabular-nums; padding-top: 2px; }}
  .row-org {{ font-weight: 800; }}
  .row-meta {{ color: var(--text-secondary); font-size: 12.5px; white-space: nowrap; margin-left: 4px; }}
  .meta-label {{ font-weight: 700; color: var(--text-tertiary); }}
  .tag-chip {{ background: var(--tag-bg); color: var(--tag-text); font-size: 10.5px; font-weight: 800; padding: 1px 6px; border-radius: 8px; margin-left: 6px; }}
  @media (max-width: 760px) {{
    .sidebar {{ display: none; }}
    .channel-header, .message-list {{ padding-left: 14px; padding-right: 14px; }}
    .row {{ grid-template-columns: 16px 1fr; }}
  }}
</style>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Lato:wght@400;700;900&display=swap">

<div class="preview-banner">
  <span>\U0001F50E <b>로컬 미리보기</b></span>
  <span class="dot">&middot;</span>
  <span>실제로 Slack에 발송되지 않았습니다</span>
  <span class="dot">&middot;</span>
  <span>기준일: {period_label} 게시/등록분</span>
</div>
{warning_html}
<div class="app">
  <nav class="sidebar">
    <div class="workspace-name">Day1Company</div>
    <div class="side-group">
      <div class="side-label">채널</div>
      <div class="side-item"><span class="hash">#</span> general</div>
      <div class="side-item"><span class="hash">#</span> 공공사업그룹</div>
      <div class="side-item active unread"><span class="hash">#</span> g2b-입찰알림 <span class="badge">{total_count}</span></div>
    </div>
    <div class="side-group">
      <div class="side-label">앱</div>
      <div class="side-item">G2B 알림봇</div>
    </div>
  </nav>
  <main class="main">
    <header class="channel-header">
      <span class="hash">#</span>
      <h1>g2b-입찰알림</h1>
      <span class="members">\U0001F465 12명</span>
    </header>
    <div class="message-list">
      <div class="msg">
        <div class="avatar">\U0001F916</div>
        <div>
          <div class="msg-head">
            <span class="bot-name">G2B 알림봇</span>
            <span class="bot-badge">APP</span>
            <span class="msg-time">오전 10:00</span>
          </div>
          <div class="section-list">
            {urgent_html}
            {bid_html}
            {pre_spec_html}
            {bizinfo_html}
          </div>
        </div>
      </div>
    </div>
  </main>
</div>
"""


def build_preview_html(bid_items, pre_spec_items, bizinfo_items, start_date, end_date, warnings=None) -> str:
    period_label = start_date.isoformat() if start_date == end_date else f"{start_date.isoformat()}~{end_date.isoformat()}"
    today = date.today()
    total = len(bid_items) + len(pre_spec_items) + len(bizinfo_items)

    warning_html = ""
    if warnings:
        items = "".join(f"<div>- {_esc(w)}</div>" for w in warnings)
        warning_html = f'<div class="warn-note">⚠️ 일부 데이터 수집 실패{items}</div>'

    return _PAGE_TEMPLATE.format(
        period_label=_esc(period_label),
        total_count=total,
        warning_html=warning_html,
        urgent_html=_urgent_html(bid_items, pre_spec_items, bizinfo_items, today),
        bid_html=_section_html("bid", bid_items, period_label),
        pre_spec_html=_section_html("pre_spec", pre_spec_items, period_label),
        bizinfo_html=_section_html("bizinfo", bizinfo_items, period_label),
    )


def write_and_open_preview(bid_items, pre_spec_items, bizinfo_items, start_date, end_date, warnings=None, path=None, auto_open=True) -> str:
    path = path or PREVIEW_PATH
    html_content = build_preview_html(bid_items, pre_spec_items, bizinfo_items, start_date, end_date, warnings)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"[미리보기] {path} 생성 완료")
    if auto_open:
        webbrowser.open("file://" + os.path.abspath(path))
    return path
