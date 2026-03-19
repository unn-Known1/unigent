from __future__ import annotations

import io
import json
import os
import queue
import threading
import time
import traceback
from pathlib import Path
from typing import Any
from typing import Any
from datetime import datetime

from .agent import Config, logger, FileManager, check_needs
from .automation import Watchdog, TaskQueue

# ══════════════════════════════════════════════════════════════════════════════
#  CELL 20 — Telegram Bot Integration
#
#  Controls the agent entirely from Telegram:
#    • Send tasks, get results back
#    • Stream or snapshot logs
#    • Toggle live log push on/off
#    • Download any workspace file
#    • Full status dashboard
#    • All secured by chat-ID whitelist
#
#  Setup (one-time):
#    1. Create bot via @BotFather → copy token
#    2. Start a chat with your bot → get your chat_id (use /id command)
#    3. Add both to Colab Secrets:
#         TELEGRAM_BOT_TOKEN  =  123456:ABC-xxx
#         TELEGRAM_CHAT_ID    =  987654321
#    4. Run:  tg = TelegramBot(hub);  tg.start()
#
#  Uses Telegram HTTP API with long-polling — no webhook, works in Colab.
# ══════════════════════════════════════════════════════════════════════════════


def _get_secret(key: str) -> str | None:
    """Read from Colab Secrets → env var → return None if missing."""
    # 1. Try Colab Secrets
    try:
        from google.colab import userdata
        val = userdata.get(key)
        if val and val.strip():
            return val.strip()
    except Exception:
        pass
    # 2. Fall back to environment variable
    val = os.environ.get(key, "").strip()
    return val if val else None


