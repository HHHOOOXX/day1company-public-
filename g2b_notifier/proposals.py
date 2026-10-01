"""제안 검토 이력 자동 수집 — Google Drive "[제안 및 검토]" 폴더 → DB(proposals).

2026-10-01 요청: "매일 발송되는 메시지에서 실제 제안검토가 이루어져서 제안을 넣었던 공고들을 db에 기록해서
필터링 고도화에 참고자료로 쓰고 싶은데, 일일이 입력하지 않고 자동으로". 팀은 제안 검토를 시작하면
Google Drive "[제안 및 검토]" 폴더 아래에 사업별 폴더를 만든다. 매일 실행 때 이 폴더들을 읽어 DB의 공고와
짝지어 기록한다 — 팀이 추가로 입력할 것은 없다.

짝짓기(키워드 기반): 폴더 이름은 팀원이 자유롭게 짓는다("26_09_성균관대_국방AX전문인력양성", "26_08_수원대 ai
교육과정 운영" 등 — 공식 사업명과 다르다). 그래서 문장 전체 유사도가 아니라 '단서 키워드가 공고에 들어 있는가'로 본다.
  1) 단서 모으기: 폴더 이름 + 폴더 안 파일 이름. 팀이 공고문·제안요청서를 내려받아 넣어 두는 경우가 많고, 그 파일
     이름에 공식 사업명이 남는다(실사례: 성균관대 폴더의 "1.입찰공고문(2026 국방 AX 전문인력양성 보수교육 운영
     용역).pdf"). 공고문/RFP류 하위 폴더도 한 단계 더 본다. 견적서·제안서처럼 우리 쪽 문서 이름은 단서에서 뺀다.
  2) 파일 이름에 나라장터 공고번호(R26BK…/R26BD…)가 있으면 그 공고로 바로 확정한다.
  3) 아니면 단서 키워드마다 공고의 "발주기관 + 공고명"(공백·기호 제거)에 들어 있는지 본다. 약칭은 포함 관계로
     잡히고("수원대" ⊂ "수원대학교"), 표기 차이는 동의어로 잡는다("이러닝"↔"E-LEARNING"). 긴 키워드가 통째로
     안 맞으면 한글/영문 경계로 쪼개 부분 점수를 준다("국방AX전문인력양성" → 국방·AX·전문인력양성).
     점수 = 찾은 키워드 가중치 합 / 전체 키워드 가중치 합(0~1). 가중치는 IDF — 많은 공고에 들어 있는 흔한
     키워드("교육", "AI")일수록 낮다. 공고 제목이 길어도 점수가 깎이지 않는다(폴더 쪽 키워드 기준 비율이라서).
비교 대상은 폴더 생성일 기준 MATCH_WINDOW_DAYS 전 ~ 며칠 뒤에 올라온 공고:
  - DB postings(필터를 통과해 수집·발송된 공고) → "발송했던 공고에 제안 검토"
  - DB raw_titles(필터에서 빠진 원본 공고, 60일 보관) → "필터가 놓친 공고" — 필터 규칙 점검 대상
기록 상태(match_status):
  matched      : 발송(수집)했던 공고와 확실히 짝지어짐
  missed       : 필터에서 빠졌던 원본 공고와 확실히 짝지어짐 — 필터가 놓친 공고
  needs_review : 비슷한 공고는 있지만 확신이 낮음 — 리포트에서 사람이 확인
  not_collected: 수집 기간 안인데 짝지을 공고가 없음 — 우리 수집 범위 밖(견적·수의계약, 다른 조달시스템 등)
  before_db    : DB 수집 시작 전에 만든 폴더라 짝지을 공고 자체가 없음
matched/missed/before_db는 확정으로 보고 다시 계산하지 않는다(raw_titles 60일 보관이 끝나도 짝이 유지되게).

필터링에 쓰는 곳: 짝지어진 공고의 발주기관을 classify의 '과거 제안 기관' 목록에 더한다(config.PROPOSAL_HISTORY를
손으로 고치지 않아도 자동 갱신). 필터가 놓친 공고는 수집 경고로 남겨 대시보드에 보인다.

인증: GitHub Actions에서는 Google 서비스 계정 키(JSON)를 시크릿 GOOGLE_SERVICE_ACCOUNT_JSON으로 넣고,
"[제안 및 검토]" 폴더를 그 서비스 계정 이메일에 '뷰어'로 공유한다. 로컬에서는 GOOGLE_SERVICE_ACCOUNT_FILE에
키 파일 경로를 넣어도 된다. 둘 다 없으면 연동만 건너뛰고 나머지는 정상 동작한다.
"""

