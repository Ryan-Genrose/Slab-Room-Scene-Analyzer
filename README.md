# GENROSE Room Scene Analyzer v0.5.0

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
