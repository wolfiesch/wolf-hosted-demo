"""Web front for the demo: pick a sample or upload a file, run it, show the part-level diff."""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
import re
import shlex
import shutil
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
)
from fastapi.staticfiles import StaticFiles

from . import runner, verify

ROOT = Path(__file__).resolve().parent
SAMPLES_DIR = Path(os.environ.get("SAMPLES_DIR", ROOT.parent / "samples"))
RUNS_DIR = Path(os.environ.get("RUNS_DIR", "/tmp/wolf-demo-runs"))
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "20")) * 1024 * 1024
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "2"))
QUEUE_WAIT_S = int(os.environ.get("QUEUE_WAIT_S", "60"))
OUTPUT_TTL_S = int(os.environ.get("OUTPUT_TTL_S", "900"))
RUNS_PER_HOUR = int(os.environ.get("RUNS_PER_HOUR", "30"))
MAX_TEXT = 200
OUTPUT_DIRS = ("wolf-roundtrip", "libreoffice-roundtrip", "wolf-edit", "library-edit", "previews")
RUN_ID = re.compile(r"^[0-9a-f]{32}$")

SAMPLES = json.loads((SAMPLES_DIR / "samples.json").read_text())

TOOLS = runner.Tools.from_env()
STATE: dict = {}
RECENT: dict[str, deque] = defaultdict(deque)


def install_bytes(soffice: Path) -> int:
    """Size of the LibreOffice installation directory the demo runs."""
    root = Path(os.environ.get("LIBREOFFICE_ROOT") or soffice.resolve().parent.parent)
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file() and not p.is_symlink())


async def janitor() -> None:
    while True:
        cutoff = time.time() - OUTPUT_TTL_S
        for run in RUNS_DIR.glob("*"):
            if run.is_dir() and run.stat().st_mtime < cutoff:
                shutil.rmtree(run, ignore_errors=True)
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(_: FastAPI):
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    identity = runner.tool_identity(TOOLS)
    identity["libreoffice_install_bytes"] = await anyio.to_thread.run_sync(install_bytes, TOOLS.soffice)
    STATE["identity"] = identity
    STATE["slots"] = asyncio.Semaphore(MAX_CONCURRENT)
    task = asyncio.create_task(janitor())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
# Fonts, the hero poster (copied from the wolfsdk.com site so the demo shares its look) and the
# page-diff worker.
# The slim image has no /etc/mime.types, so name the two asset types explicitly.
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("image/webp", ".webp")
app.mount("/assets", StaticFiles(directory=ROOT / "static" / "assets"), name="assets")


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "?")


def admit(ip: str) -> None:
    now = time.time()
    window = RECENT[ip]
    while window and window[0] < now - 3600:
        window.popleft()
    if len(window) >= RUNS_PER_HOUR:
        raise HTTPException(429, f"limit of {RUNS_PER_HOUR} runs per hour reached; try again later")
    window.append(now)


def public_command(argv: list[str], work: Path) -> str:
    """The command as a reader would type it: tool names, paths relative to the run directory."""
    replacements = {str(TOOLS.wolf): "wolf", str(TOOLS.soffice): "soffice"}
    out = []
    for arg in argv:
        arg = replacements.get(arg, arg)
        arg = arg.replace(f"file://{work}/", "file://./").replace(f"{work}/", "").replace(str(work), ".")
        out.append(arg)
    return shlex.join(out)


def scrub(result: dict, work: Path, run_id: str) -> dict:
    arms = list(result["roundtrip"]) + (result["edit"]["arms"] if result["edit"] else [])
    for arm in arms:
        arm["command"] = public_command(arm["command"], work)
        arm["detail"] = arm["detail"].replace(str(work) + "/", "").replace(str(work), ".")
        if arm.get("output_file"):
            arm["download"] = f"/api/runs/{run_id}/{arm['output_file']}"
        arm["preview"] = [f"/api/runs/{run_id}/previews/{name}" for name in arm.get("preview", [])]
    result["previews"] = [f"/api/runs/{run_id}/previews/{name}" for name in result["previews"]]
    result["run_id"] = run_id
    result["expires_in_s"] = OUTPUT_TTL_S
    return result


WORKER = '"/assets/pagediff.js"'