import json
import math
import os
import re
from datetime import date, datetime, timedelta

import requests

PROPOSAL_FOLDER_ID = os.getenv("PROPOSAL_FOLDER_ID", "1khhx5B-BheOOxAf88O2rspRhvrKiD6ig")  # [제안 및 검토]
_DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
_FOLDER_MIME = "application/vnd.google-apps.folder"

# 폴더 생성일 기준 이 기간 안에 올라온 공고와만 짝짓는다(공고를 보고 며칠~몇 주 안에 검토 폴더를 만든다).
MATCH_WINDOW_DAYS = 45
MATCH_AFTER_DAYS = 3
# 키워드 적중률(0~1) 기준. 적중률만으로 정하지 않고 '무엇이 맞았는지'도 본다:
#   확정(matched/missed): 적중률 >= MATCH_THRESHOLD, 기관명이 있으면 기관명 적중, 사업 내용 키워드(기관명·흔한 약어
#                        제외) 1개 이상 적중 — 기관명이 없는 폴더는 사업 내용 키워드 2개 이상.
#   확인 필요(needs_review): 적중률 >= REVIEW_THRESHOLD이고 사업 내용 키워드 1개 이상 적중(기관명만 맞은 건 제외).
# 2026-10-01 검증(팀 명명 습관대로 지은 예시 폴더 15개 — 같은 사업 8 / 다른 사업 3 / 실제 최근 폴더 4)에서 같은 사업
# 8개는 전부 확정, 나머지 7개는 하나도 확정되지 않았다.
MATCH_THRESHOLD = 0.60
REVIEW_THRESHOLD = 0.35

# 폴더 이름 끝에 붙는 결과 표시(예: "26_07 한밭대 앵커사업_패찰").
_OUTCOME_WORDS = {"패찰": "패찰", "탈락": "패찰", "미선정": "패찰", "수주": "수주", "낙찰": "수주", "선정": "수주"}
_FOLDER_RE = re.compile(r"^\s*(\d{2})[_.\s-]*(\d{1,2})[_.\s-]*(.*)$")

# 단서로 쓰지 않는 말: 우리 쪽 문서·회사명, 절차 용어, 거의 모든 공고에 나오는 말.
_STOPWORDS = {
    "데이원컴퍼니", "데이원", "패스트캠퍼스", "견적서", "비교견적서", "비교견적", "비교견적용", "오프라인교육견적서",
    "제안서", "제안", "원본", "참고자료", "자료", "관련", "모음", "최종", "수정", "외부공유용",
    "공고문", "입찰공고문", "공고", "입찰", "제안요청서", "과업지시서", "과업내용서", "RFP", "사업", "용역",
    "운영", "및", "등", "W", "WITH", "년", "학년도", "차", "안내", "PDF", "HWP", "HWPX", "XLSX", "DOCX",
    # 거의 모든 교육 공고에 들어가 단서가 안 되는 말(2026-10-01 검증: "한국특허정보원 AI 역량 강화 교육" 폴더가
    # 이 단어들만으로 무관한 "교원 해외연수" 공고와 짝지어졌었다).
    "교육", "역량", "강화", "지원", "프로그램", "과정",
}
# 너무 흔한 영문 약어 — 맞아도 '사업 내용이 같다'는 근거로 치지 않는다(2026-10-01 검증: "한양대 AI 부트캠프"가
# 'AI' 하나 때문에 다른 AI 공고와 확정됐었다).
_GENERIC_SHORT = {"AI", "SW", "IT", "ICT", "DX", "IOT", "VR", "AR"}
# 기관명으로 보는 키워드의 끝말. 폴더 이름에 기관명이 있으면, 그 기관이 공고에 맞을 때만 후보로 인정한다
# (2026-10-01 검증: "한양대 AI 부트캠프" 폴더가 기관이 다른 "제주대학교 … AI 부트캠프" 공고와 짝지어졌었다).
_ORG_SUFFIX_RE = re.compile(r"(대학교|대학|대|원|공단|재단|공사|센터|청|사|병원|협회|진흥회|부|처|시|군|구|도)$")
# 표기만 다른 같은 말(공고 텍스트 쪽에서 찾을 대체 표기).
_SYNONYMS = {
    "이러닝": ["ELEARNING"], "ELEARNING": ["이러닝"], "인공지능": ["AI"], "AI": ["인공지능"],
    "앵커": ["ANCHOR"], "ANCHOR": ["앵커"], "소프트웨어": ["SW"], "SW": ["소프트웨어"],
    "부트캠프": ["BOOTCAMP"], "라이즈": ["RISE"], "RISE": ["라이즈"], "창의재단": ["과학창의재단"],
}
# 이 단어가 들어간 파일/하위 폴더 이름은 공식 사업명이 남아 있을 가능성이 높아 단서로 쓴다.
_NOTICE_FILE_RE = re.compile(r"공고|제안요청|RFP|과업|사업계획|입찰", re.I)
# 우리 쪽에서 만든 문서(견적서·제안서 등) — 사업명이 아니라 우리 표현이라 단서에서 뺀다.
_OWN_FILE_RE = re.compile(r"견적|제안서|만족도|발표|회의|정산|계약서|데이원|패스트캠퍼스", re.I)
_BID_NO_RE = re.compile(r"R\d{2}B[KD]\d{8}")
_TOKEN_SPLIT_RE = re.compile(r"[^0-9A-Za-z가-힣]+")
_SCRIPT_SPLIT_RE = re.compile(r"[A-Za-z]+|[가-힣]+")


