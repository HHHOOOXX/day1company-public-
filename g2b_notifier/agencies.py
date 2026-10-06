"""주요 발주기관 홈페이지 게시판 공고 직접 수집 — 나라장터·기업마당에 없는 공고만.

2026-10-01 요청: 나라장터 입찰공고·사전규격, 기업마당 외에 NIPA·NIA·IITP·중진공·소진공·서울신용보증재단·
과학창의재단·창업진흥원 홈페이지에만 올라오는 공고(수행기관 공모, 사업공고 등)를 가져온다. 공식 API가 없어
각 기관 게시판 목록 페이지를 직접 파싱한다(사이트마다 구조가 달라 기관별 파서를 둔다).

수집 결과는 입찰공고 필드명(bidNtceNm/ntceInsttNm/bidClseDt 등)으로 맞춰서 classify.py의 키워드/제외키워드/
확신도/마감임박 판정과 슬랙 카드 포맷을 그대로 재사용한다.

나라장터·기업마당에 이미 있는 공고 빼기:
  1) 제목이 최근 60일 나라장터·사전규격·기업마당 원본 공고명(db.raw_titles, 필터로 걸러진 것 포함) 또는
     DB 관심 공고와 같으면(정규화 비교) 뺀다 — 기관 게시판이 며칠 늦게 올리는 경우까지 잡는다.
  2) 제목이 조달 절차 공고(입찰·사전규격·수의계약 등)면 나라장터 검색 API로 같은 공고를 찾아보고, 있을 때만 뺀다
     (find_on_g2b). 2026-10-06 점검: NIPA·NIA·IITP 입찰공고 게시판 글은 대부분 나라장터에 있었지만, NIPA 하노이
     사무소의 이메일 접수 입찰("한-베트남 AI·디지털 포럼 운영 대행 용역")과 NIA 홈페이지 자체 [사전규격공개]
     ("본 사전공개는 정식 공고가 아니므로…")는 나라장터에 없었다. 같은 날 요청: "나라장터에서 실제로 찾을 수 없는
     공고들은 빼지 말고 일단 필터로 구분해서 가져와야 한다". 그래서 입찰공고 게시판도 수집한다.
  3) 상세페이지 본문에 나라장터 공고번호(R26BK…/R26BD…)나 나라장터 링크가 있으면 뺀다.
채용·행사 개최·참가자 모집·공모전 같은 공지도 뺀다(_is_non_bid_notice). 단 제목에 교육·콘텐츠 신호가 강하면
(classify.has_strong_fit) 빼지 않고 확인필요로 낮춘다(2026-10-06 요청). 수행기관·운영기관 모집, "기획·운영",
"용역"처럼 우리가 사업자로 들어가는 공모(_is_vendor_call — 예: 과학창의재단 "모두의 AI 챌린지 프로그램 기획·운영
사업", "클릭온 AI 프로그램 기획·운영 참여 기관 공모")는 절대 이 규칙으로 빼지 않는다.

그 다음 키워드 필터를 통과한 건은 상세페이지 본문 규칙 검사 + 과거 나라장터·기업마당 사례 검색(RAG)으로 제외 대상을
한 번 더 걸러낸다(rag_screen.py — 외부 API 없이 로컬 계산만 씀).

미지원(AGENCY_UNSUPPORTED): 창업진흥원 입찰공고 — 로그인이 필요한 자체 전자입찰 시스템(ebid.kised.or.kr).
어차피 입찰공고라 나라장터 쪽에서 받는다.
"""

import html
import json
import re
import ssl
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests
import urllib3

from datetime import timedelta
from difflib import SequenceMatcher

from .classify import (
    DEADLINE_GATE_DAYS,
    attach_confidence,
    downgrade,
    has_strong_fit,
    is_relevant_bid,
    passes_deadline_gate,
)
from .config import KR_PROXY_URL, PRE_SPEC_BASE_URL, SERVICE_KEY
from .g2b import COLLECTION_WARNINGS, RAW_SOURCE_TITLES, _call_api, _deadline_exceeded, get_lookback_range
from .slack import deadline_date
from .rag_screen import screen_items

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
_TIMEOUT = 15

AGENCY_UNSUPPORTED = {
    "창업진흥원 입찰공고": "로그인이 필요한 자체 전자입찰 시스템(ebid.kised.or.kr)이라 수집 불가",
}


class _LegacyTLSAdapter(requests.adapters.HTTPAdapter):
    """소진공(semas.or.kr) 서버는 구형 TLS 재협상만 지원해서 기본 OpenSSL 설정으로는 핸드셰이크
    자체가 실패한다(SSLError: UNSAFE_LEGACY_RENEGOTIATION_DISABLED). 이 호스트에만 마운트한다."""

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
        ctx.set_ciphers("DEFAULT@SECLEVEL=0")
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": _UA})
HTTP.mount("https://www.semas.or.kr", _LegacyTLSAdapter())


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_DATE_RE = re.compile(r"(20\d{2})[-.](\d{2})[-.](\d{2})")


def _text(html_fragment: str) -> str:
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", html_fragment or ""))).strip()


def _dates(text: str) -> list:
    """텍스트 안의 YYYY-MM-DD / YYYY.MM.DD 날짜를 등장 순서대로 'YYYY-MM-DD' 문자열로 반환한다."""
    return [f"{y}-{m}-{d}" for y, m, d in _DATE_RE.findall(text or "")]


