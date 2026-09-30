# 나라장터 공고 모니터링 · Slack 자동 알림 봇

공공사업팀(데이원컴퍼니)의 반복 업무 중 하나인 **입찰공고 서칭**을 자동화하는 파이썬 기반 에이전트입니다. 나라장터·사전규격·기업마당에 매일 새로 올라오는 공고를 수집해 우리 팀 사업 영역(교육·콘텐츠 운영, 창업지원 등)에 맞는 건만 골라내고, 평일 아침 정해진 시각에 Slack 채널로 요약해 보내줍니다.

사람이 매일 나라장터 사이트를 열어 하나씩 훑어보던 일을, "오늘 확인해야 할 공고 목록"으로 자동 정리해 받아보는 방식으로 바꾸는 것이 목표입니다.

---

## 주요 기능

### 1. 3개 소스 자동 수집
- **나라장터 입찰공고정보서비스**: 용역/공사/물품/외자 공고
- **나라장터 사전규격정보서비스**: 입찰공고 전 단계인 사전규격(용역) — 더 일찍 기회를 포착
- **기업마당(bizinfo.go.kr)**: NIPA/NIA/IITP/중진공 등이 올리는 중소기업 지원사업 공고

### 2. 우리 팀 사업에 맞는 공고만 골라내는 필터링
- 교육/콘텐츠/창업지원 등 사업영역 키워드 + 대학교·지자체·공공기관 등 발주처 유형 키워드로 1차 필터링
- 실제로 무관한 것으로 확인된 유형(축제 대행, 시스템 유지보수/감리, 해외연수, 홍보 대행 등)은 제외 키워드로 걸러냄
- 확신도에 따라 **확실포함 / 확인필요(review)** 두 등급으로 분류해, 애매한 건도 놓치지 않고 "확인해볼 만한 공고"로 함께 노출
- 과거 실제 수주 이력(`WIN_HISTORY`) 및 제안 검토 이력(`PROPOSAL_HISTORY`)과 발주기관이 겹치면 ⭐확실후보로 자동 승격
- ALIO(공공기관 경영정보 공개시스템) 공공기관 마스터 리스트를 화이트리스트로 연동해 발주기관 판별 정확도 보강

### 3. 참가 자격 자동 확인
- 업종제한이 걸린 공고는 나라장터 공식 API로 실제 제한 업종코드를 조회해 우리 회사 보유 업종코드와 자동 대조
- 지역제한(본사 소재지 조건)도 소관기관 기준으로 자동 판별
- 사전규격/기업마당처럼 업종·지역 제한이 API 필드로 제공되지 않는 경우, 첨부된 제안요청서/공고문 원문(hwp/hwpx/pdf)을 직접 열어 텍스트를 추출하고 업종코드·지역 조건 문구를 탐지

### 4. 마감 임박 안전장치
- 마감까지 7일 미만으로 남은 공고는 원칙적으로 발송하지 않음(검토 시간 부족 방지)
- 단, 과거 수주 이력과 발주기관이 겹치는 ⭐확실후보(다른 조건도 모두 깨끗하게 통과한 경우)는 예외적으로 발송
- 이미 발송된 ⭐확실후보는 마감일까지 매일 리마인드

### 5. 중복 발송 방지
- 공고 상태를 SQLite(`data/notifier.db`)에 저장해, 같은 공고가 다시 조회되어도 재발송하지 않음
- 정정/재공고로 여러 차수가 올라온 경우 최신 차수 한 건만 유지

### 6. Slack 메시지 & 대시보드
- Slack Incoming Webhook으로 Block Kit 형태의 정리된 메시지 발송 (오늘의 추천 공고 / 확인해볼 만한 공고 / ⭐확실후보 리마인드)
- 채널에는 핵심 공고만 요약해서 보내고, 그날 수집된 전체 목록은 정적 HTML 대시보드(`docs/index.html`, GitHub Pages 배포 가능)로 제공 — 클라이언트 사이드 정렬/필터/검색 지원

### 7. 안정적인 스케줄링
- 한국 공휴일(설/추석 등)에는 수집·발송을 건너뜀
- GitHub Actions 스케줄 지연/스킵에 대비해 외부 크론 트리거 → 내부 백업 스케줄 → 2차 백업까지 3중 안전망 구성 (이미 정상 발송된 날엔 백업 실행이 조용히 종료)

---

## 동작 방식 (요약)

```
매일 평일 아침
   │
   ▼
나라장터 입찰공고 + 사전규격 + 기업마당 수집
   │
   ▼
키워드 필터링 → 참가자격(업종/지역) 확인 → 확신도 판정
   │
   ▼
이전에 발송한 공고 제외 (SQLite 중복 체크)
   │
   ├─▶ Slack 채널로 요약 메시지 발송
   └─▶ 대시보드(docs/index.html) 갱신 후 저장소에 커밋
```

---

