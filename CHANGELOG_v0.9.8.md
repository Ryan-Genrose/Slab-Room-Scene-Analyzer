# Room Scene Analyzer v0.9.8

Production notification hardening.

## Email delivery

- Added a preferred **Google Apps Script + MailApp** notification path.
- Added live webhook preflight before a new review link can be created.
- Added **TEST EMAIL** on the Analyzer page.
- New review links are blocked when email delivery is not configured or the webhook health check fails.
- Review submissions are still written to production storage **before** email is attempted.
- Added up to three bounded notification attempts.
- Added `notification.json` per batch with delivery status, provider, recipient, attempts, timestamp, and error details.
- Added **SEND EMAIL NOW / RESEND EMAIL** controls for the currently loaded submitted batch.

## Review recovery

- Added **Review Inbox + Email Delivery**.
- Manual refresh lists existing GCS review batches without touching scene images.
- Shows Awaiting Review / In Progress / Submitted status.
- Previously submitted batches can have email sent or retried without requiring the reviewer to resubmit.
- Review CSV can be downloaded directly from the inbox.

## Safety

- The Apps Script webhook only permits email to `marketing@genrose.com`.
- Webhook requests require a private shared token.
- The webhook URL/token stay in Streamlit Secrets and are never exposed in the review page.
- Existing Formspree, Resend, and SMTP code paths remain supported for compatibility, but Apps Script is now the recommended GENROSE production configuration.
