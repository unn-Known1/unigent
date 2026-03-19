from __future__ import annotations



import threading

import time

from typing import Any



_WIDGETS_OK = False
from .agent import logger
# ══════════════════════════════════════════════════════════════════════════════

#  CELL 19 — Live Log Streamer

#

#  Three ways to use:

#    1. LogStreamer widget  — rich auto-refreshing ipywidgets panel

#    2. tail_logs()        — simple print-to-cell tail (no widget needed)

#    3. watch_logs()       — blocking live tail like `tail -f` in terminal

# ══════════════════════════════════════════════════════════════════════════════



try:

    import ipywidgets as widgets

    from IPython.display import display, HTML, clear_output

    _WIDGETS_OK = True

except ImportError:

    _WIDGETS_OK = False





# ─────────────────────────────────────────────────────────────────────────────

#  Colour map — log level → HTML colour

# ─────────────────────────────────────────────────────────────────────────────



_LEVEL_COLOR: dict[str, str] = {

    "debug":   "#5a6278",

    "info":    "#d4dae8",

    "warning": "#f0a04a",

    "error":   "#f05a4a",

}



_LEVEL_BADGE: dict[str, str] = {

    "debug":   "#1a1d24",

    "info":    "#1a2535",

    "warning": "#2d1f0a",

    "error":   "#2d0a0a",

}



# ─────────────────────────────────────────────────────────────────────────────

#  HTML rendering helpers

# ─────────────────────────────────────────────────────────────────────────────



_CSS = """

<style>

@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&display=swap');

.ls-root {

  font-family: 'JetBrains Mono', monospace;

  font-size: 12px;

  background: #0d0f12;

  color: #d4dae8;

  border-radius: 8px;

  overflow: hidden;

  border: 1px solid #252935;

}

.ls-header {

  background: #151820;

  padding: 10px 16px;

  border-bottom: 1px solid #252935;

  display: flex;

  justify-content: space-between;

  align-items: center;

  font-size: 12px;

}

.ls-title  { color: #4af0a0; font-weight: 600; letter-spacing: .5px; }

.ls-meta   { color: #5a6278; font-size: 11px; }

.ls-body   {

  padding: 10px 14px;

  overflow-y: auto;

  max-height: 480px;

  min-height: 200px;

  display: flex;

  flex-direction: column;

  gap: 2px;

}

.ls-row {

  display: flex;

  gap: 10px;

  padding: 3px 6px;

  border-radius: 4px;

  align-items: baseline;

  animation: fadeIn .15s ease;

}

.ls-ts    { color: #3a4060; min-width: 85px; }

.ls-lvl   { min-width: 52px; font-weight: 600; }

.ls-msg   { flex: 1; word-break: break-word; white-space: pre-wrap; }

.ls-extra { color: #5a6278; font-size: 11px; }

@keyframes fadeIn {

  from { opacity: 0; transform: translateY(2px); }

  to   { opacity: 1; transform: translateY(0); }

}

.ls-body::-webkit-scrollbar       { width: 4px; }

.ls-body::-webkit-scrollbar-track { background: transparent; }

.ls-body::-webkit-scrollbar-thumb { background: #252935; border-radius: 4px; }

</style>

"""





def _record_to_html(r: dict[str, Any]) -> str:

    level   = r.get("level", "info").lower()

    color   = _LEVEL_COLOR.get(level, "#d4dae8")

    bg      = _LEVEL_BADGE.get(level, "#0d0f12")

    ts      = r.get("ts", "")[:19].replace("T", " ")

    msg     = (r.get("msg") or "")[:300]

    extra   = {k: v for k, v in r.items() if k not in ("ts", "level", "msg")}

    ext_str = "  " + "  ".join(f"{k}={v}" for k, v in list(extra.items())[:3]) if extra else ""

    # escape HTML special chars

    msg     = msg.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    ext_str = ext_str.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    return (

        f'<div class="ls-row" style="background:{bg};">'

        f'<span class="ls-ts">{ts}</span>'

        f'<span class="ls-lvl" style="color:{color};">{level.upper()}</span>'

        f'<span class="ls-msg" style="color:{color};">{msg}</span>'

        f'<span class="ls-extra">{ext_str}</span>'

        f'</div>'

    )





# ─────────────────────────────────────────────────────────────────────────────

#  LogStreamer — auto-refreshing ipywidgets panel

# ─────────────────────────────────────────────────────────────────────────────



