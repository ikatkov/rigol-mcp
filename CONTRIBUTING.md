# Contributing

Thanks for your interest in improving `rigol-mcp`! It's a single-file MCP server,
so contributions are easy to scope.

## Development setup

```sh
python -m venv .venv
# Windows: .venv\Scripts\activate     macOS/Linux: source .venv/bin/activate
pip install -e ".[plot]"
pip install ruff
```

## Before opening a PR

- **Lint:** `ruff check .` (and `ruff format .` if you want auto-formatting).
- **Smoke test:** `python -c "import rigol_mcp"` must succeed — this validates that
  all tools register without a scope attached. CI runs this on Python 3.10–3.13.
- **Offline reference tests:** `python -m unittest discover -s tests -v` checks
  source provenance, page retrieval, SCPI abbreviation search and MCP transport
  without a scope connection. Run `python scripts/cache_manual.py` once first
  (requires Poppler's `pdftotext`). The guide's PDF and Markdown stay in the private
  cache and must not be committed or redistributed with the MIT-licensed code.
- If you touched behavior that needs real hardware, describe how you tested it
  (scope model + firmware) in the PR, since CI can't reach an instrument.

## Guidelines

- Keep the server **dependency-light** — `mcp[cli]` is the only hard dependency;
  `matplotlib` stays optional (`plot` extra).
- Preserve the **stateless, one-session-per-call** transport model.
- Document scope quirks in the README's *Notes / gotchas* section — that section
  is the most valuable part of the project for other DS1000Z owners.
- New tools should have a clear docstring; the docstring is what Claude reads to
  decide when and how to call the tool.

## Reporting bugs

Open an issue with your scope model, firmware version (`*IDN?`), and the exact
tool call / SCPI command that misbehaved.