def parse_folder_name(name: str) -> dict:
    """"26_08_수원대학교_AI중핵교과" -> {"month": "2026-08", "label": "수원대학교 AI중핵교과", "outcome": None}."""
    m = _FOLDER_RE.match(name or "")
    month, rest = (f"20{m.group(1)}-{int(m.group(2)):02d}", m.group(3)) if m else ("", name or "")
    outcome = None
    for word, label in _OUTCOME_WORDS.items():
        if re.search(rf"[_\s]{word}\s*$", rest):
            outcome = label
            rest = re.sub(rf"[_\s]{word}\s*$", "", rest)
            break
    label = re.sub(r"[_]+", " ", rest).strip(" :")
    return {"month": month, "label": label, "outcome": outcome}


def _norm(text: str) -> str:
    return _TOKEN_SPLIT_RE.sub("", text or "").upper()


def notice_names(files: list) -> list:
    """폴더 안 파일 이름 중 공식 사업명 단서가 될 만한 것만, 확장자·번호 머리말을 떼고 반환한다.
    "1.입찰공고문(2026 국방 AX 전문인력양성 보수교육 운영 용역).pdf" -> 괄호 안 사업명을 우선 쓴다."""
    out = []
    for name in files:
        if not _NOTICE_FILE_RE.search(name) or _OWN_FILE_RE.search(name):
            continue
        stem = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", name)
        inner = re.findall(r"\(([^()]{6,})\)", stem)
        out.append(max(inner, key=len) if inner else re.sub(r"^\s*\d+[.)_\s]+", "", stem))
    return out


def folder_keywords(label: str, files: list) -> list:
    """단서 키워드 목록(정규화, 중복 제거). 폴더 이름 + 공고문류 파일 이름에서 뽑는다."""
    texts = [label] + notice_names(files)
    seen, keywords = set(), []
    for text in texts:
        for tok in _TOKEN_SPLIT_RE.split(text or ""):
            tok = tok.upper()
            if len(tok) < 2 or tok.isdigit() or tok in _STOPWORDS or re.fullmatch(r"\d{2,4}년?", tok):
                continue
            if tok not in seen:
                seen.add(tok)
                keywords.append(tok)
    return keywords


def _is_org_keyword(keyword: str) -> bool:
    return len(keyword) >= 2 and bool(re.search("[가-힣]", keyword)) and bool(_ORG_SUFFIX_RE.search(keyword))


