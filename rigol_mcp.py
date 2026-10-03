#!/usr/bin/env python3
"""
Rigol DS1054Z (DS1000Z series) MCP server.

Talks to the scope directly over its raw SCPI socket (TCP 5555) — no Rigol
UltraSigma, no NI-VISA, no drivers. The scope is an LXI Core instrument, so
plain ASCII SCPI over a TCP socket is all it needs.

Exposes tools to Claude Code:
  - scope_info        : identity + current settings snapshot
  - get_screenshot    : live display as a PNG (Claude can SEE the scope)
  - get_stats         : measurements (Vpp, freq, duty, rise/fall, ...)
  - get_waveform      : capture a trace (NORM screen or RAW deep memory) -> stats + CSV
  - plot_waveform     : capture + render a PNG plot (needs matplotlib)
  - run_control       : run / stop / single / force / autoscale
  - set_channel       : per-channel scale/offset/coupling/display/probe
  - set_timebase      : timebase scale + offset
  - set_trigger       : edge source/level/slope/sweep
  - set_acquire       : memory depth
  - scpi              : raw SCPI escape hatch
  - search_manual     : search the cached programming guide (offline)
  - get_manual        : read the guide index or complete PDF pages (offline)

Config via environment variables:
  RIGOL_HOST     scope IP        (default 192.168.178.96)
  RIGOL_PORT     raw SCPI port   (default 5555)
  RIGOL_TIMEOUT  socket timeout  (default 10 seconds)
  RIGOL_OUT_DIR  CSV output dir  (default ./captures next to this file)
  RIGOL_MANUAL_DIR optional directory containing the private manual cache
"""

import csv
import os
import socket
import threading
import time
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP, Image

import rigol_reference

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
RIGOL_HOST = os.environ.get("RIGOL_HOST", "192.168.178.96")
RIGOL_PORT = int(os.environ.get("RIGOL_PORT", "5555"))
RIGOL_TIMEOUT = float(os.environ.get("RIGOL_TIMEOUT", "10"))
OUT_DIR = Path(os.environ.get("RIGOL_OUT_DIR", Path(__file__).resolve().parent / "captures"))

INVALID = 9.9e37  # Rigol returns 9.9E37 when a measurement has no valid value

mcp = FastMCP(
    "Rigol DS1054Z",
    instructions=(
        "Use the locally cached DS1000Z/MSO1000Z guide before unsupported SCPI commands: "
        "search_manual(query) finds relevant PDF pages; get_manual(page) reads their full text. "
        "get_manual() or rigol://manual/index provides the index. These lookups are offline "
        "and never connect to the scope. Prefer this cached guide over web searches; browse "
        "only if local documentation is insufficient or a different manual/revision is needed. "
        "If missing, run python scripts/cache_manual.py once from the checkout. "
        "Scope tools must be called sequentially because the scope accepts one client at a time."
    ),
)


# --------------------------------------------------------------------------- #
# Cached vendor documentation (no scope connection)
# --------------------------------------------------------------------------- #
@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def search_manual(query: str, limit: int = 5) -> dict:
    """Search the locally cached RIGOL DS1000Z/MSO1000Z programming guide.

    Use before raw SCPI commands instead of searching the web for the manual.
    Accepts short or long SCPI spellings (e.g. :WAV:PRE? or :WAVeform:PREamble?)
    and case-insensitive keywords (e.g. MATH FFT). All terms must appear on a page.
    Returns ranked snippets and one-based PDF page numbers; use get_manual(page)
    to read complete syntax, parameter tables, explanations and examples.
    limit: 1-10. Offline, read-only; does not contact or change the scope.
    """
    return rigol_reference.search_pages(query, limit)


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def get_manual(page: int | None = None, page_count: int = 1) -> str:
    """Read the cached programming guide index or complete pages without internet.

    With no page, returns edition, usage guidance and a concise command-family index.
    With page, returns the full extracted text starting at that one-based PDF page
    (1-260). page_count: 1-5, for commands spanning adjacent pages. Physical PDF
    numbers differ from the printed chapter page numbers. Use search_manual to
    locate commands. Diagrams remain in the cached original PDF.
    Offline, read-only; does not contact or change the scope.
    """
    if page is None:
        if page_count != 1:
            raise ValueError("Specify page when requesting page_count other than 1")
        return rigol_reference.index()
    return rigol_reference.read_pages(page, page_count)