class TelegramBot:
    """
    Telegram bot that wraps the AutomationHub.
    Uses only the `requests` library + Telegram HTTP API — no third-party
    bot frameworks needed.

    Commands:
        /help              — list all commands
        /task <text>       — submit a task to the agent queue
        /status            — full automation hub status
        /queue             — task queue state (pending / done)
        /logs [n]          — last n log lines (default 30)
        /logs_on           — start streaming new logs to Telegram
        /logs_off          — stop log streaming
        /loglevel <level>  — set stream filter (debug/info/warning/error)
        /logfilter <text>  — only stream lines containing <text>
        /files [path]      — list workspace files
        /getfile <path>    — send a file from the workspace
        /memory            — show MEMORY.md contents
        /needs             — show pending needs
        /save              — trigger Drive save now
        /id                — show your chat_id (for whitelist setup)
    """

    _API = "https://api.telegram.org/bot{token}/{method}"

    # Maximum message length Telegram allows
    _MAX_MSG   = 4096
    # How often to push new logs when streaming is on (seconds)
    _LOG_PUSH_INTERVAL = 5
    # Long-poll timeout (seconds) — keeps connection alive, saves quota
    _POLL_TIMEOUT = 30

    def __init__(
        self,
        hub:            "AutomationHub | None" = None,
        token:          str | None = None,
        allowed_chat_ids: list[int] | None = None,
    ) -> None:
        """
        Args:
            hub:              AutomationHub from autorun(). If None, creates
                              a minimal standalone agent.
            token:            Bot token. If None, reads TELEGRAM_BOT_TOKEN
                              from Colab Secrets / env.
            allowed_chat_ids: Whitelist of Telegram chat IDs allowed to
                              control the bot. If None, reads TELEGRAM_CHAT_ID
                              from Colab Secrets / env (single user).
        """
        self._hub   = hub
        self._token = token or _get_secret("TELEGRAM_BOT_TOKEN")
        if not self._token:
            raise ValueError(
                "No Telegram bot token found.\n"
                "  Add TELEGRAM_BOT_TOKEN to Colab Secrets or set the env var."
            )

        # Whitelist
        if allowed_chat_ids:
            self._allowed: set[int] = set(allowed_chat_ids)
        else:
            raw = _get_secret("TELEGRAM_CHAT_ID")
            self._allowed = {int(raw)} if raw and raw.lstrip("-").isdigit() else set()

        # State
        self._stop_ev          = threading.Event()
        self._poll_thread:     threading.Thread | None = None
        self._log_thread:      threading.Thread | None = None
        self._log_streaming:   bool      = False
        self._log_level_filter: str | None = None
        self._log_text_filter:  str | None = None
        self._log_seen_count:  int        = 0
        self._update_offset:   int        = 0
        self._notify_queue:    queue.Queue = queue.Queue()   # outbound messages
        self._send_thread:     threading.Thread | None = None
        self._chat_mode_ids:   set[int]   = set()           # chats in direct-AI mode

        # Verify token works
        me = self._api("getMe")
        if not me.get("ok"):
            raise RuntimeError(f"Telegram token invalid: {me}")
        self._bot_name = me["result"].get("username", "agent_bot")
        print(f"✓ Telegram bot @{self._bot_name} initialised")
        if self._allowed:
            print(f"  Whitelisted chat IDs: {sorted(self._allowed)}")
        else:
            print("  ⚠  No whitelist set — send /id to your bot to get your chat_id,")
            print("     then add TELEGRAM_CHAT_ID to Colab Secrets and restart.")

    # ─────────────────────────────────────────────────────────────────
    #  HTTP helpers
    # ─────────────────────────────────────────────────────────────────

    def _api(self, method: str, **kwargs) -> dict:
        """Call a Telegram Bot API method. Returns the JSON response dict."""
        import requests
        url = self._API.format(token=self._token, method=method)
        try:
            resp = requests.post(url, timeout=35, **kwargs)
            return resp.json()
        except Exception as e:
            logger.warning(f"Telegram API error [{method}]: {e}")
            return {"ok": False, "error": str(e)}

    def _send(self, chat_id: int, text: str, parse_mode: str = "HTML") -> None:
        """Queue a message for delivery (non-blocking)."""
        self._notify_queue.put(("text", chat_id, text, parse_mode))

    def _send_file(self, chat_id: int, path: Path, caption: str = "") -> None:
        """Queue a file for delivery."""
        self._notify_queue.put(("file", chat_id, path, caption))

    def _chunk(self, text: str) -> list[str]:
        """Split long text into Telegram-safe chunks."""
        return [text[i: i + self._MAX_MSG] for i in range(0, max(len(text), 1), self._MAX_MSG)]

    # ─────────────────────────────────────────────────────────────────
    #  Send worker (drains _notify_queue sequentially)
    # ─────────────────────────────────────────────────────────────────

    def _send_worker(self) -> None:
        while not self._stop_ev.is_set() or not self._notify_queue.empty():
            try:
                item = self._notify_queue.get(timeout=1)
            except queue.Empty:
                continue
            kind = item[0]
            if kind == "text":
                _, chat_id, text, parse_mode = item
                for chunk in self._chunk(text):
                    self._api("sendMessage",
                              json={"chat_id": chat_id, "text": chunk,
                                    "parse_mode": parse_mode})
            elif kind == "file":
                _, chat_id, path, caption = item
                try:
                    with open(path, "rb") as fh:
                        self._api("sendDocument",
                                  data={"chat_id": chat_id, "caption": caption},
                                  files={"document": fh})
                except Exception as e:
                    self._api("sendMessage",
                              json={"chat_id": chat_id,
                                    "text": f"❌ Could not send file: {e}"})
            self._notify_queue.task_done()

    # ─────────────────────────────────────────────────────────────────
    #  Command dispatcher
    # ─────────────────────────────────────────────────────────────────

    def _dispatch(self, chat_id: int, text: str) -> None:
        """Parse and route a single incoming message."""
        text = text.strip()
        cmd, _, args = text.partition(" ")
        cmd = cmd.lower().lstrip("/")

        # ── Always-allowed ─────────────────────────────────────────
        if cmd == "id":
            self._send(chat_id, f"Your chat_id: <code>{chat_id}</code>")
            return
        if cmd in ("start", "help"):
            self._cmd_help(chat_id)
            return

        # ── Whitelist gate ─────────────────────────────────────────
        if self._allowed and chat_id not in self._allowed:
            self._send(chat_id,
                       "⛔ Unauthorised. Your chat_id is "
                       f"<code>{chat_id}</code> — add it to TELEGRAM_CHAT_ID.")
            return

        # ── Dispatch ───────────────────────────────────────────────
        handlers = {
            "task":      self._cmd_task,
            "t":         self._cmd_task,       # shorthand
            "status":    self._cmd_status,
            "queue":     self._cmd_queue,
            "logs":      self._cmd_logs,
            "logs_on":   self._cmd_logs_on,
            "logs_off":  self._cmd_logs_off,
            "loglevel":  self._cmd_loglevel,
            "logfilter": self._cmd_logfilter,
            "files":     self._cmd_files,
            "getfile":   self._cmd_getfile,
            "memory":    self._cmd_memory,
            "needs":     self._cmd_needs,
            "save":      self._cmd_save,
            "panel":     self._cmd_panel,      # ← main control panel
        }
        fn = handlers.get(cmd)
        if fn:
            try:
                fn(chat_id, args.strip())
            except Exception as e:
                self._send(chat_id, f"❌ Error: <code>{e}</code>")
                logger.error(f"Telegram cmd error [{cmd}]: {e}\n{traceback.format_exc()}")
        else:
            # Direct chat mode: every plain message goes straight to the AI
            if chat_id in self._chat_mode_ids:
                self._cmd_task(chat_id, text)
            elif len(text) > 8 and not text.startswith("/"):
                self._cmd_task(chat_id, text)
            else:
                self._send(chat_id, "❓ Unknown command. Send /help or /panel.")

    # ─────────────────────────────────────────────────────────────────
    #  Command implementations
    # ─────────────────────────────────────────────────────────────────

    def _cmd_help(self, chat_id: int, _: str = "") -> None:
        self._send(chat_id, (
            "🤖 <b>Agent Bot Commands</b>\n\n"
            "<b>🎛 Control Panel</b>\n"
            "  /panel         — interactive button control panel\n\n"
            "<b>Tasks</b>\n"
            "  /task &lt;text&gt;   — submit a task\n"
            "  /t &lt;text&gt;      — shorthand for /task\n"
            "  (or just send any message as a task)\n\n"
            "<b>Status</b>\n"
            "  /status        — full hub status\n"
            "  /queue         — task queue state\n"
            "  /memory        — show MEMORY.md\n"
            "  /needs         — pending needs\n\n"
            "<b>Logs</b>\n"
            "  /logs [n]      — last n lines (default 30)\n"
            "  /logs_on       — stream new logs here\n"
            "  /logs_off      — stop streaming\n"
            "  /loglevel &lt;l&gt;  — filter: debug/info/warning/error\n"
            "  /logfilter &lt;t&gt; — only lines containing t\n\n"
            "<b>Files</b>\n"
            "  /files [path]  — list workspace files\n"
            "  /getfile &lt;p&gt;  — send a file\n\n"
            "<b>System</b>\n"
            "  /save          — save workspace to Drive now\n"
            "  /id            — show your chat_id\n"
        ))

    def _cmd_task(self, chat_id: int, args: str) -> None:
        if not args:
            self._send(chat_id, "Usage: /task &lt;your task description&gt;")
            return
        if self._hub is None:
            self._send(chat_id, "❌ No AutomationHub attached. Run autorun() first.")
            return

        self._send(chat_id, f"📥 Queued: <i>{args[:200]}</i>")

        # Run in a thread so we can reply when done
        def _run():
            try:
                result = self._hub.agent.run(args)
                reply  = f"✅ <b>Done</b>\n\n{result[:3800]}"
                if len(result) > 3800:
                    reply += f"\n\n<i>…{len(result)-3800:,} chars truncated</i>"
            except Exception as e:
                reply = f"❌ Task failed: <code>{e}</code>"
            self._send(chat_id, reply)

        threading.Thread(target=_run, daemon=True).start()

    def _cmd_status(self, chat_id: int, _: str = "") -> None:
        if self._hub is None:
            self._send(chat_id, "❌ No hub attached.")
            return
        s  = self._hub.agent.counter.summary()
        hb = getattr(self._hub.agent, "_heartbeat_thread", None)

        def _dot(thread):
            return "🟢" if thread and thread.is_alive() else "🔴"

        lines = [
            "📊 <b>Agent Status</b>",
            f"  Model:      <code>{Config.MODEL}</code>",
            f"  Workspace:  <code>{Config.BASE_DIR}</code>",
            "",
            "<b>Threads</b>",
            f"  {_dot(hb)} heartbeat",
            f"  {_dot(self._hub.queue._thread)} task_queue",
            f"  {_dot(self._hub.scheduler._thread)} scheduler",
            f"  {_dot(self._hub.need_poller._thread)} need_poller",
            f"  {_dot(self._hub.watchdog._thread)} watchdog",
            f"  {'🟢' if self._log_streaming else '⚪'} log_stream",
            "",
            "<b>API Stats</b>",
            f"  Requests: {s['total_requests']} (✓{s['success']} ✗{s['errors']})",
            f"  Tokens:   {s['tokens_total']:,}",
            f"  Uptime:   {s['uptime_seconds']}s",
        ]
        self._send(chat_id, "\n".join(lines))

    def _cmd_queue(self, chat_id: int, _: str = "") -> None:
        if self._hub is None:
            self._send(chat_id, "❌ No hub attached.")
            return
        tq      = self._hub.queue
        pending = list(tq._q.queue)
        done    = tq._done[-5:]
        lines   = [f"📋 <b>Task Queue</b>  —  {len(pending)} pending  /  {len(tq._done)} done"]
        if tq._current:
            lines.append(f"\n▶ <b>Running:</b> {tq._current[:80]}")
        if pending:
            lines.append("\n<b>Pending:</b>")
            for i, item in enumerate(pending[:10]):
                lines.append(f"  {i+1}. {item['task'][:70]}")
        if done:
            lines.append("\n<b>Last completed:</b>")
            for item in done:
                ok = "✓" if not item.get("error") else "✗"
                lines.append(f"  {ok} {item['task'][:60]}")
        self._send(chat_id, "\n".join(lines))

    def _cmd_logs(self, chat_id: int, args: str) -> None:
        n = int(args) if args.isdigit() else 30
        records = logger.read_recent(n)
        if not records:
            self._send(chat_id, "No logs found.")
            return
        order  = ["debug", "info", "warning", "error"]
        emojis = {"debug": "⚙", "info": "ℹ", "warning": "⚠", "error": "❌"}
        lines  = [f"📋 <b>Last {len(records)} log records</b>\n"]
        for r in records:
            lvl    = r.get("level", "info")
            ts     = (r.get("ts") or "")[:16].replace("T", " ")
            msg    = (r.get("msg") or "")[:120]
            emoji  = emojis.get(lvl, "•")
            lines.append(f"{emoji} <code>{ts}</code>  {msg}")
        self._send(chat_id, "\n".join(lines))

    def _cmd_logs_on(self, chat_id: int, _: str = "") -> None:
        self._log_streaming  = True
        self._log_seen_count = len(logger.read_recent(9999))
        if not (self._log_thread and self._log_thread.is_alive()):
            self._log_thread = threading.Thread(
                target=self._log_push_loop,
                args=(chat_id,),
                daemon=True,
                name="tg-log-push",
            )
            self._log_thread.start()
        self._send(chat_id,
                   "🟢 <b>Log streaming ON</b>\n"
                   f"  Level filter: {self._log_level_filter or 'all'}\n"
                   f"  Text filter:  {self._log_text_filter or 'none'}\n"
                   "Send /logs_off to stop.")

    def _cmd_logs_off(self, chat_id: int, _: str = "") -> None:
        self._log_streaming = False
        self._send(chat_id, "⚪ Log streaming OFF.")

    def _cmd_loglevel(self, chat_id: int, args: str) -> None:
        valid = {"debug", "info", "warning", "error", "all"}
        lvl   = args.lower().strip()
        if lvl not in valid:
            self._send(chat_id, f"Valid levels: {', '.join(sorted(valid))}")
            return
        self._log_level_filter = None if lvl == "all" else lvl
        self._send(chat_id, f"Log level filter → <code>{lvl}</code>")

    def _cmd_logfilter(self, chat_id: int, args: str) -> None:
        self._log_text_filter = args.lower().strip() if args.strip() else None
        self._send(chat_id,
                   f"Log text filter → <code>{self._log_text_filter or 'none'}</code>")

    def _cmd_files(self, chat_id: int, args: str) -> None:
        sub_path = args.strip() or ""
        try:
            target = (Config.FILES_DIR / sub_path) if sub_path else Config.FILES_DIR
            if not target.exists():
                self._send(chat_id, f"❌ Path not found: {target}")
                return
            entries = sorted(target.rglob("*"))
            files   = [e for e in entries if e.is_file()][:50]
            lines   = [f"📁 <b>{target}</b>  ({len(files)} files)\n"]
            for f in files:
                size = f.stat().st_size
                rel  = str(f.relative_to(Config.FILES_DIR))
                sz   = f"{size/1024:.1f}KB" if size > 1024 else f"{size}B"
                lines.append(f"  📄 <code>{rel}</code>  <i>{sz}</i>")
            self._send(chat_id, "\n".join(lines))
        except Exception as e:
            self._send(chat_id, f"❌ {e}")

    def _cmd_getfile(self, chat_id: int, args: str) -> None:
        if not args:
            self._send(chat_id, "Usage: /getfile &lt;relative/path/to/file&gt;")
            return
        # Try workspace-relative first, then absolute
        candidates = [
            Config.FILES_DIR / args,
            Config.BASE_DIR  / args,
            Path(args),
        ]
        target = next((p for p in candidates if p.exists() and p.is_file()), None)
        if not target:
            self._send(chat_id, f"❌ File not found: <code>{args}</code>")
            return
        size_mb = target.stat().st_size / (1024 * 1024)
        if size_mb > 49:
            self._send(chat_id,
                       f"❌ File too large ({size_mb:.1f} MB). Telegram limit is 50 MB.")
            return
        self._send(chat_id, f"📤 Sending <code>{target.name}</code>…")
        self._send_file(chat_id, target, caption=str(target.relative_to(Config.BASE_DIR)))

    def _cmd_memory(self, chat_id: int, _: str = "") -> None:
        content = FileManager.read_file(Config.CORE_DIR / "MEMORY.md", default="*(empty)*")
        self._send(chat_id, f"🧠 <b>MEMORY.md</b>\n\n{content[:3800]}")

    def _cmd_needs(self, chat_id: int, _: str = "") -> None:
        needs = check_needs()
        if not needs:
            self._send(chat_id, "✅ No pending needs.")
            return
        lines = [f"🔔 <b>{len(needs)} Pending Need(s)</b>\n"]
        for i, n in enumerate(needs, 1):
            lines.append(
                f"<b>{i}. [{n.get('priority','?')}]</b> {n.get('needs','?')}\n"
                f"   Reason:   {n.get('reason','?')}\n"
                f"   Blocking: {n.get('blocking','?')}"
            )
        self._send(chat_id, "\n".join(lines))

    def _cmd_save(self, chat_id: int, _: str = "") -> None:
        if self._hub is None:
            self._send(chat_id, "❌ No hub attached.")
            return
        self._send(chat_id, "💾 Saving workspace to Drive…")
        result = self._hub.persistence.save(label="telegram")
        if result:
            self._send(chat_id, f"✅ Saved → <code>{result}</code>")
        else:
            self._send(chat_id, "❌ Save failed — check Drive is mounted.")

    # ─────────────────────────────────────────────────────────────────
    #  Inline keyboard helpers
    # ─────────────────────────────────────────────────────────────────

    def _keyboard(self, rows: list[list[tuple[str, str]]]) -> dict:
        """
        Build an inline_keyboard reply_markup.
        Each row is a list of (label, callback_data) tuples.

        Example:
            self._keyboard([
                [("⏸ Pause", "queue:pause"), ("▶ Resume", "queue:resume")],
                [("🛑 Stop All", "master:stop")],
            ])
        """
        return {
            "inline_keyboard": [
                [{"text": label, "callback_data": data} for label, data in row]
                for row in rows
            ]
        }

    def _send_kb(
        self,
        chat_id:  int,
        text:     str,
        rows:     list[list[tuple[str, str]]],
        parse_mode: str = "HTML",
    ) -> dict:
        """Send a message with an inline keyboard. Returns the API response."""
        return self._api(
            "sendMessage",
            json={
                "chat_id":      chat_id,
                "text":         text,
                "parse_mode":   parse_mode,
                "reply_markup": self._keyboard(rows),
            },
        )

    def _edit_kb(
        self,
        chat_id:    int,
        message_id: int,
        text:       str,
        rows:       list[list[tuple[str, str]]] | None = None,
        parse_mode: str = "HTML",
    ) -> None:
        """Edit an existing message's text and optionally its keyboard."""
        payload: dict[str, Any] = {
            "chat_id":    chat_id,
            "message_id": message_id,
            "text":       text,
            "parse_mode": parse_mode,
        }
        if rows is not None:
            payload["reply_markup"] = self._keyboard(rows)
        else:
            payload["reply_markup"] = {"inline_keyboard": []}
        self._api("editMessageText", json=payload)

    def _answer_cb(self, callback_query_id: str, text: str = "", alert: bool = False) -> None:
        """Acknowledge a callback query (removes the loading spinner)."""
        self._api("answerCallbackQuery", json={
            "callback_query_id": callback_query_id,
            "text":  text,
            "show_alert": alert,
        })

    # ─────────────────────────────────────────────────────────────────
    #  Callback query dispatcher
    # ─────────────────────────────────────────────────────────────────

    def _handle_callback(self, cb: dict) -> None:
        """Route an inline button press to the correct handler."""
        cq_id      = cb["id"]
        chat_id    = cb["message"]["chat"]["id"]
        message_id = cb["message"]["message_id"]
        data       = cb.get("data", "")

        # Whitelist gate
        if self._allowed and chat_id not in self._allowed:
            self._answer_cb(cq_id, "⛔ Unauthorised.", alert=True)
            return

        # ── Route by prefix ─────────────────────────────────────────
        prefix, _, arg = data.partition(":")

        if prefix == "master":
            self._cb_master(cq_id, chat_id, message_id, arg)
        elif prefix == "queue":
            self._cb_queue(cq_id, chat_id, message_id, arg)
        elif prefix == "clear":
            self._cb_clear(cq_id, chat_id, message_id, arg)
        elif prefix == "logs":
            self._cb_logs(cq_id, chat_id, message_id, arg)
        elif prefix == "chat":
            self._cb_chat_mode(cq_id, chat_id, message_id, arg)
        elif prefix == "panel":
            self._answer_cb(cq_id)
            self._cmd_panel(chat_id, "")
        else:
            self._answer_cb(cq_id, "Unknown action.")

    # ─────────────────────────────────────────────────────────────────
    #  Button action handlers
    # ─────────────────────────────────────────────────────────────────

    def _cb_master(self, cq_id: str, chat_id: int, msg_id: int, arg: str) -> None:
        if arg == "stop":
            self._answer_cb(cq_id, "🛑 Stopping everything…", alert=True)
            self._edit_kb(chat_id, msg_id,
                          "🛑 <b>Master Stop triggered.</b>\nAll services shutting down…")
            # Run in thread so answer_cb delivers before the bot stops
            def _do_stop():
                time.sleep(0.5)
                if self._hub:
                    self._hub.stop_all()
                self.stop()
            threading.Thread(target=_do_stop, daemon=True).start()

        elif arg == "stop_confirm":
            # Show confirmation before stopping
            self._answer_cb(cq_id)
            self._edit_kb(chat_id, msg_id,
                "⚠️ <b>Confirm Master Stop?</b>\n"
                "This stops ALL services: queue, scheduler, heartbeat, autosave.",
                rows=[
                    [("🛑 Yes, stop everything", "master:stop")],
                    [("❌ Cancel",               "panel:")],
                ],
            )

        elif arg == "save_noop":
            # Save button on panel — trigger and update message
            self._answer_cb(cq_id, "💾 Saving…")
            if self._hub:
                result = self._hub.persistence.save(label="telegram-panel")
                ok = "✅ Saved." if result else "❌ Save failed."
            else:
                ok = "❌ No hub attached."
            self._edit_kb(chat_id, msg_id,
                f"💾 <b>{ok}</b>",
                rows=[[("🔙 Panel", "panel:")]],
            )

    def _cb_queue(self, cq_id: str, chat_id: int, msg_id: int, arg: str) -> None:
        if self._hub is None:
            self._answer_cb(cq_id, "No hub attached.", alert=True)
            return

        if arg == "pause":
            self._hub.queue._paused = True
            self._answer_cb(cq_id, "⏸ Queue paused.")
            self._edit_kb(chat_id, msg_id,
                "⏸ <b>Task Queue Paused</b>\nCurrent task finishes, then queue stops.",
                rows=[[("▶ Resume Queue", "queue:resume"),
                       ("🗑 Clear All",    "clear:all"),
                       ("🔙 Panel",        "panel:")]],
            )

        elif arg == "resume":
            self._hub.queue._paused = False
            # Restart worker thread if it stopped
            if not (self._hub.queue._thread and self._hub.queue._thread.is_alive()):
                self._hub.queue.start()
            self._answer_cb(cq_id, "▶ Queue resumed.")
            self._edit_kb(chat_id, msg_id,
                "▶ <b>Task Queue Resumed</b>",
                rows=[[("⏸ Pause Queue", "queue:pause"),
                       ("🗑 Clear All",   "clear:all"),
                       ("🔙 Panel",       "panel:")]],
            )

        elif arg == "show":
            self._answer_cb(cq_id)
            pending = list(self._hub.queue._q.queue)
            if not pending:
                self._edit_kb(chat_id, msg_id,
                    "📋 <b>Queue is empty.</b>",
                    rows=[[("🔙 Panel", "panel:")]],
                )
                return
            rows: list[list[tuple[str, str]]] = []
            lines = [f"📋 <b>Queue  —  {len(pending)} pending</b>\n"]
            for i, item in enumerate(pending[:8]):
                short = item["task"][:40]
                lines.append(f"  {i+1}. {short}")
                rows.append([(f"❌ Remove #{i+1}: {short[:25]}", f"clear:task:{i}")])
            rows.append([("🗑 Clear All", "clear:all"), ("🔙 Panel", "panel:")])
            self._edit_kb(chat_id, msg_id, "\n".join(lines), rows=rows)

    def _cb_clear(self, cq_id: str, chat_id: int, msg_id: int, arg: str) -> None:
        if self._hub is None:
            self._answer_cb(cq_id, "No hub attached.", alert=True)
            return

        if arg == "all":
            self._hub.queue.clear()
            self._answer_cb(cq_id, "🗑 All queued tasks cleared.")
            self._edit_kb(chat_id, msg_id,
                "🗑 <b>Queue cleared.</b> All pending tasks removed.",
                rows=[[("🔙 Panel", "panel:")]],
            )

        elif arg.startswith("task:"):
            idx = int(arg.split(":")[1])
            pending = list(self._hub.queue._q.queue)
            if idx < len(pending):
                removed = pending.pop(idx)
                # Rebuild the queue without that item
                self._hub.queue._q.queue.clear()
                for item in pending:
                    self._hub.queue._q.put(item)
                self._answer_cb(cq_id, f"❌ Removed: {removed['task'][:40]}")
                # Refresh the queue view
                self._cb_queue(cq_id, chat_id, msg_id, "show")
            else:
                self._answer_cb(cq_id, "Task not found (may have already run).", alert=True)

    def _cb_logs(self, cq_id: str, chat_id: int, msg_id: int, arg: str) -> None:
        if arg == "on":
            self._log_streaming  = True
            self._log_seen_count = len(logger.read_recent(9999))
            if not (self._log_thread and self._log_thread.is_alive()):
                self._log_thread = threading.Thread(
                    target=self._log_push_loop, args=(chat_id,),
                    daemon=True, name="tg-log-push",
                )
                self._log_thread.start()
            self._answer_cb(cq_id, "📡 Log streaming ON")
            self._edit_kb(chat_id, msg_id,
                "📡 <b>Log streaming ON</b>\nNew log lines pushed every 5s.",
                rows=[
                    [("⚠ Warnings only", "logs:level:warning"),
                     ("❌ Errors only",   "logs:level:error")],
                    [("⚙ All levels",    "logs:level:all")],
                    [("⏹ Stop streaming","logs:off"),
                     ("🔙 Panel",         "panel:")],
                ],
            )

        elif arg == "off":
            self._log_streaming = False
            self._answer_cb(cq_id, "⚪ Log streaming OFF")
            self._edit_kb(chat_id, msg_id,
                "⚪ <b>Log streaming stopped.</b>",
                rows=[[("📡 Start streaming", "logs:on"), ("🔙 Panel", "panel:")]],
            )

        elif arg.startswith("level:"):
            lvl = arg.split(":")[1]
            self._log_level_filter = None if lvl == "all" else lvl
            self._answer_cb(cq_id, f"Level filter → {lvl}")
            self._edit_kb(chat_id, msg_id,
                f"📡 <b>Log streaming ON</b>  —  level: <code>{lvl}</code>",
                rows=[
                    [("⚠ Warnings only", "logs:level:warning"),
                     ("❌ Errors only",   "logs:level:error"),
                     ("⚙ All",           "logs:level:all")],
                    [("⏹ Stop streaming", "logs:off"), ("🔙 Panel", "panel:")],
                ],
            )

    def _cb_chat_mode(self, cq_id: str, chat_id: int, msg_id: int, arg: str) -> None:
        if arg == "on":
            self._chat_mode_ids.add(chat_id)
            self._answer_cb(cq_id, "💬 Direct chat mode ON")
            self._edit_kb(chat_id, msg_id,
                "💬 <b>Direct Chat Mode ON</b>\n"
                "Every message you send now goes directly to the AI.\n"
                "Send /panel or tap the button below to turn it off.",
                rows=[[("⏹ Exit chat mode", "chat:off"), ("🔙 Panel", "panel:")]],
            )
        elif arg == "off":
            self._chat_mode_ids.discard(chat_id)
            self._answer_cb(cq_id, "💬 Direct chat mode OFF")
            self._edit_kb(chat_id, msg_id,
                "💬 <b>Direct Chat Mode OFF</b>\nMessages now require /task prefix.",
                rows=[[("💬 Enable chat mode", "chat:on"), ("🔙 Panel", "panel:")]],
            )

    # ─────────────────────────────────────────────────────────────────
    #  Control panel command  (/panel)
    # ─────────────────────────────────────────────────────────────────

    def _cmd_panel(self, chat_id: int, _: str = "") -> None:
        """Send the main control panel with all action buttons."""
        if self._hub is None:
            self._send(chat_id, "❌ No hub attached. Run autorun() first.")
            return

        # Build live status line
        def _dot(t): return "🟢" if t and t.is_alive() else "🔴"
        paused = getattr(self._hub.queue, "_paused", False)
        qsize  = self._hub.queue._q.qsize()
        chat_on = chat_id in self._chat_mode_ids
        log_on  = self._log_streaming

        status_lines = [
            "🎛 <b>Control Panel</b>",
            "",
            f"  {_dot(self._hub.queue._thread)}  task_queue"
            + (f"  ⏸ paused" if paused else f"  ({qsize} pending)"),
            f"  {_dot(self._hub.scheduler._thread)}  scheduler",
            f"  {_dot(self._hub.need_poller._thread)}  need_poller",
            f"  {_dot(self._hub.watchdog._thread)}  watchdog",
            f"  {'📡' if log_on else '⚪'}  log stream",
            f"  {'💬' if chat_on else '⚪'}  direct chat",
        ]

        rows: list[list[tuple[str, str]]] = [
            # Row 1 — Queue controls
            [
                ("⏸ Pause Queue",  "queue:pause")  if not paused else ("▶ Resume Queue", "queue:resume"),
                ("📋 View Queue",   "queue:show"),
                ("🗑 Clear All",    "clear:all"),
            ],
            # Row 2 — Log streaming
            [
                ("📡 Logs ON",  "logs:on")  if not log_on else ("⏹ Logs OFF", "logs:off"),
            ],
            # Row 3 — Direct chat
            [
                ("💬 Chat Mode ON",  "chat:on")  if not chat_on else ("💬 Chat Mode OFF", "chat:off"),
            ],
            # Row 4 — Save + Danger
            [
                ("💾 Save to Drive", "master:save_noop"),
            ],
            # Row 5 — Master stop (confirmation required)
            [
                ("🛑 MASTER STOP", "master:stop_confirm"),
            ],
        ]
        self._send_kb(chat_id, "\n".join(status_lines), rows)



    def _log_push_loop(self, chat_id: int) -> None:
        order  = ["debug", "info", "warning", "error"]
        emojis = {"debug": "⚙", "info": "ℹ", "warning": "⚠", "error": "❌"}

        while self._log_streaming and not self._stop_ev.is_set():
            time.sleep(self._LOG_PUSH_INTERVAL)
            try:
                all_records = logger.read_recent(500)
                new = all_records[self._log_seen_count:]
                if not new:
                    continue
                self._log_seen_count = len(all_records)

                # Apply filters
                if self._log_level_filter and self._log_level_filter in order:
                    min_i = order.index(self._log_level_filter)
                    new = [r for r in new
                           if order.index(r.get("level", "debug")) >= min_i]
                if self._log_text_filter:
                    new = [r for r in new
                           if self._log_text_filter in (r.get("msg") or "").lower()]

                if not new:
                    continue

                # Bundle into one message (max 20 lines per push)
                lines = [f"📡 <b>Live Logs</b>  (+{len(new)} new)\n"]
                for r in new[-20:]:
                    lvl   = r.get("level", "info")
                    ts    = (r.get("ts") or "")[:16].replace("T", " ")
                    msg   = (r.get("msg") or "")[:100]
                    emoji = emojis.get(lvl, "•")
                    lines.append(f"{emoji} <code>{ts}</code>  {msg}")

                if len(new) > 20:
                    lines.append(f"\n<i>…{len(new)-20} more lines omitted</i>")

                self._send(chat_id, "\n".join(lines))
            except Exception as e:
                logger.warning(f"Telegram log push error: {e}")

    # ─────────────────────────────────────────────────────────────────
    #  BotFather command registration
    # ─────────────────────────────────────────────────────────────────

    # Full command list registered with Telegram so they appear as
    # autocomplete suggestions when the user types "/" in the chat.
    _BOT_COMMANDS: list[dict[str, str]] = [
        # ── Control panel ────────────────────────────────────────────
        {"command": "panel",      "description": "🎛 Interactive control panel with buttons"},
        {"command": "status",     "description": "📊 Full hub & service status"},
        # ── Tasks ────────────────────────────────────────────────────
        {"command": "task",       "description": "🤖 Submit a task  e.g. /task write a readme"},
        {"command": "t",          "description": "⚡ Shorthand for /task"},
        {"command": "queue",      "description": "📋 View task queue (pending / running / done)"},
        # ── Logs ─────────────────────────────────────────────────────
        {"command": "logs",       "description": "📄 Last N log lines  e.g. /logs 50"},
        {"command": "logs_on",    "description": "📡 Start streaming live logs here"},
        {"command": "logs_off",   "description": "⏹ Stop log streaming"},
        {"command": "loglevel",   "description": "🔍 Set stream filter  e.g. /loglevel warning"},
        {"command": "logfilter",  "description": "🔎 Filter by text  e.g. /logfilter tool"},
        # ── Memory & needs ───────────────────────────────────────────
        {"command": "memory",     "description": "🧠 Show MEMORY.md contents"},
        {"command": "needs",      "description": "🔔 Show pending agent needs"},
        # ── Files ────────────────────────────────────────────────────
        {"command": "files",      "description": "📁 List workspace files  e.g. /files src/"},
        {"command": "getfile",    "description": "📤 Download a file  e.g. /getfile reports/x.md"},
        # ── System ───────────────────────────────────────────────────
        {"command": "save",       "description": "💾 Save workspace to Google Drive now"},
        {"command": "id",         "description": "🪪 Show your Telegram chat_id"},
        {"command": "help",       "description": "❓ Show all commands"},
    ]

    def _register_commands(self) -> None:
        """
        Register all commands with Telegram via setMyCommands so they appear
        as autocomplete suggestions when the user types '/' in the chat.
        Called automatically during start().
        """
        resp = self._api("setMyCommands", json={"commands": self._BOT_COMMANDS})
        if resp.get("ok"):
            logger.info(f"Registered {len(self._BOT_COMMANDS)} bot commands with Telegram")
            print(f"  ✓ {len(self._BOT_COMMANDS)} commands registered with Telegram")
        else:
            logger.warning(f"setMyCommands failed: {resp}")
            print(f"  ⚠  Could not register commands: {resp.get('description', resp)}")



    def _poll_loop(self) -> None:
        logger.info("Telegram bot polling started")
        while not self._stop_ev.is_set():
            try:
                resp = self._api(
                    "getUpdates",
                    json={
                        "offset":  self._update_offset,
                        "timeout": self._POLL_TIMEOUT,
                        "allowed_updates": ["message", "callback_query"],
                    },
                )
                if not resp.get("ok"):
                    time.sleep(5)
                    continue
                for update in resp.get("result", []):
                    self._update_offset = update["update_id"] + 1

                    # ── Plain message ─────────────────────────────
                    msg = update.get("message", {})
                    if msg:
                        chat_id = msg.get("chat", {}).get("id")
                        text    = msg.get("text", "").strip()
                        if chat_id and text:
                            threading.Thread(
                                target=self._dispatch,
                                args=(chat_id, text),
                                daemon=True,
                            ).start()

                    # ── Inline button press ───────────────────────
                    cb = update.get("callback_query")
                    if cb:
                        threading.Thread(
                            target=self._handle_callback,
                            args=(cb,),
                            daemon=True,
                        ).start()

            except Exception as e:
                logger.warning(f"Telegram poll error: {e}")
                time.sleep(5)

    # ─────────────────────────────────────────────────────────────────
    #  Lifecycle
    # ─────────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start polling and the send worker. Non-blocking."""
        if self._poll_thread and self._poll_thread.is_alive():
            print("ℹ  Telegram bot already running.")
            return
        self._stop_ev.clear()

        self._send_thread = threading.Thread(
            target=self._send_worker, daemon=True, name="tg-send"
        )
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="tg-poll"
        )
        self._send_thread.start()
        self._poll_thread.start()

        # Register commands with Telegram (shows autocomplete when typing /)
        self._register_commands()

        print(f"🤖 @{self._bot_name} is online and listening.")
        print(f"   Open Telegram and send /help to @{self._bot_name}")

        # Notify all whitelisted users that the bot is online
        self._broadcast_startup()

    def _broadcast_startup(self) -> None:
        """Send a startup notification to all whitelisted chat IDs."""
        if not self._allowed:
            return
        # Give the send worker a moment to be ready
        time.sleep(0.5)
        ts    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            "🟢 <b>Agent Bot Online</b>",
            f"  Time:      <code>{ts}</code>",
            f"  Model:     <code>{Config.MODEL}</code>",
            f"  Workspace: <code>{Config.BASE_DIR}</code>",
            "",
            "<b>Services running:</b>",
            f"  {'🟢' if self._hub and self._hub.queue._thread and self._hub.queue._thread.is_alive() else '⚪'} task_queue",
            f"  {'🟢' if self._hub and self._hub.scheduler._thread and self._hub.scheduler._thread.is_alive() else '⚪'} scheduler",
            f"  {'🟢' if self._hub and self._hub.need_poller._thread and self._hub.need_poller._thread.is_alive() else '⚪'} need_poller",
            f"  {'🟢' if self._hub and self._hub.watchdog._thread and self._hub.watchdog._thread.is_alive() else '⚪'} watchdog",
            f"  {'🟢' if self._hub and self._hub.persistence._autosave_thread and self._hub.persistence._autosave_thread.is_alive() else '⚪'} autosave",
            "",
            "Send /help for all commands.",
        ]
        for cid in self._allowed:
            self._send(cid, "\n".join(lines))

    def stop(self) -> None:
        """Gracefully stop the bot and notify all whitelisted users."""
        # Send shutdown notice before stopping the send worker
        self._broadcast_shutdown()
        # Give the send worker time to flush the shutdown message
        time.sleep(2)
        self._stop_ev.set()
        self._log_streaming = False
        print(f"⏹  @{self._bot_name} stopped.")

    def _broadcast_shutdown(self) -> None:
        """Send a shutdown notification to all whitelisted chat IDs."""
        if not self._allowed:
            return
        ts    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        uptime = ""
        if self._hub:
            s      = self._hub.agent.counter.summary()
            uptime = (
                f"\n<b>Session summary:</b>\n"
                f"  Requests: {s['total_requests']} (✓{s['success']} ✗{s['errors']})\n"
                f"  Tokens:   {s['tokens_total']:,}\n"
                f"  Uptime:   {s['uptime_seconds']}s"
            )
        msg = (
            f"🔴 <b>Agent Bot Offline</b>\n"
            f"  Time: <code>{ts}</code>"
            f"{uptime}\n\n"
            f"<i>Workspace saved to Drive before shutdown.</i>"
        )
        for cid in self._allowed:
            self._send(cid, msg)

    def notify(self, text: str) -> None:
        """
        Push a message to all whitelisted chat IDs from code.
        Useful for task completion alerts from other cells.

        Usage:
            tg.notify("✅ Daily report generated!")
        """
        if not self._allowed:
            print("⚠  No whitelisted chat IDs — cannot send notification.")
            return
        for cid in self._allowed:
            self._send(cid, text)

    def notify_service(self, service: str, state: str, detail: str = "") -> None:
        """
        Push a service state-change notification.

        Args:
            service: service name e.g. 'task_queue', 'scheduler'
            state:   'started' | 'stopped' | 'restarted' | 'error'
            detail:  optional extra context

        Usage:
            tg.notify_service("scheduler", "started")
            tg.notify_service("task_queue", "error", "thread died unexpectedly")
        """
        icons = {
            "started":   "🟢",
            "stopped":   "⚪",
            "restarted": "🔄",
            "error":     "🔴",
        }
        icon = icons.get(state, "•")
        ts   = datetime.now().strftime("%H:%M:%S")
        msg  = f"{icon} <b>{service}</b> {state}"
        if detail:
            msg += f"\n  <i>{detail}</i>"
        msg += f"\n  <code>{ts}</code>"
        self.notify(msg)

    @property
    def running(self) -> bool:
        return bool(self._poll_thread and self._poll_thread.is_alive())


