#!/usr/bin/env python3
"""Convert the locally cached PDF to page-addressable Markdown using Poppler.

Run from any directory: python scripts/convert_manual.py
This is an explicit maintenance step; the MCP server never downloads or converts
the manual at startup or during tool calls.
"""

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

REFERENCE_DIR = Path(__file__).resolve().parents[1] / "rigol_reference"
SOURCE_URL = (
    "https://www.batronix.com/pdf/Rigol/ProgrammingGuide/"
    "MSO1000Z_DS1000Z_ProgrammingGuide_EN.pdf"
)


def convert(reference_dir: Path):
    pdf = reference_dir / "programming-guide.pdf"
    result = subprocess.run(
        ["pdftotext", "-layout", "-enc", "UTF-8", str(pdf), "-"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    pages = result.stdout.split("\f")
    if not pages[-1].strip():
        pages.pop()
    metadata = {
        "title": "RIGOL MSO1000Z/DS1000Z Programming Guide",
        "edition": "December 2015",
        "publication_number": "PGA19108-1110",
        "manual_software_version": "00.04.03.SP2",
        "source_url": SOURCE_URL,
        "pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
        "page_count": len(pages),
        "extraction": "Poppler pdftotext -layout -enc UTF-8",
    }
    header = (
        f"# {metadata['title']}\n\n"
        f"Edition: {metadata['edition']}. Publication: {metadata['publication_number']}.\n\n"
        f"Source: [{metadata['title']}]({SOURCE_URL})\n\n"
        f"Original PDF SHA-256: `{metadata['pdf_sha256']}`\n\n"
        "This is a text extraction of the cached vendor PDF, not a rewritten manual.\n"
        "PDF page numbers below are one-based physical pages; the printed manual uses\n"
        "separate roman and chapter page numbers. Layout is preserved inside text blocks.\n"
        "Images and graphical diagrams remain in programming-guide.pdf. The original\n"
        "vendor copyright and notices are retained; this manual is not covered by the\n"
        "MCP server's MIT license.\n\n"
    )
    sections = []
    for number, page in enumerate(pages, 1):
        # Strip trailing spaces, retaining the column layout and internal blank lines.
        body = "\n".join(line.rstrip() for line in page.splitlines()).strip("\n")
        # Choose a fence that cannot collide with any text in the source page.
        runs = re.findall(r"`+", body)
        fence = "`" * max(3, 1 + max((len(run) for run in runs), default=0))
        sections.append(f"## PDF page {number}\n\n{fence}text\n{body}\n{fence}\n")
    (reference_dir / "programming-guide.md").write_text(
        header + "\n".join(sections), encoding="utf-8"
    )
    (reference_dir / "manifest.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Converted {len(pages)} pages; PDF SHA-256 {metadata['pdf_sha256']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=REFERENCE_DIR)
    args = parser.parse_args()
    convert(args.directory.resolve())


if __name__ == "__main__":
    main()
