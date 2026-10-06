"""로컬 브라우저에서 열어보는 실무자용 공고 대시보드 미리보기.

`notify`가 실제로 보낼 내용과 동일한 데이터(관심 공고 목록)를 받아서, 표 형태의 정적 HTML
대시보드를 만든다. 정렬/필터/검색은 전부 클라이언트 사이드 JS. Slack 발송도, DB 기록도 하지 않는다.
"""

import html
import os
import webbrowser
from datetime import date

from .classify import tag_business_area
from .slack import dday_parts, deadline_date, format_money

PREVIEW_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "preview.html")

_SOURCE_META = {
    "bid": {"label": "입찰공고", "cls": "src-bid", "date_label": "마감"},
    "pre_spec": {"label": "사전규격", "cls": "src-prespec", "date_label": "입찰 전 의견마감"},
    "bizinfo": {"label": "기업마당", "cls": "src-bizinfo", "date_label": "신청마감"},
    "agency": {"label": "기관공고", "cls": "src-agency", "date_label": "마감"},
}


def _esc(s) -> str:
    return html.escape(str(s) if s is not None else "", quote=True)


def _dday(item: dict, today: date):
    """(표시용 텍스트, CSS 클래스, 정렬용 키) 반환. 마감일 파싱 불가면 '미정' 취급으로 정렬 시 맨 뒤로 보낸다."""
    date_str, delta, passed = dday_parts(item, today)
    if delta is None:
        return _esc(date_str), "dday-unknown", "9999-99-99"

    d = deadline_date(item)
    sort_key = d.isoformat()
    if item.get("_deadline_basis"):
        # 직찰 공고: 마감일 대신 개찰일시로 채운 값(g2b._fill_missing_bid_deadline)이라 구분해서 보여준다.
        date_str = f"{date_str} {item['_deadline_basis']}"
    if passed:
        return f"{date_str} 마감", "dday-passed", sort_key
    if delta <= 3:
        return f"{date_str} (D-{delta})" if delta else f"{date_str} (D-day)", "dday-urgent", sort_key
    if delta <= 7:
        return f"{date_str} (D-{delta})", "dday-soon", sort_key
    return f"{date_str} (D-{delta})", "dday-normal", sort_key


def _table_row(idx: int, source: str, item: dict, today: date) -> str:
    meta = _SOURCE_META[source]
    org = item.get("ntceInsttNm", "")
    title = item.get("bidNtceNm", "")
    url = item.get("bidNtceDtlUrl", "")
    title_html = f'<a href="{_esc(url)}" target="_blank" rel="noopener">{_esc(title)}</a>' if url else _esc(title)

    tier = item.get("_tier", "include")
    reasons = item.get("_reasons", [])
    star = item.get("_star")
    reason_html = f'<div class="row-reason">{_esc(" / ".join(reasons))}</div>' if reasons and tier == "review" else ""
    codes = item.get("_industry_codes")
    codes_html = (
        "".join(
            f'<span class="code-chip" title="업종제한사항(입찰공고) 또는 제안요청서 원문(사전규격)에서 확인된 보유 업종코드">{_esc(c)}</span>'
            for c in sorted(codes)
        )
        if codes
        else '<span class="code-empty">-</span>'
    )
    if star:
        # 확실후보(⭐)는 그 자체로 최상위 신호라 확실포함/확인필요 배지를 같이 보여주지 않는다.
        fit_html = f'<span class="fit-chip fit-star" title="과거 수주: {_esc(star["org"])} / {_esc(star["project"])}">⭐ 확실후보</span>'
    elif tier == "review":
        fit_html = '<span class="fit-chip fit-review">⚠ 확인필요</span>'
    else:
        fit_html = '<span class="fit-chip fit-include">✅ 확실포함</span>'

    budget_raw = item.get("asignBdgtAmt", "")
    budget = format_money(budget_raw)
    contract_method = _esc(item.get("cntrctCnclsMthdNm", "")) or "-"

    induty_note = ""
    if item.get("indstrytyLmtYn") == "Y":
        induty_note = (
            '<span class="induty-chip" title="업종제한(제한경쟁) 공고 — 보유 업종코드로 참가자격 확인됨">업종제한</span>'
        )

    posted = (item.get("bidNtceDt", "") or "")[:10] or "-"
    dday_text, dday_cls, deadline_sort = _dday(item, today)

    tags = tag_business_area(title)
    tags_html = "".join(f'<span class="area-chip">{_esc(t)}</span>' for t in tags) or "-"

    search_blob = _esc(f"{org} {title}".lower())

    return f'''<tr class="data-row" data-source="{source}" data-tier="{tier}" data-deadline="{deadline_sort}" data-budget="{int(budget_raw) if str(budget_raw).isdigit() else 0}" data-search="{search_blob}">
  <td class="col-idx">{idx}</td>
  <td><span class="src-chip {meta["cls"]}">{meta["label"]}</span></td>
  <td class="col-org">{_esc(org)}</td>
  <td class="col-title">{title_html}{reason_html}</td>
  <td class="col-area">{tags_html}</td>
  <td class="col-budget">{budget}</td>
  <td class="col-method">{contract_method}{induty_note}</td>
  <td class="col-codes">{codes_html}</td>
  <td class="col-posted">{posted}</td>
  <td class="col-deadline"><span class="dday-badge {dday_cls}">{dday_text}</span><div class="deadline-label">{meta["date_label"]}</div></td>
  <td class="col-fit">{fit_html}</td>
</tr>'''


