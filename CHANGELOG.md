# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-07-02

### Added
- Initial release of the Rigol DS1000Z / MSO1000Z MCP server.
- Tools: `scope_info`, `get_screenshot`, `get_stats`, `get_waveform`,
  `plot_waveform`, `run_control`, `set_channel`, `set_timebase`,
  `set_trigger`, `set_acquire`, and a raw `scpi` escape hatch.
- Raw SCPI-over-TCP transport (port 5555) with IEEE 488.2 block reads — no
  VISA, UltraSigma, or drivers required.
- Chunked deep-memory (RAW) capture with automatic reassembly and retry.
- `main()` console entry point and `rigol-mcp` script.
- Packaging (`pyproject.toml`), MIT license, CI (ruff + multi-version import
  smoke test).

[Unreleased]: https://github.com/DVSProductions/rigol-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/DVSProductions/rigol-mcp/releases/tag/v0.1.0