## 폴더 구조

```
.
├── g2b_notifier/          # 핵심 로직 패키지
│   ├── config.py          #   서비스키, 필터링 키워드, 수주 이력, 공휴일 목록 등 설정
│   ├── g2b.py              #   나라장터 입찰공고/사전규격 API 연동
│   ├── bizinfo.py          #   기업마당 API 연동
│   ├── alio.py             #   ALIO 공공기관 마스터 리스트 연동
│   ├── classify.py         #   관심 공고 판별 + 확신도 판정 + 사업영역 태깅
│   ├── doc_extract.py      #   첨부파일(hwp/hwpx/pdf) 본문 텍스트 추출
│   ├── db.py                #   SQLite 기반 상태 저장(중복 발송 방지)
│   ├── slack.py             #   Slack 메시지 포맷팅 및 발송
│   ├── preview.py           #   로컬/대시보드용 HTML 미리보기 생성
│   └── cli.py                #   CLI 진입점(notify/daily/preview/classify 등)
├── test_g2b_api.py         # GitHub Actions가 호출하는 하위호환용 진입 스크립트
├── docs/index.html          # 자동 생성되는 공고 대시보드 (GitHub Pages 배포 대상)
├── data/notifier.db         # 발송 이력 SQLite (워크플로우가 자동 커밋)
├── .github/workflows/
│   ├── notify.yml            # 평일 아침 정시 발송 워크플로우
│   └── notify-backup.yml     # 정시 발송이 지연/스킵됐을 때의 2차 백업 워크플로우
└── requirements.txt
```

---

## 로컬 실행

### 1) 준비
```bash
pip install -r requirements.txt
```

### 2) `.env` 파일 생성 (저장소 루트에 위치, 커밋되지 않음)
```env
G2B_SERVICE_KEY=나라장터_공공데이터포털_서비스키
SLACK_WEBHOOK_URL=슬랙_인커밍_웹훅_URL
BIZINFO_SERVICE_KEY=기업마당_서비스키          # 선택
ALIO_SERVICE_KEY=ALIO_서비스키                # 선택
DASHBOARD_URL=대시보드_공개_URL                # 선택, 설정 시 슬랙 메시지에 링크 첨부
```

### 3) CLI 명령어

| 명령어 | 설명 |
|---|---|
| `python test_g2b_api.py` | 최근 공고 원본 미리보기 (필드 확인용) |
| `python test_g2b_api.py daily [카테고리]` | 오늘자 관심 공고 콘솔 미리보기 (DB에 기록 안 함) |
| `python test_g2b_api.py preview [카테고리]` | Slack 카드처럼 생긴 HTML을 로컬 브라우저로 미리보기 |
| `python test_g2b_api.py notify [카테고리] [--dry-run] [--quiet-if-empty]` | 실제 Slack 발송 + DB 기록 |
| `python test_g2b_api.py classify [카테고리] [일수]` | 필터링 키워드 튜닝을 위한 분류 교차 집계 |

---

## GitHub Actions 자동 실행

`.github/workflows/notify.yml`이 평일 아침 정해진 시각에 위 CLI의 `notify` 명령을 자동 실행하고, 결과(발송 이력 DB, 대시보드)를 저장소에 커밋합니다. 사용하려면 저장소의 **Settings → Secrets and variables → Actions**에 다음 값을 등록하세요.

- `G2B_SERVICE_KEY` (필수)
- `SLACK_WEBHOOK_URL` (필수)
- `BIZINFO_SERVICE_KEY` (선택)
- `ALIO_SERVICE_KEY` (선택)
- `DASHBOARD_URL` (선택)

`notify-backup.yml`은 정시 실행이 GitHub Actions 스케줄 지연 등으로 놓쳤을 때만 대신 발송하는 백업 워크플로우로, 별도 설정 없이 같은 시크릿을 공유합니다.

---

## 커스터마이징

이 저장소의 필터링 키워드(`g2b_notifier/config.py`)는 데이원컴퍼니 공공AX사업그룹의 실제 사업 영역(교육 운영, 콘텐츠 기획·개발, 창업지원 등)과 과거 수주 이력을 기준으로 튜닝되어 있습니다. 다른 팀/조직에서 그대로 가져다 쓰는 경우 다음 값들을 자신의 사업 영역에 맞게 수정해야 합니다.

- `EDU_KEYWORDS` / `ORG_KEYWORDS` / `EXCLUDE_KEYWORDS` — 관심 공고 판별 키워드
- `WIN_HISTORY` / `PROPOSAL_HISTORY` — ⭐확실후보 판정에 쓰이는 자사 수주/제안 이력
- `COMPANY_INDUSTRY_CODES` / `COMPANY_HQ_REGION` — 참가자격(업종/지역) 자동 판별 기준
- `KR_HOLIDAYS_BY_YEAR` — 연도가 바뀔 때마다 갱신 필요