def _row(org, key, notice_id, title, url, posted, deadline="") -> dict:
    """기관 게시판 한 줄을 입찰공고 필드명으로 맞춘 dict로 만든다(bizinfo._normalize_bizinfo와 같은 역할)."""
    return {
        "bidNtceNm": _text(title),
        "ntceInsttNm": org,
        "bidNtceNo": f"{key}:{notice_id}",
        "bidNtceOrd": "0",
        "bidNtceDt": posted,
        "bidClseDt": deadline,
        "bidNtceDtlUrl": url,
        "_agency_board": key,
    }


# ---------------------------------------------------------------- 기관별 파서


def _fetch_nipa(board: str, key: str, page: int = 1) -> list:
    """NIPA 사업공고(2-2)/입찰공고(2-3). 한 <tr>에 제목 링크(/home/2-x/ID)와 등록일이 있고,
    사업공고는 "신청기간 : 시작 ~ 종료"가 같이 들어있다(종료일을 마감으로 쓴다)."""
    html = HTTP.get(f"https://www.nipa.kr/home/{board}?curPage={page}", timeout=_TIMEOUT).text
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        m = re.search(rf'<a href="/home/{board}/(\d+)"[^>]*>(.*?)</a>', tr, re.S)
        if not m:
            continue
        title = re.sub(r"<!--.*?-->", "", m.group(2), flags=re.S)
        period = re.search(r"신청기간\s*:\s*([^<]*)", tr)
        deadline = _dates(period.group(1))[-1] if period and _dates(period.group(1)) else ""
        posted = _dates(_text(tr))[-1] if _dates(_text(tr)) else ""
        rows.append(_row("정보통신산업진흥원", key, m.group(1), title,
                         f"https://www.nipa.kr/home/{board}/{m.group(1)}", posted, deadline))
    return rows


def _fetch_nia(cb_idx: str, key: str, page: int = 1) -> list:
    """NIA 게시판(cbIdx=99835 공지사항, 78336 입찰공고). <li> 안에 doBbsFView('게시판','글번호',...) 링크와
    'YYYY.MM.DD' 등록일이 있다."""
    page_html = HTTP.get(f"https://www.nia.or.kr/site/nia_kor/ex/bbs/List.do?cbIdx={cb_idx}&pageIndex={page}",
                    timeout=_TIMEOUT).text
    rows = []
    for m in re.finditer(rf"doBbsFView\('{cb_idx}','(\d+)'[^)]*\);return false;\"[^>]*>(.*?)</a>", page_html, re.S):
        bc_idx, body = m.group(1), m.group(2)
        subject = re.search(r'<span class="subject[^"]*">(.*?)<em', body, re.S)
        src = re.search(r'<span class="src">(.*?)</span>', body, re.S)
        posted = _dates(src.group(1))[0] if src and _dates(src.group(1)) else ""
        rows.append(_row(
            "한국지능정보사회진흥원", key, bc_idx, subject.group(1) if subject else "",
            f"https://www.nia.or.kr/site/nia_kor/ex/bbs/View.do?cbIdx={cb_idx}&bcIdx={bc_idx}&parentSeq={bc_idx}",
            posted,
        ))
    return rows


def _fetch_kosac_biz(page: int = 1) -> list:
    """과학창의재단 사업공고(/menus/274/bns). 서버 렌더링된 <tr>에 등록일/공고번호, 제목 링크,
    접수기간("시작~종료")이 있다."""
    html = HTTP.get(f"https://www.kosac.re.kr/menus/274/bns?page={page}", timeout=_TIMEOUT).text
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        m = re.search(r'<a href="/menus/274/bns/(PBANC_\d+)[^"]*">(.*?)</a>', tr, re.S)
        if not m:
            continue
        info = re.search(r'<div class="info">(.*?)</div>', tr, re.S)
        period = re.search(r'<p class="period">(.*?)</p>', tr, re.S)
        posted = _dates(info.group(1))[0] if info and _dates(info.group(1)) else ""
        deadline = _dates(period.group(1))[-1] if period and _dates(period.group(1)) else ""
        rows.append(_row("한국과학창의재단", "kosac_biz", m.group(1), m.group(2),
                         f"https://www.kosac.re.kr/menus/274/bns/{m.group(1)}", posted, deadline))
    return rows


def _fetch_semas_notice(page: int = 1) -> list:
    """소진공 공지사항(bCd=1). 상세는 fncGoDetail('글번호') -> webBoardView.kmdc POST지만 GET 쿼리로도 열린다."""
    html = HTTP.get(f"https://www.semas.or.kr/web/board/webBoardList.kmdc?bCd=1&pNm=BOA0101&page={page}",
                    timeout=_TIMEOUT).text
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        m = re.search(r"fncGoDetail\('(\d+)'\);\"[^>]*>(.*?)</a>", tr, re.S)
        if not m:
            continue
        posted = _dates(_text(tr))[0] if _dates(_text(tr)) else ""
        rows.append(_row("소상공인시장진흥공단", "semas_notice", m.group(1), m.group(2),
                         f"https://www.semas.or.kr/web/board/webBoardView.kmdc?bCd=1&b_idx={m.group(1)}&pNm=BOA0101",
                         posted))
    return rows


