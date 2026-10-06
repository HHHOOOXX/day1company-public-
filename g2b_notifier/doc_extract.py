"""사전규격/기업마당 첨부파일(hwp/hwpx/pdf) 본문 텍스트 추출 + 업종코드/지역제한 탐지.

사전규격 API는 업종제한 여부/API가 아예 없어서(g2b.py 상단 주석 참고), 첨부된 제안요청서/입찰공고문
원문을 직접 열어 "업종코드: 6527"처럼 명시된 등록업종 조건을 찾아내는 방법으로 대체한다.
실제 발주기관이 올린 .hwp/.hwpx 파일로 검증 완료(각각 '이러닝콘텐츠업 (업종코드: 6527)',
'소프트웨어사업자(컴퓨터관련서비스사업, 업종코드: 1468)' 형태의 문구를 정상적으로 찾아냄).

기업마당(bizinfo) 공고도 마찬가지로 사업개요(bsnsSumryCn)엔 없고 첨부 공고문 PDF 안에만
"신청자격: 강원특별자치도 소재(본사 또는 공장) 기업" 같은 지역 제한이 적혀있는 경우가 있어
PDF 추출도 같이 지원한다(실제 사례로 검증: PBLN_000000000126590).
"""

import re
import struct
import xml.etree.ElementTree as ET
import zipfile
import zlib
from io import BytesIO

import olefile
from pypdf import PdfReader

from .config import DOC_EXCLUDE_KEYWORDS

_HWPTAG_PARA_TEXT = 0x10 + 51  # HWPTAG_BEGIN(0x10) + 51


def extract_hwp_text(raw: bytes) -> str:
    """구버전 .hwp(OLE 컴파운드 파일) 본문 텍스트를 추출한다.
    BodyText/SectionN 스트림을 필요시 zlib 압축 해제하고, 문단 텍스트 레코드(태그 67)만 모아
    UTF-16LE로 디코딩한다. 표/컨트롤 등 다른 레코드가 섞여 일부 노이즈가 남지만, 키워드/코드
    탐지에는 지장이 없다."""
    try:
        ole = olefile.OleFileIO(BytesIO(raw))
    except Exception:
        return ""

    try:
        header = ole.openstream("FileHeader").read()
        compressed = bool(struct.unpack("<I", header[36:40])[0] & 1)
    except Exception:
        compressed = True  # 대부분의 실제 문서는 압축 저장이라 기본값으로 시도

    texts = []
    for entry in ole.listdir():
        if not entry or entry[0] != "BodyText":
            continue
        try:
            section = ole.openstream(entry).read()
        except Exception:
            continue
        if compressed:
            try:
                section = zlib.decompress(section, -15)
            except zlib.error:
                continue

        pos, n = 0, len(section)
        while pos + 4 <= n:
            header_val = struct.unpack("<I", section[pos : pos + 4])[0]
            pos += 4
            tag_id = header_val & 0x3FF
            size = (header_val >> 20) & 0xFFF
            if size == 0xFFF:
                if pos + 4 > n:
                    break
                size = struct.unpack("<I", section[pos : pos + 4])[0]
                pos += 4
            chunk = section[pos : pos + size]
            pos += size
            if tag_id == _HWPTAG_PARA_TEXT:
                try:
                    t = chunk.decode("utf-16le", errors="ignore")
                except Exception:
                    continue
                texts.append("".join(c for c in t if c >= " " or c in "\n\r\t"))

    return "\n".join(texts)


def extract_hwpx_text(raw: bytes) -> str:
    """신버전 .hwpx(ZIP+XML) 본문 텍스트를 추출한다. Contents/sectionN.xml의 <hp:t> 텍스트런을 모은다."""
    try:
        zf = zipfile.ZipFile(BytesIO(raw))
    except Exception:
        return ""

    section_names = sorted(n for n in zf.namelist() if re.match(r"Contents/section\d+\.xml$", n))
    texts = []
    for name in section_names:
        try:
            root = ET.fromstring(zf.read(name))
        except Exception:
            continue
        for el in root.iter():
            if (el.tag.endswith("}t") or el.tag == "t") and el.text:
                texts.append(el.text)

    return " ".join(texts)