@app.get("/")
async def index():
    # The CDN caches /assets for hours, so the page asks for the worker by its content hash and a
    # deploy reaches visitors at once.
    static = ROOT / "static"
    page = (static / "index.html").read_text()
    if WORKER not in page:
        raise RuntimeError(f"index.html no longer loads the worker as {WORKER}")
    digest = hashlib.sha256((static / "assets" / "pagediff.js").read_bytes()).hexdigest()[:12]
    return HTMLResponse(page.replace(WORKER, f'"/assets/pagediff.js?v={digest}"'), headers={"Cache-Control": "no-cache"})


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/verify.py")
async def verify_source():
    return PlainTextResponse((ROOT / "verify.py").read_text())


@app.get("/baselines.py")
async def baselines_source():
    return PlainTextResponse((ROOT / "baselines.py").read_text())


@app.get("/inventory.py")
async def inventory_source():
    return PlainTextResponse((ROOT / "inventory.py").read_text())


@app.get("/api/info")
async def info():
    return {"tools": STATE["identity"], "samples": SAMPLES, "limits": {
        "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
        "output_ttl_minutes": OUTPUT_TTL_S // 60,
        "runs_per_hour": RUNS_PER_HOUR,
    }}


@app.get("/api/samples/{name}")
async def sample_file(name: str):
    if name not in {s["file"] for s in SAMPLES}:
        raise HTTPException(404, "no such sample")
    return FileResponse(SAMPLES_DIR / name, filename=name)


@app.get("/api/runs/{run_id}/{folder}/{name}")
async def download(run_id: str, folder: str, name: str):
    path = RUNS_DIR / run_id / folder / name
    if not RUN_ID.match(run_id) or folder not in OUTPUT_DIRS or "/" in name or name.startswith(".") or not path.is_file():
        raise HTTPException(404, "output not found or expired")
    if folder == "previews":
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=900"})
    return FileResponse(path, filename=name)


async def save_upload(upload: UploadFile, target: Path) -> None:
    written = 0
    with target.open("wb") as handle:
        while chunk := await upload.read(1024 * 1024):
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                raise HTTPException(413, f"file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
            handle.write(chunk)


def safe_name(filename: str) -> str:
    stem, dot, ext = Path(filename).name.rpartition(".")
    ext = ext.lower()
    if not dot or ext not in runner.FORMATS:
        raise HTTPException(400, "upload an .xlsx, .docx or .pptx file")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.")[:80] or "upload"
    return f"{stem}.{ext}"


@app.post("/api/run")
async def run_demo(
    request: Request,
    sample: str = Form(""),
    find: str = Form(""),
    replace: str = Form(""),
    file: UploadFile | None = File(None),
):
    if len(find) > MAX_TEXT or len(replace) > MAX_TEXT:
        raise HTTPException(400, f"find and replace are limited to {MAX_TEXT} characters")
    if "\x00" in find or "\x00" in replace:
        raise HTTPException(400, "find and replace must be plain text")
    admit(client_ip(request))

    run_id = uuid.uuid4().hex
    work = RUNS_DIR / run_id
    work.mkdir(parents=True)
    try:
        if file is not None and file.filename:
            source = work / safe_name(file.filename)
            await save_upload(file, source)
        else:
            match = next((s for s in SAMPLES if s["file"] == sample), None)
            if match is None:
                raise HTTPException(400, "pick a sample or upload a file")
            source = work / match["file"]
            shutil.copyfile(SAMPLES_DIR / match["file"], source)

        slots: asyncio.Semaphore = STATE["slots"]
        try:
            await asyncio.wait_for(slots.acquire(), timeout=QUEUE_WAIT_S)
        except TimeoutError:
            raise HTTPException(503, "the demo is busy; try again in a minute") from None
        try:
            result = await anyio.to_thread.run_sync(runner.demo, TOOLS, source, work, find, replace)
        finally:
            slots.release()
    except verify.PackageError as error:
        shutil.rmtree(work, ignore_errors=True)
        raise HTTPException(400, f"not a usable Office file: {error}") from None
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    finally:
        if file is not None:
            await file.close()

    # The input is deleted now; only the outputs stay, for download, until the janitor runs.
    source.unlink(missing_ok=True)
    shutil.rmtree(work / "lo-profile", ignore_errors=True)
    return JSONResponse(scrub(result, work, run_id))
