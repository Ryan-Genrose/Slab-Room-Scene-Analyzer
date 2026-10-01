# GENROSE Room Scene Analyzer v0.9.7

## Critical production-review fix

- Fixed **APPLY SUBMITTED REVIEW** so only scenes explicitly approved by the reviewer become production-approved results.
- Reviewer-rejected scenes are preserved for audit/history, including reviewer notes, but are marked `REJECTED_BY_REVIEWER` and excluded from renamed-image ZIP export.
- Added a **Review Approval** column to the Analyzer export table (`APPROVED`, `REJECTED`, or `NOT REVIEWED`).
- Production preflight now runs only against scenes eligible for production export; rejected scenes no longer create blockers.
- After applying a review, the Analyzer reports how many scenes were approved and how many were rejected/excluded.

## Submission/email clarity

- Renamed the reviewer button from **SUBMIT REVIEW TO MARKETING** to **SUBMIT REVIEW**.
- The app now explicitly confirms that the review was saved to production storage before attempting email notification.
- If no email provider is configured, the app says so clearly instead of presenting the review as emailed.
- Email behavior itself is unchanged when a provider is configured.

## Compatibility

- No new Python packages are required.
- Existing `review.json`, `draft.json`, and `submission.json` objects remain compatible.
- Existing submitted reviews can be safely applied after deploying v0.9.7; approval flags already stored in those submissions will be honored.
