/**
 * 공고 피드백 버튼(✅ 우리 핏 / ❌ 아님) 수신용 Google Apps Script 웹앱.
 *
 * Slack 카드의 버튼을 누르면 Slack이 이 웹앱(doPost)으로 클릭 내용을 보내고, 이 스크립트가 연결된 Google 시트의
 * "feedback" 탭에 한 줄씩 기록한다. 매일 공고 알림을 보내는 GitHub Actions 실행이 doGet(action=export)으로 기록을 받아
 * DB(data/notifier.db posting_feedback)에 넣는다(g2b_notifier/feedback.py). 설치 방법은 apps_script/README.md.
 *
 * 설정값: 아래 SETTINGS에 직접 채우거나, 스크립트 속성(프로젝트 설정 → 스크립트 속성)에 같은 이름으로 넣는다(속성이 우선).
 * 저장소에 올라가는 이 파일의 SETTINGS는 비워 둔다 — 공개 저장소라 토큰을 커밋하면 안 된다.
 *   EXPORT_TOKEN              필수. GitHub 시크릿 FEEDBACK_EXPORT_TOKEN과 같은 값(기록 내보내기 보호용).
 *   SLACK_VERIFICATION_TOKEN  권장. Slack 앱 Basic Information → Verification Token. 다른 곳에서 보낸 가짜 클릭을 거른다
 *                             (Apps Script는 요청 헤더를 못 읽어서 서명 검증 대신 이 토큰을 쓴다).
 *   LINK_TOKEN                링크 방식(아래)을 쓸 때만. GitHub 시크릿 FEEDBACK_LINK_TOKEN과 같은 값.
 *
 * 링크 방식: Slack 앱에 Interactivity를 켤 수 없거나 버튼 클릭이 실패하면, 버튼이 이 웹앱 주소를 브라우저로 여는
 * 방식으로 바꿀 수 있다(GitHub 시크릿 FEEDBACK_LINK_TOKEN 설정). doGet이 기록하고 "기록했어요" 페이지를 보여준다.
 * 누가 눌렀는지는 남지 않는다.
 */

var SHEET_NAME = 'feedback';
var LABELS = { fit: '✅ 우리 핏', not_fit: '❌ 아님' };

var SETTINGS = {
  EXPORT_TOKEN: '',
  SLACK_VERIFICATION_TOKEN: '',
  LINK_TOKEN: '',
};

function setting_(key) {
  return PropertiesService.getScriptProperties().getProperty(key) || SETTINGS[key] || '';
}

function getSheet_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(SHEET_NAME);
  if (!sheet) {
    sheet = ss.insertSheet(SHEET_NAME);
    sheet.appendRow(['at', 'posting_id', 'label', 'user_id', 'user_name', 'title']);
  }
  return sheet;
}

function record_(postingId, label, userId, userName, title) {
  getSheet_().appendRow([new Date().toISOString(), postingId, label, userId || '', userName || '', title || '']);
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

// Slack Interactivity 요청: application/x-www-form-urlencoded, payload=<JSON>
function doPost(e) {
  var payload = JSON.parse((e && e.parameter && e.parameter.payload) || '{}');
  var expected = setting_('SLACK_VERIFICATION_TOKEN');
  if (expected && payload.token !== expected) {
    return ContentService.createTextOutput('');
  }
  var user = payload.user || {};
  (payload.actions || []).forEach(function (action) {
    if (!LABELS[action.action_id]) return;
    var value = {};
    try { value = JSON.parse(action.value); } catch (err) { value = { p: action.value }; }
    record_(value.p, action.action_id, user.id, user.name || user.username, value.t);
    if (payload.response_url) {
      // 누른 사람에게만 보이는 확인 메시지
      UrlFetchApp.fetch(payload.response_url, {
        method: 'post',
        contentType: 'application/json',
        muteHttpExceptions: true,
        payload: JSON.stringify({
          response_type: 'ephemeral',
          replace_original: false,
          text: LABELS[action.action_id] + '(으)로 기록했어요: ' + (value.t || value.p),
        }),
      });
    }
  });
  return ContentService.createTextOutput('');
}

function doGet(e) {
  var p = (e && e.parameter) || {};

  // GitHub Actions가 기록을 가져갈 때: ?action=export&token=...
  if (p.action === 'export') {
    if (!p.token || p.token !== setting_('EXPORT_TOKEN')) {
      return json_({ ok: false, error: 'unauthorized' });
    }
    var values = getSheet_().getDataRange().getValues().slice(1);
    var rows = values
      .filter(function (r) { return r[1] && LABELS[r[2]]; })
      .map(function (r) {
        // 시트가 ISO 문자열을 날짜로 바꿔 저장할 수 있어서 다시 ISO 문자열로 맞춘다(봇이 시각 순으로 마지막 클릭을 고른다).
        var at = r[0] instanceof Date ? r[0].toISOString() : String(r[0]);
        return { at: at, posting_id: String(r[1]), label: String(r[2]), user_id: String(r[3]), user_name: String(r[4]) };
      });
    return json_({ ok: true, rows: rows });
  }

  // 링크 방식 버튼: ?p=<posting_id>&l=fit|not_fit&k=<LINK_TOKEN>&t=<제목>
  var linkToken = setting_('LINK_TOKEN');
  if (p.p && LABELS[p.l] && linkToken && p.k === linkToken) {
    record_(p.p, p.l, 'link', '', p.t);
    return HtmlService.createHtmlOutput(
      '<meta name="viewport" content="width=device-width,initial-scale=1">' +
      '<div style="font-family:sans-serif;padding:32px;font-size:18px">' + LABELS[p.l] +
      '(으)로 기록했어요.<br><small style="color:#666">이 창은 닫아도 돼요.</small></div>'
    );
  }
  return ContentService.createTextOutput('ok');
}
