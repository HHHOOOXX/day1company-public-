# 공고 피드백 버튼 설치 (Google Apps Script)

Slack 공고 카드마다 붙는 **✅ 우리 핏 / ❌ 아님** 버튼의 클릭을 받아 Google 시트에 쌓는 웹앱입니다.
매일 실행되는 알림 봇이 이 기록을 가져가 DB(`posting_feedback`)에 넣고, ✅ 공고는 팀 맞춤 공고로 바로 반영합니다.

## 1. 시트 + 스크립트 만들기 (5분)
1. Google 드라이브에서 새 스프레드시트를 만듭니다(예: "공고 피드백").
2. 메뉴 **확장 프로그램 → Apps Script**를 열고, 기본 코드를 지운 뒤 `feedback.gs` 내용을 붙여넣고 저장합니다.
3. 설정값 넣기. 코드 맨 위 `SETTINGS`의 따옴표 안에 직접 넣거나(간단), **프로젝트 설정(톱니바퀴) → 스크립트 속성**에 같은 이름으로 넣습니다(속성이 우선). 저장소의 이 파일에는 토큰을 넣어 커밋하지 마세요(공개 저장소).
   - `EXPORT_TOKEN`: 아무도 추측 못 할 긴 문자열(예: 비밀번호 생성기로 40자). 아래 GitHub 시크릿에도 같은 값을 넣습니다.
   - `SLACK_VERIFICATION_TOKEN`: Slack 앱 설정 → Basic Information → App Credentials → **Verification Token**
4. 오른쪽 위 **배포 → 새 배포 → 유형: 웹 앱**
   - 실행 사용자: **나**
   - 액세스 권한: **모든 사용자** (Slack 서버가 로그인 없이 호출해야 해서)
   - 배포 후 나오는 **웹 앱 URL**(`https://script.google.com/macros/s/…/exec`)을 복사합니다.
   - 처음 배포할 때 권한 승인 화면이 나오면 허용합니다.

## 2. Slack 앱에 버튼 연결
api.slack.com/apps → 공고 알림 앱 → **Interactivity & Shortcuts** → Interactivity **On** →
**Request URL**에 위 웹 앱 URL을 넣고 저장합니다.

## 3. GitHub 시크릿 등록
저장소 **Settings → Secrets and variables → Actions**에 추가합니다.
- `FEEDBACK_WEBAPP_URL`: 웹 앱 URL
- `FEEDBACK_EXPORT_TOKEN`: 1-3에서 정한 `EXPORT_TOKEN`과 같은 값

`FEEDBACK_WEBAPP_URL`이 있어야 카드에 버튼이 붙습니다(없으면 지금처럼 버튼 없이 발송).

## 확인
- 다음 발송 메시지의 카드 아래에 버튼이 보이고, 누르면 나에게만 "✅ 우리 핏(으)로 기록했어요" 메시지가 뜹니다.
- 시트의 `feedback` 탭에 한 줄씩 쌓입니다.
- 다음 날 실행 로그에 `[피드백] 버튼 기록 N건 …`이 찍히고, `python test_g2b_api.py feedback`으로 DB에 들어간 결과를 볼 수 있습니다.

## 버튼을 눌렀는데 Slack에 경고(⚠️)가 뜨면
Apps Script는 응답을 한 번 다른 주소로 넘기는(redirect) 방식이라 Slack이 "응답 실패"로 표시하는 경우가 있습니다.
이때도 시트에 기록됐는지 먼저 확인하세요(기록됐다면 경고는 무시해도 됩니다). 기록이 안 되면 **링크 방식**으로 바꿉니다.
- 스크립트 속성에 `LINK_TOKEN`(긴 임의 문자열)을 추가하고, GitHub 시크릿 `FEEDBACK_LINK_TOKEN`에 같은 값을 넣습니다.
- Slack 앱의 Interactivity는 꺼도 됩니다. 버튼을 누르면 브라우저 창이 열리며 "기록했어요"가 표시됩니다(누른 사람은 기록되지 않음).

## 스크립트를 고친 경우
배포 → **배포 관리 → 수정(연필) → 버전: 새 버전 → 배포**. URL은 그대로 유지됩니다.
