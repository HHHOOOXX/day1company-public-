"""서비스키, 상수, 필터링 키워드를 모아둔 설정 모듈."""

import os
import sys
from urllib.parse import unquote

from dotenv import load_dotenv

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(dotenv_path=os.path.join(REPO_ROOT, ".env"))

_raw_key = os.getenv("G2B_SERVICE_KEY")
if not _raw_key:
    print("[에러] .env 파일에 G2B_SERVICE_KEY 가 설정되어 있지 않습니다.")
    print("       .env 파일에 아래 줄을 추가하세요:")
    print("       G2B_SERVICE_KEY=발급받은_서비스키")
    sys.exit(1)

# Encoding형/Decoding형 어느 쪽이 들어와도 동일하게 동작하도록 정규화
SERVICE_KEY = unquote(_raw_key)

# ALIO(공공기관 알리오), 기업마당(bizinfo) 서비스키는 아직 발급 전이라 없어도 됨 (discover 모드만 동작)
ALIO_SERVICE_KEY = os.getenv("ALIO_SERVICE_KEY")
BIZINFO_SERVICE_KEY = os.getenv("BIZINFO_SERVICE_KEY")

# 나라장터 입찰공고정보서비스 - 업무구분별 오퍼레이션
OPERATIONS = {
    "용역": "getBidPblancListInfoServc",
    "공사": "getBidPblancListInfoCnstwk",
    "물품": "getBidPblancListInfoThng",
    "외자": "getBidPblancListInfoFrgcpt",
}
BASE_URL = "https://apis.data.go.kr/1230000/ad/BidPublicInfoService"

# 나라장터 사전규격정보서비스(용역) - data.go.kr 상품 15129437
# 입찰공고정보서비스(BidPublicInfoService)와 별개 상품이라 활용신청을 따로 해야 정상 호출된다.
PRE_SPEC_BASE_URL = "https://apis.data.go.kr/1230000/ao/HrcspSsstndrdInfoService"
PRE_SPEC_LIST_OPERATION = "getPublicPrcureThngInfoServc"  # 사전규격 용역 목록 조회

# 기업마당(중소기업 지원사업 통합공고) - crtfcKey 발급 후 사용
BIZINFO_API_URL = "https://www.bizinfo.go.kr/uss/rss/bizinfoApi.do"

# ALIO(공공기관 경영정보 공개시스템) - opendata.alio.go.kr 활용신청 승인 화면에서 확인한 End Point
ALIO_PUBLIC_INST_URL = "https://opendata.alio.go.kr/v1/publicinst/list.do"  # 기관정보
ALIO_BUSINESS_URL = "https://opendata.alio.go.kr/v1/business/list.do"  # 사업정보

# 우리팀(교육회사, 대학교/지자체 대상 교육·양성사업) 필터링용 키워드 후보.
# classify 모드로 실제 분류값과의 교차 결과를 보고 다듬어 나가면 됩니다.
EDU_KEYWORDS = [
    "양성", "육성", "인재양성", "역량강화", "창작자", "크리에이터",
    "콘텐츠", "아카데미", "부트캠프", "멘토링", "교육과정", "직무교육",
    "위탁교육", "이러닝", "온라인교육", "운영", "위탁운영", "교육생",
    "강사", "AI", "인공지능", "디지털콘텐츠",
]

# 대학교/지자체/공공기관(및 산하기관) 발주처 판별용 키워드
ORG_KEYWORDS = [
    "대학교", "대학원", "산학협력단",
    "시청", "군청", "구청", "도청", "교육청", "재단", "진흥원",
    "테크노파크",
]

# 교육 키워드+발주기관 조건은 만족하지만 우리팀 사업과 무관한 공고(축제/행사 대행성) 제외용 키워드.
# 예: "OO축제 무대설치 및 운영 용역"처럼 "운영"이 걸려 오탐되는 케이스를 걸러낸다.
# (피드백: 2026-09-14 발송분 중 축제 무대설치 공고가 섞여있었음)
EXCLUDE_KEYWORDS = [
    "축제", "축전", "페스티벌", "페스타", "FESTIVAL",
    "무대설치", "무대 설치", "무대제작", "무대설비",
    "음향", "조명설치", "행사대행", "행사 대행", "행사운영", "행사진행",
    "불꽃놀이", "체육대회", "박람회", "전시부스", "부스설치", "부스운영",
    "야시장", "플리마켓", "공연", "이벤트대행", "축제운영", "해맞이",
]

# 중앙부처·청·위원회 정적 리스트 (ALIO처럼 공식 마스터 API를 못 찾아 수기 관리).
# ntceInsttNm(발주기관명) 안에 이 이름이 포함되어 있는지로 매칭한다.
CENTRAL_GOV_ORGS = [
    # 19부
    "기획재정부", "교육부", "과학기술정보통신부", "외교부", "통일부", "법무부",
    "국방부", "행정안전부", "국가보훈부", "문화체육관광부", "농림축산식품부",
    "산업통상자원부", "보건복지부", "환경부", "고용노동부", "여성가족부",
    "국토교통부", "해양수산부", "중소벤처기업부",
    # 처
    "법제처", "식품의약품안전처", "인사혁신처", "대통령경호처",
    # 청
    "국세청", "관세청", "조달청", "통계청", "검찰청", "병무청", "방위사업청",
    "경찰청", "소방청", "국가유산청", "농촌진흥청", "산림청", "특허청", "기상청",
    "해양경찰청", "질병관리청", "새만금개발청", "행정중심복합도시건설청",
    # 위원회
    "방송통신위원회", "공정거래위원회", "금융위원회", "국민권익위원회",
    "원자력안전위원회", "개인정보보호위원회", "국가인권위원회", "규제개혁위원회",
]

# 사업영역 5축 태깅용 키워드 (GO/NO-GO 판단이 아니라 참고용 라벨)
BUSINESS_AREA_KEYWORDS = {
    "교육운영": [
        "교육", "운영", "위탁운영", "직무교육", "재직자", "부트캠프", "아카데미",
        "교육과정", "LMS", "마이크로디그리", "PBL", "경진대회", "학사혁신",
    ],
    "콘텐츠 기획·개발·제작": [
        "콘텐츠", "제작", "개발", "기획", "영상", "이러닝", "디지털콘텐츠",
    ],
    "연구·조사·성과분석": [
        "연구", "조사", "성과분석", "성과관리", "성과지표", "환류체계", "실태조사",
    ],
    "전략수립": [
        "전략수립", "발전전략", "중장기", "로드맵", "혁신전략", "RISE", "대학혁신",
    ],
    "컨설팅": [
        "컨설팅", "자문", "진단", "고도화", "PMC", "거버넌스",
    ],
}
