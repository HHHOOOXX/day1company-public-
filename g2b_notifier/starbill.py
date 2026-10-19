"""스타빌(starbill.co.kr) 전자입찰 공고 수집 — 명지대·명지전문대·고려대처럼 나라장터가 아니라 스타빌로 자체 입찰을 내는 대학 공고.

2026-10-19 요청: 명지대 "AI를 활용한 창업 아카데미 용역"이 스타빌에만 올라와 메시지에서 빠졌다. 스타빌 첫 화면의 최근 공고 표
(항목·구분·발주처·공고명·접수마감일)를 읽어 용역 입찰만 모으고, 공고 상세(공고문 원문)에서 참가자격을 나라장터 첨부와 같은 규칙으로
확인한다. 게시일이 표에 없어 기간으로 거르지 않고, 이미 보낸 공고는 DB(notified)로 거른다.

스타빌 서버는 중간 인증서를 보내지 않아 그대로는 인증서 검증에 실패한다. 검증을 끄지 않고, 서버 인증서의 AIA(CA Issuers) 주소에서
중간 인증서를 받아 certifi 신뢰 목록에 더한 묶음으로 검증한다(_ca_bundle).
"""

import os
import re
import ssl
import tempfile
from datetime import datetime
from html import unescape

import certifi
import requests

from .classify import (
    attach_confidence,
    downgrade,
    downgrade_for_doc_exclude,
    is_relevant_bid,
    keep_for_partner_bid,
)
from .doc_extract import certificate_evidence, matches_exclude_keyword, region_restriction_evidence
from .g2b import COLLECTION_WARNINGS

HOST = "www.starbill.co.kr"
BASE_URL = f"https://{HOST}"
TIMEOUT = 20
_HEADERS = {"User-Agent": "Mozilla/5.0"}

# 첫 화면 공고 표의 한 줄: 항목(용역/물품…), 구분(입찰/견적…), 발주처, 링크, 공고명, 접수마감일("10-28 15:00").
_ROW_RE = re.compile(
    r"<tr>\s*<td>\s*(?P<kind>.*?)\s*</td>\s*<td>\s*(?P<type>.*?)\s*</td>\s*<td class=\"left\">\s*(?P<org>.*?)\s*</td>"
    r"\s*<td class=\"left\">\s*<a href=\"(?P<href>/ebidEmail\.do\?[^\"]+)\">\s*(?P<title>.*?)</a>\s*</td>"
    r"\s*<td class=\"date\">\s*(?P<due>.*?)\s*</td>",
    re.S,
)
_TAG_RE = re.compile(r"<[^>]+>")
_BODY_RE = re.compile(r'<div class="ebidConFull_body">(.*?)</div>', re.S)

_bundle_path = None


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", unescape(_TAG_RE.sub(" ", html or ""))).strip()


def _ca_bundle() -> str:
    """certifi 신뢰 목록 + 스타빌 중간 인증서 파일 경로. 서버 인증서는 내용(AIA 주소)만 읽고, 접속 검증은 이 묶음으로 한다."""
    global _bundle_path
    if _bundle_path and os.path.exists(_bundle_path):
        return _bundle_path
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding
    from cryptography.x509.oid import AuthorityInformationAccessOID, ExtensionOID

    leaf = x509.load_pem_x509_certificate(ssl.get_server_certificate((HOST, 443), timeout=TIMEOUT).encode())
    aia = leaf.extensions.get_extension_for_oid(ExtensionOID.AUTHORITY_INFORMATION_ACCESS).value
    issuer_url = next(d.access_location.value for d in aia if d.access_method == AuthorityInformationAccessOID.CA_ISSUERS)
    raw = requests.get(issuer_url, timeout=TIMEOUT).content
    try:
        inter = x509.load_der_x509_certificate(raw)
    except ValueError:
        inter = x509.load_pem_x509_certificate(raw)
    with open(certifi.where(), "rb") as f:
        roots = f.read()
    fd, path = tempfile.mkstemp(prefix="starbill-ca-", suffix=".pem")
    with os.fdopen(fd, "wb") as f:
        f.write(roots + b"\n" + inter.public_bytes(Encoding.PEM))
    _bundle_path = path
    return path


def _get(path: str) -> str:
    resp = requests.get(BASE_URL + path, headers=_HEADERS, timeout=TIMEOUT, verify=_ca_bundle())
    resp.raise_for_status()
    return resp.content.decode("utf-8", errors="replace")


