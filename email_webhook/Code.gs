/**
 * GENROSE Room Scene Analyzer — review-complete email webhook
 *
 * Deploy this as a Google Apps Script Web App:
 *   Execute as: Me
 *   Who has access: Anyone
 *
 * The webhook is protected by a shared token stored in Script Properties.
 * It can send only to the fixed GENROSE review inbox below.
 */

const REVIEW_RECIPIENT = 'marketing@genrose.com';
const SENDER_NAME = 'GENROSE Room Scene Review';
const TOKEN_PROPERTY = 'ROOM_SCENE_WEBHOOK_TOKEN';

function setupWebhookToken() {
  const token = Utilities.getUuid() + Utilities.getUuid().replace(/-/g, '');
  PropertiesService.getScriptProperties().setProperty(TOKEN_PROPERTY, token);
  Logger.log('ROOM_SCENE_WEBHOOK_TOKEN=' + token);
  return token;
}

function currentWebhookToken() {
  const token = PropertiesService.getScriptProperties().getProperty(TOKEN_PROPERTY);
  if (!token) {
    throw new Error('Webhook token is not configured. Run setupWebhookToken() once.');
  }
  Logger.log('ROOM_SCENE_WEBHOOK_TOKEN=' + token);
  return token;
}

function doGet(e) {
  try {
    const action = (e && e.parameter && e.parameter.action) || 'health';
    const suppliedToken = (e && e.parameter && e.parameter.token) || '';
    requireToken_(suppliedToken);

    if (action !== 'health') {
      return json_({ok: false, error: 'Unsupported action'});
    }

    return json_({
      ok: true,
      service: 'GENROSE Room Scene Review Email',
      recipient: REVIEW_RECIPIENT
    });
  } catch (err) {
    return json_({ok: false, error: String(err && err.message ? err.message : err)});
  }
}

function doPost(e) {
  try {
    const payload = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    requireToken_(payload.token || '');

    // The recipient is intentionally fixed here. A caller cannot turn this
    // endpoint into an arbitrary mail relay by changing the POST body.
    const requestedRecipient = String(payload.to || REVIEW_RECIPIENT).trim().toLowerCase();
    if (requestedRecipient !== REVIEW_RECIPIENT.toLowerCase()) {
      throw new Error('Recipient is not allowed.');
    }

    const subject = String(payload.subject || 'GENROSE Room Scene Review').slice(0, 240);
    const textBody = String(payload.text || 'A room-scene review was submitted.');
    const htmlBody = String(payload.html || '').slice(0, 200000);

    MailApp.sendEmail({
      to: REVIEW_RECIPIENT,
      subject: subject,
      body: textBody,
      htmlBody: htmlBody || undefined,
      name: SENDER_NAME,
      replyTo: REVIEW_RECIPIENT
    });

    return json_({
      ok: true,
      recipient: REVIEW_RECIPIENT,
      batch_id: String(payload.batch_id || ''),
      sent_at: new Date().toISOString()
    });
  } catch (err) {
    return json_({ok: false, error: String(err && err.message ? err.message : err)});
  }
}

function requireToken_(suppliedToken) {
  const expected = PropertiesService.getScriptProperties().getProperty(TOKEN_PROPERTY);
  if (!expected) {
    throw new Error('Webhook token has not been configured. Run setupWebhookToken() once.');
  }
  if (!suppliedToken || suppliedToken !== expected) {
    throw new Error('Unauthorized webhook request.');
  }
}

function json_(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