@mcp.resource("rigol://manual/index", mime_type="text/markdown")
def manual_index() -> str:
    """Cached programming guide metadata, lookup instructions and command-family index."""
    return rigol_reference.index()


@mcp.resource("rigol://manual/page/{page}", mime_type="text/markdown")
def manual_page(page: int) -> str:
    """Full extracted text of a one-based physical PDF page in the cached guide."""
    return rigol_reference.read_pages(page)


# --------------------------------------------------------------------------- #
# matplotlib font-cache warmup (see plot_waveform)
# --------------------------------------------------------------------------- #
# matplotlib builds a font cache the first time it resolves a font. That build
# is normally quick, but on Windows it opens/stats thousands of font files and,
# with antivirus intercepting each one, can stall for minutes-to-tens-of-minutes.
# plot_waveform imports matplotlib lazily, so without care that whole build lands
# *inside* the first tool call, with no feedback to the client -- the tool looks
# hung. Two guards:
#   1) Pin a stable, writable, persistent MPLCONFIGDIR so the cache is built at
#      most once *ever*. Without this, a spawn environment that hands matplotlib
#      a non-persistent cache dir (stripped HOME, wiped TEMP) makes it rebuild on
#      every call -- a permanent stall rather than a one-time warmup.
#   2) Warm the cache in a background thread at startup, off the request path, so
#      the first plot_waveform is instant and the stall (if any) never blocks a
#      tool call or the MCP handshake.
_MPL_CACHE = Path(__file__).resolve().parent / ".mplcache"
try:
    _MPL_CACHE.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(_MPL_CACHE))
except OSError:
    pass  # e.g. read-only site-packages install: fall back to matplotlib's default


def _warm_matplotlib():
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot  # noqa: F401  -- pulls in font_manager
        from matplotlib.font_manager import FontProperties, findfont

        findfont(FontProperties())  # force the one-time font-cache build now
    except Exception:
        pass  # matplotlib is optional; plot_waveform reports the real error if used


threading.Thread(target=_warm_matplotlib, name="mpl-warm", daemon=True).start()


