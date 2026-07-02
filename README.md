# Rigol DS1054Z MCP server

[![CI](https://github.com/DVSProductions/rigol-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/DVSProductions/rigol-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![MCP](https://img.shields.io/badge/MCP-server-orange.svg)](https://modelcontextprotocol.io)

Drive a Rigol DS1054Z (or any DS1000Z / MSO1000Z series scope) from Claude Code
over Ethernet — no UltraSigma, no NI-VISA, no drivers. The scope is an LXI
instrument that accepts plain SCPI on a raw TCP socket (port **5555**); this
server wraps that as MCP tools.

## Tools

| Tool | What it does |
|------|--------------|
| `scope_info` | `*IDN?` + timebase, sample rate, memory depth, trigger, per-channel state |
| `get_screenshot` | live display as a **PNG** (Claude can see the trace/menus) |
| `get_stats` | measurements: Vpp, Vmax/min/avg/rms, freq, period, width, duty, rise/fall |
| `get_waveform` | capture a trace (NORM screen ~1200 pts, or RAW deep memory) → stats + CSV |
| `plot_waveform` | capture + render a PNG plot (needs `matplotlib`) |
| `run_control` | run / stop / single / force / autoscale |
| `set_channel` | per-channel scale / offset / coupling / display / probe |
| `set_timebase` | timebase scale + offset |
| `set_trigger` | edge source / level / slope / sweep |
| `set_acquire` | memory depth |
| `scpi` | raw SCPI escape hatch for anything else |

## Setup

Clone the repo, then either install the dependencies directly:

```sh
pip install "mcp[cli]"      # required
pip install matplotlib      # optional, only for plot_waveform
```

…or install the package itself (adds a `rigol-mcp` console script):

```sh
pip install .               # or:  pip install ".[plot]"  to include matplotlib
```

Find the scope's IP on the unit: **Utility → IO Setting → LAN Conf**
(this one is `192.168.178.96`). Sanity-check the link with a browser at
`http://<scope-ip>` — it serves an LXI welcome page.

## Register with Claude Code

```sh
claude mcp add --env RIGOL_HOST=192.168.178.96 --scope user rigol -- python D:/ryzzenDL/rigol-mcp/rigol_mcp.py
```

> The `--env` flag must have another option (here `--scope user`) between it and
> the server name, or the CLI mis-parses the name. If `python` isn't on PATH,
> use the full `python.exe` path or `-- cmd /c python D:/...`.

If you `pip install`ed the package, use the console script instead of a path:

```sh
claude mcp add --env RIGOL_HOST=192.168.178.96 --scope user rigol -- rigol-mcp
```

Or add it to a `.mcp.json` (project scope) — see [`.mcp.example.json`](.mcp.example.json):

```json
{
  "mcpServers": {
    "rigol": {
      "type": "stdio",
      "command": "python",
      "args": ["D:/ryzzenDL/rigol-mcp/rigol_mcp.py"],
      "env": { "RIGOL_HOST": "192.168.178.96" }
    }
  }
}
```

Then `claude mcp list` to confirm it connects, and ask Claude things like
*"screenshot the scope"*, *"what's the frequency on channel 1?"*, or
*"capture CH1 deep memory and plot it"*.

## Environment variables

| Var | Default | Meaning |
|-----|---------|---------|
| `RIGOL_HOST` | `192.168.178.96` | scope IP |
| `RIGOL_PORT` | `5555` | raw SCPI socket port |
| `RIGOL_TIMEOUT` | `10` | socket timeout (seconds) |
| `RIGOL_OUT_DIR` | `./captures` | where `get_waveform` writes CSVs |

## Notes / gotchas

- **RAW captures stop the scope.** Internal memory is only readable in STOP
  state; the deep-memory read also chunks at 250k points (a hard per-read limit)
  and reassembles — handled for you.
- Asking for the whole multi-megapoint buffer in one shot makes the scope reply
  *"Memory lack in waveform reading!"*; always chunk (this server does).
- VXI-11 (`pyvisa`/`python-vxi11`) also works but is slower and flakier on old
  firmware; the raw socket used here is the robust path.
- A single TCP session is opened per tool call and closed after — stateless and
  resilient. (Port 5555 is single-client, so don't fire scope tools concurrently.)
- **`get_stats` mutates the display.** On the DS1000Z, *querying* `:MEAS:ITEM? X`
  also *adds X to the on-screen measurement bar* — there is no pure-read variant.
  Reading stats will reshuffle the scope's displayed measurement list (cosmetic).
- **Changing a channel's `scale` also moves its `offset`.** The scope keeps the
  trace's division position, so V/div and offset-in-volts track together — pass
  `offset` explicitly alongside `scale` if you need an exact vertical position.
- **`set_acquire AUTO` only reverts to AUTO mode while the scope is running.**
  Sent while stopped it's ignored and `:ACQ:MDEP?` keeps the last fixed depth.
- **`RTIMe`/`FTIMe` return `9.9E37` (→ `null`) on ringy/overshooting edges** —
  the scope's built-in rise/fall measurement gives up when the signal crosses the
  90% threshold multiple times. Compute 10–90% from `get_waveform` samples instead.
- **First `plot_waveform` call can take 2–3 minutes** on a fresh machine while
  matplotlib builds its font cache (one-time, looks like a hang). Subsequent
  calls are fast. To avoid it, pre-warm once: `python -c "import matplotlib.pyplot"`.
- **`scpi` query detection:** a command is treated as a query if it contains `?`
  (queries may carry args after the `?`, e.g. `:MEAS:ITEM? VPP,CHAN1`).

## Contributing

Issues and PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). CI lints with
[ruff](https://docs.astral.sh/ruff/) and runs an import smoke test on Python
3.10–3.13 (no scope required).

## License

[MIT](LICENSE) © Valentino Saitz
