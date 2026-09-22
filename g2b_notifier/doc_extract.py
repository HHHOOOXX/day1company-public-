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
# 변형도 있어(실사례: 사전규격 R26BD00276775 — 한동대학교 산학협력단 공고 과업지시서), 가운뎃점·공백을
# 허용하는 정규식으로 잡는다. 소상공인/중소기업 확인서를 참가자격으로 요구하는 공고는 데이원컴퍼니가
# 발급받을 수 없는 자격이라 참가 불가로 판정한다.
_INELIGIBLE_CERTIFICATE_RE = re.compile(r"(?:소상공인|중소기업)[·ㆍ\s]*확인서")


def requires_ineligible_certificate(text: str) -> bool:
    """본문 텍스트에 우리가 발급받을 수 없는 확인서(소상공인확인서/중소기업확인서류)가
    참가자격/제출서류로 명시돼 있으면 True."""
    return bool(_INELIGIBLE_CERTIFICATE_RE.search(text or ""))


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