# --------------------------------------------------------------------------- #
# Low-level SCPI-over-socket transport
# --------------------------------------------------------------------------- #
class Scope:
    """One short-lived TCP/SCPI session. Use as a context manager."""

    def __init__(self, host=RIGOL_HOST, port=RIGOL_PORT, timeout=RIGOL_TIMEOUT):
        self.host, self.port, self.timeout = host, port, timeout
        self.s = None

    def __enter__(self):
        self.s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.s.settimeout(self.timeout)
        return self

    def __exit__(self, *exc):
        try:
            self.s.close()
        except Exception:
            pass

    def write(self, cmd: str):
        self.s.sendall((cmd + "\n").encode("ascii"))

    def _recv_exactly(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self.s.recv(n - len(buf))
            if not chunk:
                raise OSError("socket closed early")
            buf += chunk
        return bytes(buf)

    def _read_line(self) -> bytes:
        buf = bytearray()
        while not buf.endswith(b"\n"):
            chunk = self.s.recv(4096)
            if not chunk:
                raise OSError("socket closed early")
            buf += chunk
        return bytes(buf)

    def query(self, cmd: str) -> str:
        """Text query: send and read one '\\n'-terminated line."""
        self.write(cmd)
        return self._read_line().decode("ascii", "replace").strip()

    def query_block(self, cmd: str) -> bytes:
        """
        Binary query returning an IEEE 488.2 definite-length block:
            '#'  <1 digit N>  <N length digits>  <data...>  '\\n'
        Returns the raw <data> bytes (header + trailing newline stripped).
        """
        self.write(cmd)
        if self._recv_exactly(1) != b"#":
            raise OSError("expected IEEE 488.2 block header '#'")
        ndig = int(self._recv_exactly(1).decode("ascii"))
        nlen = int(self._recv_exactly(ndig).decode("ascii"))
        data = self._recv_exactly(nlen)
        # drain the single trailing '\n' the scope appends after the block
        prev = self.s.gettimeout()
        try:
            self.s.settimeout(0.5)
            self.s.recv(1)
        except TimeoutError:
            pass
        finally:
            self.s.settimeout(prev)
        return data


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _chan(channel) -> str:
    """Normalize '1' / 'ch1' / 'chan1' / 'CHANnel1' / 'math' -> 'CHANnel1' / 'MATH'."""
    c = str(channel).upper().strip()
    if c == "MATH":
        return "MATH"
    c = c.replace("CHANNEL", "").replace("CHAN", "").replace("CH", "")
    if c in ("1", "2", "3", "4"):
        return "CHANnel" + c
    raise ValueError(f"invalid channel {channel!r}; use 1-4 or MATH")


def _norm_mode(mode: str) -> str:
    m = str(mode).upper().strip()
    m = {"NORMAL": "NORM", "MAXIMUM": "MAX"}.get(m, m)
    if m not in ("NORM", "RAW", "MAX"):
        raise ValueError(f"invalid mode {mode!r}; use NORM, RAW, or MAX")
    return m


def _capture(channel: str, mode: str, single: bool):
    """
    Grab a trace and return (src, mode, volts, xinc, xorig).
    NORM  : ~1200 on-screen points, fast, scope keeps running.
    RAW   : full acquisition memory, chunked in <=250k reads; scope is STOPPED.
    """
    src = _chan(channel)
    mode = _norm_mode(mode)
    timeout = max(RIGOL_TIMEOUT, 60) if mode == "RAW" else RIGOL_TIMEOUT

    with Scope(timeout=timeout) as s:
        if single:
            s.write(":SINGle")
            time.sleep(0.3)
            for _ in range(200):                       # wait for a stable acquisition
                if s.query(":TRIGger:STATus?") == "STOP":
                    break
                time.sleep(0.05)
        if mode == "RAW":
            s.write(":STOP")                           # internal memory readable only when stopped

        s.write(f":WAVeform:SOURce {src}")
        s.write(f":WAVeform:MODE {mode}")
        s.write(":WAVeform:FORMat BYTE")

        pre = s.query(":WAVeform:PREamble?").split(",")
        xinc, xorig = float(pre[4]), float(pre[5])
        yinc, yorig, yref = float(pre[7]), float(pre[8]), float(pre[9])

        if mode == "RAW":
            mdep = s.query(":ACQuire:MDEPth?")
            try:
                n_pts = int(float(mdep))
            except ValueError:
                n_pts = int(float(pre[2]))             # preamble fallback (AUTO depth)
            raw = bytearray()
            CHUNK = 250000                             # BYTE-mode per-read maximum
            pos = 1
            while pos <= n_pts:
                end = min(n_pts, pos + CHUNK - 1)
                want = end - pos + 1
                for _ in range(10):                    # a 250k chunk occasionally needs a retry
                    s.write(f":WAVeform:STARt {pos}")
                    s.write(f":WAVeform:STOP {end}")
                    data = s.query_block(":WAVeform:DATA?")
                    if len(data) == want:
                        raw += data
                        break
                    time.sleep(0.05)
                else:
                    raise OSError(f"chunk {pos}-{end} never returned {want} bytes")
                pos += CHUNK
                time.sleep(0.01)
        else:
            s.write(":WAVeform:STARt 1")
            s.write(":WAVeform:STOP 1200")
            raw = s.query_block(":WAVeform:DATA?")

    volts = [(b - yorig - yref) * yinc for b in raw]
    return src, mode, volts, xinc, xorig


def _stats(volts):
    n = len(volts)
    if n == 0:
        return {}
    vmin, vmax = min(volts), max(volts)
    vmean = sum(volts) / n
    vrms = (sum(v * v for v in volts) / n) ** 0.5
    return {
        "v_min": vmin,
        "v_max": vmax,
        "v_pp": vmax - vmin,
        "v_mean": vmean,
        "v_rms": vrms,
    }


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
@mcp.tool()
def scope_info() -> str:
    """Return the oscilloscope identity (*IDN?) plus a snapshot of its current
    settings: timebase, sample rate, memory depth, trigger, and the state of
    each of the four analog channels. Good first call to confirm the link."""
    with Scope() as s:
        lines = [
            f"IDN:           {s.query('*IDN?')}",
            f"Host:          {RIGOL_HOST}:{RIGOL_PORT}",
            f"Timebase:      {s.query(':TIMebase:MAIN:SCALe?')} s/div  "
            f"offset {s.query(':TIMebase:MAIN:OFFSet?')} s",
            f"Sample rate:   {s.query(':ACQuire:SRATe?')} Sa/s",
            f"Memory depth:  {s.query(':ACQuire:MDEPth?')} pts",
            f"Trigger:       status={s.query(':TRIGger:STATus?')} "
            f"sweep={s.query(':TRIGger:SWEep?')} "
            f"src={s.query(':TRIGger:EDGe:SOURce?')} "
            f"slope={s.query(':TRIGger:EDGe:SLOPe?')} "
            f"level={s.query(':TRIGger:EDGe:LEVel?')} V",
            "Channels:",
        ]
        for n in (1, 2, 3, 4):
            on = s.query(f":CHANnel{n}:DISPlay?")
            scal = s.query(f":CHANnel{n}:SCALe?")
            off = s.query(f":CHANnel{n}:OFFSet?")
            coup = s.query(f":CHANnel{n}:COUPling?")
            prob = s.query(f":CHANnel{n}:PROBe?")
            state = "ON " if on in ("1", "ON") else "off"
            lines.append(
                f"  CH{n}: {state}  {scal} V/div  offset {off} V  {coup}  probe {prob}x"
            )
    return "\n".join(lines)


@mcp.tool()
def get_screenshot() -> Image:
    """Capture the live oscilloscope display exactly as shown on screen and
    return it as a PNG image. Use this to visually inspect the trace, cursors,
    measurements, and menus — it is the fastest way to 'see' what the scope sees."""
    with Scope(timeout=max(RIGOL_TIMEOUT, 15)) as s:
        png = s.query_block(":DISPlay:DATA? ON,OFF,PNG")
    return Image(data=png, format="png")


@mcp.tool()
def get_stats(channel: str = "CH1", items: list[str] | None = None) -> dict:
    """Read measurement values for a channel. Values are SI units: volts,
    seconds, Hz; a value of null means the scope reported it as unmeasurable.

    channel: 1-4 or MATH (default CH1).
    items:   optional list of measurement tokens. Default set covers the common
             ones. Valid tokens include: VPP, VMAX, VMIN, VTOP, VBASe, VAMP,
             VAVG, VRMS, OVERshoot, PREShoot, FREQuency, PERiod, PWIDth, NWIDth,
             PDUTy (+duty), NDUTy (-duty), RTIMe, FTIMe. (Note: there is no
             'DUTY' token — use PDUTy/NDUTy.)"""
    src = _chan(channel)
    use = items or [
        "VPP", "VMAX", "VMIN", "VAVG", "VRMS",
        "FREQuency", "PERiod", "PWIDth", "NWIDth", "PDUTy", "RTIMe", "FTIMe",
    ]
    out = {}
    with Scope() as s:
        for it in use:
            raw = s.query(f":MEASure:ITEM? {it},{src}")
            try:
                val = float(raw)
            except ValueError:
                val = None
            out[it] = None if (val is None or abs(val) >= INVALID) else val
    return {"source": src, "measurements": out}


@mcp.tool()
def get_waveform(
    channel: str = "CH1",
    mode: str = "NORM",
    single: bool = False,
    save_csv: bool = True,
    preview_points: int = 200,
) -> dict:
    """Capture a waveform trace and return summary statistics, a decimated
    preview array, sample timing, and (optionally) a CSV of the full trace.

    mode   : NORM = ~1200 on-screen points, fast, scope keeps running.
             RAW  = full acquisition memory (up to millions of points); this
                    STOPS the scope and reads internal memory in 250k chunks.
             MAX  = screen memory while running, internal memory while stopped.
    single : if true, fire one :SINGle acquisition and wait for it before
             reading (recommended for a clean RAW capture of a transient).
    save_csv      : write the full time,volts trace to ./captures/*.csv.
    preview_points: how many decimated samples to inline in the response."""
    src, mode, volts, xinc, xorig = _capture(channel, mode, single)
    n = len(volts)

    result = {
        "source": src,
        "mode": mode,
        "samples": n,
        "x_increment_s": xinc,
        "duration_s": xinc * n,
        "sample_rate_Sa_s": (1.0 / xinc) if xinc else None,
        **_stats(volts),
    }

    if n:
        step = max(1, n // max(1, preview_points))
        result["preview_volts"] = [round(volts[i], 5) for i in range(0, n, step)][:preview_points]

    if save_csv and n:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = OUT_DIR / f"{src}_{mode}_{stamp}.csv"
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["time_s", "volts"])
            for i, v in enumerate(volts):
                w.writerow([xorig + i * xinc, v])
        result["csv_path"] = str(path)

    return result


@mcp.tool()
def plot_waveform(channel: str = "CH1", mode: str = "NORM", single: bool = False) -> Image:
    """Capture a waveform and return a rendered PNG plot of it (voltage vs time).
    Requires matplotlib (pip install matplotlib). For very deep RAW captures the
    plot is decimated to ~4000 points for legibility."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as e:
        raise RuntimeError(
            "matplotlib is required for plot_waveform: pip install matplotlib"
        ) from e

    src, mode, volts, xinc, xorig = _capture(channel, mode, single)
    n = len(volts)
    if n == 0:
        raise RuntimeError("no samples captured")

    step = max(1, n // 4000)
    idx = range(0, n, step)
    xs = [(xorig + i * xinc) * 1e3 for i in idx]  # ms
    ys = [volts[i] for i in idx]

    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.plot(xs, ys, linewidth=0.8)
    ax.set_xlabel("time (ms)")
    ax.set_ylabel("volts")
    ax.set_title(f"{src}  {mode}  {n} samples")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    import io
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    plt.close(fig)
    return Image(data=buf.getvalue(), format="png")


@mcp.tool()
def run_control(action: str) -> str:
    """Control acquisition. action is one of:
    run, stop, single, force (force a trigger), auto/autoscale (autoset)."""
    cmd = {
        "run": ":RUN",
        "stop": ":STOP",
        "single": ":SINGle",
        "force": ":TFORce",
        "auto": ":AUToscale",
        "autoscale": ":AUToscale",
    }.get(action.lower().strip())
    if not cmd:
        raise ValueError("action must be run, stop, single, force, or autoscale")
    with Scope() as s:
        s.write(cmd)
        time.sleep(0.1)
        status = s.query(":TRIGger:STATus?")
    return f"sent {cmd}; trigger status now {status}"


@mcp.tool()
def set_channel(
    channel: str,
    scale: float | None = None,
    offset: float | None = None,
    coupling: str | None = None,
    display: bool | None = None,
    probe: float | None = None,
) -> str:
    """Configure an analog channel (1-4). Only the arguments you pass are applied.
    scale    : V/div.
    offset   : V.
    coupling : AC, DC, or GND.
    display  : true/false to show/hide the channel.
    probe    : attenuation factor (e.g. 1, 10, 100)."""
    src = _chan(channel)
    if src == "MATH":
        raise ValueError("set_channel is for analog channels 1-4, not MATH")
    n = src[-1]
    applied = []
    with Scope() as s:
        if scale is not None:
            s.write(f":CHANnel{n}:SCALe {scale}")
            applied.append(f"scale={scale}V/div")
        if offset is not None:
            s.write(f":CHANnel{n}:OFFSet {offset}")
            applied.append(f"offset={offset}V")
        if coupling is not None:
            cp = coupling.upper()
            if cp not in ("AC", "DC", "GND"):
                raise ValueError("coupling must be AC, DC, or GND")
            s.write(f":CHANnel{n}:COUPling {cp}")
            applied.append(f"coupling={cp}")
        if display is not None:
            s.write(f":CHANnel{n}:DISPlay {'ON' if display else 'OFF'}")
            applied.append(f"display={'ON' if display else 'OFF'}")
        if probe is not None:
            s.write(f":CHANnel{n}:PROBe {probe}")
            applied.append(f"probe={probe}x")
    return f"CH{n}: " + (", ".join(applied) if applied else "no changes requested")


@mcp.tool()
def set_timebase(scale: float | None = None, offset: float | None = None) -> str:
    """Set the main timebase. scale = seconds/div (5e-9 .. 50). offset = seconds."""
    applied = []
    with Scope() as s:
        if scale is not None:
            s.write(f":TIMebase:MAIN:SCALe {scale}")
            applied.append(f"scale={scale}s/div")
        if offset is not None:
            s.write(f":TIMebase:MAIN:OFFSet {offset}")
            applied.append(f"offset={offset}s")
    return "timebase: " + (", ".join(applied) if applied else "no changes requested")


@mcp.tool()
def set_trigger(
    source: str | None = None,
    level: float | None = None,
    slope: str | None = None,
    sweep: str | None = None,
) -> str:
    """Configure the edge trigger.
    source : channel 1-4 (or AC).
    level  : trigger level in volts.
    slope  : POS, NEG, or RFAL (rise+fall).
    sweep  : AUTO, NORM, or SING."""
    applied = []
    with Scope() as s:
        if source is not None:
            src = "AC" if str(source).upper() == "AC" else _chan(source)
            s.write(f":TRIGger:EDGe:SOURce {src}")
            applied.append(f"source={src}")
        if slope is not None:
            sl = slope.upper()
            if sl not in ("POS", "NEG", "RFAL", "POSITIVE", "NEGATIVE"):
                raise ValueError("slope must be POS, NEG, or RFAL")
            sl = {"POSITIVE": "POS", "NEGATIVE": "NEG"}.get(sl, sl)
            s.write(f":TRIGger:EDGe:SLOPe {sl}")
            applied.append(f"slope={sl}")
        if level is not None:
            s.write(f":TRIGger:EDGe:LEVel {level}")
            applied.append(f"level={level}V")
        if sweep is not None:
            sw = sweep.upper()
            if sw not in ("AUTO", "NORM", "NORMAL", "SING", "SINGLE"):
                raise ValueError("sweep must be AUTO, NORM, or SING")
            sw = {"NORMAL": "NORM", "SINGLE": "SING"}.get(sw, sw)
            s.write(f":TRIGger:SWEep {sw}")
            applied.append(f"sweep={sw}")
    return "trigger: " + (", ".join(applied) if applied else "no changes requested")


@mcp.tool()
def set_acquire(mem_depth: str) -> str:
    """Set acquisition memory depth. Pass AUTO or a point count valid for the
    active channel count, e.g. 1 ch: 12000/120000/1200000/12000000/24000000;
    2 ch: 6000/60000/600000/6000000/12000000; 3-4 ch: 3000/30000/300000/3000000/6000000."""
    with Scope() as s:
        s.write(f":ACQuire:MDEPth {mem_depth}")
        time.sleep(0.1)
        actual = s.query(":ACQuire:MDEPth?")
        srate = s.query(":ACQuire:SRATe?")
    return f"memory depth set; now {actual} pts at {srate} Sa/s"


@mcp.tool()
def scpi(command: str) -> str:
    """Raw SCPI escape hatch. If the command is a query (contains '?', e.g.
    ':MEAS:ITEM? VPP,CHAN1'), the scope's reply is returned; otherwise the
    command is written and 'OK' is returned. Use for any command not covered by
    the other tools. First call search_manual then get_manual to check the cached
    DS1000Z programming guide for syntax, parameters and model-specific limits;
    no internet lookup is needed for commands covered by the cached manual."""
    with Scope() as s:
        if "?" in command:                 # '?' marks a query; it may be followed by args
            return s.query(command)
        s.write(command)
        return "OK (write sent)"


def main():
    """Console-script entry point; runs the MCP server on stdio transport."""
    mcp.run()  # stdio transport (default) — how Claude Code launches it


if __name__ == "__main__":
    main()