def _org_hit(keyword: str, text: str, org: str) -> bool:
    """기관명 키워드가 공고에 맞는지. 포함 관계("수원대" ⊂ "수원대학교")에 더해, 흔한 축약 "대학교→대"를 풀어서
    본다("서울대병원" ↔ 서울대학교병원). 아무 글자나 건너뛰는 방식은 쓰지 않는다 — 2026-10-01 검증에서
    "한양대"가 "한양사이버대학교"에 잘못 맞았다."""
    if _hit(keyword, text) >= 1.0:
        return True
    short = lambda t: t.replace("대학교", "대")
    return short(keyword) in short(org or text)


def _hit(keyword: str, text: str) -> float:
    """키워드가 공고 텍스트(정규화)에 들어 있는 정도(0~1). 통째로 있으면 1, 동의어로 있으면 1,
    한글/영문 덩어리로 쪼갠 조각 중 일부만 있으면 그 비율(2글자 이상 조각만)."""
    if keyword in text or any(s in text for s in _SYNONYMS.get(keyword, [])):
        return 1.0
    parts = [p.upper() for p in _SCRIPT_SPLIT_RE.findall(keyword) if len(p) >= 2]
    if len(parts) < 2:
        return 0.0
    found = sum(1 for p in parts if p in text or any(s in text for s in _SYNONYMS.get(p, [])))
    return found / len(parts)


# ---------------------------------------------------------------- Drive 읽기

def _access_token():
    """서비스 계정으로 Drive 읽기 토큰을 받는다. 키가 없으면 None."""
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    path = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    if not raw and path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    if not raw:
        return None
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_info(json.loads(raw), scopes=[_DRIVE_SCOPE])
    creds.refresh(Request())
    return creds.token


