# Room Scene Analyzer v0.9.9

## Email notification hotfix

This release fixes two production issues found after a real review submission.

### Fixed Outlook forwarding rule mismatch
- TEST EMAIL used a subject beginning with `GENROSE Room Scene Review`.
- Real review submissions previously used `Room Scene Review Submitted ...`.
- An Outlook rule created to match the tested subject therefore did not catch real submissions.
- Real submissions now use the same stable prefix:
  `GENROSE Room Scene Review — Submitted — <reviewer> — <approved>/<total> approved`

### Fixed ugly Formspree email body
- Formspree was being sent both a plain-text `message` field and a raw `html` field.
- Formspree treated `html` as ordinary form data, so the full HTML table appeared literally in the notification email.
- Formspree now receives one clean plain-text `Review Summary` field only.
- Approved and not-approved scenes are grouped into separate readable sections.
- Each scene shows final material, room, filename, SKU, original filename, and notes without the giant raw HTML dump.

### Safer retry workflow
- A previously submitted review with no notification record now shows a direct `RETRY EMAIL NOTIFICATION` button on the review page.
- Retrying email uses the already-saved submission and does not require the reviewer to redo or alter the review.

### Clearer Formspree status
- The app no longer claims Formspree is sending directly to `marketing@genrose.com`.
- Formspree's actual recipient is correctly treated as something configured in the Formspree workflow.
- This matches the production setup where Formspree sends to the accessible work mailbox and Outlook forwards matching review notifications internally to the Marketing group.

## Deployment
Replace the existing `app.py` with the v0.9.9 file or deploy the complete v0.9.9 package.

No Streamlit Secrets changes are required. Keep the existing `FORMSPREE_ENDPOINT`.
