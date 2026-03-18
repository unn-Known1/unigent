from __future__ import annotations

import os, sys, json, re, time, signal, hashlib, logging, logging.handlers
import traceback, tempfile, textwrap, importlib.util, subprocess, resource
import functools, threading, concurrent.futures, inspect
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from urllib.parse import urlparse

# Detect Colab environment
try:
    import google.colab
    IS_COLAB = True
except ImportError:
    IS_COLAB = False

#  CONFIGURATION
_DEFAULT_ROOT = Path("/content/agent") if IS_COLAB else Path.home() / "agent_workspace"
MODEL         = "stepfun-ai/step-3.5-flash"
BASE_URL      = "https://integrate.api.nvidia.com/v1"
MAX_TOKENS    = 131_072
MAX_ITERS     = 1000
CODE_TIMEOUT  = 60
SHELL_TIMEOUT = 120
WEB_TIMEOUT   = 20

def _cfg_int(env: str, default: int) -> int:
    if raw := os.environ.get(env, ""):
        try:
            return int(raw)
        except ValueError:
            print(f"  \033[93m[Config] WARNING: {env}={raw!r} not int; using {default}\033[0m",
                  file=sys.stderr)
    return default

class Config:
    """Central configuration — override via environment variables."""
    # Model
    MODEL    = os.environ.get("NVIDIA_MODEL",    MODEL)
    BASE_URL = os.environ.get("NVIDIA_BASE_URL", BASE_URL)
    MAX_TOKENS = _cfg_int("AGENT_MAX_TOKENS", MAX_TOKENS)
    MAX_ITERS  = _cfg_int("AGENT_MAX_ITERS",  MAX_ITERS)

    # Directories
    BASE_DIR    = Path(os.environ.get("AGENT_WORKSPACE", str(_DEFAULT_ROOT)))
    FILES_DIR   = BASE_DIR / "files"
    SUBTOOL_DIR = BASE_DIR / "subtools"
    SKILLS_DIR  = BASE_DIR / "skills"
    CORE_DIR    = BASE_DIR / "core"
    WORK_DIR    = BASE_DIR / "work"
    MEMORY_DIR  = BASE_DIR / "memory"
    MEM_FILE    = BASE_DIR / "memory.json"
    STATS_FILE  = BASE_DIR / "api_stats.jsonl"

    # Timeouts
    CODE_TIMEOUT  = _cfg_int("AGENT_CODE_TIMEOUT",  CODE_TIMEOUT)
    SHELL_TIMEOUT = _cfg_int("AGENT_SHELL_TIMEOUT", SHELL_TIMEOUT)
    WEB_TIMEOUT   = _cfg_int("AGENT_WEB_TIMEOUT",   WEB_TIMEOUT)

    # Limits
    MAX_FILE_SIZE_MB         = 50
    MAX_WEB_CONTENT          = 15_000
    MAX_PYTHON_MEMORY_MB     = 256
    SYSTEM_PROMPT_MEM_LIMIT  = 25
    SYSTEM_PROMPT_TOOL_LIMIT = 15
    MAX_PARALLEL_REQUESTS    = 8
    CONTEXT_TOKEN_BUDGET     = _cfg_int("AGENT_CTX_BUDGET", 245_000)
    TOOL_RESULT_CHARS_MAX    = 50_000
    TOOL_CACHE_SIZE          = 256
    API_RETRY_MAX            = 5
    API_RETRY_BACKOFF        = 1.5
    SKILLS_CACHE_TTL         = 30.0
    HEARTBEAT_INTERVAL       = _cfg_int("AGENT_HEARTBEAT_INTERVAL", 1800)

    @classmethod
    def setup_dirs(cls):
        for d in (cls.FILES_DIR, cls.SUBTOOL_DIR, cls.SKILLS_DIR,
                  cls.CORE_DIR, cls.WORK_DIR, cls.MEMORY_DIR):
            d.mkdir(parents=True, exist_ok=True)

    @classmethod
    def validate(cls):
        for ok, msg in (
            (cls.MAX_TOKENS > 0,                "MAX_TOKENS must be positive"),
            (cls.MAX_ITERS > 0,                 "MAX_ITERS must be positive"),
            (cls.CODE_TIMEOUT > 0,              "CODE_TIMEOUT must be positive"),
            (cls.CONTEXT_TOKEN_BUDGET > 10_000, "CONTEXT_TOKEN_BUDGET too small"),
            (cls.TOOL_RESULT_CHARS_MAX > 0,     "TOOL_RESULT_CHARS_MAX must be positive"),
            (cls.MAX_FILE_SIZE_MB > 0,          "MAX_FILE_SIZE_MB must be positive"),
            (cls.HEARTBEAT_INTERVAL >= 60,      "HEARTBEAT_INTERVAL must be >= 60 s"),
        ):
            if not ok:
                raise ValueError(f"[Config] {msg}")

Config.setup_dirs()
Config.validate()

print(f"✓ Configuration loaded")
print(f"  Workspace: {Config.BASE_DIR}")
print(f"  Model: {Config.MODEL}")

class AgentLogger:
    """JSON-structured rotating logger + colour console output + daily log files."""

    LOG_FILE = Config.BASE_DIR / "agent.log"

    _LEVELS: dict[str, int] = {
        "DEBUG":   logging.DEBUG,
        "INFO":    logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR":   logging.ERROR,
    }
    _COLORS: dict[str, str] = {
        "debug":   "\033[90m",
        "info":    "\033[97m",
        "warning": "\033[93m",
        "error":   "\033[91m",
    }

    # Limits applied when emitting / formatting records
    _MSG_MAX   = 10_000
    _EXTRA_MAX = 2_000
    _FMT_EXTRA = 5   # max extra fields shown in format_record
    _FMT_MSG   = 200 # max msg chars shown in format_record

    # Rotating file handler settings
    _ROTATE_BYTES   = 10 * 1024 * 1024  # 10 MB
    _ROTATE_BACKUPS = 5

    def __init__(self) -> None:
        lvl = self._LEVELS.get(
            os.environ.get("AGENT_LOG_LEVEL", "DEBUG").upper(),
            logging.DEBUG,
        )
        self._logger = logging.getLogger("agent")
        self._logger.setLevel(lvl)
        self._logger.propagate = False

        if not self._logger.handlers:
            self._add_rotating_handler()
            self._add_daily_handler()

    # ── Handler setup ────────────────────────────────────────────────

    def _add_rotating_handler(self) -> None:
        try:
            fh = logging.handlers.RotatingFileHandler(
                self.LOG_FILE,
                maxBytes=self._ROTATE_BYTES,
                backupCount=self._ROTATE_BACKUPS,
                encoding="utf-8",
            )
            fh.setFormatter(logging.Formatter("%(message)s"))
            fh.setLevel(logging.DEBUG)
            self._logger.addHandler(fh)
        except Exception:
            pass  # Silently degrade — console output still works

    def _add_daily_handler(self) -> None:
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            daily = Config.MEMORY_DIR / f"agent_{today}.log"
            dfh   = logging.FileHandler(daily, encoding="utf-8")
            dfh.setLevel(logging.DEBUG)
            dfh.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
            ))
            self._logger.addHandler(dfh)
        except Exception:
            pass  # Silently degrade

    # ── Emit ─────────────────────────────────────────────────────────

    def _emit(self, level: str, msg: str, **extra: Any) -> None:
        rec: dict[str, Any] = {
            "ts":    datetime.now().isoformat(timespec="milliseconds"),
            "level": level,
            "msg":   str(msg)[: self._MSG_MAX],
            **{k: str(v)[: self._EXTRA_MAX] for k, v in extra.items()},
        }
        getattr(self._logger, level, self._logger.info)(json.dumps(rec))

    def debug(self,   msg: str, **kw: Any) -> None: self._emit("debug",   msg, **kw)
    def info(self,    msg: str, **kw: Any) -> None: self._emit("info",    msg, **kw)
    def warning(self, msg: str, **kw: Any) -> None: self._emit("warning", msg, **kw)
    def error(self,   msg: str, **kw: Any) -> None: self._emit("error",   msg, **kw)

    # ── Reading ──────────────────────────────────────────────────────

    def read_recent(self, n: int = 50) -> list[dict[str, Any]]:
        """Return the last *n* log records from the rotating log file."""
        if not self.LOG_FILE.exists():
            return []
        try:
            lines = self.LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            return []

        recs: list[dict[str, Any]] = []
        for line in reversed(lines):
            try:
                recs.append(json.loads(line))
                if len(recs) >= n:
                    break
            except Exception:
                continue  # Skip malformed lines

        return list(reversed(recs))

    # ── Formatting ───────────────────────────────────────────────────

    def format_record(self, r: dict[str, Any]) -> str:
        color  = self._COLORS.get(r.get("level", ""), "\033[97m")
        extra  = {k: v for k, v in r.items() if k not in ("ts", "level", "msg")}
        ext    = ""
        if extra:
            pairs = list(extra.items())[: self._FMT_EXTRA]
            ext   = "  " + "  ".join(f"{k}={v}" for k, v in pairs)
        ts    = r.get("ts", "?")[:23]
        level = r.get("level", "?").upper()
        msg   = r.get("msg",   "")[: self._FMT_MSG]
        return f"{color}{ts}  {level:<7}  {msg}{ext}\033[0m"

    # ── Maintenance ──────────────────────────────────────────────────

    def rotate_old_daily_logs(self, keep_days: int = 30) -> None:
        """Remove daily log files older than *keep_days* days."""
        try:
            cutoff = datetime.now() - timedelta(days=keep_days)
            for lf in Config.MEMORY_DIR.glob("agent_*.log"):
                try:
                    date_part = lf.stem.removeprefix("agent_")
                    if datetime.strptime(date_part, "%Y-%m-%d") < cutoff:
                        lf.unlink()
                except (ValueError, OSError):
                    continue
        except Exception as e:
            self.warning(f"Log rotation failed: {e}")


logger = AgentLogger()
print("✓ Logger initialized")


# ---------- Retry Decorator ----------

_RETRYABLE_KEYS: tuple[str, ...] = (
    "429", "rate limit", "too many",
    "503", "502", "500",
    "server error", "overloaded",
    "timeout", "connection",
)


def retry_api(max_retries: int | None = None, backoff: float | None = None):
    """
    Decorator that retries a function on transient API errors with
    exponential back-off.  Only exceptions whose string representation
    contains one of ``_RETRYABLE_KEYS`` are retried.
    """
    mr = max_retries if max_retries is not None else Config.API_RETRY_MAX
    bf = backoff     if backoff     is not None else Config.API_RETRY_BACKOFF

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(mr):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    is_last      = attempt == mr - 1
                    is_retryable = any(k in str(e).lower() for k in _RETRYABLE_KEYS)
                    if not is_retryable or is_last:
                        raise
                    wait = bf ** attempt
                    print(f"\033[93m  [retry] {attempt + 1}/{mr} after {wait:.1f}s — {e}\033[0m")
                    time.sleep(wait)
        return wrapper
    return decorator


# ---------- Rate Limiter ----------

class RateLimiter:
    """Sliding-window rate limiter (thread-safe)."""

    _WINDOW_SECONDS = 60

    def __init__(self, calls_per_minute: int = 60) -> None:
        self._calls:     list[float] = []
        self._max_calls: int         = calls_per_minute
        self._lock       = threading.Lock()

    def wait_if_needed(self) -> None:
        """Block if the call rate would exceed the configured limit."""
        with self._lock:
            now    = time.time()
            cutoff = now - self._WINDOW_SECONDS
            # Discard timestamps outside the sliding window
            self._calls = [t for t in self._calls if t > cutoff]

            if len(self._calls) >= self._max_calls:
                # Sleep until the oldest call ages out of the window
                wait = self._WINDOW_SECONDS - (now - self._calls[0]) + 0.1
                if wait > 0:
                    time.sleep(wait)

            self._calls.append(time.time())


# ---------- LRU Tool Cache ----------