def extract_pdf_text(raw: bytes) -> str:
    """PDF 본문 텍스트를 추출한다(pypdf, 페이지별 텍스트를 이어붙임)."""
    try:
        reader = PdfReader(BytesIO(raw))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return ""


def extract_document_text(raw: bytes, filename: str) -> str:
    """파일 확장자 기준으로 적절한 추출기를 골라 본문 텍스트를 반환한다.
    지원하지 않는 형식(doc/zip 등)이거나 파싱 실패 시 빈 문자열을 반환한다(호출부는 이를
    '신호 없음'으로 취급하고 조용히 다음 판정으로 넘어간다)."""
    name = (filename or "").lower()
    if name.endswith(".hwpx"):
        return extract_hwpx_text(raw)
    if name.endswith(".hwp"):
        return extract_hwp_text(raw)
    if name.endswith(".pdf"):
        return extract_pdf_text(raw)
    return ""


_PAREN_CODE_RE = re.compile(r"\((\d{3,4})\)")
_LABELED_CODE_RE = re.compile(r"업종코드\s*[:：]?\s*(\d{3,4})")

# "소상공인확인서"/"중소기업확인서"뿐 아니라 "중·소기업·소상공인 확인서"처럼 가운뎃점/공백이 섞인
# 변형, "중소기업자 확인서"처럼 "자"가 끼어드는 변형(2026-09-22 실사례로 확인: 입찰공고
# R26BK01729268 — 부산동성초등학교, "「중소기업기본법」제2조에 따른 중소기업자로서... 중소기업자
# 확인서를 소지한 자이여야 합니다" — 옛 정규식은 "중소기업" 바로 뒤에 "확인서"가 와야만 매칭돼서
# 이 건을 완전히 놓쳤음)도 있어, "자" 유무와 가운뎃점·공백을 모두 허용하는 정규식으로 잡는다.
# 소상공인/중소기업 확인서를 참가자격으로 요구하는 공고는 데이원컴퍼니(중견기업 — 소상공인/중소기업
# 카테고리에 속하지 않음)가 발급받을 수 없는 자격이라 참가 불가로 판정한다.
_INELIGIBLE_CERTIFICATE_RE = re.compile(r"(?:소상공인|중소기업)(?:자)?[·ㆍ\s]*확인서")

# 2026-10-06: 확인서 단어가 나오기만 하면 제외하던 방식은 참가자격이 아닌 문맥에도 걸렸다(실사례: R26BK01751973
# 우즈베키스탄 행정아카데미 PMC — "가점 증빙서류 등 기타 중소기업 확인서"). 문맥을 셋으로 나눈다:
#   - 바로 앞에 가점·우대·해당 시 같은 말이 있으면 참가자격이 아니므로 무시한다.
#   - 바로 뒤에 "~를 소지한 자"가 오면 참가자격 제한으로 확정한다(실사례: "소기업·소상공인 확인서를 소지한 자",
#     "소기업·소상공인확인서(입찰마감일 전일까지 발급된 것으로 유효기간 내에 있어야 함)를 소지한 자").
#   - 그 밖에(제출서류 목록에 "중·소기업·소상공인 확인서 1부"만 있는 경우 등)는 제한인지 문서만으로 확정할 수
#     없어 확인필요로 보낸다(실사례: R26BK01753055 경제금융교육 통합 홈페이지 구축).
_CERT_BONUS_CONTEXT_RE = re.compile(r"가점|가산|우대|감면|면제|해당\s*시|해당자|해당\s*업체|해당하는\s*경우|해당되는\s*경우")
_CERT_REQUIRED_CONTEXT_RE = re.compile(r"소지|보유한\s*자|발급받은\s*자")
_CERT_BEFORE_WINDOW = 40
_CERT_AFTER_WINDOW = 80


