# GENROSE Room Scene Analyzer v0.9.3

## Review workflow fixes

- Fixed `StreamlitAPIException` caused by changing filename widget state after the widget had already been instantiated.
- Reset buttons now use Streamlit callbacks and safely restore the current generated filename.
- Material selection on the review page now immediately drives the canonical material name, SKU, GENROSE slab reference, product-page link, and AUTO filename.
- Suggested material candidates on the review page are selectable with `USE` buttons.
- Room candidate display remains informational on the review page as requested.

## Notes

- Added `Analyst Note` to the Analyzer correction workspace.
- Analyst notes travel with the review batch and are displayed read-only to the reviewer.
- Added a separate multiline `Reviewer Note` field.
- Both note fields are included in review submissions and CSV exports.

## Filename behavior

- Added explicit AUTO/MANUAL behavior.
- In AUTO mode, material/SKU/room changes regenerate the output filename.
- Directly editing the filename switches that item to MANUAL mode.
- MANUAL filenames survive material/room corrections.
- `RESET NAME` / `RESET GENERATED NAME` returns the filename to AUTO mode.

## Review durability

- Review choices are autosaved to `draft.json` in the same review-batch storage location.
- Drafts are restored after refresh/reopen.
- Review progress shows total scenes, approved, changed, and needs-input counts.
- Completed submissions can be applied back to the currently loaded Analyzer batch.
- Completed review data can be downloaded as CSV from the Analyzer.

## Export safety

- Added production preflight for unresolved materials, missing SKUs, duplicate filenames, and unresolved decision states.
- Added renamed-image ZIP export after preflight passes.
- Original image bytes are preserved; the ZIP contains renamed copies only.

## Additional crash prevention

The same unsafe widget-state mutation pattern existed in several Analyzer controls. Candidate-material selection, room-candidate selection, and needs-input actions now use safe callback/state transitions as well.

## Validation performed

- Python source successfully passes `python -m py_compile app.py`.
- No new Python dependencies were added.
- Existing Google Cloud / Streamlit secrets configuration remains compatible.
