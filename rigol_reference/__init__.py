"""Offline, page-addressable reference for the Rigol DS1000Z/MSO1000Z MCP.

Only standard-library modules are needed at runtime. Packaged data is read lazily
through importlib.resources so editable installs and wheels behave identically.
"""

import json
import os
import re
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

_PAGE_HEADER = re.compile(r"^## PDF page (\d+)\n", re.MULTILINE)
_SCPI_PART = re.compile(r":([A-Za-z]+)(<[nN]>|\d+)?")
_OPTIONAL_NODES = re.compile(r"\[((?::[A-Za-z]+(?:<[nN]>|\d+)?)+)\]")
_COMMAND_HEADING = re.compile(r"^(?::[A-Za-z]|\*[A-Z])\S*$", re.MULTILINE)
MAX_READ_PAGES = 5
MAX_SEARCH_RESULTS = 10


def _reference_dir():
    configured = os.environ.get("RIGOL_MANUAL_DIR")
    if configured:
        return Path(configured).expanduser()
    local = files(__name__)
    if local.joinpath("programming-guide.md").is_file():
        return local
    return Path.home() / ".cache" / "rigol-mcp" / "reference"


def _read_text(filename: str) -> str:
    try:
        return _reference_dir().joinpath(filename).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValueError(
            "Required one-time manual download: the PDF and Markdown are not bundled, "
            "and the local cache is missing. Install Poppler's pdftotext, then run "
            "python scripts/cache_manual.py from the server checkout with internet access "
            "to download the PDF and generate Markdown. Retry the documentation lookup "
            "after setup; later lookups are offline. For a custom cache, set RIGOL_MANUAL_DIR "
            "for both setup and the server. No automatic download or scope connection "
            "was attempted."
        ) from exc


@lru_cache(maxsize=1)
def manifest() -> dict:
    return json.loads(_read_text("manifest.json"))


@lru_cache(maxsize=1)
def pages() -> tuple[str, ...]:
    markdown = _read_text("programming-guide.md")
    headers = list(_PAGE_HEADER.finditer(markdown))
    expected = manifest()["page_count"]
    if [int(match[1]) for match in headers] != list(range(1, expected + 1)):
        raise ValueError("Cached manual page index is incomplete; rerun scripts/convert_manual.py")
    result = []
    for index, match in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(markdown)
        section = markdown[match.end():end].strip("\n")
        lines = section.splitlines()
        if not lines or not re.fullmatch(r"`{3,}text", lines[0]):
            raise ValueError(f"Malformed cached manual page {index + 1}")
        if lines[-1] != lines[0][:-4]:
            raise ValueError(f"Unclosed cached manual page {index + 1}")
        result.append("\n".join(lines[1:-1]))
    return tuple(result)


@lru_cache(maxsize=1)
def _aliases() -> dict[str, str]:
    aliases = {}
    for page in pages():
        for match in _SCPI_PART.finditer(page):
            token = match[1]
            short = "".join(char for char in token if char.isupper())
            if short:
                if token != token.upper():
                    aliases[token.upper()] = short
                else:
                    aliases.setdefault(token.upper(), short)
                aliases.setdefault(short, short)
    return aliases


@lru_cache(maxsize=1)
def _indexed_aliases() -> frozenset[str]:
    aliases = _aliases()
    return frozenset(
        aliases.get(match[1].upper(), match[1].upper())
        for page in pages()
        for match in _SCPI_PART.finditer(page)
        if match[2] and match[2].lower() == "<n>"
    )


def _normalize(text: str) -> str:
    aliases = _aliases()
    indexed = _indexed_aliases()

    def normalize_part(match):
        token = aliases.get(match[1].upper(), match[1].upper())
        suffix = match[2] or ""
        if token in indexed and suffix:
            suffix = "<n>"
        return ":" + token + suffix

    text = _SCPI_PART.sub(normalize_part, text)
    return " ".join(text.casefold().split())


@lru_cache(maxsize=1)
def _search_pages() -> tuple[str, ...]:
    return tuple(_search_text(page) for page in pages())


def _search_text(text: str) -> str:
    normalized = _normalize(text)
    # SCPI brackets denote optional nodes: :TIM[:MAIN]:SCAL may be sent as
    # either :TIM:MAIN:SCAL or :TIM:SCAL. Retain the literal notation as well.
    included = _OPTIONAL_NODES.sub(r"\1", normalized)
    omitted = _OPTIONAL_NODES.sub("", normalized)
    return " ".join(dict.fromkeys((normalized, included, omitted)))


def _title(page: str) -> str:
    commands = _COMMAND_HEADING.findall(page)
    if commands:
        return ", ".join(dict.fromkeys(commands))[:200]
    lines = [line.strip() for line in page.splitlines() if line.strip()]
    for line in lines[:8]:
        if "RIGOL" not in line and "Programming Guide" not in line:
            return line[:200]
    return "Blank page" if not lines else lines[0][:200]