def _list_children(token: str, parent_ids: list) -> list:
    """여러 폴더의 바로 아래 항목을 한 번에 조회한다(쿼리 길이 제한 때문에 20개씩)."""
    items = []
    for i in range(0, len(parent_ids), 20):
        chunk = parent_ids[i:i + 20]
        q = "(" + " or ".join(f"'{pid}' in parents" for pid in chunk) + ") and trashed = false"
        page_token = None
        while True:
            params = {"q": q, "fields": "nextPageToken, files(id, name, mimeType, createdTime, parents, owners(emailAddress))",
                      "pageSize": 1000, "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
            if page_token:
                params["pageToken"] = page_token
            resp = requests.get(_DRIVE_FILES_URL, params=params, headers={"Authorization": f"Bearer {token}"}, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            items += data.get("files", [])
            page_token = data.get("nextPageToken")
            if not page_token:
                break
    return items


def fetch_proposal_folders(skip_ids=frozenset()):
    """[제안 및 검토] 바로 아래 폴더 목록 [{"id", "name", "created", "owner", "files": [파일·하위폴더 이름]}].
    skip_ids(이미 확정된 폴더)는 안쪽 파일을 다시 읽지 않는다. 인증 정보가 없으면 None."""
    token = _access_token()
    if token is None:
        return None
    folders = []
    for f in _list_children(token, [PROPOSAL_FOLDER_ID]):
        if f.get("mimeType") != _FOLDER_MIME:
            continue
        owners = f.get("owners") or [{}]
        folders.append({"id": f["id"], "name": f["name"], "created": f.get("createdTime", "")[:10],
                        "owner": owners[0].get("emailAddress", ""), "files": []})
    pending = {f["id"]: f for f in folders if f["id"] not in skip_ids}
    children = _list_children(token, list(pending))
    # 공고문/RFP류 하위 폴더는 한 단계 더 본다(예: "제안요청서(rfp) 원본").
    sub_parent = {}
    for item in children:
        parent = pending.get((item.get("parents") or [""])[0])
        if parent is None:
            continue
        parent["files"].append(item["name"])
        if item.get("mimeType") == _FOLDER_MIME and _NOTICE_FILE_RE.search(item["name"]):
            sub_parent[item["id"]] = parent
    for item in _list_children(token, list(sub_parent)) if sub_parent else []:
        parent = sub_parent.get((item.get("parents") or [""])[0])
        if parent is not None:
            parent["files"].append(item["name"])
    return folders


# ---------------------------------------------------------------- 짝짓기

def _to_date(s: str):
    try:
        return datetime.strptime((s or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _candidates(postings: list, raw_titles: list) -> list:
    """비교 대상 공고 [(정규화 텍스트, 게시일, 정보)]. postings에 있는 공고는 raw 쪽에서 뺀다."""
    out, seen = [], set()
    for pid, source, title, org, posted, notified in postings:
        out.append((_norm(f"{org}{title}"), _to_date(posted), {
            "posting_id": pid, "posting_source": source, "posting_title": title, "posting_org": org,
            "notified": bool(notified), "kind": "postings", "org_norm": _norm(org)}))
        seen.add(_norm(title))
    for _n, title, source, last_seen in raw_titles:
        if _norm(title) in seen:
            continue
        out.append((_norm(title), _to_date(last_seen), {
            "posting_id": None, "posting_source": source, "posting_title": title, "posting_org": "",
            "notified": False, "kind": "raw", "org_norm": ""}))
    return out


def match_folders(folders: list, postings: list, raw_titles: list, db_start) -> list:
    """폴더마다 가장 잘 맞는 공고를 찾아 기록용 dict를 만든다.
    folders: [{"id", "name", "created", "owner", "files"}]
    postings: db.get_postings_for_matching() — [(id, source, title, org, posted, notified), ...]
    raw_titles: db.get_raw_titles_dated() — [(norm, title, source, last_seen_date), ...]
    db_start: DB에 공고가 쌓이기 시작한 날(이보다 먼저 만든 폴더는 before_db)."""
    cands = _candidates(postings, raw_titles)
    n = len(cands) or 1
    df_cache = {}

    def idf(kw):
        if kw not in df_cache:
            df = sum(1 for text, _, _ in cands if _hit(kw, text) >= 1.0)
            df_cache[kw] = math.log((n + 1) / (df + 1)) + 1.0
        return df_cache[kw]

    results = []
    for folder in folders:
        parsed = parse_folder_name(folder["name"])
        files = folder.get("files") or []
        keywords = folder_keywords(parsed["label"], files)
        created = _to_date(folder["created"])
        rec = {"folder_id": folder["id"], "folder_name": folder["name"], "label": parsed["label"],
               "folder_month": parsed["month"], "outcome": parsed["outcome"], "created": folder["created"],
               "owner": folder.get("owner", ""), "posting_id": None, "posting_source": None, "posting_title": None,
               "posting_org": None, "notified": False, "score": 0.0,
               "evidence": json.dumps({"keywords": keywords, "notice_files": notice_names(files)}, ensure_ascii=False)}
        if created is None or (db_start and created < db_start):
            rec["match_status"] = "before_db"
            results.append(rec)
            continue
        lo, hi = created - timedelta(days=MATCH_WINDOW_DAYS), created + timedelta(days=MATCH_AFTER_DAYS)
        window = [(text, info) for text, posted, info in cands if posted and lo <= posted <= hi]

        # 파일 이름에 나라장터 공고번호가 있으면 바로 확정
        bid_nos = {m for name in files for m in _BID_NO_RE.findall(name)}
        exact = next((info for _t, info in window if info["posting_id"] and any(b in info["posting_id"] for b in bid_nos)), None)
        if exact:
            rec.update({k: exact[k] for k in ("posting_id", "posting_source", "posting_title", "posting_org", "notified")})
            rec.update({"score": 1.0, "match_status": "matched" if exact["kind"] == "postings" else "missed"})
            results.append(rec)
            continue

        total = sum(idf(k) for k in keywords) or 1.0
        org_keywords = [k for k in keywords if _is_org_keyword(k)]
        best_score, best_info, best_hits = 0.0, None, []
        for text, info in window:
            # 폴더 이름에 기관명이 있으면 그 기관이 맞는 공고만 후보로 본다(원본 공고는 기관명이 없어 제목으로만 본다).
            if org_keywords and not any(_org_hit(k, text, info["org_norm"]) for k in org_keywords):
                continue
            hits = [(k, 1.0 if k in org_keywords and _org_hit(k, text, info["org_norm"]) else _hit(k, text))
                    for k in keywords]
            score = sum(idf(k) * h for k, h in hits) / total
            if score > best_score:
                best_score, best_info, best_hits = score, info, [k for k, h in hits if h >= 1.0]
        rec["score"] = round(best_score, 3)
        # 사업 내용 키워드: 기관명·흔한 약어를 뺀 적중 키워드
        topic_hits = [k for k in best_hits if k not in org_keywords and k not in _GENERIC_SHORT]
        need_topics = 1 if org_keywords else 2
        if best_info and best_score >= REVIEW_THRESHOLD and topic_hits:
            rec.update({k: best_info[k] for k in ("posting_id", "posting_source", "posting_title", "posting_org", "notified")})
            if best_score >= MATCH_THRESHOLD and len(topic_hits) >= need_topics:
                rec["match_status"] = "missed" if best_info["kind"] == "raw" else "matched"
            else:
                rec["match_status"] = "needs_review"
            rec["evidence"] = json.dumps({"keywords": keywords, "notice_files": notice_names(files),
                                          "matched_keywords": best_hits}, ensure_ascii=False)
        else:
            rec["match_status"] = "not_collected"
        results.append(rec)
    return results


_STATUS_LABEL = {
    "matched": "발송 공고와 연결", "missed": "필터가 놓친 공고", "needs_review": "연결 확인 필요",
    "not_collected": "수집 범위 밖", "before_db": "DB 수집 이전",
}


def sync_proposals(conn, warnings: list) -> dict:
    """Drive 폴더를 읽어 DB proposals를 갱신하고 상태별 건수를 반환한다. 인증 정보가 없거나 실패하면 건너뛴다."""
    from . import db

    try:
        folders = fetch_proposal_folders(skip_ids=db.get_settled_proposal_ids(conn))
    except Exception as exc:  # Drive 장애가 공고 발송을 막으면 안 된다
        print(f"[제안이력] Drive 폴더 조회 실패: {exc!r}")
        warnings.append(f"제안 이력(Drive) 조회 실패 ({exc.__class__.__name__})")
        return {}
    if folders is None:
        print("[제안이력] GOOGLE_SERVICE_ACCOUNT_JSON이 없어 Drive 연동을 건너뜀")
        return {}
    return record_matches(conn, folders, warnings)


def record_matches(conn, folders: list, warnings: list) -> dict:
    """폴더 목록을 짝지어 DB에 기록한다(이미 확정된 폴더는 그대로 둔다)."""
    from . import db

    settled = db.get_settled_proposal_ids(conn)
    pending = [f for f in folders if f["id"] not in settled]
    results = match_folders(pending, db.get_postings_for_matching(conn), db.get_raw_titles_dated(conn),
                            db.get_db_start_date(conn))
    db.upsert_proposals(conn, results, date.today().isoformat())

    counts = db.count_proposals_by_status(conn)
    print(f"[제안이력] Drive 폴더 {len(folders)}개 / " + ", ".join(
        f"{_STATUS_LABEL.get(k, k)} {v}건" for k, v in sorted(counts.items())))
    for rec in results:
        if rec["match_status"] == "missed":
            msg = (f"제안 검토한 공고를 필터가 제외함: '{rec['posting_title']}' "
                   f"(Drive 폴더 '{rec['folder_name']}', 키워드 적중 {rec['score']:.0%}) — 필터 규칙 점검 필요")
            print(f"  [놓친 공고] {msg}")
            warnings.append(msg)
    return counts


def print_report(conn) -> None:
    """`python test_g2b_api.py proposals` — 제안 이력 기록 현황."""
    from . import db

    rows = db.get_proposals(conn)
    if not rows:
        print("기록된 제안 이력이 없습니다. (GOOGLE_SERVICE_ACCOUNT_JSON 설정 후 notify 실행 시 쌓입니다)")
        return
    for status in ("missed", "needs_review", "matched", "not_collected", "before_db"):
        group = [r for r in rows if r["match_status"] == status]
        if not group:
            continue
        print(f"\n===== {_STATUS_LABEL[status]} {len(group)}건 =====")
        for r in group:
            outcome = f" [{r['outcome']}]" if r["outcome"] else ""
            print(f"  {r['created']} {r['folder_name']}{outcome}")
            ev = json.loads(r.get("evidence") or "{}")
            if ev.get("notice_files"):
                print(f"      단서 파일: {ev['notice_files']}")
            if r["posting_title"]:
                sent = ", 슬랙 발송됨" if r["notified"] else ""
                print(f"      → {r['posting_title']} ({r['posting_org'] or r['posting_source']}, "
                      f"키워드 적중 {r['score']:.0%}{sent}) / 맞은 키워드 {ev.get('matched_keywords', [])}")
