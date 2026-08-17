# GENROSE Room Scene Analyzer v0.6.2

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


## v0.6.2 critical fix
A Google Cloud Vision 403/API-disabled error no longer wipes out the filename parser.
Filename material matching and English/Italian room detection always run first and
remain visible even if Google Cloud fails. Cloud Vision is now enrichment/fallback,
not a single point of failure.


## v0.6.2 UI pass
Redesigned the daily interface: collapsed admin sidebar, cleaner dark workspace, compact summary cards, cleaner queue/preview/match layout, and reduced diagnostic clutter.


## v0.6.2 visual redesign
Full readability/design pass: consistent dark cards, readable queue items, scrollable review workspace, high-contrast controls, polished evidence presentation, and diagnostics kept out of the primary workflow.


## v0.6.2 — dedicated room classifier
Room detection is no longer just keyword matching over generic Vision labels.
It now combines Google Vision Label Detection, Web Detection best guesses/entities,
Object Localization, OCR, filename English/Italian terms, and weighted scene rules.

New supported spaces include Reception, Lobby, ConferenceRoom, Showroom, Retail,
Restaurant, and Hallway. Reception can be inferred from combinations such as
desk/counter + commercial/lobby/waiting cues even when Google never literally
returns "reception desk".


## v0.6.2 — conservative slab identification
Material matching is now intentionally conservative:
- filename is the primary material source
- GENROSE website cache verifies canonical material/SKU
- Google Product Search is the only visual material fallback
- generic Google Vision labels/web/OCR can support a candidate but cannot create one
- results below 60% become Needs Review instead of inventing a material such as Red
- Google Vision remains fully active for room/space classification
