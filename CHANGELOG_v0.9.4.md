# GENROSE Room Scene Analyzer — v0.9.4

## Added

- **Batch-level reviewer note on Analyzer**
  - New Note for reviewer field sits directly above CREATE REVIEW LINK.
  - The note is stored in `review.json`, shown at the top of the Review Page, preserved in autosaved drafts and final submissions, and included in the review email.
  - Existing per-image Analyst Notes remain unchanged and still appear with their individual scenes.

- **Manual material thumbnails**
  - When the currently selected material has no existing thumbnail/reference, the Analyzer exposes an image uploader.
  - Accepts JPG, JPEG, PNG and WebP.
  - Images are EXIF-corrected, normalized to a max 1200 × 1200 JPEG, and stored by SKU.
  - Uses Google Cloud Storage when configured; local `.runtime_data/material_thumbnails/` fallback remains available for development.
  - The uploaded thumbnail automatically appears in the Analyzer, suggested-material cards, and Review Page.
  - Manual thumbnails are intentionally display-only and do not alter analyzer similarity scoring or the strict slab signature library.

## Changed

- **Approval defaults are now explicit**
  - Every fresh Review Page item starts with Approved unchecked.
  - Restored drafts and already-submitted reviews retain the previously saved checked/unchecked state.

- Review reference messaging now uses `material thumbnail` terminology so manually supplied references and strict GENROSE slab references share one clear UI concept.

## Compatibility

- No new Python packages required.
- No new Streamlit secrets required.
- Existing v0.9.3 Google Cloud / Vision / Product Search configuration remains compatible.