def _printed_page(page: str) -> str | None:
    for line in reversed(page.splitlines()):
        if "MSO1000Z/DS1000Z Programming Guide" in line:
            match = re.search(r"(?:^|\s)([IVX]+|\d+-\d+)(?:\s|$)", line)
            if match:
                return match[1]
    return None


def read_pages(page: int, page_count: int = 1) -> str:
    if not 1 <= page_count <= MAX_READ_PAGES:
        raise ValueError(f"page_count must be between 1 and {MAX_READ_PAGES}")
    cached = pages()
    if not 1 <= page <= len(cached) or page + page_count - 1 > len(cached):
        raise ValueError(f"Requested range must be within PDF pages 1-{len(cached)}")
    sections = []
    for number in range(page, page + page_count):
        body = cached[number - 1]
        runs = re.findall(r"`+", body)
        fence = "`" * max(3, 1 + max((len(run) for run in runs), default=0))
        printed = _printed_page(body)
        label = f" (printed page {printed})" if printed else ""
        sections.append(f"## PDF page {number}{label}\n\n{fence}text\n{body}\n{fence}")
    source = manifest()["source_url"]
    return f"Source: {source}\n\n" + "\n\n".join(sections)


def search_pages(query: str, limit: int = 5) -> dict:
    if not query.strip() or len(query) > 300:
        raise ValueError("query must contain 1-300 characters of command syntax or keywords")
    if not 1 <= limit <= MAX_SEARCH_RESULTS:
        raise ValueError(f"limit must be between 1 and {MAX_SEARCH_RESULTS}")
    terms = _normalize(query).split()
    phrase = " ".join(terms)
    found = []
    for number, (body, normalized) in enumerate(zip(pages(), _search_pages(), strict=True), 1):
        if not all(term in normalized for term in terms):
            continue
        headings = _search_text(" ".join(_COMMAND_HEADING.findall(body)))
        # Prefer actual command definitions to TOC entries and cross-references.
        score = sum(min(normalized.count(term), 10) for term in terms)
        score += 25 if phrase in normalized else 0
        score += 50 if all(term in headings for term in terms) else 0
        score -= 40 if number in range(5, 11) else 0
        lines = body.splitlines()
        hit_line = next(
            (index for index, line in enumerate(lines) if terms[0] in _search_text(line)), 0
        )
        # Full pages are retrieved separately; search results stay small.
        snippet = "\n".join(lines[max(0, hit_line - 2):hit_line + 20])[:1800]
        found.append((score, {
            "pdf_page": number,
            "printed_page": _printed_page(body),
            "title": _title(body),
            "resource_uri": f"rigol://manual/page/{number}",
            "source_url": f"{manifest()['source_url']}#page={number}",
            "snippet": snippet,
        }))
    found.sort(key=lambda item: (-item[0], item[1]["pdf_page"]))
    return {
        "query": query,
        "total_matching_pages": len(found),
        "results": [item[1] for item in found[:limit]],
        "next_step": "Use get_manual(page=pdf_page) for full syntax, parameters and examples. "
                     "Read adjacent pages if a command continues. Try fewer terms if no results.",
    }


def index() -> str:
    info = manifest()
    families = []
    for number, body in enumerate(pages(), 1):
        for match in re.finditer(
            r"^(?::\S+ Commands|IEEE488\.2 Common Commands)[ \t]*$", body, re.MULTILINE
        ):
            families.append(f"| {match[0].strip()} | {number} |")
    return (
        f"# {info['title']}\n\n"
        f"Cached edition: {info['edition']}; publication {info['publication_number']}; "
        f"manual software version {info['manual_software_version']}.\n\n"
        f"Source: {info['source_url']}\n\n"
        f"PDF SHA-256: `{info['pdf_sha256']}`\n\n"
        f"All {info['page_count']} PDF pages are available locally. No network or scope "
        "connection is used for documentation lookup.\n\n"
        "Search with search_manual(query), then get_manual(page=pdf_page, page_count=1). "
        "Search accepts SCPI short/long spellings, numbered command nodes such as "
        ":CHAN4, and case-insensitive keywords. "
        "Multiple whitespace-separated terms must all occur on the same page.\n\n"
        f"Page reads are limited to {MAX_READ_PAGES} pages per call. Page numbers are "
        "one-based physical PDF pages, not the printed chapter page numbers. "
        "MCP resources also expose rigol://manual/page/{page}.\n\n"
        "Use this reference before raw SCPI commands. The manual covers models with "
        "different options; confirm applicability to the connected scope. Extracted "
        "text preserves column spacing but does not include graphical diagrams. The "
        "cached programming-guide.pdf contains the original figures and formatting.\n\n"
        "The vendor copyright and notices remain in the manual; it is not covered by "
        "the server's MIT license.\n\n"
        "## Navigation\n\n"
        "Original table of contents: PDF pages 5-10. Programming overview and SCPI "
        "syntax: pages 11-16. Command system: pages 17-242. Programming demos: pages "
        "243-260. Use get_manual to read these pages in batches.\n\n"
        "| Command family | PDF starting page |\n| --- | --- |\n" + "\n".join(families)
    )