_PAGE_TEMPLATE = """<!doctype html>
<meta charset="utf-8">
<title>AX사업기획실 공고목록</title>
<style>
  :root {{
    --bg: #F5F6F8; --card-bg: #FFFFFF; --border: #E3E5E9;
    --text-primary: #17181C; --text-secondary: #5B5F68; --text-tertiary: #9298A2;
    --accent: #2F6FED; --accent-soft: #EAF1FF;
    --warn-bg: #FFF4E0; --warn-border: #F0D08A; --warn-text: #7A5300;
    --danger-bg: #FDECEC; --danger-border: #F3B4B4; --danger-text: #9B2C2C;
    --ok-bg: #E7F7EE; --ok-text: #1E7B45;
    --src-bid-bg: #E8EEFD; --src-bid-text: #2A4FB0;
    --src-prespec-bg: #F2E9FC; --src-prespec-text: #6B3FA0;
    --src-bizinfo-bg: #E4F6EC; --src-bizinfo-text: #1E7B45;
    --src-agency-bg: #FDF0E4; --src-agency-text: #A0521C;
    --dday-urgent: #D8352B; --dday-soon: #C97A0A; --dday-normal: #5B5F68; --dday-passed: #ABB0B8;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{
    margin: 0; background: var(--bg); color: var(--text-primary);
    font-family: 'Lato', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
  }}
  a {{ color: var(--accent); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .page {{ max-width: 1400px; margin: 0 auto; padding: 18px 20px 60px; }}
  .top-banner {{
    background: var(--accent-soft); border: 1px solid #CBDCFB; color: #1D4AA8; border-radius: 10px;
    font-size: 13px; padding: 9px 16px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 14px;
  }}
  .top-banner b {{ font-weight: 800; }}
  .top-banner .dot {{ opacity: .5; }}
  .warn-note {{
    background: var(--danger-bg); border: 1px solid var(--danger-border); color: var(--danger-text);
    font-size: 13px; padding: 8px 16px; font-weight: 700; border-radius: 10px; margin-bottom: 14px;
  }}
  .page-head {{ display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 14px; flex-wrap: wrap; }}
  .page-head h1 {{ font-size: 20px; margin: 0; font-weight: 900; }}
  .page-head .period {{ color: var(--text-secondary); font-size: 13px; }}
  .stat-row {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin-bottom: 16px; }}
  .stat-card {{ background: var(--card-bg); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; }}
  .stat-card .num {{ font-size: 22px; font-weight: 900; line-height: 1.2; }}
  .stat-card .lbl {{ font-size: 12px; color: var(--text-secondary); margin-top: 2px; }}
  .stat-card.accent .num {{ color: var(--accent); }}
  .stat-card.warn .num {{ color: var(--warn-text); }}
  .stat-card.danger .num {{ color: var(--dday-urgent); }}
  .legend-note {{
    background: var(--warn-bg); border: 1px solid var(--warn-border); color: var(--warn-text);
    font-size: 12.5px; padding: 9px 16px; border-radius: 10px; margin-bottom: 14px; line-height: 1.6;
  }}
  .legend-note b {{ font-weight: 800; }}
  .toolbar {{ display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 12px; }}
  .filter-group {{ display: flex; gap: 4px; background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 3px; }}
  .filter-btn {{ border: none; background: transparent; color: var(--text-secondary); font-size: 12.5px; font-weight: 700; padding: 6px 11px; border-radius: 6px; cursor: pointer; }}
  .filter-btn:hover {{ background: var(--bg); }}
  .filter-btn.active {{ background: var(--accent); color: #fff; }}
  .search-box {{ flex: 1 1 220px; max-width: 320px; }}
  .search-box input {{
    width: 100%; border: 1px solid var(--border); border-radius: 8px; padding: 7px 12px; font-size: 13px;
    background: var(--card-bg); color: var(--text-primary);
  }}
  .search-box input:focus {{ outline: 2px solid var(--accent); outline-offset: -1px; }}
  .result-count {{ color: var(--text-tertiary); font-size: 12.5px; margin-left: auto; }}
  .table-wrap {{ background: var(--card-bg); border: 1px solid var(--border); border-radius: 12px; overflow: auto; max-height: 78vh; }}
  table {{ border-collapse: collapse; width: 100%; min-width: 1080px; font-size: 13px; }}
  thead th {{
    position: sticky; top: 0; background: #FAFBFC; border-bottom: 1px solid var(--border); z-index: 1;
    text-align: left; padding: 10px 10px; font-size: 12px; color: var(--text-secondary); font-weight: 800;
    white-space: nowrap; cursor: pointer; user-select: none;
  }}
  thead th:hover {{ color: var(--text-primary); }}
  thead th.sorted-asc::after {{ content: " \\25B2"; font-size: 9px; }}
  thead th.sorted-desc::after {{ content: " \\25BC"; font-size: 9px; }}
  tbody td {{ padding: 9px 10px; border-bottom: 1px solid var(--border); vertical-align: top; line-height: 1.5; }}
  tbody tr:hover {{ background: #FAFBFF; }}
  tbody tr.is-hidden {{ display: none; }}
  .col-idx {{ color: var(--text-tertiary); font-variant-numeric: tabular-nums; }}
  .col-org {{ font-weight: 800; white-space: nowrap; max-width: 200px; overflow: hidden; text-overflow: ellipsis; }}
  .col-title {{ min-width: 260px; }}
  .col-budget, .col-method, .col-posted, .col-codes {{ white-space: nowrap; color: var(--text-secondary); }}
  .row-reason {{ color: var(--warn-text); font-size: 11.5px; margin-top: 3px; font-style: italic; }}
  .src-chip {{ font-size: 10.5px; font-weight: 800; padding: 2px 7px; border-radius: 8px; white-space: nowrap; }}
  .src-bid {{ background: var(--src-bid-bg); color: var(--src-bid-text); }}
  .src-prespec {{ background: var(--src-prespec-bg); color: var(--src-prespec-text); }}
  .src-bizinfo {{ background: var(--src-bizinfo-bg); color: var(--src-bizinfo-text); }}
  .src-agency {{ background: var(--src-agency-bg); color: var(--src-agency-text); }}
  .area-chip {{ display: inline-block; background: #F0F1F4; color: #4A4E57; font-size: 10.5px; font-weight: 700; padding: 1px 6px; border-radius: 7px; margin: 1px 3px 1px 0; white-space: nowrap; }}
  .induty-chip {{ display: inline-block; background: #EAF1FF; color: #2A4FB0; font-size: 10px; font-weight: 800; padding: 1px 6px; border-radius: 7px; margin-left: 5px; cursor: help; }}
  .fit-chip {{ font-size: 11px; font-weight: 800; padding: 2px 8px; border-radius: 8px; white-space: nowrap; display: inline-block; }}
  .fit-include {{ background: var(--ok-bg); color: var(--ok-text); }}
  .fit-review {{ background: var(--warn-bg); color: var(--warn-text); border: 1px solid var(--warn-border); }}
  .fit-star {{ background: #FFF4CC; color: #8A6D00; border: 1px solid #E8C34A; margin-right: 4px; cursor: help; }}
  .fit-codes {{ background: #E3F2FD; color: #0D47A1; border: 1px solid #90CAF9; margin-right: 4px; cursor: help; }}
  .code-chip {{ display: inline-block; background: #E3F2FD; color: #0D47A1; border: 1px solid #90CAF9; font-size: 11px; font-weight: 800; padding: 1px 7px; border-radius: 7px; cursor: help; font-variant-numeric: tabular-nums; margin: 1px 3px 1px 0; }}
  .code-empty {{ color: var(--text-tertiary); }}
  .dday-badge {{ font-weight: 800; font-size: 12.5px; white-space: nowrap; }}
  .dday-urgent {{ color: var(--dday-urgent); }}
  .dday-soon {{ color: var(--dday-soon); }}
  .dday-normal {{ color: var(--dday-normal); }}
  .dday-passed {{ color: var(--dday-passed); }}
  .dday-unknown {{ color: var(--text-tertiary); }}
  .deadline-label {{ font-size: 10.5px; color: var(--text-tertiary); margin-top: 1px; }}
  .empty-state {{ text-align: center; color: var(--text-tertiary); padding: 40px 0; font-size: 13.5px; }}
  @media (max-width: 760px) {{
    .page {{ padding: 14px 12px 40px; }}
  }}
</style>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Lato:wght@400;700;900&display=swap">

<div class="page">
  <div class="top-banner">
    <span>\U0001F50E <b>로컬 미리보기</b></span>
    <span class="dot">&middot;</span>
    <span>실제로 Slack에 발송되지 않았습니다</span>
    <span class="dot">&middot;</span>
    <span>기준일: {period_label} 게시/등록분</span>
  </div>
  {warning_html}

  <div class="page-head">
    <h1>\U0001F4CB AX사업기획실 공고목록</h1>
    <span class="period">{period_label} 기준 · 총 {total_count}건</span>
  </div>

  <div class="stat-row">
    <div class="stat-card"><div class="num">{total_count}</div><div class="lbl">전체 건수</div></div>
    <div class="stat-card accent"><div class="num">{include_count}</div><div class="lbl">✅ 확실포함</div></div>
    <div class="stat-card warn"><div class="num">{review_count}</div><div class="lbl">⚠ 확인필요</div></div>
    <div class="stat-card danger"><div class="num">{urgent_count}</div><div class="lbl">\U0001F525 마감 3일 이내</div></div>
  </div>

  {legend_html}

  <div class="toolbar">
    <div class="filter-group" data-filter-group="source">
      <button class="filter-btn active" data-source="all">전체</button>
      <button class="filter-btn" data-source="bid">입찰공고</button>
      <button class="filter-btn" data-source="pre_spec">사전규격</button>
      <button class="filter-btn" data-source="bizinfo">기업마당</button>
      <button class="filter-btn" data-source="agency">기관공고</button>
    </div>
    <div class="filter-group" data-filter-group="tier">
      <button class="filter-btn active" data-tier="all">전체</button>
      <button class="filter-btn" data-tier="include">✅ 확실포함</button>
      <button class="filter-btn" data-tier="review">⚠ 확인필요</button>
    </div>
    <div class="search-box">
      <input type="text" id="searchInput" placeholder="발주기관/용역명 검색">
    </div>
    <span class="result-count" id="resultCount"></span>
  </div>

  <div class="table-wrap">
    <table id="dataTable">
      <thead>
        <tr>
          <th data-key="idx" data-type="num">#</th>
          <th data-key="source" data-type="text">구분</th>
          <th data-key="org" data-type="text">발주기관</th>
          <th data-key="title" data-type="text">용역명</th>
          <th data-key="area" data-type="text">사업영역</th>
          <th data-key="budget" data-type="num">예산</th>
          <th data-key="method" data-type="text">계약방법</th>
          <th data-key="codes" data-type="text">업종코드 확인</th>
          <th data-key="posted" data-type="text">게시일</th>
          <th data-key="deadline" data-type="text" class="sorted-asc">마감</th>
          <th data-key="fit" data-type="text">적합성</th>
        </tr>
      </thead>
      <tbody>
        {table_rows}
      </tbody>
    </table>
    <div class="empty-state" id="emptyState" style="display:none;">조건에 맞는 공고가 없습니다.</div>
  </div>
</div>

<script>
(function() {{
  var table = document.getElementById('dataTable');
  var tbody = table.querySelector('tbody');
  var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr.data-row'));
  var state = {{ source: 'all', tier: 'all', query: '', sortKey: 'deadline', sortDir: 'asc' }};

  function cellText(row, key) {{
    var idx = {{ idx: 0, source: 1, org: 2, title: 3, area: 4, budget: 5, method: 6, codes: 7, posted: 8, deadline: 9, fit: 10 }}[key];
    return row.children[idx].textContent.trim();
  }}

  function sortValue(row, key) {{
    if (key === 'deadline') return row.getAttribute('data-deadline');
    if (key === 'budget') return parseInt(row.getAttribute('data-budget') || '0', 10);
    if (key === 'idx') return parseInt(cellText(row, 'idx'), 10);
    return cellText(row, key).toLowerCase();
  }}

  function applyAll() {{
    var visible = 0;
    rows.forEach(function(row) {{
      var matchesSource = state.source === 'all' || row.getAttribute('data-source') === state.source;
      var matchesTier = state.tier === 'all' || row.getAttribute('data-tier') === state.tier;
      var matchesQuery = !state.query || row.getAttribute('data-search').indexOf(state.query) !== -1;
      var show = matchesSource && matchesTier && matchesQuery;
      row.classList.toggle('is-hidden', !show);
      if (show) visible++;
    }});
    document.getElementById('resultCount').textContent = visible + ' / ' + rows.length + '건 표시';
    document.getElementById('emptyState').style.display = visible === 0 ? 'block' : 'none';
  }}

  function applySort() {{
    var dir = state.sortDir === 'asc' ? 1 : -1;
    rows.sort(function(a, b) {{
      var va = sortValue(a, state.sortKey), vb = sortValue(b, state.sortKey);
      if (va < vb) return -1 * dir;
      if (va > vb) return 1 * dir;
      return 0;
    }});
    rows.forEach(function(row) {{ tbody.appendChild(row); }});
  }}

  table.querySelectorAll('thead th').forEach(function(th) {{
    th.addEventListener('click', function() {{
      var key = th.getAttribute('data-key');
      if (state.sortKey === key) {{
        state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc';
      }} else {{
        state.sortKey = key;
        state.sortDir = 'asc';
      }}
      table.querySelectorAll('thead th').forEach(function(h) {{ h.classList.remove('sorted-asc', 'sorted-desc'); }});
      th.classList.add(state.sortDir === 'asc' ? 'sorted-asc' : 'sorted-desc');
      applySort();
    }});
  }});

  document.querySelectorAll('[data-filter-group="source"] .filter-btn').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      document.querySelectorAll('[data-filter-group="source"] .filter-btn').forEach(function(b) {{ b.classList.remove('active'); }});
      btn.classList.add('active');
      state.source = btn.getAttribute('data-source');
      applyAll();
    }});
  }});

  document.querySelectorAll('[data-filter-group="tier"] .filter-btn').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      document.querySelectorAll('[data-filter-group="tier"] .filter-btn').forEach(function(b) {{ b.classList.remove('active'); }});
      btn.classList.add('active');
      state.tier = btn.getAttribute('data-tier');
      applyAll();
    }});
  }});

  document.getElementById('searchInput').addEventListener('input', function(e) {{
    state.query = e.target.value.trim().toLowerCase();
    applyAll();
  }});

  applySort();
  applyAll();
}})();
</script>
"""


