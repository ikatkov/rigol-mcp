#!/usr/bin/env python3
"""Download and convert the vendor guide once, keeping its contents private.

Run from a checkout: python scripts/cache_manual.py
The server never calls this script or downloads documentation automatically.
"""

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path
from urllib.request import Request, urlopen

from convert_manual import REFERENCE_DIR, convert


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, help="Override the private reference directory")
    parser.add_argument("--refresh", action="store_true", help="Explicitly download the PDF again")
    args = parser.parse_args()
    # Keep existing editable-install caches working; new checkouts use a private
    # user directory that installed wheels can also find.
    default = (
        REFERENCE_DIR if (REFERENCE_DIR / "programming-guide.pdf").is_file()
        else Path.home() / ".cache" / "rigol-mcp" / "reference"
    )
    directory = (
        args.directory or Path(os.environ.get("RIGOL_MANUAL_DIR", default))
    ).expanduser().resolve()
    metadata = json.loads((REFERENCE_DIR / "manifest.json").read_text(encoding="utf-8"))
    expected_hash = metadata["pdf_sha256"]
    pdf = directory / "programming-guide.pdf"
    markdown = directory / "programming-guide.md"
    if not args.refresh and pdf.is_file():
        if hashlib.sha256(pdf.read_bytes()).hexdigest() != expected_hash:
            raise SystemExit(
                "Cached PDF checksum differs from the reference; use --refresh to replace it"
            )
        if markdown.is_file() and (directory / "manifest.json").is_file():
            print(f"Manual already cached at {directory}; no download or conversion needed")
            return
    if not shutil.which("pdftotext"):
        raise SystemExit("Install Poppler's pdftotext before caching the manual")
    directory.mkdir(parents=True, exist_ok=True)
    if args.refresh or not pdf.is_file():
        request = Request(
            metadata["source_url"], headers={"User-Agent": "rigol-mcp-manual-cache/1"}
        )
        with urlopen(request, timeout=60) as response:  # noqa: S310 -- fixed HTTPS source
            content = response.read(12 * 1024 * 1024 + 1)
        if len(content) > 12 * 1024 * 1024 or not content.startswith(b"%PDF-"):
            raise SystemExit("Vendor response is not a supported PDF; existing cache was preserved")
        if hashlib.sha256(content).hexdigest() != expected_hash:
            raise SystemExit(
                "Downloaded PDF checksum differs from the pinned edition; cache was preserved"
            )
        temporary = directory / "programming-guide.pdf.tmp"
        temporary.write_bytes(content)
        temporary.replace(pdf)
    convert(directory)
    print(f"Manual cached at {directory}; MCP lookups are now offline")


if __name__ == "__main__":
    main()