def _deadline(due: str, today) -> str:
    """"10-28 15:00" → "2026-10-28 15:00:00". 연도가 없어서, 반년 넘게 지난 달이면 내년으로 본다(12월에 1월 마감 공고)."""
    m = re.match(r"(\d{1,2})-(\d{1,2})\s*(\d{1,2}:\d{2})?", due)
    if not m:
        return ""
    month, day = int(m.group(1)), int(m.group(2))
    year = today.year + (1 if month < today.month - 6 else 0)
    return f"{year:04d}-{month:02d}-{day:02d} {m.group(3) or '00:00'}:00"


def parse_list(html: str, today) -> list:
    """첫 화면 공고 표 → 공고 dict 목록(용역 입찰만). 나라장터 공고와 같은 키를 써서 분류·카드 코드를 그대로 쓴다."""
    items = []
    for m in _ROW_RE.finditer(html):
        kind, bid_type = _text(m["kind"]), _text(m["type"])
        if kind != "용역" or "입찰" not in bid_type:
            continue
        href = unescape(m["href"])
        q = dict(re.findall(r"(\w+)=([^&]+)", href.split("?", 1)[1]))
        items.append({
            "bidNtceNo": f"SB-{q.get('buyerCode', '')}-{q.get('orgSeq', '')}",
            "bidNtceOrd": "0",
            "bidNtceNm": _text(m["title"]),
            "ntceInsttNm": _text(m["org"]),
            "bidNtceDtlUrl": BASE_URL + href,
            "bidClseDt": _deadline(_text(m["due"]), today),
            "_detail_path": href,
        })
    return items


def _check_detail(item: dict) -> bool:
    """공고문 원문의 참가자격을 나라장터 첨부와 같은 규칙으로 본다. 제외면 False."""
    no = item["bidNtceNo"]
    try:
        html = _get(item.pop("_detail_path"))
    except Exception as exc:
        downgrade(item, "스타빌 공고문을 열지 못함 — 참가자격(중소기업·지역 제한)을 직접 확인 필요")
        print(f"  [스타빌] {no} 공고문 열기 실패: {exc!r}")
        return True
    body = _BODY_RE.search(html)
    text = _text(body.group(1) if body else html)
    regions, note = region_restriction_evidence(text)
    if regions:
        print(f"  [지역제외] {no} 스타빌 공고문 참가자격에 {regions} 소재 조건(서울 없음) -> 배제 | 근거: {note[:80]}")
        return False
    cert, note = certificate_evidence(text)
    if cert == "restricted" and not keep_for_partner_bid(item, "중소기업·소상공인 제한", (note or "")[:60]):
        print(f"  [제외] {no} 스타빌 공고 중소기업·소상공인 한정 -> 배제 | 근거: {(note or '')[:80]}")
        return False
    if cert == "mention":
        downgrade(item, f"공고문에 소상공인·중소기업 확인서가 언급됨(참가자격 제한인지 불확실): \"{(note or '')[:60]}\" — 직접 확인 필요")
    hits = matches_exclude_keyword(text)
    if hits:
        downgrade_for_doc_exclude(item, hits, "스타빌 공고문")
    return True


def get_relevant_starbill(alio_orgs=None, today=None) -> list:
    """스타빌 첫 화면의 용역 입찰 중 관심 공고. 접속 실패면 빈 목록(경고만 남긴다)."""
    today = today or datetime.now().date()
    try:
        items = parse_list(_get("/"), today)
    except Exception as exc:
        print(f"[스타빌] 공고 목록을 받지 못했어요: {exc!r}")
        COLLECTION_WARNINGS.append(f"스타빌: 목록 수집 실패 ({exc.__class__.__name__})")
        return []
    print(f"[요청] 스타빌 첫 화면 용역 입찰 {len(items)}건")
    items = [i for i in items if i["bidClseDt"] and i["bidClseDt"][:10] >= today.isoformat()]
    relevant = [i for i in items if is_relevant_bid(i, alio_orgs)]
    attach_confidence(relevant, alio_orgs)
    kept = [i for i in relevant if _check_detail(i)]
    print(f"[필터링] 스타빌: 마감 전 {len(items)}건 → 관심 {len(relevant)}건 → 참가자격 확인 후 {len(kept)}건")
    return kept
