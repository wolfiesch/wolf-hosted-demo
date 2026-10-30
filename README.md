# wolf-hosted-demo

Hosted page that shows what survives a save. A visitor picks a sample or uploads an XLSX, DOCX or
PPTX. The server saves it twice and diffs every ZIP part against the original:

| Step | WolfSDK | Comparison |
|---|---|---|
| Open and save, no edit | `wolf convert --to <same format>` | LibreOffice `soffice --convert-to` |
| Replace one text | `wolf edit` (DOCX, PPTX), `wolf transact` on `workbook` (XLSX) | openpyxl, python-docx, python-pptx run or cell replace (`app/baselines.py`) |

`app/verify.py` (standard library only) does the classification and is served at `/verify.py`
so anyone can recheck a downloaded result.

The page also shows the first four pages of the original and of every result, all drawn by
`wolf render --to png` (WolfRender) at 96 dpi, and boxes what differs from the original in the
browser. `app/static/assets/pagediff.js` runs in Web Workers: it lines up rows of ink first, so
a row that grew does not mark everything below it, then lines up columns within each band of
rows, so a centered title that grew does not shift the text under it. It boxes each remaining
difference as changed content, changed color, or moved content. One renderer draws every column, so a boxed
area is a change in the file, not in the viewer. `PREVIEW_PAGES`, `PREVIEW_DPI` and
`PREVIEW_WORKERS` tune it. The Python-library card
shows the `app/baselines.py` functions that ran. Under each set of pages, `app/inventory.py`
(standard library only, served at `/inventory.py`) counts what a page cannot show, such as
comments, comment authors, tracked changes, custom XML parts and document properties, in the
original and in every result, so a dropped comment shows up even when the pages look the same.

## Run locally

```sh
uv sync
WOLF_BIN=bin/wolf uv run uvicorn app.main:app --port 8765 --no-access-log
```

`bin/` is gitignored. Put the `wolf` v0.2.17 release binary for your platform and its
`SHA256SUMS` there. WolfSDK is in private beta; email wolfgang@synthgl.com for access.
LibreOffice must be on `PATH` (or set `SOFFICE_BIN`).

## Deploy

`deploy/push.sh` copies the source to a Docker host over SSH and builds `wolf-demo:latest`
there; `deploy/push.sh --restart` also installs `deploy/wolf-demo.service` and restarts it. Set
`HOST`, `NETWORK` and optionally `DEST` in the environment or in a gitignored `deploy/.env` (see
the script header). The container listens on port 8080 on `NETWORK`; a reverse proxy on that
network serves the public route, caps request bodies at 25 MB, and sets `X-Forwarded-For` to the
real client so the per-IP rate limit works.

## Limits and data handling

20 MB uploads, 30 runs per hour per IP, two concurrent runs. The upload is deleted when the run
finishes; results stay downloadable for 15 minutes. Access logs are off.

## Samples

See `samples/samples.json` for provenance and licenses. The XLSX is UK government data under the
Open Government Licence v3.0; the others were authored for this demo.
