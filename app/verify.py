#!/usr/bin/env python3
"""Part-level diff of an Office file (XLSX, DOCX, PPTX) against the file it was saved from.

Standard library only, so anyone can check a demo output on their own machine:

    python3 verify.py roundtrip original.docx saved.docx
    python3 verify.py edit original.docx edited.docx --find "30 days" --replace "45 days"

An Office file is a ZIP package of parts. Each part of the saved file is compared with the same
part of the original (decompressed bytes). A part that changed, appeared, or disappeared is
classified as:

- related: the operation had a reason to write it. For a round trip (open and save, no edit)
  nothing is related, except that an XLSX save recalculates, so formula cells and
  xl/calcChain.xml may change. For an edit (replace one literal text), parts of the original that
  contain the find text are related.
- rewritten without content change: an XML part that canonicalizes (C14N 2.0) to the same
  document; a content-type or relationship part with the same entries in another order; or a
  worksheet whose cell values are all unchanged (shared strings are resolved to their text, so a
  renumbered shared-string table compares equal).
- unrelated content change: anything else. A worksheet with any changed cell outside the
  expected cells (formula cells for a round trip, cells holding the find text for an edit)
  counts here.

Exit status 0 means no unrelated content change.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import PurePosixPath
from xml.sax.saxutils import escape

MAX_PARTS = 5_000
MAX_EXPANDED_BYTES = 400 * 1024 * 1024
MAX_C14N_BYTES = 16 * 1024 * 1024

CELL = re.compile(rb"<(?:\w+:)?c\b[^>]*?(?:/>|>.*?</(?:\w+:)?c>)", re.DOTALL)
WORKSHEET_REL = "/relationships/worksheet"


class PackageError(ValueError):
    """The file is not a readable OOXML package within the size limits."""


def read_parts(path) -> dict[str, bytes]:
    try:
        package = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as error:
        raise PackageError(f"not a ZIP package: {error}") from error
    with package:
        infos = [info for info in package.infolist() if not info.is_dir()]
        if len(infos) > MAX_PARTS:
            raise PackageError(f"{len(infos)} parts exceeds the limit of {MAX_PARTS}")
        expanded = sum(info.file_size for info in infos)
        if expanded > MAX_EXPANDED_BYTES:
            raise PackageError(
                f"{expanded} expanded bytes exceeds the limit of {MAX_EXPANDED_BYTES}"
            )
        try:
            return {info.filename: package.read(info) for info in infos}
        except (zipfile.BadZipFile, OSError, RuntimeError) as error:
            raise PackageError(f"unreadable part: {error}") from error


def worksheet_parts(parts: dict[str, bytes]) -> set[str]:
    rels = parts.get("xl/_rels/workbook.xml.rels", b"")
    targets = set()
    for match in re.finditer(rb"<(?:\w+:)?Relationship\b[^>]*>", rels):
        tag = match.group(0)
        kind = re.search(rb'Type="([^"]*)"', tag)
        target = re.search(rb'Target="([^"]*)"', tag)
        if not kind or not target or not kind.group(1).endswith(WORKSHEET_REL.encode()):
            continue
        value = target.group(1).decode()
        if value.startswith("/"):
            targets.add(value.lstrip("/"))
            continue
        resolved: list[str] = []
        for segment in PurePosixPath("xl", value).parts:
            if segment == "..":
                if resolved:
                    resolved.pop()
            elif segment != ".":
                resolved.append(segment)
        targets.add("/".join(resolved))
    return targets


def edit_related(parts: dict[str, bytes], find: str) -> set[str]:
    needles = {find.encode(), escape(find).encode(), escape(find, {'"': "&quot;"}).encode()}
    return {name for name, data in parts.items() if any(n in data for n in needles)}


SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
ATTR = re.compile(rb'\b(r|s|t)="([^"]*)"')
VALUE = re.compile(rb"<(?:\w+:)?v>(.*?)</(?:\w+:)?v>", re.DOTALL)
FORMULA_TEXT = re.compile(rb"<(?:\w+:)?f\b[^>]*?(?:/>|>(.*?)</(?:\w+:)?f>)", re.DOTALL)
INLINE_TEXT = re.compile(rb"<(?:\w+:)?t\b[^>]*?(?:/>|>(.*?)</(?:\w+:)?t>)", re.DOTALL)


def shared_strings(parts: dict[str, bytes]) -> list[str]:
    data = parts.get("xl/sharedStrings.xml")
    if not data:
        return []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    strings = []
    for item in root.iter(f"{SHEET_NS}si"):
        texts = [child.text or "" for child in item if child.tag == f"{SHEET_NS}t"]
        for run in item.iter(f"{SHEET_NS}r"):
            texts += [t.text or "" for t in run.iter(f"{SHEET_NS}t")]
        strings.append("".join(texts))
    return strings


def cell_values(sheet: bytes, strings: list[str]) -> dict[str, tuple]:
    """Each cell as (style, kind, formula, value), shared strings resolved to their text, so a
    renumbered shared-string table or an inline string with the same text compares equal."""
    cells = {}
    for position, match in enumerate(CELL.finditer(sheet)):
        cell = match.group(0)
        head = cell.split(b">", 1)[0]
        attrs = {k.decode(): v.decode() for k, v in ATTR.findall(head)}
        kind = attrs.get("t", "n")
        formula = FORMULA_TEXT.search(cell)
        value_match = VALUE.search(cell)
        value = html.unescape(value_match.group(1).decode("utf-8", "replace")) if value_match else ""
        if kind == "s" and value.strip().isdigit() and int(value) < len(strings):
            kind, value = "text", strings[int(value)]
        elif kind == "inlineStr":
            kind = "text"
            value = "".join(
                html.unescape((m.group(1) or b"").decode("utf-8", "replace"))
                for m in INLINE_TEXT.finditer(cell)
            )
        cells[attrs.get("r", f"#{position}")] = (
            attrs.get("s", "0"),
            kind,
            (formula.group(1) or b"").decode("utf-8", "replace") if formula else None,
            value,
        )
    return cells


def worksheet_change(
    before: bytes,
    after: bytes,
    strings_before: list[str],
    strings_after: list[str],
    may_change,
) -> tuple[str, str]:
    """Classify a changed worksheet: ("equivalent" | "related" | "unrelated", note)."""
    if CELL.sub(b"C", before) != CELL.sub(b"C", after):
        return "unrelated", "sheet structure outside cells changed"
    old, new = cell_values(before, strings_before), cell_values(after, strings_after)
    changed = [ref for ref in old.keys() | new.keys() if old.get(ref) != new.get(ref)]
    if not changed:
        return "equivalent", "cell values unchanged (shared-string indexes renumbered)"
    outside = [ref for ref in changed if ref not in old or not may_change(old[ref])]
    if outside:
        return "unrelated", f"{len(outside)} cell(s) changed outside the expected cells, e.g. {min(outside)}"
    return "related", f"{len(changed)} expected cell(s) changed"


def compare(
    source: dict[str, bytes],
    output: dict[str, bytes],
    related: set[str],
    may_change_cell=None,
) -> dict:
    """Part-level diff. For XLSX, worksheets are judged cell by cell: a cell may change when
    `may_change_cell(cell)` holds; any other changed cell makes the worksheet unrelated."""
    sheets = worksheet_parts(source) if "xl/workbook.xml" in source else set()
    strings_before = shared_strings(source) if sheets else []
    strings_after = shared_strings(output) if sheets else []
    rows = []
    for name in sorted(source.keys() | output.keys()):
        before, after = source.get(name), output.get(name)
        if before == after:
            continue
        status = "added" if before is None else "removed" if after is None else "changed"
        is_related, equivalent, note = name in related, None, ""
        if status == "changed" and name in sheets and may_change_cell is not None:
            verdict, note = worksheet_change(
                before, after, strings_before, strings_after, may_change_cell
            )
            is_related = verdict == "related"
            equivalent = note if verdict == "equivalent" else None
        elif status == "changed" and not is_related:
            equivalent = equivalence(name, before, after)
        rows.append(
            {
                "part": name,
                "status": status,
                "related": is_related,
                "equivalent": equivalent,
                "before_bytes": None if before is None else len(before),
                "after_bytes": None if after is None else len(after),
                "note": note,
            }
        )
    unrelated = [row for row in rows if not row["related"] and not row["equivalent"]]
    return {
        "source_parts": len(source),
        "output_parts": len(output),
        "identical_parts": sum(1 for n in source if output.get(n) == source[n]),
        "touched_parts": len(rows),
        "related_parts_changed": sum(1 for row in rows if row["related"]),
        "rewritten_without_content_change": sum(1 for row in rows if row["equivalent"]),
        "unrelated_content_changes": len(unrelated),
        "unrelated_removed": sum(1 for row in unrelated if row["status"] == "removed"),
        "unrelated_added": sum(1 for row in unrelated if row["status"] == "added"),
        "parts": rows,
    }


def equivalence(name: str, before: bytes, after: bytes) -> str | None:
    """Why a changed part holds the same content, or None."""
    if name == "[Content_Types].xml" or name.endswith(".rels"):
        if same_entries(before, after):
            return "same entries in a different order (order carries no meaning here)"
        return None
    if same_xml(before, after):
        return "same XML after canonicalization (bytes rewritten only)"
    return None


def same_entries(before: bytes, after: bytes) -> bool:
    """Content-type and relationship parts are unordered sets of entries (ECMA-376 Part 2)."""
    try:
        old, new = ET.fromstring(before), ET.fromstring(after)
    except ET.ParseError:
        return False

    def entries(root):
        return sorted((child.tag, sorted(child.attrib.items())) for child in root)

    return old.tag == new.tag and entries(old) == entries(new)


def same_xml(before: bytes, after: bytes) -> bool:
    """Whether two XML parts are the same document once serialized canonically (C14N 2.0):
    a part rewritten with different quoting, attribute order, or declarations, but no
    different elements, attributes, or text."""
    if max(len(before), len(after)) > MAX_C14N_BYTES:
        return False
    try:
        return ET.canonicalize(before.decode("utf-8")) == ET.canonicalize(after.decode("utf-8"))
    except (UnicodeDecodeError, ET.ParseError, ValueError):
        return False


def count_text(parts: dict[str, bytes], text: str) -> int:
    needles = {text.encode(), escape(text).encode()}
    return sum(max(data.count(n) for n in needles) for data in parts.values())


def verify_roundtrip(original, saved) -> dict:
    """An XLSX save recalculates, so formula cells (and xl/calcChain.xml) may change."""
    source, output = read_parts(original), read_parts(saved)
    related = {"xl/calcChain.xml"} if "xl/workbook.xml" in source else set()
    return compare(source, output, related, lambda cell: cell[2] is not None)


def verify_edit(original, edited, find: str, replace: str | None = None) -> dict:
    """Parts holding the find text may change; in a worksheet, only cells holding it."""
    source, output = read_parts(original), read_parts(edited)
    result = compare(source, output, edit_related(source, find), lambda cell: find in cell[3])
    result["find_occurrences_before"] = count_text(source, find)
    result["find_occurrences_after"] = count_text(output, find)
    if replace:
        result["replace_occurrences_after"] = count_text(output, replace)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    rt = sub.add_parser("roundtrip", help="open-and-save with no edit")
    rt.add_argument("original")
    rt.add_argument("saved")
    ed = sub.add_parser("edit", help="replace-text edit")
    ed.add_argument("original")
    ed.add_argument("edited")
    ed.add_argument("--find", required=True)
    ed.add_argument("--replace")
    args = parser.parse_args()
    if args.mode == "roundtrip":
        result = verify_roundtrip(args.original, args.saved)
    else:
        result = verify_edit(args.original, args.edited, args.find, args.replace)
    json.dump(result, sys.stdout, indent=2)
    print()
    return 0 if result["unrelated_content_changes"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
