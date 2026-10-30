"""Run one demo: a no-edit round trip (Wolf vs LibreOffice) and a replace-text edit
(Wolf vs the common Python library for the format), then diff every output part by part."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import baselines, inventory, verify

FORMATS = ("xlsx", "docx", "pptx")
TIMEOUT_S = int(os.environ.get("DEMO_TIMEOUT_S", "90"))
CLOCK = "2026-01-01T00:00:00Z"
SEED = "0"
APP_ROOT = Path(__file__).resolve().parent.parent
PREVIEW_PAGES = int(os.environ.get("PREVIEW_PAGES", "4"))
PREVIEW_DPI = os.environ.get("PREVIEW_DPI", "96")
PREVIEW_WORKERS = int(os.environ.get("PREVIEW_WORKERS", "3"))


@dataclass(frozen=True)
class Tools:
    wolf: Path
    soffice: Path

    @classmethod
    def from_env(cls) -> Tools:
        wolf = Path(os.environ.get("WOLF_BIN", APP_ROOT / "bin" / "wolf"))
        soffice = Path(os.environ.get("SOFFICE_BIN") or shutil.which("soffice") or "soffice")
        return cls(wolf=wolf, soffice=soffice)


def run(argv: list[str], env: dict | None = None, cwd: Path | None = None) -> tuple[int | None, str, str, float]:
    """Run in a fresh session; kill the whole process group on timeout (soffice forks)."""
    started = time.monotonic()
    with subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=env,
        cwd=cwd,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            return None, "", f"timeout after {TIMEOUT_S}s", time.monotonic() - started
    return process.returncode, stdout, stderr, time.monotonic() - started


def first_line(argv: list[str]) -> str:
    code, out, err, _ = run(argv)
    text = (out or err).strip()
    return text.splitlines()[0] if text else f"exit {code}"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tool_identity(tools: Tools) -> dict:
    return {
        "wolf": first_line([str(tools.wolf), "--version"]),
        "wolf_binary_bytes": tools.wolf.stat().st_size,
        "libreoffice": first_line([str(tools.soffice), "--version"]),
    }


def wolf_detail(stdout: str, stderr: str) -> str:
    """Wolf prints a JSON receipt; surface its errors and warnings, not the whole receipt."""
    for text in (stdout, stderr):
        try:
            receipt = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(receipt, dict):
            notes = [
                f"{item.get('code', 'note')}: {item.get('message', '')}"
                for key in ("errors", "warnings")
                for item in receipt.get(key) or []
                if isinstance(item, dict)
            ]
            return "; ".join(notes)[:600] or receipt.get("status", "")
    return (stderr or stdout).strip()[:600]


def arm_result(name, label, argv, code, stdout, stderr, seconds, output: Path, ok_codes, detail=None):
    saved = code is not None and code in ok_codes and output.is_file()
    return {
        "arm": name,
        "label": label,
        "command": argv,
        "exit": code,
        "seconds": round(seconds, 2),
        "saved": saved,
        "detail": detail if detail is not None else (stderr or stdout).strip()[:600],
        "output": output if saved else None,
    }


def wolf_roundtrip(tools: Tools, source: Path, work: Path) -> dict:
    fmt = source.suffix.lstrip(".")
    outdir = work / "wolf-roundtrip"
    argv = [str(tools.wolf), "convert", str(source), "--to", fmt, "--outdir", str(outdir), "--clock", CLOCK, "--seed", SEED]
    code, out, err, seconds = run(argv)
    # Wolf exits 1 for a partial result that still wrote its output.
    return arm_result("wolf", "Wolf", argv, code, out, err, seconds, outdir / source.name, (0, 1), wolf_detail(out, err))


def libreoffice_roundtrip(tools: Tools, source: Path, work: Path) -> dict:
    fmt = source.suffix.lstrip(".")
    outdir = work / "libreoffice-roundtrip"
    profile = work / "lo-profile"
    argv = [
        str(tools.soffice),
        "--headless",
        "--norestore",
        f"-env:UserInstallation=file://{profile}",
        "--convert-to",
        fmt,
        "--outdir",
        str(outdir),
        str(source),
    ]
    env = dict(os.environ, HOME=str(work))
    code, out, err, seconds = run(argv, env=env)
    # soffice prints fontconfig chatter on stderr even when it succeeds; keep it for failures only.
    detail = "" if code == 0 else None
    return arm_result("libreoffice", "LibreOffice", argv, code, out, err, seconds, outdir / source.name, (0,), detail)


def wolf_edit(tools: Tools, source: Path, work: Path, find: str, replace: str) -> dict:
    fmt = source.suffix.lstrip(".")
    outdir = work / "wolf-edit"
    ops = work / "wolf-ops.json"
    if fmt == "xlsx":
        ops.write_text(json.dumps([{"op": "replace-text", "target": "workbook", "find": find, "replace": replace}]))
        argv = [str(tools.wolf), "transact", str(source), "--sha256", sha256(source), "--ops", str(ops)]
    else:
        ops.write_text(json.dumps([{"op": "replace-text", "find": find, "replace": replace, "occurrence": "all"}]))
        argv = [str(tools.wolf), "edit", str(source), "--edits", str(ops)]
    argv += ["--outdir", str(outdir), "--clock", CLOCK, "--seed", SEED]
    code, out, err, seconds = run(argv)
    result = arm_result("wolf", "Wolf", argv, code, out, err, seconds, outdir / source.name, (0, 1), wolf_detail(out, err))
    result["ops"] = json.loads(ops.read_text())
    return result


def library_edit(source: Path, work: Path, find: str, replace: str) -> dict:
    outdir = work / "library-edit"
    outdir.mkdir()
    output = outdir / source.name
    argv = [sys.executable, "-m", "app.baselines", str(source), str(output), find, replace]
    code, out, err, seconds = run(argv, cwd=APP_ROOT)
    label = {"xlsx": "openpyxl", "docx": "python-docx", "pptx": "python-pptx"}[source.suffix.lstrip(".")]
    detail = (err or "").strip().splitlines()[-1:] if code else []
    if code == 0:
        try:
            info = json.loads(out.strip().splitlines()[-1])
            label = info["library"]
            detail = [f"{info['replacements']} run or cell replacements"]
        except (json.JSONDecodeError, IndexError, KeyError):
            pass
    shown = ["python3", "baselines.py", source.name, f"library-edit/{source.name}", find, replace]
    result = arm_result("library", label, shown, code, out, err, seconds, output, (0,), "; ".join(detail))
    result["code"] = library_code(source.suffix.lstrip(".").lower())
    return result


def library_code(fmt: str) -> str:
    """The baselines.py functions that ran for this format, as shipped."""
    functions = [] if fmt == "xlsx" else [baselines._replace_runs]
    functions.append(baselines.EDITORS[fmt])
    return "\n\n".join(inspect.getsource(f).rstrip() for f in functions)


def render_pages(tools: Tools, document: Path, work: Path, key: str) -> list[str]:
    """Draw the first pages of `document` as PNG. Every file in a run, original included, goes
    through this one renderer, so a visible difference comes from the file, not the viewer.
    Returns file names below work/previews in page order; empty when the file does not render."""
    previews = work / "previews"
    scratch = previews / f"render-{key}"
    argv = [str(tools.wolf), "render", str(document), "--to", "png", "--outdir", str(scratch),
            "--pages", f"1-{PREVIEW_PAGES}", "--dpi", PREVIEW_DPI]
    if document.suffix.lower() == ".docx":
        argv += ["--comments", "balloons"]
    run(argv)
    pages = []
    for png in scratch.glob(f"{document.stem}-*.png"):
        number = png.stem.rpartition("-")[2]
        if number.isdigit():
            pages.append((int(number), png))
    names = []
    for number, png in sorted(pages):
        target = previews / f"{key}-{number}.png"
        png.replace(target)
        names.append(target.name)
    shutil.rmtree(scratch, ignore_errors=True)
    return names


def render_previews(tools: Tools, source: Path, work: Path, arms: list[dict]) -> list[str]:
    """Render the original and every saved output; set each arm's `preview`. Returns the original's pages."""
    (work / "previews").mkdir()
    jobs = {"original": source}
    for arm in arms:
        if arm["output"] is not None:
            jobs[arm["output"].parent.name] = arm["output"]
    with ThreadPoolExecutor(PREVIEW_WORKERS) as pool:
        rendered = dict(zip(jobs, pool.map(lambda key: render_pages(tools, jobs[key], work, key), jobs)))
    for arm in arms:
        arm["preview"] = rendered.get(arm["output"].parent.name, []) if arm["output"] is not None else []
    return rendered["original"]


def score(arm: dict, compare, source_bytes: bytes, fmt: str) -> dict:
    output = arm.pop("output")
    if output is None:
        arm["diff"] = None
        return arm
    try:
        arm["diff"] = compare(output)
        arm["inventory"] = inventory.inventory(verify.read_parts(output), fmt)
    except verify.PackageError as error:
        arm["saved"] = False
        arm["detail"] = f"output is not a readable package: {error}"
        arm["diff"] = None
        return arm
    arm["output_bytes"] = output.stat().st_size
    arm["output_file"] = output.relative_to(output.parent.parent).as_posix()
    arm["byte_identical_file"] = output.read_bytes() == source_bytes
    return arm


def demo(tools: Tools, source: Path, work: Path, find: str, replace: str) -> dict:
    """`source` sits in `work`; every output lands below `work`."""
    fmt = source.suffix.lstrip(".").lower()
    if fmt not in FORMATS:
        raise verify.PackageError(f"unsupported format .{fmt}; use .xlsx, .docx or .pptx")
    source_parts = verify.read_parts(source)  # size and ZIP checks before any tool runs
    source_bytes = source.read_bytes()

    roundtrip = [wolf_roundtrip(tools, source, work), libreoffice_roundtrip(tools, source, work)]
    edit = None
    if find:
        edit = [wolf_edit(tools, source, work, find, replace), library_edit(source, work, find, replace)]
    previews = render_previews(tools, source, work, roundtrip + (edit or []))

    for arm in roundtrip:
        score(arm, lambda out: verify.verify_roundtrip(source, out), source_bytes, fmt)
    if edit:
        for arm in edit:
            score(arm, lambda out: verify.verify_edit(source, out, find, replace), source_bytes, fmt)

    return {
        "file": source.name,
        "format": fmt,
        "bytes": len(source_bytes),
        "sha256": hashlib.sha256(source_bytes).hexdigest(),
        "parts": len(source_parts),
        "inventory": inventory.inventory(source_parts, fmt),
        "previews": previews,
        "roundtrip": roundtrip,
        "edit": {"find": find, "replace": replace, "arms": edit} if edit else None,
    }