def certificate_requirement(text: str):
    """본문 텍스트의 소상공인/중소기업 확인서 언급을 문맥으로 판정한다.
    "restricted": 확인서 소지를 참가자격으로 요구함(데이원컴퍼니는 중견기업이라 발급 불가 → 참가 불가).
    "mention": 확인서를 언급하지만 참가자격 제한인지 확정할 수 없음(제출서류 목록 등) → 확인필요.
    None: 언급이 없거나 가점·해당 시 제출 같은 참가자격과 무관한 문맥뿐."""
    text = text or ""
    found = None
    for m in _INELIGIBLE_CERTIFICATE_RE.finditer(text):
        if _CERT_BONUS_CONTEXT_RE.search(text[max(0, m.start() - _CERT_BEFORE_WINDOW):m.start()]):
            continue
        if _CERT_REQUIRED_CONTEXT_RE.search(text[m.end():m.end() + _CERT_AFTER_WINDOW]):
            return "restricted"
        found = "mention"
    return found


def requires_ineligible_certificate(text: str) -> bool:
    """본문 텍스트에 우리가 발급받을 수 없는 확인서(소상공인확인서/중소기업확인서류)가
    참가자격으로 명시돼 있으면 True."""
    return certificate_requirement(text) == "restricted"


# 2026-09-22 피드백: 나라장터 API의 title 필드(사전규격 prdctClsfcNoNm은 품명 분류값, 입찰공고
# bidNtceNm도 종종 영어 고유명사가 섞여 실제 내용을 가늠하기 어려운 경우가 있음)만으로는 진짜 사업
# 내용을 알 수 없는 공고가 있다(실사례: R26BD00276858 — 고려대학교 ANCHOR사업단 공고. API 제목은
# 일반 분류값이었지만 실제로는 "KU Global Tech Career Fair 운영 용역"으로, 행사 기획·부스 설치·
# 홍보물 제작 등을 포함한 채용박람회 운영 대행 용역이었음). 그래서 첨부 문서 본문 전체를 검사한다.
# 2026-10-06: 본문에는 제목용 EXCLUDE_KEYWORDS 대신 구체적인 표현만 모은 DOC_EXCLUDE_KEYWORDS를 쓰고,
# 걸린 공고도 제외하지 않고 확인필요로 낮춘다(호출부).
def matches_exclude_keyword(text: str) -> list:
    """텍스트에 DOC_EXCLUDE_KEYWORDS 중 하나라도 있으면 매칭된 키워드 목록을 반환한다(없으면 빈 리스트)."""
    upper = (text or "").upper()
    return [kw for kw in DOC_EXCLUDE_KEYWORDS if kw.upper() in upper]


def find_industry_codes(text: str, window: int = 400) -> set:
    """본문 텍스트에서 '업종코드' 문구 근처의 3~4자리 숫자를 후보 업종코드로 뽑는다.
    실제 문서는 두 가지 패턴이 섞여 나온다:
      1) "(업종코드: 6527)" — 라벨과 코드가 같은 괄호 안, 콜론으로 구분(_LABELED_CODE_RE로 직접 매칭).
      2) "...소프트웨어사업자(디지털콘텐츠개발서비스사업)(1469)를 모두 등록한 자" — 라벨 붙은 코드
         바로 뒤에 라벨 없이 괄호로만 표시된 코드가 이어 나옴(윈도우 내 모든 괄호 숫자를 추가로 수집).
    호출부에서 COMPANY_INDUSTRY_CODES와 교집합을 내 실제 업종코드만 걸러낸다."""
    if not text:
        return set()
    codes = set()
    for m in re.finditer("업종코드", text):
        segment = text[m.start() : m.start() + window]
        codes.update(_LABELED_CODE_RE.findall(segment))
        codes.update(_PAREN_CODE_RE.findall(segment))
    return codes
