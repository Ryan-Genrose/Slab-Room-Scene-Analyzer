# GENROSE Room Scene Analyzer — Email Notification Setup

v0.9.8 uses **Google Apps Script + MailApp** as the preferred production email path. There is no third-party mail account to maintain. The review submission is still saved to Google Cloud Storage first; the email is a notification layer on top of that durable record.

## What this gives you

- Every submitted review is saved to the existing `slab-room-scenes` Google Cloud Storage bucket first.
- The app then emails `marketing@genrose.com` automatically.
- Email delivery is recorded as `review_batches/<batch-id>/notification.json`.
- The Analyzer blocks creation of **new** review links unless the email webhook passes a live health check.
- The Analyzer has a **TEST EMAIL** button.
- The new **Review Inbox + Email Delivery** panel can find old submitted batches and send/retry notifications without asking the reviewer to submit again.
- If email fails after a reviewer submits, the review itself remains safe in Google Cloud Storage.

## One-time setup — about 3 minutes

Do this while signed into the **GENROSE Google account you want the email to come from**. If the web app is deployed by `marketing@genrose.com`, the mail is sent by that account. If you deploy it from your own GENROSE Google account, the sender will be your account, with Reply-To set to `marketing@genrose.com`.

### 1. Create the Google Apps Script webhook

1. Open `https://script.new`.
2. Name it `GENROSE Room Scene Review Email`.
3. Delete the starter code.
4. Paste the contents of `email_webhook/Code.gs` from this package.
5. Save.

### 2. Generate the shared webhook token

1. In the function dropdown at the top, select `setupWebhookToken`.
2. Click **Run**.
3. Google will ask you to authorize the script to send mail. Approve it.
4. Open **Execution log** at the bottom.
5. Copy the value after:

   `ROOM_SCENE_WEBHOOK_TOKEN=`

Keep that value private. Do not commit it to GitHub.

### 3. Deploy the script as a Web App

1. Click **Deploy → New deployment**.
2. Select **Web app**.
3. Description: `Room Scene Review Email`.
4. **Execute as:** `Me`.
5. **Who has access:** `Anyone`.
6. Click **Deploy**.
7. Copy the Web App URL ending in `/exec`.

The Streamlit app calls this URL from the server. The webhook also requires the private token generated above.

### 4. Add three values to Streamlit Secrets

Open the deployed Room Scene Analyzer in Streamlit Community Cloud, then open its **Secrets** editor and add:

```toml
REVIEW_EMAIL = "marketing@genrose.com"
REVIEW_EMAIL_WEBHOOK = "https://script.google.com/macros/s/YOUR_DEPLOYMENT_ID/exec"
REVIEW_EMAIL_WEBHOOK_TOKEN = "PASTE_THE_TOKEN_FROM_SETUPWEBHOOKTOKEN_HERE"
```

Leave your existing Google Cloud secrets exactly as they are.

Save the Secrets. Streamlit will restart the app.

### 5. Prove it works before sending another review link

On the Analyzer screen:

1. The review area should say **Google Apps Script configured → marketing@genrose.com**.
2. Click **TEST EMAIL**.
3. Confirm the test message arrives at `marketing@genrose.com`.
4. Only after the test succeeds, create a new review link.

v0.9.8 also performs a live webhook health check whenever **CREATE REVIEW LINK** is pressed. If email delivery is not configured correctly, the app refuses to create the link instead of silently pretending notifications work.

## Recovering reviews that were already submitted

Open **Review Inbox + Email Delivery** on the Analyzer page and click **REFRESH REVIEW INBOX**.

It will show batches as:

- `AWAITING REVIEW`
- `IN PROGRESS`
- `SUBMITTED`

Select any previously submitted batch. If it has no successful email-delivery record, click **SEND / RETRY EMAIL**. The original `submission.json` is used; Cyndi does **not** need to review or submit it again.

## What gets stored in Google Cloud

For a submitted batch:

```text
review_batches/<batch-id>/
├── review.json
├── draft.json
├── submission.json
├── notification.json
└── images/
```

`notification.json` contains the delivery state (`SENT`, `FAILED`, or `NOT_CONFIGURED`), provider, recipient, number of attempts, timestamp, and last error if one occurred.

## Important

Do **not** put the Apps Script token into GitHub or the checked-in `secrets.toml.example`. It belongs only in:

- Google Apps Script **Script Properties** (created by `setupWebhookToken()`), and
- Streamlit **Secrets** as `REVIEW_EMAIL_WEBHOOK_TOKEN`.