def _fetch_semas_biz(page: int = 1) -> list:
    """소진공 사업공고(bCd=2001). 게시판 메뉴가 POST 폼으로만 열리고, 목록은 소상공인24(sbiz24.kr)로
    링크되는 카드(<a class="aconbox">)다. 등록일이 따로 없어 신청 시작일을 등록일로 쓴다.
    (소진공 '입찰정보' 메뉴는 "조달청 입찰공고 바로가기" 안내뿐이라 나라장터와 같은 데이터 — 수집하지 않는다.)"""
    html = HTTP.post("https://www.semas.or.kr/web/board/webBoardList.kmdc",
                     data={"bCd": "2001", "pNm": "BOA0101", "page": str(page)}, timeout=_TIMEOUT).text
    rows = []
    for m in re.finditer(r'<a class="aconbox"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
        url, card = m.group(1), m.group(2)
        title = re.search(r'<div class="cut_text1">(.*?)</div>', card, re.S)
        period = re.search(r'<div class="date">(.*?)</div>', card, re.S)
        ds = _dates(period.group(1)) if period else []
        notice_id = url.rstrip("/").rsplit("/", 1)[-1]
        rows.append(_row("소상공인시장진흥공단", "semas_biz", notice_id, title.group(1) if title else "",
                         url, ds[0] if ds else "", ds[-1] if ds else ""))
    return rows


def _fetch_kosmes_notice(page: int = 1) -> list:
    """중진공 공지사항. 화면은 AXGrid가 /sh/nts/notice_list.json(POST, 폼 인코딩)을 불러 그린다.
    activatedTab 01=중진공, 02=유관기관. VALI_DT(유효일)는 목록 헤더상 '유효일(마감기한)'이라 마감으로 쓴다."""
    resp = HTTP.post(
        "https://www.kosmes.or.kr/sh/nts/notice_list.json",
        data={"nowPage": str(page), "pageCount": "10", "rowCount": "30", "param": "proc=List",
              "bKind": "popluar", "activatedTab": "01"},
        headers={"X-Requested-With": "XMLHttpRequest",
                 "Referer": "https://www.kosmes.or.kr/nsh/SH/NTS/SHNTS001M0.do"},
        timeout=_TIMEOUT,
    )
    rows = []
    for x in resp.json().get("ds_infoList", []):
        rows.append(_row("중소벤처기업진흥공단", "kosmes_notice", x.get("SLNO"), x.get("TITL_NM", ""),
                         f"https://www.kosmes.or.kr/nsh/SH/NTS/SHNTS001F0.do?seqNo={x.get('SLNO')}&tabPage=01",
                         x.get("REG_DTM", ""), x.get("VALI_DT", "")))
    return rows


def _kr_proxies():
    """국내 IP가 필요한 사이트(서울신보)용 프록시 설정. config.KR_PROXY_URL이 없으면 None(직접 접속)."""
    return {"http": KR_PROXY_URL, "https": KR_PROXY_URL} if KR_PROXY_URL else None


def _fetch_kosmes_bid(page: int = 1) -> list:
    """중진공 입찰정보(SHNTS005M0). AXGrid가 /sh/nts/notice03.json(POST)을 불러 그린다. 글별 상세페이지가 없고
    첨부파일만 있어 링크는 목록 페이지로 둔다(_no_detail_page — 목록 페이지 메뉴 텍스트를 본문으로 검사하지 않게).
    2026-10-06 확인: 최근 16건 모두 나라장터에 같은 공고가 있었다 — 나라장터에 없는 입찰이 올라올 때를 대비해 수집한다."""
    list_url = "https://www.kosmes.or.kr/nsh/SH/NTS/SHNTS005M0.do"
    resp = HTTP.post(
        "https://www.kosmes.or.kr/sh/nts/notice03.json",
        data={"nowPage": str(page), "pageCount": "10", "rowCount": "20", "param": "proc=List"},
        headers={"X-Requested-With": "XMLHttpRequest", "Referer": list_url},
        timeout=_TIMEOUT,
    )
    rows = []
    for x in resp.json().get("ds_infoList", []):
        reg = x.get("TO_CHAR(REG_DTM,'YYYYMMDD')") or ""
        posted = f"{reg[:4]}-{reg[4:6]}-{reg[6:8]}" if len(reg) == 8 else (x.get("BIDPRICE_STIME") or "")[:10]
        row = _row("중소벤처기업진흥공단", "kosmes_bid", x.get("BUBD_BID_PUAN_SLNO"), x.get("TITL_NM", ""),
                   list_url, posted, (x.get("BIDPRICE_TTIME") or "")[:10])
        row["_no_detail_page"] = True
        rows.append(row)
    return rows


def _fetch_seoulshinbo(page: int = 1) -> list:
    """서울신용보증재단 사업공고(mng_cd=STRY0006). 상권지원 사업공고와 재무팀 자체 입찰공고
    ("[제2026재무팀-66호]입찰공고(...용역)")가 한 게시판에 같이 올라온다. 서버 인증서 체인이 불완전해
    인증서 검증을 끈다(공개 게시판 읽기만 하므로 위험 낮음). 상세는 bbs.goView(페이지, 글번호) ->
    /wbase/contents/bbs/view/{글번호}.do (pageIndex 없이 열면 HTTP 500)."""
    page_html = HTTP.get(f"https://www.seoulshinbo.co.kr/wbase/contents/bbs/list.do?mng_cd=STRY0006&pageIndex={page}",
                    timeout=_TIMEOUT, verify=False, proxies=_kr_proxies()).text
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", page_html, re.S):
        m = re.search(r"bbs\.goView\('\d+',\s*'(\d+)'\)\"><span[^>]*>(.*?)</span>", tr, re.S)
        if not m:
            continue
        posted = _dates(_text(tr))[0] if _dates(_text(tr)) else ""
        rows.append(_row("서울신용보증재단", "seoulshinbo", m.group(1), m.group(2),
                         f"https://www.seoulshinbo.co.kr/wbase/contents/bbs/view/{m.group(1)}.do"
                         "?mng_cd=STRY0006&pageIndex=1", posted))
    return rows


def _fetch_iitp(menu: str, board_seq: str, key: str, page: int = 1) -> list:
    """IITP 게시판(메뉴 S1T12C37=공지사항/board 7). 화면은 Vue 앱이고 목록은 /board-svc/api/bbs/A/list.do
    (JSON POST)로 불러온다. 페이지 meta의 CSRF 토큰을 헤더에 실어야 해서 목록 페이지를 먼저 한 번 연다.
    입찰공고 게시판(S1T12C38/board 8)도 같은 API로 읽히지만 수집하지 않는다 — [자체조달 ...](IITP가 직접
    계약)·[중앙조달 ...](조달청 대행) 모두 공고는 나라장터에 올라간다(2026-10-01 상세페이지로 확인:
    "[자체조달 사전규격공개] 양자클러스터..." 본문에 나라장터 사전규격번호 R26BD00278386)."""
    list_url = f"https://www.iitp.kr/web/lay1/bbs/S1T12C{menu}/A/{board_seq}/list.do"
    page_html = HTTP.get(list_url, timeout=_TIMEOUT).text
    token = re.search(r'name="_csrf" content="([^"]+)"', page_html)
    if not token:
        raise ValueError("CSRF 토큰을 찾지 못함 — 페이지 구조 변경 가능성")
    resp = HTTP.post(
        "https://www.iitp.kr/board-svc/api/bbs/A/list.do",
        json={"cms_menu_seq": menu, "cpage": page, "rows": 20, "keyword": "", "condition": "", "sort": ""},
        headers={"X-CSRF-TOKEN": token.group(1), "Referer": list_url},
        timeout=_TIMEOUT,
    )
    rows = []
    for x in resp.json().get("list") or []:
        seq = x.get("article_seq")
        rows.append(_row("정보통신기획평가원", key, seq, x.get("title", ""),
                         f"https://www.iitp.kr/web/lay1/bbs/S1T12C{menu}/A/{board_seq}/view.do?article_seq={seq}",
                         x.get("reg_dt") or "", (x.get("end_dt") or "")[:10]))
    return rows


def _fetch_kised(pages: int = 3) -> list:
    """창업진흥원 사업공고. 창업진흥원 홈페이지 사업공고 목록은 HTML에 없고(입찰공고는 로그인이 필요한
    자체 전자입찰 시스템 ebid.kised.or.kr), 같은 공고가 K-Startup 포털(창업진흥원 운영)에 올라온다.
    K-Startup 모집중 목록은 전 기관 창업지원사업이 섞여 하루 15건 넘게 올라오므로, 등록 최신순 몇
    페이지를 읽고 기관명이 창업진흥원인 건만 남긴다. 카드 하단 span: [사업명, 기관, 등록일자, 시작일자, 마감일자, 조회]."""
    rows = []
    for page in range(1, pages + 1):
        body = HTTP.get(f"https://www.k-startup.go.kr/web/contents/bizpbanc-ongoing.do?page={page}",
                        timeout=_TIMEOUT).text
        body = body[body.find('id="bizPbancList"'):]
        for sn, title, bottom in re.findall(
            r"go_view\((\d+)\);'>\s*<div class=\"tit_wrap\">\s*<p class=\"tit\">(.*?)</p>.*?<div class=\"bottom\">(.*?)</div>",
            body, re.S,
        ):
            spans = [_text(x) for x in re.findall(r'<span class="list">(.*?)</span>', bottom, re.S)]
            if len(spans) < 5 or "창업진흥원" not in spans[1]:
                continue
            rows.append(_row("창업진흥원", "kised_biz", sn, title,
                             f"https://www.k-startup.go.kr/web/contents/bizpbanc-ongoing.do?schM=view&pbancSn={sn}",
                             (_dates(spans[2]) or [""])[0], (_dates(spans[4]) or [""])[0]))
    return rows


# 2026-10-06 요청: 연휴가 끼면 며칠치 글이 첫 페이지를 넘길 수 있어 게시판마다 AGENCY_PAGES쪽까지 읽는다.
AGENCY_PAGES = 3


def _paged(fetch_page, pages: int = AGENCY_PAGES):
    """fetch_page(page) -> rows 를 1~pages쪽까지 읽어 합친다. 상단 고정글은 쪽마다 반복되므로 글번호로 중복을 없애고,
    새 글이 하나도 없는 쪽이 나오면(마지막 쪽이거나 사이트가 쪽 번호를 무시함) 거기서 멈춘다.
    첫 쪽 실패는 예외를 그대로 올려 _fetch_one이 재시도·경고하게 하고, 뒤쪽 실패는 그때까지 읽은 것만 쓴다."""
    def fetch():
        rows, seen = [], set()
        for page in range(1, pages + 1):
            try:
                batch = fetch_page(page)
            except Exception as exc:
                if page == 1:
                    raise
                print(f"  [경고] {page}쪽 읽기 실패 — {page - 1}쪽까지만 사용: {exc!r}")
                break
            new = [r for r in batch if r["bidNtceNo"] not in seen]
            if not new:
                break
            seen.update(r["bidNtceNo"] for r in new)
            rows.extend(new)
        return rows
    return fetch


# (표시명, 파서). 표시명은 로그/경고 메시지용.
AGENCY_SOURCES = [
    ("NIPA 사업공고", _paged(lambda p: _fetch_nipa("2-2", "nipa_biz", p))),
    ("NIPA 입찰공고", _paged(lambda p: _fetch_nipa("2-3", "nipa_bid", p))),
    ("NIA 공지사항", _paged(lambda p: _fetch_nia("99835", "nia_notice", p))),
    ("NIA 입찰공고", _paged(lambda p: _fetch_nia("78336", "nia_bid", p))),
    ("IITP 공지사항", _paged(lambda p: _fetch_iitp("37", "7", "iitp_notice", p))),
    ("IITP 입찰공고", _paged(lambda p: _fetch_iitp("38", "8", "iitp_bid", p))),
    ("과학창의재단 사업공고", _paged(_fetch_kosac_biz)),
    ("소진공 공지사항", _paged(_fetch_semas_notice)),
    ("소진공 사업공고", _paged(_fetch_semas_biz)),
    ("중진공 공지사항", _paged(_fetch_kosmes_notice)),
    ("중진공 입찰정보", _paged(_fetch_kosmes_bid)),
    ("서울신보 사업공고", _paged(_fetch_seoulshinbo)),
    ("창업진흥원 사업공고(K-Startup)", lambda: _fetch_kised(AGENCY_PAGES)),
]

# 창업진흥원은 K-Startup 전체 목록 중 해당 기관 건만 고르는 방식이라, 며칠 동안 0건이어도 정상이다.
_EMPTY_OK_SOURCES = {"창업진흥원 사업공고(K-Startup)"}

# 입찰공고 전용 게시판. 제목에 '입찰' 같은 단어가 없어도(중진공: "OO 연구용역") 전부 나라장터에서 같은 공고를 찾아본다.
_BID_BOARD_KEYS = {"nipa_bid", "nia_bid", "iitp_bid", "kosmes_bid"}


def _fetch_one(name, fetcher) -> list:
    """한 게시판 수집. 실패해도 예외를 전파하지 않고 경고만 남긴다 — 기관 사이트 하나가 죽었다고
    나머지 소스(나라장터 등)까지 발송이 막히면 안 된다. 파싱 결과가 0건이면 구조 변경 신호라 경고한다."""
    if _deadline_exceeded():
        COLLECTION_WARNINGS.append(f"{name}: 시간 상한 초과로 건너뜀")
        return []
    for attempt in (1, 2):
        try:
            rows = fetcher()
            break
        except Exception as exc:
            if attempt == 2:
                print(f"[경고] {name} 수집 실패: {exc!r}")
                COLLECTION_WARNINGS.append(f"{name}: 수집 실패 ({exc.__class__.__name__})")
                return []
    if not rows and name not in _EMPTY_OK_SOURCES:
        print(f"[경고] {name}: 목록에서 공고를 하나도 못 읽음 — 사이트 구조 변경 여부 확인 필요")
        COLLECTION_WARNINGS.append(f"{name}: 목록 파싱 0건(구조 변경 확인 필요)")
    return [r for r in rows if r["bidNtceNm"]]


def fetch_all_agency_rows() -> dict:
    """모든 기관 게시판을 동시에 읽어 {표시명: [row, ...]}로 반환한다(날짜 필터 전 원본, discover용)."""
    with ThreadPoolExecutor(max_workers=len(AGENCY_SOURCES)) as pool:
        futures = [(name, pool.submit(_fetch_one, name, fn)) for name, fn in AGENCY_SOURCES]
        return {name: fut.result() for name, fut in futures}


# ---------------------------------------------------------------- 나라장터/기업마당 중복 판정

_BRACKET_RE = re.compile(r"\[[^\]]*\]|\([^)]*공고[^)]*\)|\(재\)|「|」|『|』")
_NON_WORD_RE = re.compile(r"[^0-9A-Za-z가-힣]")


def normalize_title(title: str) -> str:
    """중복 비교용 제목 정규화: [조달청 입찰공고]/(재공고) 같은 머리말과 기호·공백을 걷어낸다."""
    return _NON_WORD_RE.sub("", _BRACKET_RE.sub("", title or "")).upper()


# 2026-10-01 요청: "나라장터에 올라오지 않는 공고들로만 추려서". 공공기관의 입찰·사전규격·수의계약은
# 원칙적으로 나라장터에 올라간다(확인 사례: NIA "[입찰공고]", IITP "[자체조달 사전규격공개]", 서울신보 재무팀
# 입찰공고 모두 나라장터에 같은 공고가 있었음). 그래서 제목이 조달 절차 공고면 나라장터 공고로 보고 뺀다.
# "용역"은 넣지 않는다 — 사업공고 게시판의 "OO 운영 용역 수행기관 모집"처럼 나라장터를 안 거치는 공모도 있다.
_PROCUREMENT_RE = re.compile(r"입찰|사전규격|조달|수의계약|견적|개찰|낙찰|제안요청")


def _is_procurement_notice(item: dict) -> bool:
    return bool(_PROCUREMENT_RE.search(item["bidNtceNm"]))


# 제목 앞뒤의 머리말을 걷어내 나라장터 검색어로 쓴다: "[조달청 입찰공고] OO", "[제2026재무팀-66호]입찰공고(OO)",
# "(재공고) OO", "OO (긴급공고)".
_LEAD_TAG_RE = re.compile(r"^\s*(\[[^\]]*\]|【[^】]*】|\((?:재공고|긴급|긴급공고|수정|정정)[^)]*\))\s*")
_WRAPPED_RE = re.compile(r"^(?:입찰|입찰재|재입찰|전자공개\s*수의계약|수의계약)?\s*(?:재)?공고\s*\((.+)\)\s*$")
_TRAIL_TAG_RE = re.compile(r"\s*\((?:재공고|긴급|긴급공고|수정|정정)[^)]*\)\s*$")
# 나라장터 PPSSrch 조회 기간 상한(약 한 달)을 넘지 않게 잡는다. 기관 게시판은 나라장터보다 며칠 늦거나 이르게 올린다.
_G2B_LOOKUP_BEFORE_DAYS = 20
_G2B_LOOKUP_AFTER_DAYS = 8
_G2B_LOOKUP_OPS = [
    ("getBidPblancListInfoServcPPSSrch", None, "bidNtceNm", "bidNtceNo", "bidNtceNm"),
    ("getBidPblancListInfoThngPPSSrch", None, "bidNtceNm", "bidNtceNo", "bidNtceNm"),
    ("getBidPblancListInfoCnstwkPPSSrch", None, "bidNtceNm", "bidNtceNo", "bidNtceNm"),
    ("getPublicPrcureThngInfoServcPPSSrch", PRE_SPEC_BASE_URL, "prdctClsfcNoNm", "bfSpecRgstNo", "prdctClsfcNoNm"),
]


def _g2b_search_title(title: str) -> str:
    t = _text(title)
    while True:
        stripped = _LEAD_TAG_RE.sub("", t)
        if stripped == t:
            break
        t = stripped
    wrapped = _WRAPPED_RE.match(t)
    if wrapped:
        t = wrapped.group(1)
    return _TRAIL_TAG_RE.sub("", t).strip()


def find_on_g2b(item: dict):
    """기관 게시판 글과 같은 공고를 나라장터(입찰 용역·물품·공사, 사전규격 용역)에서 제목으로 찾는다.
    찾으면 나라장터 공고번호, 못 찾으면 "", 검색 API가 응답하지 않아 판단할 수 없으면 None."""
    query = _g2b_search_title(item["bidNtceNm"])
    if len(query) < 4:
        return ""
    posted = _posted_date(item) or datetime.now().date()
    begin = posted - timedelta(days=_G2B_LOOKUP_BEFORE_DAYS)
    end = min(posted + timedelta(days=_G2B_LOOKUP_AFTER_DAYS), datetime.now().date())
    target = normalize_title(query)
    words = query.split()
    queries = [query[:40]] + ([" ".join(words[:3])] if len(words) > 3 else [])
    answered = False
    for q in queries:
        for op, base, param, no_field, name_field in _G2B_LOOKUP_OPS:
            params = {
                "ServiceKey": SERVICE_KEY, "type": "json", "inqryDiv": "1",
                "inqryBgnDt": begin.strftime("%Y%m%d0000"), "inqryEndDt": end.strftime("%Y%m%d2359"),
                "pageNo": "1", "numOfRows": "50", param: q,
            }
            result = _call_api(op, params, max_retries=2, **({"base_url": base} if base else {}))
            if result is None:
                continue
            answered = True
            for found in result.get("items") or []:
                name = normalize_title(_g2b_search_title(found.get(name_field, "")))
                if name == target or SequenceMatcher(None, target, name).ratio() >= 0.85:
                    return found.get(no_field) or "?"
    return "" if answered else None


# 우리가 사업자(수행·운영기관)로 들어가는 공모라는 신호. 이 신호가 있으면 비입찰 공지 규칙으로 빼지 않고,
# 마감 7일 미만이어도 확인필요로 보낸다(2026-10-06 요청 — 과학창의재단 공모는 접수기간이 1주 남짓인 경우가 많다:
# "KASA 찾아가는 우주항공 교육·문화 사업 운영기관 재공모" 9/4 게시 → 9/10 마감).
_VENDOR_CALL_RE = re.compile(
    r"수행\s*기관|운영\s*기관|위탁\s*기관|공급\s*기관|전문\s*기관|참여\s*기관|주관\s*기관|사업자\s*(?:모집|선정|공모)"
    r"|기획\s*[·ㆍ,]?\s*운영|운영\s*(?:대행|사업|위탁)|용역|위탁"
)
# 입찰·공모가 아닌 기관 공지(2026-10-06 점검에서 잘못 통과한 글: IITP "(재)경산이노베이션아카데미 학장 초빙 공고",
# 중진공 "재창업 특화교육·컨설팅 참가자 모집", NIA "AI 인프라 넥서스 콘퍼런스(AINEX 2026) 개최",
# NIA "2026 데이터+AI 혁신 챌린지 통합 공고 안내").
_NON_BID_RE = re.compile(
    r"초빙|채용|임용|직원\s*모집|비상임|개최|설명회|참가자\s*모집|교육생\s*모집|수강생\s*모집|참가\s*신청"
    r"|(?:참여|수요|입주|참가)\s*기업\s*모집|공모전|챌린지|경진대회|해커톤"
)


def _is_vendor_call(item: dict) -> bool:
    return bool(_VENDOR_CALL_RE.search(item["bidNtceNm"]))


def _is_non_bid_notice(item: dict) -> bool:
    return not _is_vendor_call(item) and bool(_NON_BID_RE.search(item["bidNtceNm"]))


# 상세페이지 본문에서 나라장터 공고라는 걸 확정할 수 있는 흔적: 입찰공고/사전규격 번호(R26BK01742481,
# R26BD00278386 형식), 나라장터 공고 링크 파라미터. 메뉴·푸터의 "조달청 입찰공고 바로가기" 같은 사이트 공통
# 문구(소진공 사례)에 걸리지 않도록 '나라장터'·'조달청' 단어 자체는 보지 않는다 — 확정할 수 있는 번호·링크만 본다.
_G2B_MARKER_RE = re.compile(r"\bR\d{2}B[KD]\d{8}\b|bidPbancNo=|bfSpecRegNo=|bidNtceNo=")


# 상세페이지 본문 뒤에 오는 공통 영역의 시작 표시(2026-10-01 NIPA·과학창의재단·서울신보·소진공 상세페이지 기준).
_BODY_END_RE = re.compile(r"만족도 조사|이 페이지에서 제공하는 정보|목록 복사|이전글|다음글|개인정보처리방침|Copyright")


def fetch_detail_text(item: dict) -> str:
    """상세페이지 본문 텍스트(스크립트·스타일 제거). 실패하면 빈 문자열.
    IITP·중진공·소상공인24는 본문을 자바스크립트로 그려서 여기선 메뉴 텍스트 정도만 나온다.
    상세페이지가 없는 게시판(_no_detail_page, 중진공 입찰정보)은 빈 문자열."""
    if item.get("_no_detail_page"):
        return ""
    proxies = _kr_proxies() if "seoulshinbo.co.kr" in item["bidNtceDtlUrl"] else None
    try:
        page = HTTP.get(item["bidNtceDtlUrl"], timeout=_TIMEOUT, verify=False, proxies=proxies).text
    except requests.exceptions.RequestException:
        return ""
    page = re.sub(r"<(script|style|noscript|head)[^>]*>.*?</\1>", " ", page, flags=re.S | re.I)
    text = _text(page)
    # 본문만 남긴다: 앞쪽 사이트 메뉴는 공고 제목이 처음 나오는 자리(메뉴엔 공고 제목이 없으니 곧 본문 시작)부터,
    # 뒤쪽은 만족도조사·목록·푸터가 시작되는 자리에서 자른다. rag_screen이 본문 전체에 제외 키워드를 검사하므로
    # 메뉴·푸터 단어("홍보센터", "홍보영상" 등)가 섞이면 엉뚱하게 제외된다. 제목을 못 찾으면 전체를 쓴다.
    head = _text(item["bidNtceNm"])[:15]
    pos = text.find(head) if head else -1
    if pos >= 0:
        text = text[pos:]
    ends = [m.start() for m in _BODY_END_RE.finditer(text) if m.start() > 100]
    return text[:ends[0]] if ends else text


# ---------------------------------------------------------------- 결과/행정 공지

# 기관 게시판엔 공고뿐 아니라 "제안평가결과", "선정결과 안내" 같은 결과 공지도 같이 올라온다 —
# 참여할 수 있는 공고가 아니라서 키워드가 걸려도(예: "...교육 운영 용역" 제안평가결과) 뺀다.
_RESULT_NOTICE_RE = re.compile(r"(평가|선정|개찰|심사|낙찰|협상)\s*결과|결과\s*(공고|안내|발표|공지)")


def _is_result_notice(item: dict) -> bool:
    return bool(_RESULT_NOTICE_RE.search(item["bidNtceNm"]))


def _posted_date(item: dict):
    try:
        return datetime.strptime(item.get("bidNtceDt", "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def get_daily_relevant_agency_notices(
    start_date=None, end_date=None, alio_orgs=None, known_titles=None, rag_postings=None, rag_raw_titles=None
):
    """기준 기간(get_lookback_range)에 기관 게시판에 올라온 공고 중 나라장터·기업마당에 없는 공고만 골라,
    우리팀 관심 조건(키워드∩기관, 제외키워드) → 상세페이지 나라장터 흔적 검사 → 본문 규칙·과거 사례(RAG) 검토 →
    마감임박 게이트를 통과한 건만 반환한다.
    known_titles: 이미 다른 소스(DB 누적 관심 공고 + 오늘 수집분)로 확인한 공고 제목들.
    rag_postings / rag_raw_titles: db.get_postings_for_rag() / db.get_raw_titles() — 나라장터·사전규격·
    기업마당 과거 처리 결과. raw_titles(최근 60일 원본 공고명)는 중복 판정에도 쓴다."""
    if start_date is None or end_date is None:
        start_date, end_date = get_lookback_range()
    rag_raw_titles = rag_raw_titles or []
    known = {normalize_title(t) for t in [*(known_titles or []), *RAW_SOURCE_TITLES] if t}
    known |= {norm for norm, _title, _source in rag_raw_titles}

    print(f"[요청] 기관 게시판 {len(AGENCY_SOURCES)}곳 / {start_date.isoformat()} ~ {end_date.isoformat()} 등록분")
    in_range, seen = [], set()
    skipped = {"결과공지": 0, "채용·행사 등 비입찰 공지": 0, "나라장터·기업마당 중복": 0, "나라장터 조달공고": 0}
    for name, rows in fetch_all_agency_rows().items():
        picked = [r for r in rows if (d := _posted_date(r)) and start_date <= d <= end_date]
        print(f"  - {name}: 목록 {len(rows)}건 중 기간 내 {len(picked)}건")
        for item in picked:
            if _is_result_notice(item):
                skipped["결과공지"] += 1
                continue
            if _is_non_bid_notice(item):
                if not has_strong_fit(item["bidNtceNm"]):
                    skipped["채용·행사 등 비입찰 공지"] += 1
                    continue
                item["_non_bid_doubt"] = True
            norm = normalize_title(item["bidNtceNm"])
            if norm in known or norm in seen:
                skipped["나라장터·기업마당 중복"] += 1
                continue
            seen.add(norm)
            in_range.append(item)

    # 조달 절차 제목(입찰·사전규격 등)은 나라장터에서 실제로 찾아질 때만 뺀다(모듈 docstring 2).
    procurement = [
        item for item in in_range if _is_procurement_notice(item) or item.get("_agency_board") in _BID_BOARD_KEYS
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        lookups = list(pool.map(find_on_g2b, procurement))
    for item, found in zip(procurement, lookups):
        if found:
            print(f"  [나라장터 공고] {item['bidNtceNm'][:50]} — 나라장터 {found} -> 제외")
            skipped["나라장터 조달공고"] += 1
            in_range.remove(item)
        elif found is None:
            item["_g2b_lookup_failed"] = True
        else:
            print(f"  [나라장터에 없음] {item['bidNtceNm'][:50]} — 일반 필터로 판단")

    relevant = [item for item in in_range if is_relevant_bid(item, alio_orgs)]
    skipped_text = "·".join(f"{k} {v}건" for k, v in skipped.items())
    print(f"[필터링] 기관 게시판: 기간 내 신규 {len(in_range)}건({skipped_text} 제외) → 키워드+기관 매칭 {len(relevant)}건")

    # 키워드 필터를 통과한 소수 건만 상세페이지를 열어 나라장터 공고번호/링크가 있는지 확인하고,
    # 본문은 2차 검토(rag_screen)에 재사용한다.
    with ThreadPoolExecutor(max_workers=8) as pool:
        texts = list(pool.map(fetch_detail_text, relevant))
    detail_texts, not_on_g2b = {}, []
    for item, text in zip(relevant, texts):
        marker = _G2B_MARKER_RE.search(text)
        if marker:
            print(f"  [나라장터 공고] {item['bidNtceNm'][:50]} — 본문에 '{marker.group(0)}' -> 제외")
            continue
        detail_texts[item["bidNtceNo"]] = text
        not_on_g2b.append(item)
    relevant = not_on_g2b

    attach_confidence(relevant, alio_orgs)
    for item in relevant:
        if item.pop("_non_bid_doubt", False):
            downgrade(item, "제목이 채용·행사·참가자 모집 공지로 보이지만 교육·콘텐츠 신호가 강해 남김 — 우리가 수행할 공모인지 확인 필요")
        if item.pop("_g2b_lookup_failed", False):
            downgrade(item, "나라장터 검색 API가 응답하지 않아 나라장터 중복 여부를 확인하지 못함 — 직접 확인 필요")
    relevant = screen_items(relevant, detail_texts, rag_postings or [], rag_raw_titles)
    # 2026-10-06 요청: 수행기관·운영기관을 뽑는 기관 자체 공모(나라장터에 안 올라옴)는 무조건 챙겨야 하는 유형이라
    # (실사례: 과학창의재단 "모두의 AI 챌린지 프로그램 기획·운영 사업" — 실제 제안 참여) 우선검토로 카드 앞쪽에 둔다.
    for item in relevant:
        if _is_vendor_call(item):
            downgrade(item, "기관 홈페이지에만 올라온 수행기관·운영기관 공모(나라장터 미게시) — 우선 확인 필요", priority=True)

    today = datetime.now().date()
    before_gate = len(relevant)
    gated = []
    for item in relevant:
        if passes_deadline_gate(item, today):
            gated.append(item)
            continue
        d = deadline_date(item)
        # 2026-10-06 요청: 우리가 수행기관으로 들어가는 기관 공모는 접수기간이 짧아도 놓치면 안 된다.
        if _is_vendor_call(item) and d is not None and d >= today:
            downgrade(item, f"마감까지 {(d - today).days}일 — 7일 미만이지만 기관 공모라 놓치지 않게 포함")
            gated.append(item)
    relevant = gated
    print(f"[마감임박필터링] {before_gate}건 → {len(relevant)}건 (마감 {DEADLINE_GATE_DAYS}일 미만 비확실후보 제외, 기관 공모는 유지)")
    relevant.sort(key=lambda item: item.get("bidNtceDt", ""))
    return relevant