class LogStreamer:

    """

    Live log viewer as an ipywidgets panel.

    Refreshes every *refresh_seconds* seconds automatically.



    Usage:

        ls = LogStreamer()

        ls.show()                  # renders in the cell output



        ls.filter_level("warning") # show only warnings and errors

        ls.filter_text("tool")     # show only lines containing 'tool'

        ls.set_lines(100)          # show last 100 lines

        ls.pause()  / ls.resume()

        ls.stop()

    """



    def __init__(

        self,

        lines:           int   = 50,

        refresh_seconds: float = 2.0,

    ) -> None:

        self._lines    = lines

        self._interval = refresh_seconds

        self._paused   = False

        self._stop_ev  = threading.Event()

        self._thread:  threading.Thread | None = None

        self._level_filter: str | None = None

        self._text_filter:  str | None = None



        if not _WIDGETS_OK:

            raise ImportError("ipywidgets not available. Use tail_logs() instead.")



        self._out     = widgets.Output()

        self._build_controls()



    # ── Controls ──────────────────────────────────────────────────────



    def _build_controls(self) -> None:

        btn_style = {"button_color": "#151820", "font_weight": "600"}



        self._btn_pause  = widgets.Button(description="⏸ Pause",  layout=widgets.Layout(width="90px", height="32px"))

        self._btn_clear  = widgets.Button(description="🗑 Clear",  layout=widgets.Layout(width="90px", height="32px"))

        self._btn_scroll = widgets.Button(description="⬇ Bottom", layout=widgets.Layout(width="90px", height="32px"))



        for btn in (self._btn_pause, self._btn_clear, self._btn_scroll):

            btn.style.button_color = "#151820"

            btn.style.font_weight  = "600"



        self._dd_level = widgets.Dropdown(

            options=["all", "debug", "info", "warning", "error"],

            value="all",

            description="level:",

            layout=widgets.Layout(width="160px"),

            style={"description_width": "40px"},

        )

        self._txt_filter = widgets.Text(

            placeholder="filter text…",

            layout=widgets.Layout(width="200px", height="32px"),

        )

        self._int_lines = widgets.BoundedIntText(

            value=self._lines, min=10, max=500, step=10,

            description="lines:",

            layout=widgets.Layout(width="130px"),

            style={"description_width": "40px"},

        )



        self._btn_pause.on_click(self._on_pause)

        self._btn_clear.on_click(self._on_clear)

        self._dd_level.observe(self._on_level_change, names="value")

        self._txt_filter.observe(self._on_text_filter, names="value")

        self._int_lines.observe(self._on_lines_change, names="value")



        self._controls = widgets.HBox(

            [self._dd_level, self._txt_filter, self._int_lines,

             self._btn_pause, self._btn_clear],

            layout=widgets.Layout(

                padding="8px 14px",

                background_color="#151820",

                border_top="1px solid #252935",

                flex_wrap="wrap",

                gap="8px",

            ),

        )

        self._root = widgets.VBox(

            [self._out, self._controls],

            layout=widgets.Layout(

                border="1px solid #252935",

                border_radius="8px",

                overflow="hidden",

                max_width="960px",

            ),

        )



    # ── Event handlers ────────────────────────────────────────────────



    def _on_pause(self, _) -> None:

        self._paused = not self._paused

        self._btn_pause.description = "▶ Resume" if self._paused else "⏸ Pause"



    def _on_clear(self, _) -> None:

        with self._out:

            clear_output(wait=True)



    def _on_level_change(self, change) -> None:

        v = change["new"]

        self._level_filter = None if v == "all" else v



    def _on_text_filter(self, change) -> None:

        v = change["new"].strip().lower()

        self._text_filter = v if v else None



    def _on_lines_change(self, change) -> None:

        self._lines = change["new"]



    # ── Public API ────────────────────────────────────────────────────



    def filter_level(self, level: str) -> None:

        """Show only records at *level* and above. Pass 'all' to reset."""

        self._level_filter = None if level == "all" else level.lower()

        self._dd_level.value = level



    def filter_text(self, text: str) -> None:

        """Show only records containing *text*."""

        self._text_filter = text.lower() if text else None

        self._txt_filter.value = text



    def set_lines(self, n: int) -> None:

        """Change the number of tail lines shown."""

        self._lines = n

        self._int_lines.value = n



    def pause(self)  -> None: self._paused = True;  self._btn_pause.description = "▶ Resume"

    def resume(self) -> None: self._paused = False; self._btn_pause.description = "⏸ Pause"



    def stop(self) -> None:

        self._stop_ev.set()

        print("⏹  Log streamer stopped.")



    def show(self) -> None:

        """Render the widget and start the refresh loop."""

        display(HTML(_CSS))

        display(self._root)

        self._start_refresh()



    # ── Refresh loop ──────────────────────────────────────────────────



    def _start_refresh(self) -> None:

        self._stop_ev.clear()

        self._thread = threading.Thread(

            target=self._loop, daemon=True, name="log-streamer"

        )

        self._thread.start()



    def _loop(self) -> None:

        while not self._stop_ev.wait(timeout=self._interval):

            if not self._paused:

                self._render()



    def _render(self) -> None:

        records = logger.read_recent(self._lines)



        # Apply filters

        if self._level_filter:

            order = ["debug", "info", "warning", "error"]

            min_i = order.index(self._level_filter) if self._level_filter in order else 0

            records = [r for r in records if order.index(r.get("level", "debug")) >= min_i]

        if self._text_filter:

            records = [

                r for r in records

                if self._text_filter in (r.get("msg") or "").lower()

                or any(self._text_filter in str(v).lower() for v in r.values())

            ]



        rows_html = "\n".join(_record_to_html(r) for r in records)

        now       = time.strftime("%H:%M:%S")

        total     = len(logger.read_recent(9999))



        html = (

            f'<div class="ls-root">'

            f'<div class="ls-header">'

            f'  <span class="ls-title">📋 LIVE LOGS</span>'

            f'  <span class="ls-meta">'

            f'    showing {len(records)} / {total} records'

            f'    &nbsp;·&nbsp; refreshed {now}'

            f'    &nbsp;·&nbsp; {logger.LOG_FILE}'

            f'  </span>'

            f'</div>'

            f'<div class="ls-body" id="ls-body">{rows_html}</div>'

            f'</div>'

            # Auto-scroll to bottom

            f'<script>'

            f'  var b = document.getElementById("ls-body");'

            f'  if(b) b.scrollTop = b.scrollHeight;'

            f'</script>'

        )

        with self._out:

            clear_output(wait=True)

            display(HTML(html))