# ══════════════════════════════════════════════════════════════════════════════
#  Convenience launcher
# ══════════════════════════════════════════════════════════════════════════════

def start_telegram(
    hub:   "AutomationHub | None" = None,
    token: str | None = None,
) -> TelegramBot:
    """
    Create and start the Telegram bot in one call.
    Also patches AutomationHub and Watchdog to send service notifications.

    Usage:
        tg = start_telegram(hub)           # with automation hub
        tg = start_telegram()              # standalone (reads secrets)
        tg.notify("Task finished!")        # push a message from code
        tg.stop()
    """
    tg = TelegramBot(hub=hub, token=token)
    tg.start()

    # ── Patch Watchdog to notify on restarts ──────────────────────────
    if hub is not None and not getattr(hub.watchdog, "_tg_patched", False):
        _orig_restart = Watchdog._check_and_restart.__func__

        @staticmethod
        def _notifying_restart(name, thread, restart):
            if thread is not None and not thread.is_alive():
                tg.notify_service(name, "restarted", "thread died — watchdog recovered it")
            _orig_restart(name, thread, restart)

        Watchdog._check_and_restart = _notifying_restart
        hub.watchdog._tg_patched = True

    # ── Patch TaskQueue worker to notify on task completion ───────────
    if hub is not None and not getattr(hub.queue, "_tg_patched", False):
        _orig_worker = hub.queue._worker

        def _notifying_worker():
            # Wrap _worker to intercept completions already logged in _done
            seen = 0
            import threading as _t
            # We can't easily wrap _worker mid-run, so patch _dispatch instead
            _orig_dispatch = hub.queue._worker

        # Simpler: patch the queue's done list via post-run hook on the thread
        # Instead, patch TaskQueue._worker cleanly:
        _orig_q_worker = TaskQueue._worker

        def _patched_q_worker(self_q):
            while not self_q._stop.is_set():
                try:
                    item = self_q._q.get(timeout=2)
                except __import__("queue").Empty:
                    continue
                self_q._current = item["task"]
                item["started"]  = datetime.now().isoformat()
                item["status"]   = "running"
                tg.notify(
                    f"▶ <b>Task started</b>\n"
                    f"  <i>{item['task'][:120]}</i>"
                )
                try:
                    result          = self_q._agent.run(item["task"])
                    item["result"]  = result[:500]
                    item["status"]  = "done"
                    tg.notify(
                        f"✅ <b>Task done</b>\n"
                        f"  <i>{item['task'][:80]}</i>\n\n"
                        f"{result[:800]}"
                        + (f"\n<i>…{len(result)-800:,} chars truncated</i>" if len(result) > 800 else "")
                    )
                except Exception as e:
                    item["error"]  = str(e)
                    item["status"] = "error"
                    tg.notify(
                        f"❌ <b>Task failed</b>\n"
                        f"  <i>{item['task'][:80]}</i>\n"
                        f"  <code>{e}</code>"
                    )
                finally:
                    item["finished"] = datetime.now().isoformat()
                    with self_q._lock:
                        self_q._done.append(item)
                        if len(self_q._done) > 100:
                            self_q._done = self_q._done[-100:]
                    self_q._current = None
                    self_q._q.task_done()

        if not getattr(TaskQueue._worker, "_tg_patched", False):
            TaskQueue._worker = _patched_q_worker
            TaskQueue._worker._tg_patched = True
            hub.queue._tg_patched = True

    return tg


print("✓ Telegram bot cell ready")
print()
print("  Setup:")
print("    1. Create bot via @BotFather → copy token")
print("    2. Add to Colab Secrets:")
print("         TELEGRAM_BOT_TOKEN = 123456:ABC-xxx")
print("         TELEGRAM_CHAT_ID   = 987654321  (send /id to bot to find yours)")
print()
print("  Launch:")
print("    tg = start_telegram(hub)       # hub from autorun()")
print("    tg = start_telegram()          # without hub")
print()
print("  From code:")
print("    tg.notify('✅ Task done!')")
print("    tg.stop()")