# GENROSE Room Scene Analyzer v0.9.5

## Scene-specific reviewer notes

- Removed the batch-level **Note for reviewer** field from the Analyzer.
- Removed the batch-level analyst-note banner from the top of the Review Page.
- Renamed/clarified the Analyzer note field as **Note for reviewer · this scene**.
- Each note is persisted against its individual scene and carried into the matching review card.
- Added a Streamlit callback plus a final state sync so notes survive scene switching, material/room reruns, review-link creation, and CSV export.
- Moved **CREATE REVIEW LINK** below the scene correction/note panel.
- Review CSVs, draft autosave, final submission, and review emails continue to include each scene's Analyst Note separately.

## Review page

- Analyst notes now appear inside the corresponding scene card, directly beneath the original filename.
- Fresh **Approved** checkboxes remain **unchecked** by default. Restored drafts/submitted reviews preserve their saved approval state.

## Preserved from v0.9.4

- Manual thumbnail upload for materials without a reference thumbnail.
- Material/SKU/reference/filename synchronization.
- Clickable suggested material candidates.
- Review draft autosave and completed-review round trip.
- Production preflight and renamed-image ZIP export.

No new packages, secrets, or services are required.
