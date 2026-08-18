# GENROSE Room Scene Analyzer v0.8.0

## Daily workflow
1. Drop a batch of room-scene images.
2. Click **ANALYZE**.
3. Review the automated matches.
4. Click **CREATE REVIEW LINK**.
5. Send the link to the reviewer.

## Matching hierarchy
1. Original filename first.
2. Explicit English/Italian room vocabulary.
3. Fuzzy material match against the built-in Stone/SKU master.
4. GENROSE website verification.
5. Google Cloud Vision Label Detection + Web Detection + OCR.
6. Google Vision Product Search visual similarity as a fallback.

Cloud Vision is intentionally called on every analyzed image. Product Search is only
used as a material fallback/confirmation because full room scenes can contain a lot
of visual noise.

## Naming
`SKU-StoneType-RoomType`

Example:
`QUSACBE-AcquaBella-Kitchen.jpg`

## Setup
See `DEPLOYMENT_GUIDE.md`.


## v0.8.0 critical fix
A Google Cloud Vision 403/API-disabled error no longer wipes out the filename parser.
Filename material matching and English/Italian room detection always run first and
remain visible even if Google Cloud fails. Cloud Vision is now enrichment/fallback,
not a single point of failure.


## v0.8.0 UI pass
Redesigned the daily interface: collapsed admin sidebar, cleaner dark workspace, compact summary cards, cleaner queue/preview/match layout, and reduced diagnostic clutter.


## v0.8.0 visual redesign
Full readability/design pass: consistent dark cards, readable queue items, scrollable review workspace, high-contrast controls, polished evidence presentation, and diagnostics kept out of the primary workflow.


## v0.8.0 — dedicated room classifier
Room detection is no longer just keyword matching over generic Vision labels.
It now combines Google Vision Label Detection, Web Detection best guesses/entities,
Object Localization, OCR, filename English/Italian terms, and weighted scene rules.

New supported spaces include Reception, Lobby, ConferenceRoom, Showroom, Retail,
Restaurant, and Hallway. Reception can be inferred from combinations such as
desk/counter + commercial/lobby/waiting cues even when Google never literally
returns "reception desk".


## v0.8.0 — conservative slab identification
Material matching is now intentionally conservative:
- filename is the primary material source
- GENROSE website cache verifies canonical material/SKU
- Google Product Search is the only visual material fallback
- generic Google Vision labels/web/OCR can support a candidate but cannot create one
- results below 60% become Needs Review instead of inventing a material such as Red
- Google Vision remains fully active for room/space classification


## v0.8.0 — crash fix + GENROSE visual redesign
- Fixed the Results-count TypeError caused by summing truthy SKU/material strings.
- Full repository package is safe to upload as a replacement.
- Restyled to echo the GENROSE website: white editorial header, blush navigation band,
  charcoal section bars, neutral stone/taupe accents, squared cards and image-forward layout.
- Review links use the same visual language.
- The top Download Analysis CSV control is now a real download instead of a dead button.


## v0.8.0 — manual correction + durable review links
- Added a searchable Correct Material selector directly in the main Match panel.
- Selecting a material automatically pulls its canonical base SKU and immediately
  rebuilds the `SKU-StoneType-RoomType` filename.
- Added a room-type correction selector in the same panel.
- Manually confirmed materials are clearly labeled and count as ready.
- Review links now use `st.context.url`, the actual URL of the running app, instead
  of depending on a potentially stale `APP_BASE_URL` secret.
- Existing review-page material/room overrides remain available to the reviewer.


## v0.8.0 — correction-first workflow
- Original filename is shown first in the Match panel and first on every review item.
- Output filename is a real editable text field; edit the entire filename directly.
- Manual filenames survive Streamlit reruns and CSV/review-link creation.
- Room-type candidate suggestions now have one-click USE buttons.
- Alternate material suggestions now have one-click USE buttons.
- Full searchable canonical material selector remains available.
- Added custom material + base SKU entry for items outside the 403-name master.
- Review page was rebuilt with editable material, room and final filename fields.
- Review links include an in-app relative test link and a clear Streamlit-sharing warning.
- Review batch payload now carries room/material candidates for reviewer context.


## v0.8.0 — spreadsheet-driven GENROSE slab visual references
- Bundled `data/genrose_reference_catalog.json`, generated from `ProductExport_08-17-2026.xlsx`.
- Uses the spreadsheet's Color Name / Material / Tile Type Image data as the source
  of exact slab-image filenames.
- Builds direct GENROSE asset URLs using the website's
  `/Customer-Content/www/Products/TileTypes/<product-line-slug>/<image-file>` structure.
- Direct exported slab images are tried before fragile page scraping.
- Caps Product Search at 2 successful reference images per material.
- Product Search becomes the visual fallback only when filename material evidence is weak.
- Very weak filenames trigger whole-image + center + lower-room crop searches and merge
  the best SKU scores.
- Removed the decorative PRODUCTS / CUSTOM CAPABILITIES / INSPIRATION / RESOURCES / LOCATIONS strip.


## v0.8.0 — UX rebuild + immediate reference matching
- Sync / Refresh References is now a large main-screen action with mapped/downloaded/indexed status.
- Reference sync creates immediate local color/texture signatures from the GENROSE slab images.
- Weak filenames are compared against those synced references immediately; no Product Search indexing wait is required.
- Filename matching now gives more weight to rare distinctive tokens, making partial names more useful.
- The correction rail is reorganized as Original → Material → Material alternatives → Room → Room alternatives → Output filename.
- Material alternatives appear directly under the material selector and include the GENROSE swatch, SKU, confidence, filename evidence and immediate-reference evidence.
- Room alternatives appear directly below the room selector and are one-click selectable.
- Duplicated alternate/diagnostic sections were removed from the bottom of the rail.
- Hidden sidebar is simplified to batch clearing and connection status only.