# ─────────────────────────────────────────────────────────────────────────────

#  tail_logs() — simple snapshot, no widget needed

# ─────────────────────────────────────────────────────────────────────────────



def tail_logs(

    n:     int          = 30,

    level: str | None   = None,

    text:  str | None   = None,

) -> None:

    """

    Print the last *n* log records to the cell output.

    No widgets required — works anywhere.



    Args:

        n:     number of records to show

        level: filter to 'debug' | 'info' | 'warning' | 'error'

        text:  only show records containing this string



    Usage:

        tail_logs()

        tail_logs(50, level="warning")

        tail_logs(100, text="tool")

    """

    records = logger.read_recent(n)

    if level:

        order = ["debug", "info", "warning", "error"]

        min_i = order.index(level.lower()) if level.lower() in order else 0

        records = [r for r in records if order.index(r.get("level", "debug")) >= min_i]

    if text:

        t = text.lower()

        records = [

            r for r in records

            if t in (r.get("msg") or "").lower()

            or any(t in str(v).lower() for v in r.values())

        ]

    sep = "─" * 72

    print(f"\n{sep}")

    print(f"  LOGS  —  {len(records)} records  —  {logger.LOG_FILE}")

    print(sep)

    for r in records:

        print(logger.format_record(r))

    print(sep + "\n")





# ─────────────────────────────────────────────────────────────────────────────

#  watch_logs() — blocking live tail, like `tail -f` in a terminal

# ─────────────────────────────────────────────────────────────────────────────



def watch_logs(

    poll_seconds: float     = 1.0,

    level:        str | None = None,

    text:         str | None = None,

) -> None:

    """

    Blocking live tail — prints new log lines as they arrive.

    Run in its own cell. Interrupt with the ■ Stop button or Ctrl+C.



    Args:

        poll_seconds: how often to check for new lines (default 1s)

        level:        filter level ('debug'|'info'|'warning'|'error')

        text:         only show lines containing this string



    Usage:

        watch_logs()                      # stream everything

        watch_logs(level="warning")       # warnings + errors only

        watch_logs(text="tool", level="info")

    """

    order   = ["debug", "info", "warning", "error"]

    min_lvl = order.index(level.lower()) if level and level.lower() in order else 0



    seen_count = 0

    print(f"👁  Watching logs — Ctrl+C / ■ Stop to quit\n{'─'*60}")

    try:

        while True:

            records = logger.read_recent(500)

            new     = records[seen_count:]

            for r in new:

                r_lvl = order.index(r.get("level", "debug")) if r.get("level", "debug") in order else 0

                if r_lvl < min_lvl:

                    continue

                if text and text.lower() not in (r.get("msg") or "").lower():

                    continue

                print(logger.format_record(r))

            seen_count = len(records)

            time.sleep(poll_seconds)

    except KeyboardInterrupt:

        print(f"\n{'─'*60}\n👁  Stopped.")





# ─────────────────────────────────────────────────────────────────────────────

#  Auto-show widget if running in a notebook

# ─────────────────────────────────────────────────────────────────────────────



print("✓ Log streamer ready")

print()

print("  Rich widget (auto-refresh every 2s):")

print("    ls = LogStreamer()")

print("    ls.show()")

print()

print("  Filters:")

print("    ls.filter_level('warning')   # warning + error only")

print("    ls.filter_text('tool')       # lines containing 'tool'")

print("    ls.set_lines(100)            # show last 100 lines")

print("    ls.pause() / ls.resume()")

print()

print("  Simple snapshot (no widget):")

print("    tail_logs(50)")

print("    tail_logs(level='error')")

print("    tail_logs(text='heartbeat')")

print()

print("  Live tail (blocking, like tail -f):")

print("    watch_logs()")

print("    watch_logs(level='warning')")
