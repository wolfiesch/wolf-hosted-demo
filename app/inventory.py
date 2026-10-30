"""Count what is inside an Office file that a page render does not show: comments and their
authors, tracked changes, custom XML, document properties, and the like. Standard library only.

    python3 inventory.py <file>

Prints one JSON line: [{"feature": "...", "count": n}, ...] for every feature the format can hold.
"""

from __future__ import annotations

import json
import re
import sys
from fnmatch import fnmatch
from pathlib import Path

if __package__:
    from .verify import read_parts
else:  # run as a script next to verify.py
    from verify import read_parts


def element(name: str) -> re.Pattern:
    """An XML start tag with this local name, any namespace prefix."""
    return re.compile(rb"<(?:[\w.-]+:)?" + name.encode() + rb"(?=[\s/>])")


def count_parts(*globs: str):
    return lambda parts: sum(1 for name in parts if any(fnmatch(name, g) for g in globs))


def count_matches(pattern: re.Pattern | bytes, *globs: str):
    if isinstance(pattern, bytes):
        pattern = re.compile(pattern)
    return lambda parts: sum(
        len(pattern.findall(data)) for name, data in parts.items() if any(fnmatch(name, g) for g in globs)
    )


# Core properties with a value, such as title, author, created, modified.
CORE_VALUE = re.compile(rb"<(?:dc|cp|dcterms):\w+\b[^>/]*>[^<]+<")
# A footnote or endnote the author wrote, not the separator Word keeps in every file.
NOTE = re.compile(rb"<(?:[\w.-]+:)?(?:footnote|endnote)(?=[\s>])(?![^>]*\btype=)")

COMMON = [
    ("Custom XML parts", count_parts("customXml/item[0-9]*.xml")),
    ("Custom document properties", count_matches(element("property"), "docProps/custom.xml")),
    ("Document properties with a value", count_matches(CORE_VALUE, "docProps/core.xml")),
    ("Printer settings", count_parts("*/printerSettings/*")),
    ("Thumbnail", count_parts("docProps/thumbnail.*")),
]

FEATURES = {
    "docx": [
        ("Comments", count_matches(element("comment"), "word/comments.xml")),
        ("Comment replies", count_matches(rb"\bw15:paraIdParent=", "word/commentsExtended.xml")),
        ("Comment authors", count_matches(element("person"), "word/people.xml")),
        ("Tracked insertions", count_matches(element("ins"), "word/*.xml")),
        ("Tracked deletions", count_matches(element("del"), "word/*.xml")),
        ("Footnotes and endnotes", count_matches(NOTE, "word/footnotes.xml", "word/endnotes.xml")),
        ("Content controls", count_matches(element("sdt"), "word/*.xml")),
        ("Bookmarks", count_matches(element("bookmarkStart"), "word/*.xml")),
        ("List definitions", count_matches(element("abstractNum"), "word/numbering.xml")),
        ("Headers and footers", count_parts("word/header*.xml", "word/footer*.xml")),
        ("Images", count_parts("word/media/*")),
    ],
    "pptx": [
        ("Slides", count_parts("ppt/slides/slide*.xml")),
        ("Comments", count_matches(element("cm"), "ppt/comments/*.xml")),
        ("Comment replies", count_matches(element("reply"), "ppt/comments/*.xml")),
        ("Comment authors", lambda parts: count_matches(element("author"), "ppt/authors.xml")(parts)
            + count_matches(element("cmAuthor"), "ppt/commentAuthors.xml")(parts)),
        ("Speaker notes", count_parts("ppt/notesSlides/*.xml")),
        ("Charts", count_parts("ppt/charts/chart*.xml")),
        ("Chart data workbooks", count_parts("ppt/embeddings/*")),
        ("Tables", count_matches(element("tbl"), "ppt/slides/*.xml")),
        ("Extension lists", count_matches(element("extLst"), "ppt/*.xml")),
        ("Images", count_parts("ppt/media/*")),
    ],
    "xlsx": [
        ("Worksheets", count_parts("xl/worksheets/sheet*.xml")),
        ("Formulas", count_matches(element("f"), "xl/worksheets/*.xml")),
        ("Defined names", count_matches(element("definedName"), "xl/workbook.xml")),
        ("Data validations", count_matches(element("dataValidation"), "xl/worksheets/*.xml")),
        ("Conditional formats", count_matches(element("conditionalFormatting"), "xl/worksheets/*.xml")),
        ("Comments", count_matches(element("comment"), "xl/comments*.xml")),
        ("Threaded comments", count_matches(element("threadedComment"), "xl/threadedComments/*.xml")),
        ("Charts", count_parts("xl/charts/chart*.xml")),
        ("Calculation chain", count_parts("xl/calcChain.xml")),
    ],
}


def inventory(parts: dict[str, bytes], fmt: str) -> list[dict]:
    return [{"feature": label, "count": count(parts)} for label, count in FEATURES[fmt] + COMMON]


def main() -> int:
    path = Path(sys.argv[1])
    print(json.dumps(inventory(read_parts(path), path.suffix.lstrip(".").lower())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
