from __future__ import annotations

import shutil
import threading
import zipfile
from pathlib import Path
from typing import Any
from typing import Any
from datetime import datetime

from .agent import Config, logger, Agent, IS_COLAB, setup_api_key

# ══════════════════════════════════════════════════════════════════════════════
#  CELL 17 — Session Persistence
#
#  Problem: Colab wipes /content/ on every session reset. All core files,
#  memory, skills, daily logs and the agent manifest are lost.
#
#  Solution: Three-layer persistence backed by Google Drive:
#    1. AUTO-SAVE  — background thread saves a ZIP to Drive every N minutes
#    2. MANUAL     — call save_session() at any time
#    3. SHUTDOWN   — save_session() is called automatically on SIGTERM/SIGINT
#
#  On a new session:
#    - Run this cell → it finds the latest backup and restores everything
#
#  Drive layout:
#    MyDrive/
#      agent_workspace/
#        latest.zip          ← always the most recent full backup
#        backups/
#          2024-01-15_14-30.zip
#          2024-01-15_16-00.zip
#          ...               ← rolling window of last N_BACKUPS snapshots
# ══════════════════════════════════════════════════════════════════════════════


class SessionPersistence:
    """
    Manages save/restore of the entire agent workspace to Google Drive.
    Works in Colab (Drive-backed) and degrades gracefully locally.
    """

    # ── Configuration ─────────────────────────────────────────────────
    DRIVE_ROOT     = Path("/content/drive/MyDrive/agent_workspace")
    LATEST_ZIP     = DRIVE_ROOT / "latest.zip"
    BACKUPS_DIR    = DRIVE_ROOT / "backups"
    N_BACKUPS      = 10          # rolling window kept in backups/
    AUTOSAVE_EVERY = 15 * 60     # seconds between auto-saves (15 min)

    # What to include in the ZIP (relative to Config.BASE_DIR)
    INCLUDE_DIRS = ("core", "memory", "skills", "work")
    INCLUDE_ROOT_FILES = ("agent_stats.jsonl",)  # files in workspace root

    def __init__(self) -> None:
        self._autosave_thread: threading.Thread | None = None
        self._stop_autosave:   threading.Event         = threading.Event()
        self._drive_available: bool                    = self._check_drive()

    # ── Drive availability ────────────────────────────────────────────

    @staticmethod
    def _check_drive() -> bool:
        """Return True if Google Drive is mounted and writable."""
        drive = Path("/content/drive/MyDrive")
        return drive.exists() and drive.is_dir()

    @staticmethod
    def mount_drive() -> bool:
        """Mount Google Drive if running in Colab. Returns True on success."""
        if not IS_COLAB:
            print("ℹ  Not in Colab — Drive mount skipped.")
            return False
        if Path("/content/drive/MyDrive").exists():
            print("✓ Drive already mounted.")
            return True
        try:
            from google.colab import drive as _drive
            _drive.mount("/content/drive")
            print("✓ Google Drive mounted.")
            return True
        except Exception as e:
            print(f"✗ Drive mount failed: {e}")
            return False

    # ── Save ──────────────────────────────────────────────────────────

    def save(self, label: str = "manual") -> Path | None:
        """
        Bundle the workspace into a ZIP and write it to Drive.
        Also rotates the backups/ rolling window.
        Returns the path of the written ZIP, or None on failure.
        """
        if not self._drive_available:
            self._drive_available = self._check_drive()
        if not self._drive_available:
            print("✗ Drive not available. Mount Drive first with: persistence.mount_drive()")
            return None

        try:
            self.DRIVE_ROOT.mkdir(parents=True, exist_ok=True)
            self.BACKUPS_DIR.mkdir(parents=True, exist_ok=True)

            # Build zip in a temp file first, then atomically move it
            ts      = datetime.now().strftime("%Y-%m-%d_%H-%M")
            tmp_zip = self.DRIVE_ROOT / f"_tmp_{ts}.zip"
            self._build_zip(tmp_zip)

            # Rotate: copy current latest → backups/ before overwriting
            if self.LATEST_ZIP.exists():
                dest = self.BACKUPS_DIR / f"{ts}.zip"
                shutil.copy2(self.LATEST_ZIP, dest)
                self._prune_backups()

            # Promote tmp → latest
            tmp_zip.replace(self.LATEST_ZIP)

            size_kb = self.LATEST_ZIP.stat().st_size // 1024
            print(f"💾 Saved [{label}] → {self.LATEST_ZIP}  ({size_kb} KB)  {ts}")
            logger.info(f"Session saved to Drive [{label}]", size_kb=str(size_kb))
            return self.LATEST_ZIP

        except Exception as e:
            print(f"✗ Save failed: {e}")
            logger.error(f"Session save failed: {e}")
            return None

    def _build_zip(self, dest: Path) -> None:
        """Write all workspace content into *dest* as a ZIP archive."""
        base = Config.BASE_DIR
        with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            # Include configured subdirectories
            for dir_name in self.INCLUDE_DIRS:
                src_dir = base / dir_name
                if not src_dir.exists():
                    continue
                for file_path in src_dir.rglob("*"):
                    if file_path.is_file():
                        zf.write(file_path, file_path.relative_to(base))

            # Include specific root-level files
            for fname in self.INCLUDE_ROOT_FILES:
                src = base / fname
                if src.exists():
                    zf.write(src, src.relative_to(base))

    def _prune_backups(self) -> None:
        """Keep only the N_BACKUPS most recent files in backups/."""
        zips = sorted(self.BACKUPS_DIR.glob("*.zip"), key=lambda p: p.stat().st_mtime)
        for old in zips[: max(0, len(zips) - self.N_BACKUPS)]:
            old.unlink(missing_ok=True)

    # ── Restore ───────────────────────────────────────────────────────

    def restore(self, from_backup: str | None = None) -> bool:
        """
        Restore the workspace from Drive.

        Args:
            from_backup: filename inside backups/ to restore (e.g. '2024-01-15_14-30.zip').
                         Defaults to latest.zip if not given.

        Returns True on success, False on failure.
        """
        if not self._check_drive():
            print("✗ Drive not mounted. Call persistence.mount_drive() first.")
            return False

        if from_backup:
            src = self.BACKUPS_DIR / from_backup
        else:
            src = self.LATEST_ZIP

        if not src.exists():
            print(f"✗ Backup not found: {src}")
            print("  Nothing to restore — this may be a fresh workspace.")
            return False

        try:
            base = Config.BASE_DIR
            base.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(src, "r") as zf:
                zf.extractall(base)

            size_kb = src.stat().st_size // 1024
            ts      = datetime.fromtimestamp(src.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            print(f"✅ Restored from {src.name}  ({size_kb} KB)  saved at {ts}")
            logger.info("Session restored from Drive", source=str(src))
            return True

        except Exception as e:
            print(f"✗ Restore failed: {e}")
            logger.error(f"Session restore failed: {e}")
            return False

    def list_backups(self) -> list[dict[str, Any]]:
        """Print and return a list of all available backups."""
        if not self._check_drive():
            print("Drive not mounted.")
            return []

        entries: list[dict[str, Any]] = []

        # latest.zip first
        if self.LATEST_ZIP.exists():
            st = self.LATEST_ZIP.stat()
            entries.append({
                "name":    "latest.zip  ← most recent",
                "size_kb": st.st_size // 1024,
                "saved":   datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            })

        # Rolling backups newest first
        for p in sorted(
            self.BACKUPS_DIR.glob("*.zip") if self.BACKUPS_DIR.exists() else [],
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        ):
            st = p.stat()
            entries.append({
                "name":    p.name,
                "size_kb": st.st_size // 1024,
                "saved":   datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            })

        if not entries:
            print("No backups found on Drive yet.")
            return []

        print(f"\n{'NAME':<40} {'SIZE':>8}  SAVED AT")
        print("─" * 65)
        for e in entries:
            print(f"  {e['name']:<38} {e['size_kb']:>6} KB  {e['saved']}")
        print()
        return entries

    # ── Auto-save ─────────────────────────────────────────────────────

    def start_autosave(self, interval: int | None = None) -> None:
        """Start background auto-save thread."""
        if self._autosave_thread and self._autosave_thread.is_alive():
            print("ℹ  Auto-save already running.")
            return
        interval = interval or self.AUTOSAVE_EVERY
        self._stop_autosave.clear()
        self._autosave_thread = threading.Thread(
            target=self._autosave_loop,
            args=(interval,),
            daemon=True,
            name="autosave",
        )
        self._autosave_thread.start()
        mins = interval // 60
        print(f"⏱  Auto-save started — saving every {mins} min to Drive.")
        logger.info(f"Auto-save started (interval={interval}s)")

    def stop_autosave(self) -> None:
        """Stop the background auto-save thread."""
        self._stop_autosave.set()
        if self._autosave_thread:
            self._autosave_thread.join(timeout=5)
        print("⏹  Auto-save stopped.")

    def _autosave_loop(self, interval: int) -> None:
        while not self._stop_autosave.wait(timeout=interval):
            self.save(label="auto")

    # ── Shutdown hook ─────────────────────────────────────────────────

    def register_shutdown_hook(self) -> None:
        """
        Register a save on SIGTERM/SIGINT so the workspace is preserved even
        if the Colab runtime is interrupted rather than gracefully closed.
        Adds to (not replaces) Agent's existing signal handlers.
        """
        import signal as _signal

        def _hook(sig, frame):
            print("\n💾 Saving workspace before shutdown…")
            self.save(label="shutdown")

        for sig in (_signal.SIGTERM, _signal.SIGINT):
            try:
                prev = _signal.getsignal(sig)
                def _chained(s, f, _prev=prev, _hook=_hook):
                    _hook(s, f)
                    if callable(_prev):
                        _prev(s, f)
                _signal.signal(sig, _chained)
            except (OSError, ValueError):
                pass

        print("✓ Shutdown hook registered — workspace saves on interrupt/termination.")


# ══════════════════════════════════════════════════════════════════════════════
#  Session start — auto-restore on every new Colab session
# ══════════════════════════════════════════════════════════════════════════════

def session_start(
    autosave:          bool      = True,
    autosave_interval: int | None = None,
    start_agent:       bool      = False,
) -> tuple["SessionPersistence", "Agent | None"]:
    """
    One-call setup for a new Colab session:
      1. Mounts Drive
      2. Restores previous workspace from latest.zip
      3. Starts auto-save
      4. Registers shutdown hook
      5. Optionally initialises and returns a ready Agent

    Usage (first cell after imports):
        persistence, agent = session_start(start_agent=True)

    Or if you want to initialise the agent yourself:
        persistence, _ = session_start()
        agent = Agent()
    """
    print("=" * 60)
    print("  SESSION START")
    print("=" * 60)

    p = SessionPersistence()

    # 1. Mount Drive
    mounted = p.mount_drive()

    # 2. Restore workspace
    if mounted:
        p.restore()
    else:
        print("ℹ  Skipping restore — Drive not mounted.")

    # 3. Auto-save
    if autosave and mounted:
        p.start_autosave(interval=autosave_interval)

    # 4. Shutdown hook
    p.register_shutdown_hook()

    # 5. Optional agent init
    agent = None
    if start_agent:
        try:
            setup_api_key()
            agent = Agent(stream=True)
            agent.start_background_heartbeat()
            print(f"✅ Agent ready · {Config.MODEL}")
        except Exception as e:
            import traceback
            print(f"✗ Agent init failed: {e}")
            traceback.print_exc()

    print("=" * 60)
    return p, agent


# ══════════════════════════════════════════════════════════════════════════════
#  Module-level instance + convenience shortcuts
# ══════════════════════════════════════════════════════════════════════════════

persistence = SessionPersistence()

def save_session()      -> None: persistence.save(label="manual")
def restore_session()   -> None: persistence.restore()
def list_backups()      -> None: persistence.list_backups()


# ── Auto-run on cell execution ────────────────────────────────────────────────
print("✓ SessionPersistence ready")
print()
print("  Quick-start for a NEW session:")
print("    persistence, agent = session_start(start_agent=True)")
print()
print("  Or step by step:")
print("    persistence.mount_drive()")
print("    persistence.restore()         # load previous workspace")
print("    persistence.start_autosave()  # save every 15 min")
print("    save_session()                # save right now")
print("    list_backups()                # see all available backups")
print("    persistence.restore('2024-01-15_14-30.zip')  # restore specific backup")