def build_preview_html(bid_items, pre_spec_items, bizinfo_items, start_date, end_date, warnings=None, agency_items=()) -> str:
    period_label = start_date.isoformat() if start_date == end_date else f"{start_date.isoformat()}~{end_date.isoformat()}"
    today = date.today()

    all_entries = (
        [("bid", item) for item in bid_items]
        + [("pre_spec", item) for item in pre_spec_items]
        + [("bizinfo", item) for item in bizinfo_items]
        + [("agency", item) for item in agency_items]
    )
    # 마감 임박(오늘 기준) 순으로 기본 정렬해서 보여준다.
    all_entries.sort(key=lambda pair: deadline_date(pair[1]) or date.max)

    total = len(all_entries)
    include_count = sum(1 for _, item in all_entries if item.get("_tier", "include") != "review")
    review_count = total - include_count
    urgent_count = sum(
        1 for _, item in all_entries
        if (d := deadline_date(item)) is not None and 0 <= (d - today).days <= 3
    )

    warning_html = ""
    if warnings:
        items = "".join(f"<div>- {_esc(w)}</div>" for w in warnings)
        warning_html = f'<div class="warn-note">⚠️ 일부 데이터 수집 실패{items}</div>'

    legend_html = ""
    if review_count:
        legend_html = (
            '<div class="legend-note"><b>⚠ 확인필요 판단기준</b> — '
            "① 발주기관이 대학교/지자체 핵심군이 아닌 공공기관 성격(공사·공단·협회·연구원 등) "
            "② 제목이 '운영/교육' 같은 범용 단어로만 매칭돼 실제 교육·콘텐츠 사업인지 불확실 "
            "③ 업종제한 조회 실패로 참가가능 여부 미확정 — 사유는 용역명 아래 붉은 글씨로 표시됩니다.</div>"
        )

    table_rows = "\n".join(
        _table_row(idx, source, item, today) for idx, (source, item) in enumerate(all_entries, 1)
    ) or ""

    return _PAGE_TEMPLATE.format(
        period_label=_esc(period_label),
        total_count=total,
        include_count=include_count,
        review_count=review_count,
        urgent_count=urgent_count,
        warning_html=warning_html,
        legend_html=legend_html,
        table_rows=table_rows,
    )


def write_and_open_preview(
    bid_items, pre_spec_items, bizinfo_items, start_date, end_date, warnings=None, path=None, auto_open=True,
    agency_items=(),
) -> str:
    path = path or PREVIEW_PATH
    html_content = build_preview_html(
        bid_items, pre_spec_items, bizinfo_items, start_date, end_date, warnings, agency_items=agency_items
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"[미리보기] {path} 생성 완료")
    if auto_open:
        webbrowser.open("file://" + os.path.abspath(path))
    return path