class ToolResultCache:
    """Thread-safe LRU cache for tool results with per-tool TTL support."""

    _WEB_TTL = 300  # seconds — applied to any tool whose name starts with "web_"

    def __init__(self, maxsize: int | None = None) -> None:
        self._cache:   OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._maxsize: int         = maxsize or Config.TOOL_CACHE_SIZE
        self._lock     = threading.Lock()

    # ── Internal helpers ─────────────────────────────────────────────

    def _key(self, tool: str, args: dict) -> str:
        """Return a short, stable cache key for *(tool, args)*."""
        raw = tool + json.dumps(args, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _is_expired(self, tool: str, timestamp: float) -> bool:
        """Return True if the cached entry has exceeded its TTL."""
        return tool.startswith("web_") and (time.time() - timestamp) > self._WEB_TTL

    def _evict_to_fit(self) -> None:
        """Remove the oldest entries until the cache is within *_maxsize*."""
        while len(self._cache) > self._maxsize:
            self._cache.popitem(last=False)

    # ── Public API ───────────────────────────────────────────────────

    def get(self, tool: str, args: dict) -> Any | None:
        """Return the cached result for *(tool, args)*, or ``None`` on miss/expiry."""
        k = self._key(tool, args)
        with self._lock:
            if k not in self._cache:
                return None
            result, ts = self._cache[k]
            if self._is_expired(tool, ts):
                del self._cache[k]
                return None
            self._cache.move_to_end(k)
            return result

    def set(self, tool: str, args: dict, result: Any) -> None:
        """Store *result* for *(tool, args)*, evicting the oldest entry if needed."""
        k = self._key(tool, args)
        with self._lock:
            self._cache[k] = (result, time.time())
            self._cache.move_to_end(k)
            self._evict_to_fit()

    def invalidate(self, tool_prefix: str = "") -> None:
        """Remove all entries whose key contains *tool_prefix*."""
        with self._lock:
            stale = [k for k in self._cache if tool_prefix in k]
            for k in stale:
                del self._cache[k]

    @property
    def size(self) -> int:
        """Current number of cached entries."""
        return len(self._cache)


print("✓ Utilities loaded (retry, rate limiter, cache)")

# ---------- API Counter ----------

class APICounter:
    """Thread-safe counter for API requests, token usage, and session statistics."""

    def __init__(self) -> None:
        self._lock              = threading.Lock()
        self._total:            int              = 0
        self._errors:           int              = 0
        self._tokens_in:        int              = 0
        self._tokens_out:       int              = 0
        self._tokens_reasoning: int              = 0
        self._by_model:         dict[str, int]   = {}
        self._start:            float            = time.time()

    # ── Recording ────────────────────────────────────────────────────

    def record(
        self,
        model:            str  = "",
        tokens_in:        int  = 0,
        tokens_out:       int  = 0,
        tokens_reasoning: int  = 0,
        error:            bool = False,
    ) -> None:
        """Record a single API call and its token usage."""
        with self._lock:
            self._total += 1
            if model:
                self._by_model[model] = self._by_model.get(model, 0) + 1
            self._tokens_in        += tokens_in
            self._tokens_out       += tokens_out
            self._tokens_reasoning += tokens_reasoning
            if error:
                self._errors += 1

    def next_request_number(self) -> int:
        """Return the number that the *next* request will receive."""
        with self._lock:
            return self._total + 1

    # ── Properties ───────────────────────────────────────────────────

    @property
    def total(self) -> int:
        """Total number of requests recorded so far."""
        return self._total

    # ── Reporting ────────────────────────────────────────────────────

    def summary(self) -> dict[str, Any]:
        """Return a snapshot of all counters as a plain dict."""
        elapsed = time.time() - self._start
        tokens_total = self._tokens_in + self._tokens_out + self._tokens_reasoning
        return {
            "total_requests":       self._total,
            "errors":               self._errors,
            "success":              self._total - self._errors,
            "by_model":             dict(self._by_model),
            "tokens_prompt":        self._tokens_in,
            "tokens_response":      self._tokens_out,
            "tokens_reasoning":     self._tokens_reasoning,
            "tokens_total":         tokens_total,
            "uptime_seconds":       round(elapsed, 1),
            "avg_requests_per_min": round(self._total / max(elapsed / 60, 0.01), 2),
        }

    def __str__(self) -> str:
        s = self.summary()
        return (
            f"Requests: {s['total_requests']} (✓{s['success']} ✗{s['errors']}) | "
            f"Prompt: {s['tokens_prompt']:,} │ Think: {s['tokens_reasoning']:,} │ "
            f"Resp: {s['tokens_response']:,} │ Avg: {s['avg_requests_per_min']}/min"
        )

    # ── Persistence ──────────────────────────────────────────────────

    def persist_stats(self) -> None:
        """Append the current session summary to the stats file."""
        try:
            entry = json.dumps({**self.summary(), "session_end": datetime.now().isoformat()})
            with open(Config.STATS_FILE, "a", encoding="utf-8") as f:
                f.write(entry + "\n")
        except Exception:
            pass  # Non-fatal — stats are informational only

    def load_historical(self) -> list[dict[str, Any]]:
        """Return all previously persisted session summaries."""
        if not Config.STATS_FILE.exists():
            return []
        recs: list[dict[str, Any]] = []
        for line in Config.STATS_FILE.read_text(encoding="utf-8").splitlines():
            try:
                recs.append(json.loads(line))
            except Exception:
                continue  # Skip malformed lines
        return recs


# ---------- Live Status Bar ----------

class LiveStatusBar:
    """
    In-place terminal status bar that updates as a streaming API response
    arrives, then finalises with a summary line.
    """

    REDRAW_INTERVAL = 0.35   # seconds between redraws
    LINE_WIDTH      = 120    # characters used for layout calculations
    _ANSI_RE        = re.compile(r'\033\[[0-9;]*m')
    # Keep only this many words in the rolling snippet buffer
    _WORD_BUFFER_MAX = 30

    def __init__(self, req_number: int, last_n_words: int = 8) -> None:
        self.req_number:       int        = req_number
        self.last_n_words:     int        = last_n_words
        self.reasoning_tokens: int        = 0
        self.response_tokens:  int        = 0
        self._words:           list[str]  = []
        self._lock                        = threading.Lock()
        self._last_draw:       float      = 0.0
        self._finalised:       bool       = False
        self._t0:              float      = time.monotonic()
        sys.stdout.write(self._render() + "\r")
        sys.stdout.flush()

    # ── Token helpers ────────────────────────────────────────────────

    @staticmethod
    def _approx(text: str) -> int:
        """
        Cheap token-count approximation (1 token ≈ 4 chars) for real-time display.
        Intentionally slightly optimistic — display accuracy matters less than speed.
        Note: ``Agent._est_tokens`` uses ``// 3`` (more conservative) for history
        budget management, where under-counting risks a context-window overflow.
        """
        return max(1, len(text) // 4)

    # ── Rendering ────────────────────────────────────────────────────

    def _render(self) -> str:
        total   = self.reasoning_tokens + self.response_tokens
        elapsed = time.monotonic() - self._t0
        tps     = total / elapsed if elapsed > 0.5 else 0.0

        base = (
            f"\033[90m┤\033[0m \033[93mREQ #{self.req_number:<3}\033[0m "
            f"\033[90m│\033[0m 🧠 \033[35m{self.reasoning_tokens:>7,}\033[0m "
            f"\033[90m│\033[0m 💬 \033[36m{self.response_tokens:>7,}\033[0m "
            f"\033[90m│\033[0m Σ \033[97m{total:>8,}\033[0m"
        )
        if tps > 0:
            base += f"  \033[90m{tps:>5.1f}t/s\033[0m"

        budget  = self.LINE_WIDTH - len(self._ANSI_RE.sub("", base)) - 4
        snippet = " ".join(self._words[-self.last_n_words:])
        if snippet and budget > 8:
            if len(snippet) > budget:
                snippet = snippet[: budget - 1] + "…"
            return base + f"  \033[2m…{snippet}\033[0m"
        return base

    def _redraw(self, force: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            if self._finalised:
                return
            if not force and (now - self._last_draw) < self.REDRAW_INTERVAL:
                return
            sys.stdout.write("\r" + self._render())
            sys.stdout.flush()
            self._last_draw = now

    # ── Streaming input ───────────────────────────────────────────────

    def add_reasoning(self, text: str) -> None:
        """Accumulate reasoning tokens from a streaming chunk."""
        self.reasoning_tokens += self._approx(text)
        self._redraw()

    def add_response(self, text: str) -> None:
        """Accumulate response tokens and update the rolling word snippet."""
        self.response_tokens += self._approx(text)
        if words := text.split():
            self._words.extend(words)
            # Trim to avoid unbounded growth
            if len(self._words) > self._WORD_BUFFER_MAX:
                self._words = self._words[-self.last_n_words:]
        self._redraw()

    def set_actual_tokens(self, reasoning: int = 0, response: int = 0) -> None:
        """Override approximated counts with the actual values from the API."""
        with self._lock:
            if reasoning > 0:
                self.reasoning_tokens = reasoning
            if response > 0:
                self.response_tokens = response
        self._redraw(force=True)

    # ── Finalisation ─────────────────────────────────────────────────

    def finalize(self, success: bool = True, elapsed: float = 0.0) -> None:
        """Print the final summary line and mark the bar as done."""
        with self._lock:
            self._finalised = True
            icon  = "\033[92m✓\033[0m" if success else "\033[91m✗\033[0m"
            total = self.reasoning_tokens + self.response_tokens
            time_suffix = f"  \033[90m{elapsed:.1f}s\033[0m" if elapsed else ""
            sys.stdout.write(
                f"\r{icon} REQ #{self.req_number:<3} \033[90m│\033[0m "
                f"🧠 \033[35mthink {self.reasoning_tokens:>7,}\033[0m  \033[90m│\033[0m "
                f"💬 resp \033[36m{self.response_tokens:>7,}\033[0m  \033[90m│\033[0m "
                f"Σ \033[97m{total:>8,} tok\033[0m{time_suffix}\n"
            )
            sys.stdout.flush()


print("✓ API Counter and Status Bar ready")

# ---------- General File Manager (unrestricted) ----------

class FileManager:
    """General-purpose file I/O helpers (not workspace-sandboxed)."""

    # ── Internal helpers ─────────────────────────────────────────────

    @staticmethod
    def _resolve(filepath: Path | str) -> Path:
        """
        Coerce *filepath* to an absolute ``Path``, anchoring relative
        paths under ``Config.BASE_DIR``.
        """
        p = filepath if isinstance(filepath, Path) else Path(filepath)
        return p if p.is_absolute() else Config.BASE_DIR / p

    # ── Public API ───────────────────────────────────────────────────

    @staticmethod
    def read_file(filepath: Path | str, default: str = "", encoding: str = "utf-8") -> str:
        """Return the text content of *filepath*, or *default* on any error."""
        p = FileManager._resolve(filepath)
        try:
            if p.exists():
                return p.read_text(encoding=encoding)
        except Exception as e:
            logger.warning(f"Failed to read {p}: {e}")
        return default

    @staticmethod
    def write_file(
        filepath:       Path | str,
        content:        str,
        mode:           str  = "w",
        encoding:       str  = "utf-8",
        create_parents: bool = True,
    ) -> bool:
        """Write *content* to *filepath*. Returns ``True`` on success."""
        p = FileManager._resolve(filepath)
        try:
            if create_parents:
                p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding=encoding)
            return True
        except Exception as e:
            logger.error(f"Failed to write {p}: {e}")
            return False

    @staticmethod
    def append_file(
        filepath:      Path | str,
        content:       str,
        add_timestamp: bool = True,
        encoding:      str  = "utf-8",
    ) -> bool:
        """Append *content* to *filepath*, optionally prefixed with a timestamp."""
        p = FileManager._resolve(filepath)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            if add_timestamp:
                ts      = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                content = f"[{ts}] {content}"
            with open(p, "a", encoding=encoding) as f:
                f.write(content + "\n")
            return True
        except Exception as e:
            logger.error(f"Failed to append to {p}: {e}")
            return False

    @staticmethod
    def delete_path(path: Path | str) -> bool:
        """Delete *path* (file or directory tree). Returns ``True`` on success."""
        p = FileManager._resolve(path)
        try:
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                shutil.rmtree(p)
            return True
        except Exception as e:
            logger.error(f"Failed to delete {p}: {e}")
            return False

    @staticmethod
    def list_directory(path: Path | str, pattern: str = "*") -> list[dict[str, Any]]:
        """Return metadata dicts for every entry matching *pattern* under *path*."""
        p = FileManager._resolve(path)
        try:
            results: list[dict[str, Any]] = []
            for item in p.glob(pattern):
                try:
                    stat = item.stat()
                    results.append({
                        "name":     item.name,
                        "path":     str(item),
                        "type":     "dir" if item.is_dir() else "file",
                        "size":     stat.st_size,
                        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    })
                except (OSError, PermissionError):
                    continue
            return results
        except Exception as e:
            logger.error(f"Failed to list {p}: {e}")
            return []


# ---------- Workspace-Sandboxed File Manager ----------

class SecureFiles:
    """File operations strictly confined to ``Config.FILES_DIR``."""

    # Recognised absolute aliases that all map to the workspace root,
    # sorted longest-first so the most specific match wins.
    _WORKSPACE_ALIASES: tuple[str, ...] = tuple(sorted(
        {str(Config.FILES_DIR), str(Config.BASE_DIR), str(Config.BASE_DIR) + "/files"},
        key=len, reverse=True,
    ))
    _MAX_BYTES: int = Config.MAX_FILE_SIZE_MB * 1024 * 1024

    # ── Path resolution ───────────────────────────────────────────────

    def _r(self, p: str | Path) -> Path:
        """
        Resolve *p* to an absolute path inside the workspace, raising
        ``ValueError`` if it escapes ``Config.FILES_DIR``.
        """
        p = Path(str(p).strip())

        if p.is_absolute():
            ps = str(p)
            for alias in self._WORKSPACE_ALIASES:
                if ps == alias or ps.startswith(alias + "/"):
                    rel = ps[len(alias):].lstrip("/")
                    p   = Config.FILES_DIR / rel if rel else Config.FILES_DIR
                    break
            else:
                try:
                    p.resolve().relative_to(Config.FILES_DIR.resolve())
                except ValueError:
                    raise ValueError(
                        f"Path '{ps}' is outside the workspace. "
                        f"Use relative paths or paths under {Config.FILES_DIR}"
                    )

        resolved = (Config.FILES_DIR / p).resolve() if not p.is_absolute() else p.resolve()
        resolved.relative_to(Config.FILES_DIR.resolve())  # final guard
        return resolved

    def _safe_resolve(self, path: str | Path) -> Path | str:
        """Return a resolved ``Path`` or an error string on escape attempts."""
        try:
            return self._r(path)
        except ValueError as e:
            return f"Error: {e}"

    def _check_size(self, p: Path) -> str | None:
        """Return an error string if *p* exceeds the configured size limit, else ``None``."""
        mb = p.stat().st_size / (1024 * 1024)
        return f"Error: File too large ({mb:.1f} MB)" if mb > Config.MAX_FILE_SIZE_MB else None

    # ── File operations ───────────────────────────────────────────────

    def read(self, path: str, start_line: int = 0, end_line: int = 0) -> str:
        p = self._safe_resolve(path)
        if isinstance(p, str):
            return p
        if not p.exists():
            return f"Not found: {p}"
        if err := self._check_size(p):
            return err
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            if not (start_line or end_line):
                return text
            lines = text.splitlines(keepends=True)
            total = len(lines)
            s     = max(0, (start_line or 1) - 1)
            e     = end_line or total
            return f"[lines {s + 1}–{min(e, total)} of {total}]\n" + "".join(lines[s:e])
        except Exception as e:
            return f"Read error: {e}"

    def write(self, path: str, content: str, append: bool = False, backup: bool = False) -> str:
        p = self._safe_resolve(path)
        if isinstance(p, str):
            return p
        if len(content.encode("utf-8")) > self._MAX_BYTES:
            return (
                f"Error: Content too large ({len(content):,} chars; "
                f"limit {Config.MAX_FILE_SIZE_MB} MB)."
            )
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            if backup and p.exists() and not append:
                p.with_suffix(p.suffix + ".bak").write_bytes(p.read_bytes())
            # Use an explicit context manager to guarantee the file is closed
            with p.open("a" if append else "w", encoding="utf-8") as fh:
                fh.write(content)
            return f"Written → {p} ({len(content):,} chars)"
        except Exception as e:
            return f"Write error: {e}"

    def write_many(self, writes: list[dict]) -> list[str]:
        """Write multiple files in parallel (up to 8 workers)."""
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            return list(pool.map(
                lambda w: self.write(w["path"], w["content"], w.get("append", False)),
                writes,
            ))

    def list(self, directory: str = "", metadata: bool = False) -> Any:
        """List workspace files, optionally with size/date metadata."""
        try:
            base = self._r(directory) if directory else Config.FILES_DIR
        except ValueError:
            return [f"Error: path outside workspace: {directory}"]
        if not base.exists():
            return []

        entries = sorted(
            (f for f in base.rglob("*") if not f.name.startswith(".")),
            key=lambda x: str(x),
        )
        if not metadata:
            return [str(f.relative_to(Config.FILES_DIR)) for f in entries if f.is_file()]

        result: list[dict[str, Any]] = []
        for f in entries:
            try:
                st = f.stat()
                result.append({
                    "name":       str(f.relative_to(Config.FILES_DIR)),
                    "size_bytes": st.st_size,
                    "modified":   datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                    "is_dir":     f.is_dir(),
                })
            except (OSError, PermissionError):
                continue
        return result

    def delete(self, path: str) -> str:
        p = self._safe_resolve(path)
        if isinstance(p, str):
            return p
        if not p.exists():
            return "Not found"
        if p.resolve() == Config.FILES_DIR.resolve():
            return "Error: Cannot delete workspace root"
        try:
            p.unlink()
            return f"Deleted: {p}"
        except Exception as e:
            return f"Delete error: {e}"

    def search(self, query: str, directory: str = "") -> list[str]:
        """Return file paths whose name or content contains *query* (case-insensitive)."""
        q = query.lower()
        hits: list[str] = []
        for fp in self.list(directory):
            resolved = self._safe_resolve(fp)
            if isinstance(resolved, str):
                continue  # skip error strings
            try:
                if resolved.stat().st_size > self._MAX_BYTES:
                    continue
            except OSError:
                continue
            if q in fp.lower() or q in self.read(fp).lower():
                hits.append(fp)
        return hits

    def rename(self, src: str, dst: str) -> str:
        s = self._safe_resolve(src)
        d = self._safe_resolve(dst)
        if isinstance(s, str):
            return s
        if isinstance(d, str):
            return d
        if not s.exists():
            return f"Error: not found: {src}"
        try:
            d.parent.mkdir(parents=True, exist_ok=True)
            s.rename(d)
            logger.info("file_rename", src=src, dst=dst)
            return f"Renamed {src} → {dst}"
        except Exception as e:
            return f"Rename error: {e}"


print("✓ File managers ready (SecureFiles + FileManager)")

# ---------- Persistent Memory ----------

class EfficientMemory:
    """JSON-line key-value store with tagging, fuzzy search, and persistence."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    # ── Persistence ──────────────────────────────────────────────────

    def _load(self) -> None:
        """Populate in-memory store from the JSON-lines file on disk."""
        if not Config.MEM_FILE.exists():
            return
        try:
            for line in Config.MEM_FILE.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self._data[rec["key"]] = rec
        except Exception:
            self._data = {}  # Corrupt file — start fresh; next _flush() will overwrite

    def _flush(self) -> None:
        """Atomically persist the store via a temp file."""
        tmp = Config.MEM_FILE.with_suffix(".tmp")
        try:
            tmp.write_text(
                "\n".join(json.dumps(r, default=str) for r in self._data.values()),
                encoding="utf-8",
            )
            tmp.replace(Config.MEM_FILE)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    # ── Public API ───────────────────────────────────────────────────

    def save(self, key: str, value: Any, tag: str = "") -> str:
        self._data[key] = {"key": key, "value": value, "tag": tag,
                           "ts": datetime.now().isoformat()}
        self._flush()
        return f"Saved '{key}'"

    def get(self, key: str) -> Any:
        """Return the stored value for *key*, or ``None`` if absent."""
        rec = self._data.get(key)
        return rec["value"] if rec is not None else None

    def list_keys(self, tag: str = "") -> list[str]:
        if not tag:
            return list(self._data)
        return [k for k, v in self._data.items() if v.get("tag") == tag]

    def search(self, query: str, fuzzy: bool = True) -> dict[str, Any]:
        """
        Return entries whose key or value contains *query*.
        When *fuzzy* is ``True`` (default) all words must be present but
        need not be contiguous.
        """
        q      = query.lower()
        words  = q.split()
        result: dict[str, Any] = {}
        for k, v in self._data.items():
            hay = f"{k} {v.get('value', '')}".lower()
            if q in hay or (fuzzy and all(w in hay for w in words)):
                result[k] = v["value"]
        return result

    def delete(self, key: str) -> str:
        if key not in self._data:
            return "Not found"
        del self._data[key]
        self._flush()
        return f"Deleted '{key}'"

    def clear(self) -> None:
        """Wipe all in-memory data and remove the backing file."""
        self._data = {}
        Config.MEM_FILE.unlink(missing_ok=True)


# ---------- Diff Applier ----------

class DiffApplier:
    """Apply SEARCH/REPLACE blocks or unified diffs to workspace files."""

    _SR_BLOCK = re.compile(
        r'<{7} SEARCH\r?\n(.*?)\r?\n={7}\r?\n(.*?)\r?\n>{7} REPLACE',
        re.DOTALL,
    )
    _HUNK_RE = re.compile(r'^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@')

    @staticmethod
    def _norm(s: str) -> str:
        """Strip trailing whitespace from every line for comparison."""
        return "\n".join(line.rstrip() for line in s.splitlines())

    def _resolve_path(self, path: str) -> Path | str:
        """Return a resolved workspace ``Path`` or an error string."""
        try:
            p = (Config.FILES_DIR / path).resolve()
            p.relative_to(Config.FILES_DIR.resolve())
            return p
        except ValueError:
            return f"Error: path outside workspace: {path}"

    # ── SEARCH/REPLACE ────────────────────────────────────────────────

    def _apply_search_replace(self, text: str, diff: str) -> tuple[str, int, list[str]]:
        blocks = self._SR_BLOCK.findall(diff)
        if not blocks:
            return text, 0, ["No SEARCH/REPLACE blocks found"]

        errors: list[str] = []
        applied = 0
        for search_raw, replace_raw in blocks:
            search = self._norm(search_raw)
            idx    = self._norm(text).find(search)
            if idx == -1:
                errors.append(f"SEARCH block not found:\n{search_raw[:120]}")
                continue
            pre_lines  = self._norm(text)[:idx].count("\n")
            orig_lines = text.splitlines(keepends=True)
            start = sum(len(l) for l in orig_lines[:pre_lines])
            end   = sum(len(l) for l in orig_lines[:pre_lines + search.count("\n") + 1])
            tail  = "" if replace_raw.endswith("\n") else "\n"
            text  = text[:start] + replace_raw + tail + text[end:]
            applied += 1
        return text, applied, errors

    # ── Unified diff ──────────────────────────────────────────────────

    def _apply_unified(
        self, lines: list[str], diff: str
    ) -> tuple[list[str], int, list[str]]:
        diff_lines                           = diff.splitlines(keepends=True)
        result: list[str]                    = list(lines)
        errors: list[str]                    = []
        offset = applied                     = 0
        i                                    = 0

        while i < len(diff_lines):
            if not (m := self._HUNK_RE.match(diff_lines[i])):
                i += 1
                continue
            orig_start = int(m.group(1)) - 1
            i         += 1
            removes: list[str] = []
            adds:    list[str] = []
            while i < len(diff_lines) and not self._HUNK_RE.match(diff_lines[i]):
                line = diff_lines[i]
                if line.startswith("-"):
                    removes.append(line[1:])
                elif line.startswith("+"):
                    adds.append(line[1:])
                i += 1

            pos = orig_start + offset
            if self._norm("".join(result[pos: pos + len(removes)])) != self._norm("".join(removes)):
                errors.append(f"Hunk mismatch at line {orig_start + 1}")
                continue
            result[pos: pos + len(removes)] = adds
            offset  += len(adds) - len(removes)
            applied += 1

        return result, applied, errors

    # ── Entry point ───────────────────────────────────────────────────

    def apply(self, path: str, diff: str) -> str:
        p = self._resolve_path(path)
        if isinstance(p, str):
            return p
        if not p.exists():
            return f"Error: file not found: {path}"

        try:
            original = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"Error reading file: {e}"

        if "<<<<<<< SEARCH" in diff:
            new_text, n, errors = self._apply_search_replace(original, diff)
            fmt = "search/replace"
        else:
            new_lines, n, errors = self._apply_unified(
                original.splitlines(keepends=True), diff
            )
            new_text = "".join(new_lines)
            fmt      = "unified diff"

        if not n:
            return "Error — no blocks applied:\n" + "\n".join(errors)

        try:
            p.write_text(new_text, encoding="utf-8")
            delta    = new_text.count("\n") - original.count("\n")
            warnings = f"\n  Warnings: {'; '.join(errors)}" if errors else ""
            return (
                f"✓ Patched {path} [{fmt}]  {n} block(s) applied"
                f"  Δ{delta:+d} lines{warnings}"
            )
        except Exception as e:
            return f"Error writing file: {e}"


# ---------- Secure Code Executor ----------

class SecureCodeExecutor:
    """
    Execute Python snippets in an isolated subprocess with JSON output
    parsing and optional memory resource limits.
    """

    _WRAPPER = textwrap.dedent("""\
        import json, re, math, random, statistics, collections, itertools
        import functools, datetime, string, csv, io
        from pathlib import Path

        try:
        {code}
            if 'result' in dir():
                print(json.dumps({{"result": result, "status": "success"}}, default=str))
            else:
                print(json.dumps({{"result": "OK (no output)", "status": "success"}}))
        except Exception as e:
            import traceback
            print(json.dumps({{"error": str(e), "traceback": traceback.format_exc(), "status": "error"}}))
    """)
    # Subprocess is given slightly more time than the user-facing timeout
    # to allow for process startup overhead.
    _SUBPROCESS_GRACE = 5  # seconds

    @staticmethod
    def _set_resource_limits() -> None:
        """Apply memory cap via ``RLIMIT_AS`` (Linux / macOS only)."""
        try:
            mem_bytes = Config.MAX_PYTHON_MEMORY_MB * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        except Exception:
            pass  # Silently skip on unsupported platforms

    @classmethod
    def run(cls, code: str, timeout: int | None = None, apply_limits: bool = True) -> str:
        timeout  = timeout or Config.CODE_TIMEOUT
        wrapper  = cls._WRAPPER.format(code=textwrap.indent(code, "    "))
        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
                tmp_path = f.name
                f.write(wrapper)
            preexec = cls._set_resource_limits if apply_limits else None
            result  = subprocess.run(
                [sys.executable, tmp_path],
                capture_output=True, text=True,
                timeout=timeout + cls._SUBPROCESS_GRACE,
                cwd=str(Config.FILES_DIR),
                preexec_fn=preexec,
            )
            return cls._parse_output(result)
        except subprocess.TimeoutExpired:
            return f"Error: Timed out after {timeout}s"
        except Exception as e:
            return f"Execution failed: {e}"
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    @staticmethod
    def _parse_output(result: subprocess.CompletedProcess) -> str:
        try:
            out = json.loads(result.stdout.strip())
            if out.get("status") == "error":
                return f"Execution error: {out.get('error')}\n{out.get('traceback', '')}"
            return str(out.get("result", "No result"))
        except json.JSONDecodeError:
            parts = [result.stdout.strip()]
            if result.stderr.strip():
                parts.append(f"STDERR:\n{result.stderr.strip()}")
            return "\n".join(filter(None, parts)) or "OK (no output)"


# ---------- Web Search ----------

class SecureWebSearch:
    """DuckDuckGo search and URL fetcher with SSRF protection."""

    _BLOCKED_PREFIXES: tuple[str, ...] = (
        "localhost", "127.", "0.0.0.0", "::1",
        "10.", "172.16.", "192.168.", "169.254.",
    )
    _USER_AGENT = "Mozilla/5.0 (compatible; AgenticAI/1.0)"

    @classmethod
    def _valid_url(cls, url: str) -> bool:
        """Return ``True`` only for public http(s) URLs."""
        if not url.startswith(("http://", "https://")):
            return False
        hostname = urlparse(url).hostname or ""
        return not any(hostname.startswith(b) for b in cls._BLOCKED_PREFIXES)

    @staticmethod
    def _import_requests():
        """Import *requests*, returning ``(module, None)`` or ``(None, error_str)``."""
        try:
            import requests as _r
            return _r, None
        except ImportError:
            return None, "Error: requests library not installed"

    def search(self, query: str, max_results: int = 5) -> list[dict[str, Any]]:
        requests, err = self._import_requests()
        if err:
            return [{"snippet": err, "url": ""}]
        try:
            r = requests.get(
                "https://api.duckduckgo.com/",
                timeout=Config.WEB_TIMEOUT,
                params={
                    "q": query, "format": "json",
                    "no_html": 1, "skip_disambig": 1, "no_redirect": 1,
                },
            ).json()

            results: list[dict[str, Any]] = []
            if r.get("Abstract"):
                results.append({
                    "title":   r.get("Heading", query)[:100],
                    "snippet": r["Abstract"][:300],
                    "url":     r.get("AbstractURL", ""),
                })
            for topic in r.get("RelatedTopics", [])[:max_results]:
                if isinstance(topic, dict) and topic.get("Text"):
                    results.append({
                        "title":   topic["Text"][:80],
                        "snippet": topic["Text"][:300],
                        "url":     topic.get("FirstURL", ""),
                    })
            return results[:max_results] or [{"snippet": "No results", "url": ""}]
        except Exception as e:
            return [{"snippet": f"Search error: {e}", "url": ""}]

    def fetch(self, url: str, max_chars: int | None = None) -> str:
        requests, err = self._import_requests()
        if err:
            return err
        max_chars = max_chars or Config.MAX_WEB_CONTENT
        if not self._valid_url(url):
            return "Error: Invalid or blocked URL"
        try:
            resp = requests.get(
                url, timeout=Config.WEB_TIMEOUT, stream=True,
                headers={"User-Agent": self._USER_AGENT},
            )
            resp.raise_for_status()
            if "text/" not in resp.headers.get("Content-Type", ""):
                return "Error: Non-text content"

            chunks: list[str] = []
            length = 0
            for chunk in resp.iter_content(chunk_size=8192, decode_unicode=True):
                if chunk:
                    chunks.append(chunk)
                    length += len(chunk)
                    if length > max_chars:
                        break

            raw  = "".join(chunks)
            text = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', raw)).strip()
            return text[:max_chars] + ("...(truncated)" if len(text) > max_chars else "")
        except Exception as e:
            return f"Fetch error: {e}"


print("✓ Core components ready (Memory, Diff, Code Exec, Web)")

class CachedSubtools:
    """
    Code-1 sub-tool system (create/run/list/delete dynamic Python tools)
    extended with Code-2's skill validation and Tools.md registration.
    """

    BLOCKED = re.compile(
        r'import\s+(os|sys|subprocess)|__import__|eval\s*\(|exec\s*\('
        r'|open\s*\(|compile\s*\(|getattr\s*\(|setattr\s*\(|delattr\s*\('
        r'|globals\s*\(|locals\s*\(|\.system\s*\(|\.popen\s*\(|subprocess\.',
        re.IGNORECASE,
    )
    _SOFT_PATTERNS: list[tuple[re.Pattern, str]] = [
        (re.compile(p, f), p) for p, f in [
            (r'__\w+__',         re.IGNORECASE),
            (r'ctypes',          re.IGNORECASE),
            (r'socket\.',        0),
            (r'urllib\.request', 0),
            (r'pickle\.',        re.IGNORECASE),
            (r'importlib',       0),
            (r'\.write\s*\(',    0),
            (r'shutil\.',        0),
        ]
    ]
    # Limits used in security review
    _MAX_LINES   = 300
    _MAX_IMPORTS = 20
    # Fields exposed by list()
    _LIST_FIELDS = ("description", "usage_count", "last_used", "created")
    # Fields parsed from Tools.md skill blocks
    _SKILL_ATTRS = ("type", "location", "purpose", "trigger", "input", "output")

    def __init__(self) -> None:
        self._mf: Path              = Config.SUBTOOL_DIR / "manifest.json"
        self._m:  dict[str, Any]    = {}
        self._cache: dict[str, Any] = {}
        if self._mf.exists():
            try:
                self._m = json.loads(self._mf.read_text(encoding="utf-8"))
            except Exception:
                pass  # Corrupt manifest — start fresh; _save() will overwrite

    # ── Persistence ──────────────────────────────────────────────────

    def _save(self) -> None:
        """Atomically persist the manifest via a temp file."""
        tmp = self._mf.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(self._m, indent=2, default=str), encoding="utf-8")
            tmp.replace(self._mf)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    # ── Security ─────────────────────────────────────────────────────

    def _security_review(self, code: str) -> list[str]:
        """Return a list of human-readable security warnings for *code*."""
        warnings: list[str] = [
            f"Pattern '{pat}' matched: {m.group()!r}"
            for rx, pat in self._SOFT_PATTERNS
            if (m := rx.search(code))
        ]
        lines = code.splitlines()
        if len(lines) > self._MAX_LINES:
            warnings.append(f"Code is large ({len(lines)} lines)")
        if code.count("import") > self._MAX_IMPORTS:
            warnings.append("Excessive imports detected")
        return warnings

    # ── Core sub-tool CRUD ────────────────────────────────────────────

    def create(self, name: str, description: str, code: str, force: bool = False) -> str:
        name = re.sub(r'[^a-zA-Z0-9_]', '_', name)
        if not name or not name[0].isalpha():
            return "Error: Invalid tool name"
        if m := self.BLOCKED.search(code):
            logger.warning("subtool blocked", name=name, pattern=m.group())
            return f"Error: Code contains blocked pattern: {m.group()!r}"
        if "def run(" not in code:
            return "Error: Code must define run(**kwargs)"

        warnings = self._security_review(code)
        if warnings and not force:
            logger.warning("subtool security warnings", name=name, warnings=str(warnings))
            return (
                "Security review found potential issues:\n"
                + "\n".join(f"  ⚠ {w}" for w in warnings)
                + "\nCall again with force=True to create despite warnings."
            )

        p = Config.SUBTOOL_DIR / f"{name}.py"
        p.write_text(textwrap.dedent(code), encoding="utf-8")
        self._m[name] = {
            "description":       description,
            "path":              str(p),
            "created":           datetime.now().isoformat(),
            "usage_count":       0,
            "last_used":         None,
            "security_warnings": warnings,
        }
        self._cache.pop(name, None)
        self._save()

        warn_note = f"  ⚠ {len(warnings)} warning(s) recorded." if warnings else ""
        logger.info("subtool created", name=name, force=str(force))
        return f"✓ Sub-tool '{name}' created{warn_note}"

    def run(self, name: str, **kwargs: Any) -> Any:
        if name not in self._m:
            return f"Not found: {name}"
        p = Path(self._m[name]["path"])
        if not p.exists():
            return f"Error: File missing for '{name}'"
        try:
            spec = importlib.util.spec_from_file_location(name, p)
            mod  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if not hasattr(mod, "run"):
                return f"Error: '{name}' has no run() function"
            result = mod.run(**kwargs)
            self._m[name]["usage_count"] = self._m[name].get("usage_count", 0) + 1
            self._m[name]["last_used"]   = datetime.now().isoformat()
            self._save()
            return result if isinstance(result, str) else json.dumps(result, default=str)
        except Exception as e:
            return f"Subtool error: {e}\n{traceback.format_exc()}"

    def list(self) -> dict[str, dict[str, Any]]:
        return {
            k: {f: v.get(f) for f in self._LIST_FIELDS}
            for k, v in self._m.items()
        }

    def code(self, name: str) -> str:
        if name not in self._m:
            return "Not found"
        p = Path(self._m[name]["path"])
        return p.read_text(encoding="utf-8") if p.exists() else "File missing"

    def delete(self, name: str) -> str:
        if name not in self._m:
            return "Not found"
        Path(self._m[name]["path"]).unlink(missing_ok=True)
        del self._m[name]
        self._cache.pop(name, None)
        self._save()
        return f"Deleted '{name}'"

    # ── Skills helpers ────────────────────────────────────────────────

    @staticmethod
    def _load_skill_module(name: str, filepath: str | Path) -> tuple[Any, str | None]:
        """
        Import a skill module from *filepath*.
        Returns ``(module, None)`` on success or ``(None, error_message)`` on failure.
        """
        try:
            spec = importlib.util.spec_from_file_location(name, filepath)
            if spec is None:
                return None, "Cannot load skill module"
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod, None
        except Exception as e:
            return None, str(e)

    def _parse_skills_from_tools_md(self, content: str) -> list[dict[str, Any]]:
        """Parse skill blocks from a Tools.md file string."""
        skills: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None

        for raw_line in content.split("\n"):
            line = raw_line.strip()
            if line.startswith("## Tool:"):
                if current and current.get("type") == "skill":
                    skills.append(current)
                current = {"name": line.removeprefix("## Tool:").strip(),
                           **{a: "" for a in self._SKILL_ATTRS}}
            elif current:
                if line.startswith("#"):
                    # Any new heading closes the current block
                    if current.get("type") == "skill":
                        skills.append(current)
                    current = None
                    continue
                low = line.lower()
                for attr in self._SKILL_ATTRS:
                    if low.startswith(f"- {attr}") or low.startswith(f"- **{attr}**"):
                        current[attr] = line.split(":", 1)[1].strip() if ":" in line else ""
                        break

        if current and current.get("type") == "skill":
            skills.append(current)
        return skills

    # ── Skill-manager extensions ──────────────────────────────────────

    def list_skills(self, refresh: bool = False) -> list[dict[str, Any]]:
        """List skills registered in core/Tools.md."""
        tools_content = FileManager.read_file(Config.CORE_DIR / "Tools.md", default="")
        skills = self._parse_skills_from_tools_md(tools_content)

        for skill in skills:
            loc  = skill.get("location", "")
            path = Config.SKILLS_DIR / loc.removeprefix("skills/") if loc else None
            skill["file_exists"] = path is not None and path.exists()
            skill["file_path"]   = str(path) if skill["file_exists"] else None

        return skills

    def validate_skill(self, name: str) -> dict[str, Any]:
        """Validate a named skill."""
        status: dict[str, Any] = {
            "name": name, "valid": False,
            "file_exists": False, "has_run": False, "error": None,
        }
        skill = next((s for s in self.list_skills() if s["name"] == name), None)
        if not skill:
            status["error"] = "Skill not registered in Tools.md"
            return status

        fp = skill.get("file_path")
        if not fp:
            status["error"] = "Skill file not found"
            return status

        status["file_exists"] = True
        mod, err = self._load_skill_module(name, fp)
        if err:
            status["error"] = err
            return status

        status["has_run"] = hasattr(mod, "run")
        status["valid"]   = status["has_run"]
        if not status["has_run"]:
            status["error"] = "Missing run() function"
        return status

    def validate_all_skills(self) -> dict[str, list[dict[str, Any]]]:
        """Validate all registered skills."""
        valid: list[dict[str, Any]]   = []
        invalid: list[dict[str, Any]] = []
        for skill in self.list_skills():
            v = self.validate_skill(skill["name"])
            if v["valid"]:
                valid.append(skill)
            else:
                skill["validation_error"] = v["error"]
                invalid.append(skill)
        return {"valid": valid, "invalid": invalid}

    def create_skill(self, name: str, code: str, purpose: str, trigger: str,
                     input_fmt: str = "", output_fmt: str = "") -> str:
        """Create a skill file and register it in core/Tools.md."""
        if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name):
            return f"Error: Invalid skill name '{name}'"

        skill_path = Config.SKILLS_DIR / f"{name}.py"
        if skill_path.exists():
            return f"Error: Skill '{name}' already exists. Delete first."
        if not FileManager.write_file(skill_path, code):
            return "Error: Failed to write skill file"

        tools_path    = Config.CORE_DIR / "Tools.md"
        tools_content = FileManager.read_file(tools_path, default="")
        entry = (
            f"\n## Tool: {name}\n- Type     : skill\n"
            f"- Location : skills/{name}.py\n- Purpose  : {purpose}\n"
            f"- Trigger  : {trigger}\n- Input    : {input_fmt}\n"
            f"- Output   : {output_fmt}\n"
        )

        if "## Skill Scripts" in tools_content:
            head, tail  = tools_content.split("## Skill Scripts", 1)
            new_content = head + "## Skill Scripts\n" + entry + tail.lstrip()
        else:
            new_content = tools_content + "\n" + entry

        if not FileManager.write_file(tools_path, new_content):
            skill_path.unlink(missing_ok=True)
            return "Error: Failed to register skill in Tools.md"

        logger.info(f"Created skill: {name}")
        return f"✓ Skill '{name}' created and registered"

    def run_skill(self, name: str, input_data: Any = None) -> Any:
        """Run a skill from the skills directory."""
        skill_file = Config.SKILLS_DIR / f"{name}.py"
        if not skill_file.exists():
            return f"Error: Skill '{name}' not found"

        mod, err = self._load_skill_module(name, skill_file)
        if err:
            return f"Skill error: {err}"
        if not hasattr(mod, "run"):
            return f"Error: Skill '{name}' has no run() function"

        try:
            return mod.run(input_data)
        except Exception as e:
            logger.error(f"Skill {name} execution failed: {e}")
            return f"Skill error: {e}\n{traceback.format_exc()}"


print("✓ Sub-tools & Skills manager ready")

# ---------- Shell Runner ----------

class ShellRunner:
    """Run shell commands with a safety blocklist and configurable timeout."""

    _HARD_BLOCKED = re.compile(
        r'\brm\s+-r[f]?\b'
        r'|\bdd\b|\bmkfs\b'
        r'|:\(\)\s*\{.*:\(\)|:\s*&\s*\}'
        r'|\b(?:shutdown|reboot|halt|poweroff)\b'
        r'|\bchmod\s+-R\b'
        r'|\b(?:iptables|ufw)\b'
        r'|\bcrontab\s+-r\b',
        re.IGNORECASE | re.DOTALL,
    )
    _DISABLED: bool = os.environ.get("AGENT_DISABLE_SHELL", "").lower() in ("1", "true", "yes")

    @classmethod
    def _resolve_cwd(cls, cwd: str) -> str:
        """Map the *cwd* shorthand to an absolute directory string."""
        if cwd == "/":
            return str(Config.BASE_DIR)
        return str(Config.FILES_DIR / cwd) if cwd else str(Config.FILES_DIR)

    @classmethod
    def run(cls, command: str, timeout: int | None = None, cwd: str = "") -> str:
        timeout = timeout or Config.SHELL_TIMEOUT
        if cls._DISABLED:
            return "Error: shell_run is disabled (AGENT_DISABLE_SHELL=1)."
        if m := cls._HARD_BLOCKED.search(command):
            logger.warning("shell_run blocked", command=command[:400], matched=m.group())
            return f"Error: Command blocked by safety filter (matched: {m.group()!r})."
        try:
            r = subprocess.run(
                command, shell=True, capture_output=True, text=True,
                timeout=timeout, cwd=cls._resolve_cwd(cwd),
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            parts = [s for s in (
                r.stdout.strip(),
                f"[stderr]\n{r.stderr.strip()}" if r.stderr.strip() else "",
            ) if s]
            parts.append(f"[exit code: {r.returncode}]")
            return "\n".join(parts)
        except subprocess.TimeoutExpired:
            return f"Error: Command timed out after {timeout}s"
        except Exception as e:
            return f"Error: {e}"


# ---------- Session Manager ----------

class SessionManager:
    """Daily log files, multi-day memory search, and core-context loading."""

    # Core identity files loaded into every system prompt
    _CONTEXT_FILES: tuple[str, ...] = (
        "core/SOUL.md", "core/USER.md", "core/MEMORY.md",
        "core/HEARTBEAT.md", "core/Need.md", "core/Tools.md",
    )

    @staticmethod
    def get_todays_log_path() -> Path:
        today = datetime.now().strftime("%Y-%m-%d")
        return Config.MEMORY_DIR / f"{today}.md"

    @staticmethod
    def ensure_todays_log() -> Path:
        """Create today's log file if absent and return its path."""
        path = SessionManager.get_todays_log_path()
        if not path.exists():
            now    = datetime.now()
            header = (
                f"# Daily Log - {now.strftime('%Y-%m-%d')}\n\n"
                f"## [{now.strftime('%H:%M:%S')}] Session Start\n"
                f"- Agent initialized\n"
            )
            FileManager.write_file(path, header)
        return path

    @staticmethod
    def save_log(entry: str, category: str = "General") -> bool:
        path = SessionManager.ensure_todays_log()
        ts   = datetime.now().strftime("%H:%M:%S")
        return FileManager.append_file(
            path, f"## [{ts}] {category}\n- {entry}", add_timestamp=False
        )

    @staticmethod
    def search_memory(query: str, days_back: int = 7) -> list[dict[str, Any]]:
        """Search the last *days_back* daily logs for *query* (case-insensitive)."""
        q       = query.lower()
        results: list[dict[str, Any]] = []
        try:
            for i in range(days_back):
                date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
                path = Config.MEMORY_DIR / f"{date}.md"
                if not path.exists():
                    continue
                content = FileManager.read_file(path)
                if q not in content.lower():
                    continue
                matches = [
                    {"line_num": n + 1, "text": line.strip()}
                    for n, line in enumerate(content.splitlines())
                    if q in line.lower()
                ][:10]
                results.append({"date": date, "matches": matches})
        except Exception as e:
            logger.error(f"Memory search failed: {e}")
        return results

    @staticmethod
    def load_context() -> dict[str, str]:
        """Load all core identity files into a ``{relative_path: content}`` dict."""
        return {
            rel: FileManager.read_file(Config.BASE_DIR / rel, default=f"# Missing: {rel}")
            for rel in SessionManager._CONTEXT_FILES
        }


# ---------- Need Management ----------

# Shared field names for Need.md block parsing — also used by HeartbeatManager._get_pending_needs
_NEED_FIELDS: tuple[str, ...] = ("Priority", "Needs", "Reason", "Blocking", "Status")


def post_need(priority: str, need: str, reason: str, blocking: str) -> bool:
    """Append a structured PENDING request to core/Need.md."""
    path    = Config.CORE_DIR / "Need.md"
    content = FileManager.read_file(path, default="")
    nums    = re.findall(r'Request #(\d+)', content)
    nxt     = max(int(x) for x in nums) + 1 if nums else 1
    now     = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry   = (
        f"\n## [{now}] Request #{nxt}\n"
        f"- **Priority** : {priority}\n"
        f"- **Needs**    : {need}\n"
        f"- **Reason**   : {reason}\n"
        f"- **Blocking** : {blocking}\n"
        f"- **Status**   : PENDING\n"
    )
    return FileManager.append_file(path, entry, add_timestamp=False)


def check_needs() -> list[dict[str, str]]:
    """Return all PENDING requests from core/Need.md."""
    content = FileManager.read_file(Config.CORE_DIR / "Need.md", default="")
    pending: list[dict[str, str]] = []
    for block in content.split("## [")[1:]:
        if "Status: PENDING" not in block:
            continue
        entry: dict[str, str] = {}
        for field in _NEED_FIELDS:
            if m := re.search(rf'\*\*{field}\*\*\s*:\s*(.+)', block):
                entry[field.lower()] = m.group(1).strip()
        pending.append(entry)
    return pending


# ---------- Todo List Manager ----------

class TodoListManager:
    """Hierarchical task tracker backed by ``EfficientMemory``."""

    _EMPTY: dict[str, Any] = {
        "task": "", "tasks": [], "current_task_id": None, "completed": False,
    }
    _DONE_STATUSES: frozenset[str] = frozenset({"completed", "failed"})

    def __init__(self, memory: EfficientMemory) -> None:
        self.mem  = memory
        self._key = "current_todo_list"

    # ── Persistence ──────────────────────────────────────────────────

    def _load(self) -> dict[str, Any]:
        data = self.mem.get(self._key)
        return data if isinstance(data, dict) else dict(self._EMPTY)

    def _save(self, data: dict[str, Any]) -> None:
        self.mem.save(self._key, data, tag="todo")

    # ── Tree helpers ─────────────────────────────────────────────────

    def _find(self, tasks: list[dict], task_id: str) -> dict | None:
        """Recursively locate a task by ID in the tree."""
        for t in tasks:
            if t["id"] == task_id:
                return t
            if found := self._find(t.get("children", []), task_id):
                return found
        return None

    def _flatten_ids(self, tasks: list[dict]) -> list[str]:
        """Return all task IDs in depth-first order."""
        ids: list[str] = []
        for t in tasks:
            ids.append(t["id"])
            if t.get("children"):
                ids.extend(self._flatten_ids(t["children"]))
        return ids

    def _count(self, tasks: list[dict], status: str | None = None) -> int:
        """Count tasks (and their children) matching *status*, or all if ``None``."""
        total = 0
        for t in tasks:
            total += 1 if status is None or t.get("status") == status else 0
            total += self._count(t.get("children", []), status)
        return total

    def _next_pending(self, tasks: list[dict], after_id: str) -> dict | None:
        """Return the first pending task that appears after *after_id* in DFS order."""
        flat = self._flatten_ids(tasks)
        try:
            start = flat.index(after_id) + 1
        except ValueError:
            return None
        for tid in flat[start:]:
            if (t := self._find(tasks, tid)) and t["status"] == "pending":
                return t
        return None

    @staticmethod
    def _make_task(task_id: str, description: str) -> dict[str, Any]:
        return {
            "id": task_id, "description": description, "status": "pending",
            "result": None, "error": None, "notes": "", "children": [],
        }

    # ── Public API ───────────────────────────────────────────────────

    def create(self, task: str, steps: list[str]) -> str:
        tasks = [self._make_task(str(i), s) for i, s in enumerate(steps, 1)]
        data  = {
            "task":            task,
            "tasks":           tasks,
            "completed":       False,
            "current_task_id": tasks[0]["id"] if tasks else None,
            "created":         datetime.now().isoformat(),
        }
        self._save(data)
        cur = tasks[0]["description"] if tasks else "none"
        return f"✓ Created todo list with {len(steps)} steps. Current: {cur}"

    def status(self) -> dict[str, Any]:
        data = self._load()
        if not data["tasks"]:
            return {"error": "No active todo list"}
        total     = self._count(data["tasks"])
        completed = self._count(data["tasks"], "completed")
        pct       = f"{completed / total * 100:.1f}%" if total else "0%"
        return {
            "task":         data["task"],
            "created":      data.get("created"),
            "completed":    data["completed"],
            "progress":     f"{completed}/{total} completed ({pct})",
            "current_task": (
                self._find(data["tasks"], data["current_task_id"])
                if data["current_task_id"] else None
            ),
            "tasks": data["tasks"],
            "stats": {
                s: self._count(data["tasks"], s)
                for s in ("completed", "failed", "in_progress", "pending")
            },
        }

    def add_subtask(self, parent_id: str, description: str) -> str:
        data   = self._load()
        parent = self._find(data["tasks"], parent_id)
        if not parent:
            return f"Error: Parent task '{parent_id}' not found"
        if parent.get("status") == "completed":
            return f"Error: Cannot add subtask to completed task '{parent_id}'"
        children = parent.setdefault("children", [])
        new_id   = f"{parent_id}.{len(children) + 1}"
        children.append(self._make_task(new_id, description))
        data["current_task_id"] = new_id
        self._save(data)
        return f"✓ Added subtask '{description}' (ID: {new_id}) → parent '{parent_id}'. Now current."

    def _finish_task(
        self, task_id: str, new_status: str,
        result: str | None = None, error: str | None = None,
    ) -> str:
        """Shared implementation for ``complete`` and ``fail``."""
        data = self._load()
        task = self._find(data["tasks"], task_id)
        if not task:
            return f"Error: Task '{task_id}' not found"
        if task["status"] == new_status:
            return f"Task '{task_id}' already {new_status}"

        task["status"] = new_status
        if result is not None:
            task["result"] = result
        if error is not None:
            task["error"] = error

        icon = "✓" if new_status == "completed" else "✗"
        nxt  = self._next_pending(data["tasks"], task_id)
        if nxt:
            data["current_task_id"] = nxt["id"]
            msg = f"{icon} {new_status.title()} '{task_id}'. Next: {nxt['id']} - {nxt['description']}"
        else:
            data["current_task_id"] = None
            data["completed"]       = True
            tail = "ALL TASKS DONE! 🎉" if new_status == "completed" else "NO MORE TASKS."
            msg  = f"{icon} {new_status.title()} '{task_id}'. {tail}"

        self._save(data)
        return msg

    def complete(self, task_id: str, result: str | None = None) -> str:
        return self._finish_task(task_id, "completed", result=result)

    def fail(self, task_id: str, error: str | None = None) -> str:
        return self._finish_task(task_id, "failed", error=error)

    def set_current(self, task_id: str) -> str:
        data = self._load()
        task = self._find(data["tasks"], task_id)
        if not task:
            return f"Error: Task '{task_id}' not found"
        if task["status"] in self._DONE_STATUSES:
            return f"Error: Task '{task_id}' is {task['status']}"
        data["current_task_id"] = task_id
        self._save(data)
        return f"Current task set to {task_id}: {task['description']}"

    def update(self, task_id: str, description: str | None = None, notes: str | None = None) -> str:
        data = self._load()
        task = self._find(data["tasks"], task_id)
        if not task:
            return f"Error: Task '{task_id}' not found"
        if description is not None:
            task["description"] = description
        if notes is not None:
            task["notes"] = notes
        self._save(data)
        return f"✓ Updated task {task_id}"

    def get_current_task(self) -> dict | None:
        data = self._load()
        return (
            self._find(data["tasks"], data["current_task_id"])
            if data["current_task_id"] else None
        )

    def is_completed(self) -> bool:
        data = self._load()
        return bool(data["completed"]) and data["current_task_id"] is None


print("✓ Shell, Session, Need, Todo managers ready")


# ---------- Data Classes ----------

@dataclass
class ParallelTask:
    """A single unit of work submitted to ``ParallelAPIHandler``."""
    task_id:    str
    messages:   list[dict[str, Any]]
    system:     str              = ""
    tools:      list[dict]       = field(default_factory=list)
    model:      str              = ""
    max_tokens: int              = 0


@dataclass
class ParallelResult:
    """The outcome of a single parallel API call."""
    task_id:    str
    content:    str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    error:      str                  = ""
    tokens_in:  int                  = 0
    tokens_out: int                  = 0


# ---------- Parallel API Handler ----------

class ParallelAPIHandler:
    """Execute multiple ``ParallelTask`` objects concurrently via a thread pool."""

    # ANSI colour shorthands used in progress output
    _DIM   = "\033[90m"
    _THINK = "\033[35m"
    _RESP  = "\033[36m"
    _ERR   = "\033[91m"
    _RESET = "\033[0m"

    def __init__(
        self,
        client:       Any,
        counter:      APICounter,
        rate_limiter: RateLimiter | None = None,
        max_workers:  int | None         = None,
    ) -> None:
        self.client       = client
        self.counter      = counter
        self.rate_limiter = rate_limiter
        self.max_workers  = max_workers or Config.MAX_PARALLEL_REQUESTS

    # ── Internal helpers ─────────────────────────────────────────────

    @staticmethod
    def _ts() -> str:
        """Return a millisecond-precision timestamp string for log lines."""
        return datetime.now().strftime("%H:%M:%S.%f")[:-3]

    @staticmethod
    def _extract_tokens(usage: Any) -> tuple[int, int, int]:
        """
        Return ``(tokens_in, tokens_reasoning, tokens_out)`` from a usage object.
        Mirrors the extraction logic in ``Agent._extract_usage``.
        """
        if not usage:
            return 0, 0, 0
        details   = getattr(usage, "completion_tokens_details", None)
        tokens_in = getattr(usage, "prompt_tokens",      0) or 0
        reasoning = getattr(details, "reasoning_tokens", 0) or 0
        tokens_out = max((getattr(usage, "completion_tokens", 0) or 0) - reasoning, 0)
        return tokens_in, reasoning, tokens_out

    @staticmethod
    def _serialize_tool_calls(raw_calls: Any) -> list[dict[str, Any]]:
        """Convert raw tool-call objects to plain dicts."""
        if not raw_calls:
            return []
        return [
            {
                "id":   tc.id,
                "type": "function",
                "function": {
                    "name":      tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in raw_calls
        ]

    # ── Single request ────────────────────────────────────────────────

    @retry_api()
    def _single_request(self, task: ParallelTask) -> ParallelResult:
        model      = task.model      or Config.MODEL
        max_tokens = task.max_tokens or Config.MAX_TOKENS
        msgs       = (
            [{"role": "system", "content": task.system}] if task.system else []
        ) + task.messages

        if self.rate_limiter:
            self.rate_limiter.wait_if_needed()

        req_num = self.counter.next_request_number()
        t0      = time.monotonic()
        print(
            f"  {self._DIM}[parallel] → REQ #{req_num} start"
            f"  {task.task_id}  {self._ts()}{self._RESET}"
        )

        try:
            resp    = self.client.chat.completions.create(
                model=model, max_tokens=max_tokens, messages=msgs,
                tools=task.tools or None,
                tool_choice="auto" if task.tools else None,
            )
            elapsed                   = time.monotonic() - t0
            msg                       = resp.choices[0].message
            tokens_in, reasoning, tokens_out = self._extract_tokens(resp.usage)

            self.counter.record(
                model=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                tokens_reasoning=reasoning,
            )
            print(
                f"  {self._DIM}[parallel] ← REQ #{req_num} done "
                f"  {task.task_id}  {self._ts()}"
                f"  {self._THINK}🧠{reasoning:,}{self._DIM}"
                f"  {self._RESP}💬{tokens_out:,}{self._DIM}"
                f"  {elapsed:.1f}s{self._RESET}"
            )
            return ParallelResult(
                task_id=task.task_id,
                content=msg.content or "",
                tool_calls=self._serialize_tool_calls(msg.tool_calls),
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )

        except Exception as e:
            elapsed = time.monotonic() - t0
            self.counter.record(model=model, error=True)
            print(
                f"  {self._ERR}[parallel] ✗ REQ #{req_num} error"
                f"  {task.task_id}  {elapsed:.1f}s  {e}{self._RESET}"
            )
            return ParallelResult(task_id=task.task_id, content="", error=str(e))

    # ── Public API ────────────────────────────────────────────────────

    def run_parallel(self, tasks: list[ParallelTask]) -> list[ParallelResult]:
        """
        Submit all *tasks* to the thread pool and return results in the
        same order as the input list, regardless of completion order.
        """
        results: list[ParallelResult | None] = [None] * len(tasks)
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            future_to_index = {pool.submit(self._single_request, t): i
                               for i, t in enumerate(tasks)}
            for future in concurrent.futures.as_completed(future_to_index):
                idx = future_to_index[future]
                try:
                    results[idx] = future.result()
                except Exception as e:
                    results[idx] = ParallelResult(
                        task_id=tasks[idx].task_id, content="", error=str(e)
                    )
        return results  # type: ignore[return-value]


print("✓ Parallel API handler ready")

# ---------- Heartbeat Manager ----------

class HeartbeatManager:
    """Periodic health checks: core files, pending needs, skill inventory, log rotation."""

    CORE_FILES: tuple[str, ...] = (
        "SOUL.md", "USER.md", "MEMORY.md", "HEARTBEAT.md", "Need.md", "Tools.md",
    )

    # ── Health checks ─────────────────────────────────────────────────

    @staticmethod
    def _check_core_files(status: dict[str, Any]) -> None:
        """Verify that all required core identity files exist."""
        missing = [f for f in HeartbeatManager.CORE_FILES
                   if not (Config.CORE_DIR / f).exists()]
        if missing:
            status["checks"]["context_validation"] = "critical"
            status["action_required"] = True
            status["actions"].append(f"Missing core files: {missing}")
        else:
            status["checks"]["context_validation"] = "ok"

    @staticmethod
    def _check_pending_needs(status: dict[str, Any]) -> None:
        """Flag any PENDING entries in Need.md."""
        needs = HeartbeatManager._get_pending_needs()
        if needs:
            status["checks"]["need_review"] = "attention"
            status["action_required"] = True
            status["actions"].append(f"{len(needs)} pending request(s) in Need.md")
        else:
            status["checks"]["need_review"] = "ok"

    @staticmethod
    def _check_skills(status: dict[str, Any], tools_store: CachedSubtools) -> None:
        """Validate registered skills and detect unregistered skill files."""
        issues: list[str] = [
            f"{s['name']}: {s.get('validation_error', 'unknown')}"
            for s in tools_store.validate_all_skills().get("invalid", [])
        ]
        if Config.SKILLS_DIR.exists():
            registered   = {s["name"] for s in tools_store.list_skills()}
            on_disk      = {f.stem for f in Config.SKILLS_DIR.iterdir() if f.suffix == ".py"}
            unregistered = on_disk - registered
            if unregistered:
                issues.append(f"Unregistered skills: {sorted(unregistered)}")

        if issues:
            status["checks"]["skill_inventory"] = "attention"
            status["action_required"] = True
            status["actions"].extend(issues)
        else:
            status["checks"]["skill_inventory"] = "ok"

    # ── Public API ────────────────────────────────────────────────────

    @staticmethod
    def check(tools_store: CachedSubtools | None = None) -> dict[str, Any]:
        """Run all health checks and return a structured status dict."""
        status: dict[str, Any] = {
            "timestamp":        datetime.now().isoformat(),
            "interval_seconds": Config.HEARTBEAT_INTERVAL,
            "checks":           {},
            "action_required":  False,
            "actions":          [],
        }

        HeartbeatManager._check_core_files(status)
        HeartbeatManager._check_pending_needs(status)

        if tools_store is not None:
            HeartbeatManager._check_skills(status, tools_store)

        logger.rotate_old_daily_logs()
        status["checks"]["log_rotation"] = "ok"

        severity = "ATTENTION" if status["action_required"] else "OK"
        msg      = f"HEARTBEAT {severity}"
        if status["actions"]:
            msg += f": {'; '.join(status['actions'][:3])}"
        logger.info(msg)
        return status

    @staticmethod
    def _get_pending_needs() -> list[dict[str, str]]:
        """
        Parse PENDING request blocks from core/Need.md.
        Mirrors ``check_needs()`` but is private to avoid the circular
        dependency that would arise from calling the module-level function.
        Uses the shared ``_NEED_FIELDS`` constant defined in cell 9.
        """
        content = FileManager.read_file(Config.CORE_DIR / "Need.md", default="")
        pending: list[dict[str, str]] = []
        for block in content.split("## [")[1:]:
            if "Status: PENDING" not in block:
                continue
            entry: dict[str, str] = {}
            for field in _NEED_FIELDS:
                if m := re.search(rf'\*\*{field}\*\*\s*:\s*(.+)', block):
                    entry[field.lower()] = m.group(1).strip()
            if entry:
                pending.append(entry)
        return pending

    @staticmethod
    def run_loop(
        interval:    int | None             = None,
        stop_event:  threading.Event | None = None,
        tools_store: CachedSubtools | None  = None,
    ) -> None:
        """
        Run ``check()`` repeatedly until *stop_event* is set.
        Designed to be executed on a background daemon thread.
        """
        interval = interval or Config.HEARTBEAT_INTERVAL
        logger.info(f"Heartbeat started (interval={interval}s)")
        while not (stop_event and stop_event.is_set()):
            try:
                HeartbeatManager.check(tools_store)
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")
            time.sleep(interval)
        logger.info("Heartbeat stopped")


print("✓ Heartbeat manager ready")

# Import OpenAI client
try:
    from openai import OpenAI
except ImportError:
    print("ERROR: openai not installed. Run: pip install openai")
    raise


class Agent:
    """Universal autonomous AI agent backed by the NVIDIA NIM API."""

    # Log colours / levels
    _LOG_LEVELS: dict[str, str] = {
        "error": "error", "system": "info", "user": "info",
        "assistant": "info", "tool": "info", "result": "debug",
    }
    _LOG_COLORS: dict[str, str] = {
        "system":    "\033[90m", "user":   "\033[94m",
        "assistant": "\033[92m", "tool":   "\033[93m",
        "result":    "\033[96m", "error":  "\033[91m",
    }
    _CACHEABLE_TOOLS = frozenset({"web_search", "web_fetch"})

    # Chat commands
    _CHAT_COMMANDS: dict[str, str] = {
        "exit/quit/q":   "Exit the agent",
        "status":        "Show full agent status",
        "stats":         "Show API usage statistics",
        "history":       "Show past session stats",
        "progress":      "Show current plan progress",
        "todo":          "Show current todo list",
        "logs [N]":      "Show last N log records (default 20)",
        "reset":         "Clear conversation history",
        "clear":         "Clear persistent memory",
        "stream":        "Toggle streaming on/off",
        "heartbeat":     "Run system health check",
        "help":          "Show this help",
    }

    # Tools by category — evaluated lazily so TOOLS is always up to date
    _TOOLS_BY_CATEGORY: dict[str, Callable[[], list[str]]] = {
        "Memory":    lambda: [t["name"] for t in TOOLS if t["name"].startswith("memory_")],
        "Files":     lambda: [t["name"] for t in TOOLS if t["name"].startswith("file_")],
        "Shell":     lambda: ["shell_run"],
        "Code":      lambda: ["execute_python", "diff_apply"],
        "Web":       lambda: ["web_search", "web_fetch"],
        "Sub-tools": lambda: [t["name"] for t in TOOLS if t["name"].startswith("subtool_")],
        "Skills":    lambda: [t["name"] for t in TOOLS if t["name"].startswith("skill_")],
        "Todo":      lambda: [t["name"] for t in TOOLS if t["name"].startswith("todo_")],
        "Needs":     lambda: ["post_need", "check_needs"],
        "Session":   lambda: ["save_session_log", "search_memory", "check_heartbeat",
                              "read_file", "write_file", "append_file", "delete_file",
                              "list_directory", "search_files"],
        "Parallel":  lambda: ["parallel_generate"],
        "Other":     lambda: ["task_plan", "agent_status", "api_stats"],
    }

    _STATUS_ICONS: dict[str, str] = {
        "pending": "○", "in_progress": "▶", "completed": "✓", "failed": "✗",
    }

    # Extension hints for parallel file generation
    _EXT_HINTS: dict[str, str] = {
        ".py":   "senior Python engineer. Clean, type-hinted, documented code",
        ".js":   "senior JavaScript engineer. Modern ES2022+ with JSDoc",
        ".ts":   "senior TypeScript engineer. Strict types, interfaces, JSDoc",
        ".jsx":  "senior React engineer. Functional components with hooks",
        ".tsx":  "senior React/TS engineer. Strict typing, functional components",
        ".html": "senior frontend engineer. Semantic, accessible HTML5",
        ".css":  "senior CSS engineer. Clean, modern CSS with BEM",
        ".md":   "technical writer. Clear, well-structured Markdown",
        ".json": "Output valid, well-formatted JSON only — no prose, no fences",
        ".yaml": "Output valid YAML only — no prose, no fences",
        ".sh":   "senior DevOps engineer. Robust, portable bash with error handling",
        ".sql":  "senior DB engineer. Clean, optimised SQL",
        ".rs":   "senior Rust engineer. Safe, idiomatic Rust",
        ".go":   "senior Go engineer. Idiomatic, well-documented Go",
        ".java": "senior Java engineer. Clean, well-documented Java",
        ".rb":   "senior Ruby engineer. Idiomatic, well-documented Ruby",
    }
    _ROLE_PREFIX    = "You are a "
    _NO_FENCE       = (
        "\nOutput ONLY the file content — no preamble, "
        "no markdown fences unless the file IS markdown."
    )
    _DEFAULT_SYSTEM = (
        "You are an expert software engineer and technical writer. "
        "Produce complete, high-quality output for the requested file." + _NO_FENCE
    )

    # Context-window overflow detection keywords
    _OVERFLOW_KEYS: tuple[str, ...] = (
        "context", "token", "length", "limit", "too long", "maximum",
    )

    # Embedded agent instructions (formatted in _build_system_prompt)
    _SYSTEM_INSTRUCTIONS = """\
## 🎯 TODO LIST TASK MANAGEMENT SYSTEM
Always think before you act. For complex multi-step tasks use the todo list:

### Core Tools
• **todo_create(task, steps)** — Create a new todo list with main steps
• **todo_add_subtask(parent_id, description)** — Add subtask to any parent
• **todo_complete(task_id, result?)** — Mark completed with optional result
• **todo_fail(task_id, error?)** — Mark failed with error info
• **todo_status()** — Get full todo tree with progress
• **todo_set_current(task_id)** — Set which task to work on next
• **todo_update(task_id, ...)** — Update task description/notes

### Need Management
• **post_need(priority, need, reason, blocking)** — Request something from the user
• **check_needs()** — List all PENDING user-requests

### Session & Memory
• **save_session_log(entry, category)** — Log an action to today's daily log
• **search_memory(query, days_back)** — Search historical daily logs
• **check_heartbeat()** — Run full system health check

### Workflow Pattern
1. **Plan First**: For complex tasks call todo_create()
2. **Work Sequentially**: Check todo_status() before each action
3. **Create Subtasks**: Use todo_add_subtask() for complex steps
4. **Complete Tasks**: Call todo_complete() / todo_fail() after each step
5. **Parallel Execution**: Use parallel_generate() for 3+ independent files
6. **Track Progress**: The system maintains full hierarchy and status

## ReAct Pattern — ALWAYS follow:
1. **Thought**: Check todo_status()
2. **Plan**: If no list exists, call todo_create()
3. **Act**: Work on current task
4. **Complete**: Call todo_complete()
5. **Repeat**: System auto-advances to next task
6. **Answer**: Give final response when all tasks done

## When NOT to use tools
- Greetings, casual chat, simple questions you already know
- Explaining concepts, writing short text, poems, outlines

## Path rules
- ALL file_* tool paths are RELATIVE to {files_dir}
- NEVER pass absolute paths like '{base_dir}' to file_* tools
- For read_file / write_file / append_file you may use absolute paths

## Core workflow
1. Complex tasks → todo_create first
2. shell_run to install, run, test, lint, git
3. diff_apply for targeted edits (not full rewrites)
4. parallel_generate for 3+ independent files
5. Verify with shell_run after generating code
6. Use start_line/end_line in file_read for large files

## Tool selection cheat-sheet
- Edit existing  : diff_apply
- New file (1)   : file_write
- New files (3+) : file_write_many  OR  parallel_generate
- Run anything   : shell_run
- Maths/logic    : execute_python
- Research       : web_search → web_fetch
- Needs post     : post_need

## Rules
- NEVER call file_write_many with fewer than 3 files
- Always read shell_run output and react to errors
- Context window: 262 000 tokens (budget ~{ctx_budget:,} usable)
- Max output: {max_tokens:,} tokens per call. Max iterations: {max_iters}
"""

    # Core identity file templates written on first run
    _CORE_TEMPLATES: dict[str, str] = {
        "SOUL.md":      "# SOUL.md\n## Identity\nI am an autonomous AI agent.\n",
        "USER.md":      "# USER.md\n## Profile\nUser profile and preferences.\n",
        "HEARTBEAT.md": "# HEARTBEAT.md\n## Periodic Checklist\n- Check pending needs\n- Validate skills\n",
        "MEMORY.md":    "# MEMORY.md\n## Long-term Memory\n*(empty)*\n",
        "Need.md":      "# Need.md\n## Agent Requests to User\n*(none yet)*\n",
        "Tools.md":     "# Tools.md\n## Tool and Skill Registry\n\n## Skill Scripts\n*(none yet)*\n",
    }

    # ── Construction ──────────────────────────────────────────────────

    def __init__(self, verbose: bool = True, stream: bool = True) -> None:
        if not setup_api_key():
            raise RuntimeError("Failed to setup API key")

        self.client      = OpenAI(api_key=os.environ["NVIDIA_API_KEY"], base_url=Config.BASE_URL)
        self.mem         = EfficientMemory()
        self.tools_store = CachedSubtools()
        self.files       = SecureFiles()
        self.code        = SecureCodeExecutor()
        self.web         = SecureWebSearch()
        self.shell       = ShellRunner()
        self.differ      = DiffApplier()
        self.rate_limiter = RateLimiter()
        self.counter     = APICounter()
        self.tool_cache  = ToolResultCache()
        self.parallel    = ParallelAPIHandler(
            self.client, self.counter, rate_limiter=self.rate_limiter
        )
        self.todo        = TodoListManager(self.mem)

        self.verbose:  bool                    = verbose
        self.stream:   bool                    = stream
        self.history:  list[dict[str, Any]]    = []
        self._current_plan_step:  int          = 0
        self._current_plan_total: int          = 0
        self.initialized:         bool         = False

        self._heartbeat_thread: threading.Thread | None = None
        self._stop_heartbeat:   threading.Event         = threading.Event()

        self._register_shutdown()
        self._initialize_core_files()
        self.initialized = True
        self._log(
            f"Agent ready | model={Config.MODEL} | workspace={Config.BASE_DIR} | stream={stream}"
        )

    # ── Core file initialisation ──────────────────────────────────────

    def _initialize_core_files(self) -> None:
        """Write default core identity files if they don't yet exist."""
        for filename, content in self._CORE_TEMPLATES.items():
            path = Config.CORE_DIR / filename
            if not path.exists():
                FileManager.write_file(path, content)
        SessionManager.ensure_todays_log()

    # ── Heartbeat control ─────────────────────────────────────────────

    def start_background_heartbeat(self, interval: int | None = None) -> None:
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            logger.warning("Heartbeat already running")
            return
        self._stop_heartbeat.clear()
        effective_interval = interval or Config.HEARTBEAT_INTERVAL
        self._heartbeat_thread = threading.Thread(
            target=HeartbeatManager.run_loop,
            args=(effective_interval, self._stop_heartbeat, self.tools_store),
            daemon=True,
        )
        self._heartbeat_thread.start()
        logger.info(f"Background heartbeat started (interval={effective_interval}s)")

    def stop_background_heartbeat(self) -> None:
        if self._heartbeat_thread:
            self._stop_heartbeat.set()
            self._heartbeat_thread.join(timeout=5)
            if self._heartbeat_thread.is_alive():
                logger.warning("Heartbeat thread did not stop cleanly")
            else:
                logger.info("Heartbeat stopped")

    # ── Internal helpers ──────────────────────────────────────────────

    def _register_shutdown(self) -> None:
        """Register SIGINT/SIGTERM handlers to persist stats before exit."""
        def _handler(sig, frame):
            print("\n\033[93m[AGENT] Shutting down… saving stats.\033[0m")
            self.counter.persist_stats()
            self.stop_background_heartbeat()
            sys.exit(0)
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except (OSError, ValueError):
                pass  # Signal registration not supported in this context (e.g. threads)

    def _log(self, msg: str, role: str = "system") -> None:
        getattr(logger, self._LOG_LEVELS.get(role, "info"))(str(msg)[:10000], role=role)
        if not self.verbose and role != "error":
            return
        color = self._LOG_COLORS.get(role, "\033[97m")
        print(f"{color}[{role.upper():<9}]\033[0m {str(msg)[:4000]}")

    # ── Tool dispatch ─────────────────────────────────────────────────

    def _build_dispatch(self, inp: dict[str, Any]) -> dict[str, Callable[[], Any]]:
        """
        Build a name → callable map for all supported tools.
        Lambdas close over *inp* so each is evaluated only when called.
        """
        m, t, f = self.mem, self.tools_store, self.files
        return {
            # Memory
            "memory_save":       lambda: m.save(inp["key"], inp["value"], inp.get("tag", "")),
            "memory_get":        lambda: m.get(inp["key"]),
            "memory_list":       lambda: m.list_keys(inp.get("tag", "")),
            "memory_search":     lambda: m.search(inp["query"]),
            "memory_delete":     lambda: m.delete(inp["key"]),
            # Files (sandboxed)
            "file_read":         lambda: f.read(inp["path"],
                                     int(inp.get("start_line") or 0),
                                     int(inp.get("end_line")   or 0)),
            "file_write":        lambda: f.write(inp["path"], inp["content"],
                                     inp.get("append", False), inp.get("backup", False)),
            "file_write_many":   lambda: f.write_many(inp["writes"]),
            "file_list":         lambda: f.list(inp.get("directory", ""), inp.get("metadata", False)),
            "file_delete":       lambda: f.delete(inp["path"]),
            "file_search":       lambda: f.search(inp["query"], inp.get("directory", "")),
            "file_rename":       lambda: f.rename(inp["src"], inp["dst"]),
            # Shell
            "shell_run":         lambda: self.shell.run(
                                     inp["command"],
                                     timeout=min(int(inp.get("timeout", Config.SHELL_TIMEOUT)), 600),
                                     cwd=inp.get("cwd", "")),
            # Code
            "execute_python":    lambda: (
                                     self.code.run(inp["code"], inp.get("timeout", Config.CODE_TIMEOUT))
                                     if inp.get("code") else "Missing parameter 'code'"),
            # Diff
            "diff_apply":        lambda: self.differ.apply(inp["path"], inp["diff"]),
            # Web
            "web_search":        lambda: self.web.search(inp["query"], inp.get("max_results", 5)),
            "web_fetch":         lambda: self.web.fetch(inp["url"], inp.get("max_chars", Config.MAX_WEB_CONTENT)),
            # Sub-tools
            "subtool_create":    lambda: t.create(inp["name"], inp["description"],
                                     inp["code"], inp.get("force", False)),
            "subtool_run":       lambda: t.run(
                                     inp["name"],
                                     **(inp.get("kwargs") if isinstance(inp.get("kwargs"), dict) else {})),
            "subtool_list":      lambda: t.list(),
            "subtool_delete":    lambda: t.delete(inp["name"]),
            # Skills
            "skill_list":        lambda: t.list_skills(),
            "skill_create":      lambda: t.create_skill(
                                     inp["name"], inp["code"], inp["purpose"], inp["trigger"],
                                     inp.get("input_fmt", ""), inp.get("output_fmt", "")),
            "skill_run":         lambda: t.run_skill(inp["name"], inp.get("input_data")),
            "skill_validate":    lambda: (
                                     t.validate_skill(inp["name"]) if inp.get("name")
                                     else t.validate_all_skills()),
            # Todo
            "todo_create":       lambda: self.todo.create(inp["task"], inp["steps"]),
            "todo_status":       lambda: self.todo.status(),
            "todo_add_subtask":  lambda: self.todo.add_subtask(inp["parent_id"], inp["description"]),
            "todo_complete":     lambda: self.todo.complete(inp["task_id"], inp.get("result")),
            "todo_fail":         lambda: self.todo.fail(inp["task_id"], inp.get("error")),
            "todo_set_current":  lambda: self.todo.set_current(inp["task_id"]),
            "todo_update":       lambda: self.todo.update(
                                     inp["task_id"], inp.get("description"), inp.get("notes")),
            # Needs
            "post_need":         lambda: post_need(
                                     inp["priority"], inp["need"], inp["reason"], inp["blocking"]),
            "check_needs":       lambda: check_needs(),
            # Session
            "save_session_log":  lambda: SessionManager.save_log(
                                     inp["entry"], inp.get("category", "General")),
            "search_memory":     lambda: SessionManager.search_memory(
                                     inp["query"], inp.get("days_back", 7)),
            "check_heartbeat":   lambda: HeartbeatManager.check(self.tools_store),
            # General file ops (unrestricted paths)
            "read_file":         lambda: FileManager.read_file(
                                     Path(inp["filepath"]), encoding=inp.get("encoding", "utf-8")),
            "write_file":        lambda: FileManager.write_file(
                                     Path(inp["filepath"]), inp["content"]),
            "append_file":       lambda: FileManager.append_file(
                                     Path(inp["filepath"]), inp["content"],
                                     inp.get("add_timestamp", True)),
            "delete_file":       lambda: FileManager.delete_path(Path(inp["filepath"])),
            "list_directory":    lambda: FileManager.list_directory(
                                     Path(inp.get("path", ".")), inp.get("pattern", "*")),
            "search_files":      lambda: self._search_files(
                                     inp["query"],
                                     inp.get("directory", "."),
                                     inp.get("file_pattern", "*.md")),
            # Planning / status
            "task_plan":         lambda: self._save_plan(inp["task"], inp["steps"]),
            "agent_status":      lambda: self._agent_status(),
            "api_stats":         lambda: {
                                     **self.counter.summary(),
                                     "historical_sessions": len(self.counter.load_historical()),
                                 },
            "parallel_generate": lambda: self._parallel_generate(
                                     inp["items"], inp.get("system_prompt", "")),
        }

    def _dispatch(self, name: str, inp: dict[str, Any]) -> Any:
        """Look up and invoke a tool by name, with caching and error handling."""
        try:
            self.rate_limiter.wait_if_needed()
            if name in self._CACHEABLE_TOOLS:
                if cached := self.tool_cache.get(name, inp):
                    self._log(f"  \033[90m[cache hit] {name}\033[0m", "system")
                    return cached
            fn = self._build_dispatch(inp).get(name)
            if fn is None:
                return f"Unknown tool: {name}"
            result = fn()
            if name in self._CACHEABLE_TOOLS and result and not str(result).startswith("Error"):
                self.tool_cache.set(name, inp, result)
            return result if isinstance(result, str) else json.dumps(result, default=str, indent=2)
        except Exception as e:
            msg = f"Tool error [{name}]: {e}\n{traceback.format_exc()}"
            self._log(msg, "error")
            return msg

    # ── File search ───────────────────────────────────────────────────

    def _search_files(
        self, query: str, directory: str = ".", file_pattern: str = "*.md"
    ) -> list[dict[str, Any]]:
        q      = query.lower()
        target = Path(directory) if Path(directory).is_absolute() else Config.BASE_DIR / directory
        results: list[dict[str, Any]] = []
        for filepath in target.rglob(file_pattern):
            try:
                content = FileManager.read_file(filepath)
                if q not in content.lower():
                    continue
                matches = [
                    {"line_num": n + 1, "text": line.strip()}
                    for n, line in enumerate(content.splitlines())
                    if q in line.lower()
                ][:10]
                results.append({"file": str(filepath), "matches": matches})
            except Exception:
                continue
        return results

    # ── Agent status ──────────────────────────────────────────────────

    def _agent_status(self) -> dict[str, Any]:
        dirs = {
            name: path.exists()
            for name, path in (
                ("core",   Config.CORE_DIR),
                ("memory", Config.MEMORY_DIR),
                ("skills", Config.SKILLS_DIR),
                ("work",   Config.WORK_DIR),
                ("files",  Config.FILES_DIR),
            )
        }
        skills_count = (
            len(list(Config.SKILLS_DIR.glob("*.py")))
            if Config.SKILLS_DIR.exists() else 0
        )
        return {
            "model":            Config.MODEL,
            "workspace":        str(Config.BASE_DIR),
            "initialized":      self.initialized,
            "directories":      dirs,
            "core_files":       {f: (Config.CORE_DIR / f).exists() for f in HeartbeatManager.CORE_FILES},
            "memory_keys":      self.mem.list_keys(),
            "subtools":         list(self.tools_store.list().keys()),
            "skills_count":     skills_count,
            "files":            self.files.list(),
            "api_stats":        self.counter.summary(),
            "tool_cache_size":  self.tool_cache.size,
            "plan_progress":    (
                f"{self._current_plan_step}/{self._current_plan_total}"
                if self._current_plan_total else "no active plan"
            ),
            "todo_status":      self.todo.status(),
            "heartbeat_active": (
                self._heartbeat_thread is not None and self._heartbeat_thread.is_alive()
            ),
        }

    # ── Plan tracking ─────────────────────────────────────────────────

    def _save_plan(self, task: str, steps: list[str]) -> str:
        self._current_plan_step, self._current_plan_total = 0, len(steps)
        self.mem.save("current_plan", {"task": task, "steps": steps, "current_step": 0}, tag="plan")
        return f"Plan saved ({len(steps)} steps). Use agent_status to track progress."

    def _advance_plan_step(self) -> None:
        if self._current_plan_total <= 0:
            return
        self._current_plan_step = min(self._current_plan_step + 1, self._current_plan_total)
        plan = self.mem.get("current_plan")
        if isinstance(plan, dict):
            plan["current_step"] = self._current_plan_step
            self.mem.save("current_plan", plan, tag="plan")

    # ── System prompt ─────────────────────────────────────────────────

    def _build_system_prompt(self) -> str:
        mem_keys      = self.mem.list_keys()
        subtool_names = list(self.tools_store.list().keys())
        context       = SessionManager.load_context()
        has_identity  = any(len(v) > 50 for v in context.values())

        header = (
            f"You are a universal autonomous AI agent powered by {Config.MODEL} via NVIDIA NIM.\n"
            f"Workspace: {Config.BASE_DIR} | Files dir: {Config.FILES_DIR} | "
            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"Memory keys: {mem_keys[:Config.SYSTEM_PROMPT_MEM_LIMIT]} | "
            f"Sub-tools: {subtool_names[:Config.SYSTEM_PROMPT_TOOL_LIMIT]}\n\n"
        )
        instructions = self._SYSTEM_INSTRUCTIONS.format(
            files_dir=Config.FILES_DIR, base_dir=Config.BASE_DIR,
            ctx_budget=Config.CONTEXT_TOKEN_BUDGET,
            max_tokens=Config.MAX_TOKENS, max_iters=Config.MAX_ITERS,
        )
        if has_identity:
            tools_str        = "\n".join(f"- {t['name']}: {t['description']}" for t in TOOLS)
            identity_section = build_system_prompt_from_context(context, tools_str)
            return identity_section + "\n---\n\n" + header + self._todo_context() + instructions
        return header + self._todo_context() + instructions

    def _todo_context(self) -> str:
        ts = self.todo.status()
        if "error" in ts or not ts.get("tasks"):
            return ""
        current = ts.get("current_task")
        lines   = [
            "## 📋 ACTIVE TODO LIST\n",
            f"Overall Task: {ts['task']}\n",
            f"Progress: {ts['progress']}\n",
        ]
        if current:
            lines.append(
                f"CURRENT TASK: {current['id']} - {current['description']} [{current['status']}]\n"
            )
        lines.append("Use todo_status() to see full tree. Always work on CURRENT TASK.\n\n")
        return "".join(lines)

    # ── Token-aware history management ────────────────────────────────

    @staticmethod
    def _est_tokens(text: str) -> int:
        """
        Conservative token-count approximation (1 token ≈ 3 chars) for history
        budget management.  Intentionally overestimates to provide a safety margin
        against context-window overflows.
        Note: ``LiveStatusBar._approx`` uses ``// 4`` (more accurate) for display only.
        """
        return max(1, len(text) // 3)

    def _history_tokens(self) -> int:
        total = 0
        for msg in self.history:
            content = msg.get("content") or ""
            total  += self._est_tokens(
                content if isinstance(content, str) else json.dumps(content)
            )
            if tc := msg.get("tool_calls"):
                total += self._est_tokens(json.dumps(tc))
        return total

    def _cap_result(self, result: str, _tool_name: str) -> str:
        """Truncate *result* to the configured character cap, preserving head and tail."""
        cap = Config.TOOL_RESULT_CHARS_MAX
        if len(result) <= cap:
            return result
        h = cap // 2
        return (
            f"{result[:h]}\n\n... [TRUNCATED: {len(result):,} chars total, "
            f"showing first+last {h} chars] ...\n\n{result[-h:]}"
        )

    def _summarize_messages(self, messages: list[dict[str, Any]]) -> str:
        """Summarise *messages* into 3-5 sentences via a lightweight API call."""
        if not messages:
            return ""

        def _msg_line(m: dict[str, Any]) -> str:
            content = m.get("content") or ""
            if isinstance(content, list):
                content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
            return f"[{m.get('role', '?')}]: {str(content)[:4000]}"

        dump   = "\n".join(map(_msg_line, messages))
        prompt = (
            "Summarise the following conversation history in 3-5 sentences, "
            "preserving all key facts, decisions, file names, and errors:\n\n" + dump
        )

        @retry_api(max_retries=3, backoff=1.5)
        def _call(client, model, content):
            return client.chat.completions.create(
                model=model, max_tokens=600,
                messages=[{"role": "user", "content": content}],
            )

        try:
            self.rate_limiter.wait_if_needed()
            resp = _call(self.client, Config.MODEL, prompt)
            return resp.choices[0].message.content or dump[:1000]
        except Exception as exc:
            logger.warning("summarise failed, using truncated dump", error=str(exc))
            return dump[:1000]

    def _trim_history_to_budget(self, budget: int | None = None) -> None:
        """Summarise and drop old history messages to stay within *budget* tokens."""
        budget = budget or Config.CONTEXT_TOKEN_BUDGET
        if self._history_tokens() <= budget or len(self.history) <= 4:
            return
        head   = [self.history[0]]
        tail   = self.history[-12:]
        middle = self.history[1: max(1, len(self.history) - 12)]
        if middle:
            summary = self._summarize_messages(middle)
            head.append({
                "role":    "assistant",
                "content": f"[Context summary — earlier conversation]\n{summary}",
            })
        self.history = head + tail
        while self._history_tokens() > budget and len(self.history) > 2:
            self.history = [self.history[0]] + self.history[2:]

    # ── API call helpers ──────────────────────────────────────────────

    def _extract_usage(self, usage: Any) -> tuple[int, int, int]:
        """Return ``(tokens_in, tokens_reasoning, tokens_out)`` from a usage object."""
        if not usage:
            return 0, 0, 0
        details   = getattr(usage, "completion_tokens_details", None)
        prompt    = getattr(usage, "prompt_tokens",      0) or 0
        reasoning = getattr(details, "reasoning_tokens", 0) or 0
        response  = max((getattr(usage, "completion_tokens", 0) or 0) - reasoning, 0)
        return prompt, reasoning, response

    def _serialize_tool_calls(self, raw_calls: Any) -> list[dict[str, Any]]:
        if not raw_calls:
            return []
        return [
            {
                "id": tc.id, "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in raw_calls
        ]

    def _api_call_with_overflow_retry(
        self, system: str
    ) -> tuple[str, list[dict] | None]:
        """Call the API, automatically trimming history on context-overflow errors."""
        msgs   = [{"role": "system", "content": system}] + self.history
        caller = self._call_api_streaming if self.stream else self._call_api_normal
        try:
            text, tool_calls = caller(msgs)
            if isinstance(text, str) and text.startswith("API Error"):
                return text, None
            return text, tool_calls
        except Exception as e:
            if not any(k in str(e).lower() for k in self._OVERFLOW_KEYS):
                self.counter.record(model=Config.MODEL, error=True)
                return f"API Error: {e}", None
            self._log("Context overflow — trimming history and retrying…", "system")
            self._trim_history_to_budget(budget=Config.CONTEXT_TOKEN_BUDGET // 3)
            try:
                msgs             = [{"role": "system", "content": system}] + self.history
                text, tool_calls = caller(msgs)
                return text, tool_calls
            except Exception as e2:
                self.counter.record(model=Config.MODEL, error=True)
                return f"API Error (after trim): {e2}", None

    # ── Streaming API call ────────────────────────────────────────────

    @retry_api()
    def _call_api_streaming(self, messages: list[dict]) -> tuple[str, list]:
        req_num   = self.counter.next_request_number()
        print(f"\033[92m[ASSISTANT]\033[0m  \033[90mstreaming REQ #{req_num}…\033[0m")
        bar       = LiveStatusBar(req_number=req_num, last_n_words=8)
        full_text = ""
        tc_buf:   dict[int, dict] = {}
        success   = True
        t0        = time.monotonic()

        try:
            stream = self.client.chat.completions.create(
                model=Config.MODEL, max_tokens=Config.MAX_TOKENS,
                messages=messages, tools=OAI_TOOLS, tool_choice="auto",
                stream=True, stream_options={"include_usage": True},
            )
            for chunk in stream:
                if hasattr(chunk, "usage") and chunk.usage is not None:
                    self._record_stream_usage(chunk.usage, bar, req_num)
                    continue
                if not getattr(chunk, "choices", None):
                    continue
                delta = chunk.choices[0].delta
                if r := getattr(delta, "reasoning_content", None):
                    bar.add_reasoning(r)
                if delta.content:
                    full_text += delta.content
                    bar.add_response(delta.content)
                if delta.tool_calls:
                    self._accumulate_tool_chunks(delta.tool_calls, tc_buf)
        except Exception:
            success = False
            self.counter.record(model=Config.MODEL, error=True)
            raise
        finally:
            bar.finalize(success=success, elapsed=time.monotonic() - t0)
            if success and self.counter.total < req_num:
                self.counter.record(
                    model=Config.MODEL,
                    tokens_out=bar.response_tokens,
                    tokens_reasoning=bar.reasoning_tokens,
                )
        return full_text, [tc_buf[i] for i in sorted(tc_buf)]

    def _record_stream_usage(self, usage: Any, bar: LiveStatusBar, req_num: int) -> None:
        prompt, api_reasoning, api_response = self._extract_usage(usage)
        reasoning = max(bar.reasoning_tokens, api_reasoning)
        response  = api_response or bar.response_tokens
        bar.set_actual_tokens(reasoning=reasoning, response=response)
        self.counter.record(
            model=Config.MODEL,
            tokens_in=prompt, tokens_out=response, tokens_reasoning=reasoning,
        )

    @staticmethod
    def _accumulate_tool_chunks(deltas: Any, buf: dict[int, dict]) -> None:
        for tc in deltas:
            entry = buf.setdefault(tc.index, {
                "id": "", "type": "function",
                "function": {"name": "", "arguments": ""},
            })
            if tc.id:                 entry["id"]                        = tc.id
            if tc.function.name:      entry["function"]["name"]         += tc.function.name
            if tc.function.arguments: entry["function"]["arguments"]    += tc.function.arguments

    # ── Non-streaming API call ────────────────────────────────────────

    @retry_api()
    def _call_api_normal(self, messages: list[dict]) -> tuple[str, list]:
        resp = self.client.chat.completions.create(
            model=Config.MODEL, max_tokens=Config.MAX_TOKENS,
            messages=messages, tools=OAI_TOOLS, tool_choice="auto",
        )
        prompt, reasoning, response = self._extract_usage(getattr(resp, "usage", None))
        self.counter.record(
            model=Config.MODEL,
            tokens_in=prompt, tokens_out=response, tokens_reasoning=reasoning,
        )
        msg = resp.choices[0].message
        tcs = self._serialize_tool_calls(msg.tool_calls) if msg.tool_calls else []
        return msg.content or "", tcs

    # ── Main run loop ─────────────────────────────────────────────────

    def run(self, task: str) -> str:
        """Process *task* and return the final response string."""
        self._log(task, "user")
        self.history.append({"role": "user", "content": task})
        SessionManager.save_log(f"User: {task}", category="Input")
        system = self._build_system_prompt()

        for _ in range(Config.MAX_ITERS):
            self._trim_history_to_budget()
            self._log(
                f"context ~{self._history_tokens():,} est. tokens  |  "
                f"{len(self.history)} history msgs", "system"
            )
            self._log_current_todo()

            text, tool_calls = self._api_call_with_overflow_retry(system)
            if isinstance(text, str) and text.startswith("API Error"):
                SessionManager.save_log(f"API error: {text}", category="Error")
                return text

            asst: dict[str, Any] = {"role": "assistant", "content": text}
            if tool_calls:
                asst["tool_calls"] = tool_calls
            self.history.append(asst)

            if not tool_calls:
                if self.todo.is_completed():
                    self._log("✅ ALL TODO TASKS COMPLETED!", "system")
                    result = text or "All planned tasks completed successfully."
                else:
                    result = text or "Task completed."
                SessionManager.save_log(
                    f"Agent: {result[:10000]}{'...' if len(result) > 10000 else ''}",
                    category="Output",
                )
                return result

            self._process_tool_calls(tool_calls)

        return "Max iterations reached. Break task into smaller steps."

    def _log_current_todo(self) -> None:
        ct = self.todo.get_current_task()
        if ct and not self.todo.is_completed():
            self._log(f"📋 TODO: {ct['id']} - {ct['description']} [{ct['status']}]", "system")

    def _process_tool_calls(self, tool_calls: list[dict]) -> None:
        for tc in tool_calls:
            name = tc["function"]["name"]
            try:
                inp = json.loads(tc["function"]["arguments"])
            except (json.JSONDecodeError, KeyError):
                inp = {}
            self._log(f"→ {name}({json.dumps(inp, default=str)[:120]})", "tool")
            res               = self._dispatch(name, inp)
            self._advance_plan_step()
            rs                = res if isinstance(res, str) else json.dumps(res, default=str)
            rs_stored         = self._cap_result(rs, name)
            truncated         = len(rs_stored) < len(rs)
            suffix            = (
                f"  \033[90m[stored {len(rs_stored):,}/{len(rs):,} chars]\033[0m"
                if truncated else ""
            )
            self._log(f"← {name}: {rs[:200]}{suffix}", "result")
            self.history.append({"role": "tool", "tool_call_id": tc["id"], "content": rs_stored})

    # ── Parallel task runner ──────────────────────────────────────────

    def run_parallel_tasks(self, tasks: list[str], system_override: str = "") -> list[dict[str, Any]]:
        """Run multiple independent user tasks in parallel and return their results."""
        system = system_override or (
            f"You are a secure autonomous AI agent ({Config.MODEL} via NVIDIA NIM).\n"
            f"Workspace: {Config.BASE_DIR} | Memory keys: {self.mem.list_keys()[:10]}"
        )
        results = self.parallel.run_parallel([
            ParallelTask(
                task_id=f"task_{i}",
                messages=[{"role": "user", "content": t}],
                system=system, tools=OAI_TOOLS,
            )
            for i, t in enumerate(tasks)
        ])
        output: list[dict[str, Any]] = []
        for i, r in enumerate(results):
            output.append({"task_id": r.task_id, "task": tasks[i],
                           "result": r.content, "error": r.error})
            self._log(
                f"{'✓' if not r.error else '✗'} parallel[{r.task_id}]: "
                f"{(r.content or r.error)[:120]}", "result"
            )
        return output

    # ── Parallel generation ───────────────────────────────────────────

    def _infer_system(self, paths: list[str], override: str) -> str:
        """Infer a system prompt from file extensions, or fall back to the default."""
        if override:
            return override
        exts  = {Path(p).suffix.lower() for p in paths}
        hints = [self._EXT_HINTS[e] for e in exts if e in self._EXT_HINTS]
        if len(hints) == 1:
            h    = hints[0]
            base = h if h.startswith("Output") else self._ROLE_PREFIX + h
            return base + self._NO_FENCE
        return self._DEFAULT_SYSTEM

    def _parallel_generate(self, items: list[dict], system_prompt: str = "") -> str:
        if not items:
            return "Error: no items provided"
        waves      = self._topo_waves(items)
        sys_inst   = self._infer_system([it["path"] for it in items], system_prompt)
        all_writes: list[str] = []
        all_errors: list[str] = []
        has_deps   = len(waves) > 1 and any(it.get("deps") for it in items)
        t0         = time.monotonic()

        for wi, wave in enumerate(waves):
            if has_deps:
                self._log(
                    f"⚡ parallel_generate wave {wi + 1}/{len(waves)}: "
                    f"{len(wave)} items — {[it['path'] for it in wave]}", "system"
                )
            results = self.parallel.run_parallel([
                ParallelTask(
                    task_id=it["path"],
                    messages=[{"role": "user", "content": it.get("prompt", "")}],
                    system=sys_inst, tools=[],
                    model=Config.MODEL, max_tokens=Config.MAX_TOKENS,
                )
                for it in wave
            ])
            writes, errors = self._partition_results(wave, results)
            all_writes.extend(self._save_writes(writes))
            all_errors.extend(errors)

        return self._format_gen_summary(all_writes, all_errors, items, waves, has_deps,
                                        time.monotonic() - t0)

    def _topo_waves(self, items: list[dict]) -> list[list[dict]]:
        """Topologically sort *items* by their deps into parallel execution waves."""
        by_path   = {it["path"]: it for it in items}
        remaining = set(by_path)
        completed: set[str] = set()
        waves:     list[list[dict]] = []

        while remaining:
            ready = [
                by_path[p] for p in remaining
                if all(d in completed for d in (by_path[p].get("deps") or []))
            ]
            if not ready:
                self._log(
                    f"⚠ circular/missing deps — running {len(remaining)} items unsorted", "system"
                )
                waves.append([by_path[p] for p in remaining])
                break
            waves.append(ready)
            done       = {it["path"] for it in ready}
            remaining -= done
            completed |= done
        return waves

    @staticmethod
    def _partition_results(
        wave: list[dict], results: list[ParallelResult]
    ) -> tuple[list[dict], list[str]]:
        writes: list[dict] = []
        errors: list[str]  = []
        for item, res in zip(wave, results):
            p = item["path"]
            if res.error:
                errors.append(f"✗ {p}: {res.error}")
            elif res.content:
                writes.append({"path": p, "content": res.content})
            else:
                errors.append(f"✗ {p}: empty response")
        return writes, errors

    def _save_writes(self, writes: list[dict]) -> list[str]:
        if not writes:
            return []
        if len(writes) >= 3:
            return [f"✓ {r}" for r in self.files.write_many(writes)]
        return [f"✓ {self.files.write(w['path'], w['content'])}" for w in writes]

    def _format_gen_summary(
        self,
        writes:   list[str],
        errors:   list[str],
        items:    list[dict],
        waves:    list[list[dict]],
        has_deps: bool,
        elapsed:  float,
    ) -> str:
        wave_note = f"  ({len(waves)} waves)" if has_deps else ""
        lines     = writes + errors + [
            f"\033[90m⏱  {elapsed:.1f}s total for {len(items)} files{wave_note}  "
            f"(sequential est. ≈ {elapsed * len(items):.0f}s)\033[0m"
        ]
        summary = "\n".join(lines)
        self._log(summary, "result")
        return summary

    # ── Display helpers ───────────────────────────────────────────────

    def _print_todo_tree(self, tasks: list[dict], indent: str = "    ") -> None:
        current_id = self.todo._load().get("current_task_id")
        for t in tasks:
            icon = self._STATUS_ICONS.get(t["status"], "?")
            tag  = " 👈 CURRENT" if t.get("id") == current_id else ""
            print(f"{indent}{icon} {t['id']}: {t['description']}{tag}")
            for key in ("result", "error"):
                if val := t.get(key):
                    print(f"{indent}   {key.capitalize()}: {val[:100]}{'...' if len(val) > 100 else ''}")
            if t.get("children"):
                self._print_todo_tree(t["children"], indent + "    ")

    def _print_stats(self) -> None:
        s    = self.counter.summary()
        sep  = "─" * 48
        print("\n".join(("", sep,
            f"  Total requests : {s['total_requests']}  (✓{s['success']} ✗{s['errors']})",
            f"  Prompt tokens  : {s['tokens_prompt']:,}",
            f"  Think tokens   : {s['tokens_reasoning']:,}",
            f"  Resp  tokens   : {s['tokens_response']:,}",
            f"  Total tokens   : {s['tokens_total']:,}",
            f"  Uptime         : {s['uptime_seconds']}s",
            f"  Avg req/min    : {s['avg_requests_per_min']}",
            f"  By model       : {s['by_model']}", sep, "",
        )))

    def _print_progress(self) -> None:
        plan = self.mem.get("current_plan")
        if not plan or not isinstance(plan, dict):
            print("  No active plan. Use task_plan tool to create one.")
            return
        steps, cur = plan.get("steps", []), plan.get("current_step", 0)
        print(f"\n  Task: {plan.get('task', '')}")
        for i, step in enumerate(steps):
            icon = "✓" if i < cur else ("▶" if i == cur else "○")
            print(f"  {icon} [{i + 1}/{len(steps)}] {step}")

    def _print_todo(self) -> None:
        ts = self.todo.status()
        if "error" in ts:
            print("  No active todo list.")
            return
        current = ts.get("current_task")
        lines   = [
            f"\n  📋 Task: {ts['task']}",
            f"  Progress: {ts['progress']}",
            *(
                [f"  Current: {current['id']} - {current['description']} [{current['status']}]"]
                if current else []
            ),
            f"  Stats: {ts['stats']}", "", "  Full tree:",
        ]
        print("\n".join(lines))
        self._print_todo_tree(ts["tasks"])
        print()

    def _print_logs(self, n: int = 20) -> None:
        records = logger.read_recent(n)
        if not records:
            print("  No log records found.")
            return
        sep = "  " + "─" * 72
        print(f"\n  Last {len(records)} log records from {logger.LOG_FILE}:")
        print(sep)
        for r in records:
            print("  " + logger.format_record(r))
        print(sep + "\n")

    def _print_help(self) -> None:
        Y, G, R = "\033[93m", "\033[90m", "\033[0m"
        lines   = ["", f"  {Y}Chat Commands{R}"]
        lines  += [f"    {G}•{R} {k:<24} {v}" for k, v in self._CHAT_COMMANDS.items()]
        lines  += ["", f"  {Y}Agent Tools{R}"]
        for cat, fn in self._TOOLS_BY_CATEGORY.items():
            lines.append(f"  {G}{cat}{R}")
            for tool_name in fn():
                desc = next((t["description"] for t in TOOLS if t["name"] == tool_name), "")
                lines.append(f"    {G}•{R} {tool_name:<28} {str(desc)[:80]}")
            lines.append("")
        print("\n".join(lines))

    # ── Chat loop ─────────────────────────────────────────────────────

    def chat(self) -> None:
        """Start the interactive REPL."""
        thin, thick = "─" * 65, "═" * 65
        env  = "☁️  Colab" if IS_COLAB else "💻 Local"
        hb   = (
            f"✓ active ({Config.HEARTBEAT_INTERVAL}s)"
            if self._heartbeat_thread and self._heartbeat_thread.is_alive()
            else "✗ stopped"
        )
        print("\n".join(("", thick,
            f"  🤖  Universal Agentic AI  │  {Config.MODEL}",
            f"  📁  Workspace : {Config.BASE_DIR}",
            f"  📡  Streaming : {'ON' if self.stream else 'OFF'}   {env}",
            f"  💓  Heartbeat : {hb}",
            f"  📋  Logs      : {logger.LOG_FILE}", thin,
            "  Commands: exit | status | stats | history | progress | todo",
            "            logs [N] | reset | clear | stream | heartbeat | help",
            thick, "",
        )))

        while True:
            try:
                task = input("\n[YOU] ").strip()
                if not task:
                    continue
                cmd = task.lower()

                if cmd in ("exit", "quit", "q"):
                    self.counter.persist_stats()
                    self.stop_background_heartbeat()
                    print(f"👋  Goodbye!\n{self.counter}")
                    break
                elif cmd == "status":
                    print(json.dumps(self._agent_status(), indent=2, default=str))
                elif cmd == "stats":
                    self._print_stats()
                elif cmd == "history":
                    for session in self.counter.load_historical():
                        print(json.dumps(session, indent=2, default=str))
                elif cmd == "stream":
                    self.stream = not self.stream
                    print(f"  Streaming {'ON ✓' if self.stream else 'OFF ✗'}")
                elif cmd == "reset":
                    self.history = []
                    print("  ✓ Conversation history cleared")
                elif cmd == "clear":
                    self.mem.clear()
                    print("  ✓ Persistent memory cleared")
                elif cmd == "progress":
                    self._print_progress()
                elif cmd == "todo":
                    self._print_todo()
                elif cmd.startswith("logs"):
                    parts = cmd.split()
                    self._print_logs(int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 20)
                elif cmd == "heartbeat":
                    print(json.dumps(HeartbeatManager.check(self.tools_store), indent=2, default=str))
                elif cmd == "help":
                    self._print_help()
                else:
                    result = self.run(task)
                    if not self.stream:
                        print(f"\n[RESULT]\n{result}\n")
                    print(f"\n\033[90m{self.counter}\033[0m")

            except KeyboardInterrupt:
                print("\n  Interrupted — type 'exit' to quit.")
            except Exception as e:
                self._log(f"Chat error: {e}", "error")

    def reset(self) -> None:
        """Clear conversation history."""
        self.history = []


print("✓ Agent class defined")

# ---------- Tools Schema ----------
def _props(**fields: Any) -> dict:
    """Convert keyword fields into JSON-schema property dicts."""
    return {k: v if isinstance(v, dict) else {"type": v} for k, v in fields.items()}

def _params(required: list[str] | None = None, **fields: Any) -> dict:
    """Build a JSON-schema 'parameters' object."""
    p: dict[str, Any] = {"type": "object", "properties": _props(**fields)}
    if required:
        p["required"] = required
    return p

def _tool(name: str, desc: str, params: dict | None = None) -> dict:
    """Construct a single tool definition dict."""
    return {
        "name": name,
        "description": desc,
        "parameters": params or {"type": "object", "properties": {}},
    }

_S, _I, _B = "string", "integer", "boolean"
_KEY  = {"key": _S}
_PATH = {"path": _S}
_TASK = {"task_id": {"type": _S, "description": "Task ID (from todo_status)"}}

TOOLS: list[dict] = [
    # Memory
    _tool("memory_save",   "Save a value to persistent memory.",
          _params(["key", "value"], key=_S, value={}, tag=_S)),
    _tool("memory_get",    "Retrieve a memory value by key.",
          _params(["key"], **_KEY)),
    _tool("memory_list",   "List all memory keys, optionally filtered by tag.",
          _params(tag=_S)),
    _tool("memory_search", "Search memory by query string.",
          _params(["query"], query=_S)),
    _tool("memory_delete", "Delete a memory key.",
          _params(["key"], **_KEY)),

    # Files (workspace-sandboxed)
    _tool("file_read",
          "Read a file. Use start_line/end_line for large files (1-indexed, inclusive).",
          _params(["path"], path=_S,
                  start_line={"type": _I, "description": "First line (1-indexed)"},
                  end_line={"type":   _I, "description": "Last line (inclusive)"})),
    _tool("file_write",
          "Write or append to a file. backup=true saves .bak before overwriting.",
          _params(["path", "content"], path=_S, content=_S,
                  append={"type": _B, "default": False},
                  backup={"type": _B, "default": False})),
    _tool("file_write_many",
          "Write multiple files at once in parallel (use for 3+ files).",
          _params(["writes"], writes={
              "type": "array",
              "items": {"type": "object", "required": ["path", "content"],
                        "properties": _props(path=_S, content=_S, append=_B)}})),
    _tool("file_list",
          "List files in workspace. metadata=true for size/date/type.",
          _params(directory=_S,
                  metadata={"type": _B, "default": False})),
    _tool("file_delete", "Delete a file.",
          _params(["path"], **_PATH)),
    _tool("file_search", "Search files by name or content.",
          _params(["query"], query=_S, directory=_S)),
    _tool("file_rename", "Rename or move a file within the workspace.",
          _params(["src", "dst"], src=_S, dst=_S)),

    # Shell
    _tool("shell_run",
          f"Run any shell command. Default cwd = {Config.FILES_DIR}. stdout+stderr captured.",
          _params(["command"], command=_S,
                  timeout={"type": _I, "default": Config.SHELL_TIMEOUT},
                  cwd={"type": _S, "default": "",
                       "description": "'' = files dir  '/' = workspace root  'src' = files/src/"})),

    # Code
    _tool("execute_python",
          "Execute Python snippet in sandbox. result variable is returned.",
          _params(["code"], code=_S, timeout={"type": _I, "default": Config.CODE_TIMEOUT})),

    # Diff
    _tool("diff_apply",
          "Apply unified diff or SEARCH/REPLACE blocks to a file.",
          _params(["path", "diff"], path=_S, diff=_S)),

    # Web
    _tool("web_search", "Search the web via DuckDuckGo.",
          _params(["query"], query=_S,
                  max_results={"type": _I, "default": 5, "minimum": 1, "maximum": 10})),
    _tool("web_fetch", "Fetch and extract text from any http/https URL.",
          _params(["url"], url=_S,
                  max_chars={"type": _I, "default": 5000, "minimum": 100, "maximum": 10000})),

    # Sub-tools
    _tool("subtool_create",
          "Create reusable Python sub-tool. Must define run(**kwargs). force=true overrides warnings.",
          _params(["name", "description", "code"],
                  name=_S, description=_S, code=_S,
                  force={"type": _B, "default": False})),
    _tool("subtool_run",  "Run a saved sub-tool by name.",
          _params(["name"], name=_S, kwargs={"type": "object"})),
    _tool("subtool_list", "List all saved sub-tools."),
    _tool("subtool_delete", "Delete a sub-tool.",
          _params(["name"], name=_S)),

    # Skills
    _tool("skill_list",   "List registered skills from core/Tools.md."),
    _tool("skill_create",
          "Create a new skill file and register it in core/Tools.md.",
          _params(["name", "code", "purpose", "trigger"],
                  name=_S, code=_S, purpose=_S, trigger=_S,
                  input_fmt=_S, output_fmt=_S)),
    _tool("skill_run",
          "Run a registered skill by name.",
          _params(["name"], name=_S,
                  input_data={"type": "object", "description": "Input data for the skill"})),
    _tool("skill_validate",
          "Validate one or all registered skills.",
          _params(name=_S)),

    # Todo
    _tool("todo_create",
          "Create todo list for a complex multi-step task.",
          _params(["task", "steps"],
                  task={"type": _S, "description": "Overall task description"},
                  steps={"type": "array", "items": {"type": _S},
                         "description": "List of main steps"})),
    _tool("todo_status", "Get complete todo tree with all tasks, status, and progress."),
    _tool("todo_add_subtask", "Add a subtask to any existing parent task.",
          _params(["parent_id", "description"],
                  parent_id={"type": _S}, description={"type": _S})),
    _tool("todo_complete", "Mark task completed. Auto-advances to next task.",
          _params(["task_id"], **_TASK,
                  result={"type": _S, "description": "Optional result/outcome"})),
    _tool("todo_fail", "Mark task failed. Auto-advances to next task.",
          _params(["task_id"], **_TASK,
                  error={"type": _S, "description": "Error message/reason"})),
    _tool("todo_set_current", "Manually set which task to work on next.",
          _params(["task_id"], **_TASK)),
    _tool("todo_update", "Update task description or add notes.",
          _params(["task_id"], **_TASK,
                  description={"type": _S}, notes={"type": _S})),

    # Needs
    _tool("post_need",
          "Post a structured request to Need.md for the user.",
          _params(["priority", "need", "reason", "blocking"],
                  priority=_S, need=_S, reason=_S, blocking=_S)),
    _tool("check_needs", "Return all PENDING requests from Need.md."),

    # Session & heartbeat
    _tool("save_session_log",
          "Append an entry to today's daily log.",
          _params(["entry"], entry=_S, category=_S)),
    _tool("search_memory",
          "Search through recent daily memory logs.",
          _params(["query"], query=_S,
                  days_back={"type": _I, "default": 7})),
    _tool("check_heartbeat", "Run a full system health check (heartbeat)."),

    # General file ops
    _tool("read_file",
          "Read any file by absolute or relative path (not workspace-sandboxed).",
          _params(["filepath"], filepath=_S, encoding=_S)),
    _tool("write_file",
          "Write to any file by path (creates parent dirs).",
          _params(["filepath", "content"], filepath=_S, content=_S)),
    _tool("append_file",
          "Append content to a file, optionally with timestamp.",
          _params(["filepath", "content"], filepath=_S, content=_S,
                  add_timestamp={"type": _B, "default": True})),
    _tool("delete_file", "Delete a file or directory recursively.",
          _params(["filepath"], filepath=_S)),
    _tool("list_directory",
          "List files/dirs with metadata.",
          _params(["path"], path=_S, pattern=_S)),
    _tool("search_files",
          "Search file contents by query in a directory.",
          _params(["query"], query=_S, directory=_S,
                  file_pattern={"type": _S, "default": "*.md"})),

    # Planning / status
    _tool("task_plan", "Save a step-by-step plan to memory.",
          _params(["task", "steps"], task=_S,
                  steps={"type": "array", "items": {"type": _S}})),
    _tool("agent_status", "Get full agent status: memory, subtools, files, API stats."),
    _tool("api_stats",    "Get detailed API request and token usage statistics."),

    # Parallel generation
    _tool("parallel_generate",
          "Generate multiple files IN PARALLEL. Much faster than sequential for 3+ files.",
          _params(["items"],
                  items={"type": "array",
                         "items": {"type": "object", "required": ["path", "prompt"],
                                   "properties": _props(
                                       path={"type": _S},
                                       prompt={"type": _S},
                                       deps={"type": "array", "items": {"type": _S}})}},
                  system_prompt={"type": _S})),
]

OAI_TOOLS: list[dict] = [{"type": "function", "function": t} for t in TOOLS]

# ---------- System Prompt Builder ----------
def _extract_section(text: str, heading_prefix: str = "##") -> str:
    """
    Extract content after the first Markdown heading that starts with
    `heading_prefix`, stopping before the next heading of any level.
    Falls back to the full text if no matching heading is found.
    """
    lines = text.splitlines()
    capturing = False
    collected: list[str] = []

    for line in lines:
        stripped = line.lstrip()
        if not capturing:
            # Start capturing after the first matching heading
            if stripped.startswith(heading_prefix):
                capturing = True
        else:
            # Stop at the next Markdown heading (any level)
            if stripped.startswith("#"):
                break
            collected.append(line)

    result = "\n".join(collected).strip()
    return result if result else text.strip()


def build_system_prompt_from_context(context: dict[str, str], tools_str: str) -> str:
    """Build a rich system prompt from Code-2 identity files."""
    soul  = _extract_section(context.get("core/SOUL.md",   ""))
    user  = _extract_section(context.get("core/USER.md",   ""))
    mem   = _extract_section(context.get("core/MEMORY.md", ""))
    needs = _extract_section(context.get("core/Need.md",   ""))

    today = datetime.now().strftime("%Y-%m-%d")

    return (
        f"You are an autonomous AI agent powered by {Config.MODEL} via NVIDIA NIM.\n\n"
        f"=== AGENT IDENTITY ===\n{soul}\n\n"
        f"=== USER PROFILE ===\n{user}\n\n"
        f"=== LONG-TERM MEMORY ===\n{mem}\n\n"
        f"=== CURRENT NEEDS ===\n{needs}\n\n"
        f"=== AVAILABLE TOOLS ===\n{tools_str}\n\n"
        f"=== TODAY ===\n{today}\n\n"
        "Begin by checking for pending needs, then respond to the user.\n"
    )

print("✓ Tools schema and system prompt builder ready")



# ---------- API Key Setup ----------

# Error messages are defined as constants so they're easy to update and not
# duplicated across the Colab and local branches of setup_api_key().
_KEY_URL = "https://build.nvidia.com"

_MSG_NO_KEY_COLAB = (
    "\n❌ No NVIDIA API key found.\n"
    "   ➜  Colab Secrets: Left sidebar → 🔑 → Add NVIDIA_API_KEY\n"
    "   ➜  Or paste into NVIDIA_API_KEY = \"\" at top of file.\n"
    f"   ➜  Free key: {_KEY_URL}"
)
_MSG_NO_KEY_LOCAL = (
    "\n❌ No NVIDIA API key found.\n"
    "   ➜  Paste into NVIDIA_API_KEY = \"\" at top, or\n"
    "   ➜  export NVIDIA_API_KEY=nvapi-xxxx...\n"
    f"   ➜  Free key: {_KEY_URL}"
)


def setup_api_key() -> bool:
    """
    Resolve the NVIDIA API key using the following priority order:

    1. Already present in ``os.environ["NVIDIA_API_KEY"]``
    2. Colab Secrets (``google.colab.userdata``) — Colab only
    3. Interactive hidden prompt — local TTY only

    Returns ``True`` if a key was found and set, ``False`` otherwise.
    """
    # 1. Already set in the environment
    if os.environ.get("NVIDIA_API_KEY", "").strip():
        return True

    # 2. Colab Secrets — the primary Colab key source
    if IS_COLAB:
        try:
            from google.colab import userdata
            key = userdata.get("NVIDIA_API_KEY")
            if key and key.strip():
                os.environ["NVIDIA_API_KEY"] = key.strip()
                return True
        except Exception:
            pass  # Secret not set or userdata unavailable — fall through

    # 3. Interactive local prompt (hidden input)
    if not IS_COLAB and sys.stdin.isatty():
        try:
            import getpass
            key = getpass.getpass("\n🔑 Enter your NVIDIA API key (input hidden): ").strip()
            if key:
                os.environ["NVIDIA_API_KEY"] = key
                return True
        except Exception:
            pass  # Fall through to the error message below

    # 4. No key found — print environment-appropriate guidance
    print(_MSG_NO_KEY_COLAB if IS_COLAB else _MSG_NO_KEY_LOCAL)
    return False


# ---------- Entry Points ----------

def start_chat(
    auto_heartbeat:     bool      = True,
    heartbeat_interval: int | None = None,
) -> None:
    """Launch the interactive chat interface."""
    sep = "=" * 65
    print(f"{sep}\n  UNIVERSAL AUTONOMOUS AI AGENT — Interactive Chat\n{sep}")
    agent = Agent(stream=True)
    if auto_heartbeat:
        agent.start_background_heartbeat(interval=heartbeat_interval)
    agent.chat()


def setup_colab() -> bool:
    """
    Colab-specific setup helper: install dependencies, mount Drive,
    and prompt for the API key if absent.
    """
    print("[Colab Setup]")

    # Ensure openai is installed
    try:
        import openai  # noqa: F401
    except ImportError:
        print("Installing openai…")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "openai"])
        print("  - installed")

    # Mount Google Drive (non-fatal if unavailable)
    try:
        from google.colab import drive
        drive.mount("/content/drive")
        print("  - Drive mounted")
    except Exception as e:
        print(f"  - Drive mount skipped ({e})")

    # Prompt for the API key using hidden input
    if not os.getenv("NVIDIA_API_KEY"):
        print("Please set NVIDIA_API_KEY in Colab secrets or enter below.")
        try:
            import getpass
            key = getpass.getpass("Enter NVIDIA_API_KEY (or press Enter to skip): ").strip()
        except Exception:
            key = ""
        if key:
            os.environ["NVIDIA_API_KEY"] = key
            print("  - key set for this session")

    print(
        f"\n[Colab] Workspace root: {Config.BASE_DIR}\n"
        "Start with:\n"
        "  agent = Agent()\n"
        "  agent.start_background_heartbeat()\n"
        "  agent.run('Your task here')\n"
        "  # or: start_chat()"
    )
    return True


def demo() -> None:
    """Run a quick demonstration of the agent across several representative tasks."""
    sep = "=" * 65
    print(f"\n{sep}\n  AUTONOMOUS AGENT — Demonstration\n{sep}\n")

    agent = Agent(stream=True)
    agent.start_background_heartbeat(interval=3600)

    tasks = [
        "What is your current status?",
        "List your available sub-tools and skills.",
        "Check Need.md for any pending requests.",
        "Read MEMORY.md and summarise key points.",
    ]
    for task in tasks:
        print(f"\n[User] {task}")
        response = agent.run(task)
        print(f"\n[Agent] {response}\n")
        time.sleep(1)

    agent.stop_background_heartbeat()
    print("[Demo] Complete.")


# ---------- Initialise ----------

print("✓ Entry points ready (setup_api_key, start_chat, setup_colab, demo)")
setup_api_key()
def main():
    # ── CLI shortcut: python notebook.py demo ────────────────────────
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        demo()
        sys.exit(0)

    # ── Optional Colab setup (uncomment to enable) ───────────────────
    # if IS_COLAB:
    #     setup_colab()

    # ── Startup banner ───────────────────────────────────────────────
    _W   = 70
    thin = "─" * _W
    thick = "═" * _W
    key_status = (
        "\033[92mset ✓\033[0m"
        if os.environ.get("NVIDIA_API_KEY")
        else "\033[91mNOT SET ✗\033[0m"
    )
    C = Config

    print("\n".join(("", thick,
        "  🤖  Universal Autonomous AI Agent — Combined Edition", thin,
        f"  🔑  API key      : {key_status}",
        f"  🧠  Model        : {C.MODEL}",
        f"  📁  Workspace    : {C.BASE_DIR}",
        f"  📂  Files dir    : {C.FILES_DIR}",
        f"  🏛️  Core dir     : {C.CORE_DIR}",
        f"  🔧  Skills dir   : {C.SKILLS_DIR}",
        f"  📋  Log file     : {C.BASE_DIR / 'agent.log'}", thin,
        f"  🔢  Max tokens   : {C.MAX_TOKENS:,}  (output per API call)",
        f"  🔁  Max iters    : {C.MAX_ITERS}",
        f"  📐  Context      : {C.CONTEXT_TOKEN_BUDGET:,} token budget  (window: 262 000)",
        f"  📦  Tool result  : {C.TOOL_RESULT_CHARS_MAX:,} char cap per result",
        f"  ⏱   Timeouts     : code {C.CODE_TIMEOUT}s │ shell {C.SHELL_TIMEOUT}s │ web {C.WEB_TIMEOUT}s",
        f"  🔄  Retry        : {C.API_RETRY_MAX}× backoff  │  cache {C.TOOL_CACHE_SIZE} LRU slots",
        f"  💓  Heartbeat    : every {C.HEARTBEAT_INTERVAL}s",
        f"  🛡️  Py mem limit  : {C.MAX_PYTHON_MEMORY_MB} MB",
        thick,
    )))

    # ── Launch agent ─────────────────────────────────────────────────
    _EXAMPLE_TASKS = (
        "Build a Flask REST API with auth and CRUD endpoints",
        "Debug this Python traceback: [paste error]",
        "Refactor src/app.py to use async/await",
        "Write unit tests for all functions in utils.py",
        "Create a React dashboard with charts for data.json",
        "Analyse and summarise the research paper at [url]",
        "Check for pending needs and act on them",
    )

    try:
        agent = Agent(stream=True)
        agent.start_background_heartbeat()
        hint = "\n".join(f"   • {t}" for t in _EXAMPLE_TASKS)
        print(f"\n✅ Agent ready.  Example tasks to try:\n{hint}\n")
        agent.chat()
    except KeyboardInterrupt:
        print("\n👋  Interrupted.")
    except Exception as exc:
        print(f"\n❌ Init failed: {exc}\n   ➜  Check NVIDIA_API_KEY is set correctly.")
        raise
if __name__ == "__main__":
    main()
