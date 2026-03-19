from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from .agent import Agent, check_needs, logger, setup_api_key
from .persistence import SessionPersistence

# ══════════════════════════════════════════════════════════════════════════════
#  CELL 18 — Full Automation
#
#  What this adds on top of everything already running:
#
#  ┌─────────────────────────────────────────────────────────────────────┐
#  │  ALREADY AUTOMATED (cells 11/16/17)                                 │
#  │    ✓ Heartbeat health checks (every 30 min)                         │
#  │    ✓ Core file evolution after every turn                           │
#  │    ✓ Auto-save to Drive (every 15 min)                              │
#  │    ✓ Shutdown hook saves on crash                                   │
#  ├─────────────────────────────────────────────────────────────────────┤
#  │  NEWLY AUTOMATED (this cell)                                        │
#  │    ✓ Task queue  — submit tasks; agent works through them headlessly│
#  │    ✓ Scheduler   — run tasks at fixed times or intervals            │
#  │    ✓ Need poller — auto-resolves PENDING needs without user input   │
#  │    ✓ Watchdog    — restarts agent if it crashes                     │
#  │    ✓ One-call    — autorun() wires everything up in a single call   │
#  └─────────────────────────────────────────────────────────────────────┘
# ══════════════════════════════════════════════════════════════════════════════


# ─────────────────────────────────────────────────────────────────────────────
#  1. TaskQueue — submit tasks anytime; worker thread drains them
# ─────────────────────────────────────────────────────────────────────────────

class TaskQueue:
    """
    Thread-safe FIFO task queue.  Submit tasks from any cell; the worker
    thread processes them one at a time without blocking the notebook.

    Usage:
        tq.submit("Write a readme for /content/agent/files/project/")
        tq.submit("Analyse the logs and summarise errors")
        tq.status()   # see what's pending / done
    """

    def __init__(self, agent: "Agent") -> None:
        self._agent       = agent
        self._q:    queue.Queue        = queue.Queue()
        self._done: list[dict[str, Any]] = []
        self._lock  = threading.Lock()
        self._stop  = threading.Event()
        self._thread: threading.Thread | None = None
        self._current: str | None = None

    # ── Public API ────────────────────────────────────────────────────

    def submit(self, task: str, priority: bool = False) -> None:
        """
        Add *task* to the queue.
        Set ``priority=True`` to jump ahead of existing items.
        """
        item = {"task": task, "submitted": datetime.now().isoformat(), "status": "pending"}
        if priority:
            # Rebuild queue with this item first
            tmp: list[dict] = [item]
            while not self._q.empty():
                try:
                    tmp.append(self._q.get_nowait())
                except queue.Empty:
                    break
            for t in tmp:
                self._q.put(t)
        else:
            self._q.put(item)
        print(f"📥 Queued [{self._q.qsize()} pending]: {task[:80]}")

    def submit_many(self, tasks: list[str]) -> None:
        """Submit multiple tasks at once."""
        for t in tasks:
            self.submit(t)

    def status(self) -> None:
        """Print queue state: pending, current, and last 5 completed."""
        pending = list(self._q.queue)
        print(f"\n{'─'*55}")
        print(f"  TASK QUEUE  —  pending: {len(pending)}  done: {len(self._done)}")
        print(f"{'─'*55}")
        if self._current:
            print(f"  ▶ RUNNING : {self._current[:70]}")
        for i, item in enumerate(pending):
            print(f"  {i+1:>2}. {item['task'][:70]}")
        if self._done:
            print(f"\n  Last {min(5, len(self._done))} completed:")
            for item in self._done[-5:]:
                ts  = item.get("finished", "?")[:16]
                ok  = "✓" if not item.get("error") else "✗"
                print(f"    {ok} [{ts}]  {item['task'][:60]}")
        print()

    def clear(self) -> None:
        """Discard all pending (not running) tasks."""
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        print("🗑  Queue cleared.")

    # ── Worker ────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            print("ℹ  Task queue already running.")
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._worker, daemon=True, name="task-queue"
        )
        self._thread.start()
        print("▶  Task queue worker started.")

    def stop(self) -> None:
        self._stop.set()
        print("⏹  Task queue worker stopping (finishes current task first).")

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=2)
            except queue.Empty:
                continue
            self._current = item["task"]
            item["started"]  = datetime.now().isoformat()
            item["status"]   = "running"
            try:
                print(f"\n🤖 [QUEUE] Starting: {item['task'][:80]}")
                result          = self._agent.run(item["task"])
                item["result"]  = result[:500]
                item["status"]  = "done"
                print(f"✓  [QUEUE] Done:     {item['task'][:60]}")
            except Exception as e:
                item["error"]  = str(e)
                item["status"] = "error"
                print(f"✗  [QUEUE] Error:    {item['task'][:60]}  →  {e}")
            finally:
                item["finished"] = datetime.now().isoformat()
                with self._lock:
                    self._done.append(item)
                    if len(self._done) > 100:       # cap history
                        self._done = self._done[-100:]
                self._current = None
                self._q.task_done()


# ─────────────────────────────────────────────────────────────────────────────
#  2. Scheduler — run tasks at fixed times or intervals
# ─────────────────────────────────────────────────────────────────────────────

class Scheduler:
    """
    Lightweight cron-style scheduler.  All scheduled tasks are fed into
    the TaskQueue so they're sequenced correctly.

    Usage:
        scheduler.every(30, "Summarise today's log and update MEMORY.md")
        scheduler.every(60, "Check for new files in /content/agent/files/inbox/")
        scheduler.at("09:00", "Generate daily status report")
        scheduler.cancel("Summarise today's log...")
    """

    def __init__(self, task_queue: TaskQueue) -> None:
        self._tq      = task_queue
        self._jobs:   list[dict[str, Any]] = []
        self._stop    = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock    = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────

    def every(self, minutes: int, task: str) -> str:
        """Run *task* every *minutes* minutes. Returns job id."""
        job_id = f"every_{minutes}m_{task[:20]}"
        job    = {
            "id":           job_id,
            "type":         "interval",
            "minutes":      minutes,
            "task":         task,
            "next_run":     datetime.now() + timedelta(minutes=minutes),
            "run_count":    0,
        }
        with self._lock:
            # Replace if same id already exists
            self._jobs = [j for j in self._jobs if j["id"] != job_id]
            self._jobs.append(job)
        print(f"⏱  Scheduled every {minutes} min: {task[:60]}")
        return job_id

    def at(self, time_str: str, task: str, daily: bool = True) -> str:
        """
        Run *task* at a specific time (HH:MM, 24-hour).
        Set ``daily=True`` (default) to repeat every day.
        """
        job_id  = f"at_{time_str}_{task[:20]}"
        h, m    = map(int, time_str.split(":"))
        now     = datetime.now()
        next_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if next_dt <= now:
            next_dt += timedelta(days=1)
        job = {
            "id":        job_id,
            "type":      "daily" if daily else "once",
            "time_str":  time_str,
            "task":      task,
            "next_run":  next_dt,
            "run_count": 0,
        }
        with self._lock:
            self._jobs = [j for j in self._jobs if j["id"] != job_id]
            self._jobs.append(job)
        print(f"🕐  Scheduled at {time_str}: {task[:60]}")
        return job_id

    def cancel(self, job_id_or_task: str) -> None:
        """Cancel a job by its id or task substring."""
        with self._lock:
            before = len(self._jobs)
            self._jobs = [
                j for j in self._jobs
                if job_id_or_task not in j["id"] and job_id_or_task not in j["task"]
            ]
            removed = before - len(self._jobs)
        print(f"❌  Cancelled {removed} job(s) matching '{job_id_or_task[:40]}'")

    def status(self) -> None:
        """Print all scheduled jobs and their next run time."""
        with self._lock:
            jobs = list(self._jobs)
        if not jobs:
            print("  No scheduled jobs.")
            return
        print(f"\n{'─'*60}")
        print(f"  SCHEDULER  —  {len(jobs)} job(s)")
        print(f"{'─'*60}")
        for j in sorted(jobs, key=lambda x: x["next_run"]):
            delta = j["next_run"] - datetime.now()
            mins  = int(delta.total_seconds() / 60)
            print(f"  [{j['type']:<8}] in {mins:>4} min  —  {j['task'][:55]}")
        print()

    # ── Runner ────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            print("ℹ  Scheduler already running.")
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="scheduler"
        )
        self._thread.start()
        print("▶  Scheduler started.")

    def stop(self) -> None:
        self._stop.set()
        print("⏹  Scheduler stopped.")

    def _loop(self) -> None:
        while not self._stop.wait(timeout=30):    # check every 30 seconds
            now = datetime.now()
            with self._lock:
                due = [j for j in self._jobs if j["next_run"] <= now]
            for job in due:
                self._tq.submit(job["task"])
                job["run_count"] += 1
                if job["type"] == "interval":
                    job["next_run"] = now + timedelta(minutes=job["minutes"])
                elif job["type"] == "daily":
                    job["next_run"] = now + timedelta(days=1)
                else:
                    with self._lock:           # "once" — remove after firing
                        self._jobs = [j for j in self._jobs if j["id"] != job["id"]]
                logger.info(f"Scheduler fired: {job['task'][:60]}")


# ─────────────────────────────────────────────────────────────────────────────
#  3. NeedPoller — auto-resolve PENDING needs without user input
# ─────────────────────────────────────────────────────────────────────────────

class NeedPoller:
    """
    Polls Need.md every N minutes and feeds any PENDING request back to
    the agent as a task so it can try to resolve it autonomously.

    The agent will attempt to fulfil the need using its tools. If it
    cannot (e.g. needs a real human decision), it will re-post the need
    and it will appear again next poll cycle.

    Usage:
        need_poller.start()
        need_poller.stop()
    """

    def __init__(self, task_queue: TaskQueue, interval_minutes: int = 10) -> None:
        self._tq       = task_queue
        self._interval = interval_minutes * 60
        self._stop     = threading.Event()
        self._thread:  threading.Thread | None = None
        self._seen:    set[str] = set()         # deduplicate already-queued needs

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            print("ℹ  Need poller already running.")
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="need-poller"
        )
        self._thread.start()
        print(f"👀  Need poller started (every {self._interval // 60} min).")

    def stop(self) -> None:
        self._stop.set()
        print("⏹  Need poller stopped.")

    def _loop(self) -> None:
        while not self._stop.wait(timeout=self._interval):
            try:
                needs = check_needs()
                for need in needs:
                    key = f"{need.get('priority','')}{need.get('needs','')}"
                    if key in self._seen:
                        continue
                    self._seen.add(key)
                    task = (
                        f"A pending need was found in Need.md. "
                        f"Priority: {need.get('priority','?')}. "
                        f"Need: {need.get('needs','?')}. "
                        f"Reason: {need.get('reason','?')}. "
                        f"Blocking: {need.get('blocking','?')}. "
                        f"Please attempt to resolve this autonomously using your tools. "
                        f"If you cannot resolve it without human input, explain clearly "
                        f"what is needed and why."
                    )
                    self._tq.submit(task, priority=True)
                    print(f"🔔  Need auto-queued: {need.get('needs','?')[:60]}")
            except Exception as e:
                logger.warning(f"NeedPoller error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  4. Watchdog — restarts the agent if it crashes
# ─────────────────────────────────────────────────────────────────────────────

class Watchdog:
    """
    Monitors the agent and its supporting threads. If any critical thread
    dies unexpectedly it logs the failure and attempts to restart.

    Threads monitored:
        - task_queue worker
        - scheduler
        - need_poller
        - heartbeat (agent._heartbeat_thread)
    """

    CHECK_INTERVAL = 60   # seconds

    def __init__(
        self,
        agent:        "Agent",
        task_queue:   TaskQueue,
        scheduler:    Scheduler,
        need_poller:  NeedPoller,
    ) -> None:
        self._agent       = agent
        self._tq          = task_queue
        self._sched       = scheduler
        self._poller      = need_poller
        self._stop        = threading.Event()
        self._thread:     threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            print("ℹ  Watchdog already running.")
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="watchdog"
        )
        self._thread.start()
        print(f"🐕  Watchdog started (checks every {self.CHECK_INTERVAL}s).")

    def stop(self) -> None:
        self._stop.set()
        print("⏹  Watchdog stopped.")

    def _loop(self) -> None:
        while not self._stop.wait(timeout=self.CHECK_INTERVAL):
            self._check_and_restart("task_queue",  self._tq._thread,  self._tq.start)
            self._check_and_restart("scheduler",   self._sched._thread, self._sched.start)
            self._check_and_restart("need_poller", self._poller._thread, self._poller.start)
            # Heartbeat lives on the agent
            hb = getattr(self._agent, "_heartbeat_thread", None)
            if hb and not hb.is_alive() and not self._stop.is_set():
                logger.warning("Watchdog: heartbeat died — restarting")
                print("🐕  Watchdog: restarting heartbeat…")
                self._agent.start_background_heartbeat()

    @staticmethod
    def _check_and_restart(
        name:    str,
        thread:  threading.Thread | None,
        restart: Callable[[], None],
    ) -> None:
        if thread is not None and not thread.is_alive():
            logger.warning(f"Watchdog: {name} thread died — restarting")
            print(f"🐕  Watchdog: restarting {name}…")
            try:
                restart()
            except Exception as e:
                logger.error(f"Watchdog: failed to restart {name}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  5. AutomationHub — single object holding every automation component
# ─────────────────────────────────────────────────────────────────────────────

class AutomationHub:
    """
    Holds and exposes every automation component.
    Returned by ``autorun()`` so you have one object to interact with.

    Attributes:
        agent        — the Agent instance
        queue        — TaskQueue
        scheduler    — Scheduler
        need_poller  — NeedPoller
        watchdog     — Watchdog
        persistence  — SessionPersistence

    Quick-reference:
        hub.queue.submit("do something")
        hub.scheduler.every(60, "summarise today's work")
        hub.scheduler.at("18:00", "generate end-of-day report")
        hub.status()
        hub.stop_all()
    """

    def __init__(
        self,
        agent:       "Agent",
        persistence: "SessionPersistence",
    ) -> None:
        self.agent       = agent
        self.persistence = persistence
        self.queue       = TaskQueue(agent)
        self.scheduler   = Scheduler(self.queue)
        self.need_poller = NeedPoller(self.queue)
        self.watchdog    = Watchdog(agent, self.queue, self.scheduler, self.need_poller)

    def start_all(self) -> None:
        self.queue.start()
        self.scheduler.start()
        self.need_poller.start()
        self.watchdog.start()
        print("\n✅ All automation components running.")

    def stop_all(self) -> None:
        self.watchdog.stop()
        self.need_poller.stop()
        self.scheduler.stop()
        self.queue.stop()
        self.persistence.stop_autosave()
        self.agent.stop_background_heartbeat()
        print("⏹  All automation stopped.")

    def status(self) -> None:
        """Print a full status dashboard."""
        print(f"\n{'═'*60}")
        print(f"  AUTOMATION HUB STATUS  —  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'═'*60}")

        def _t(thread):
            return "\033[92m● running\033[0m" if thread and thread.is_alive() \
                   else "\033[91m● stopped\033[0m"

        print(f"  heartbeat   {_t(getattr(self.agent, '_heartbeat_thread', None))}")
        print(f"  task_queue  {_t(self.queue._thread)}")
        print(f"  scheduler   {_t(self.scheduler._thread)}")
        print(f"  need_poller {_t(self.need_poller._thread)}")
        print(f"  watchdog    {_t(self.watchdog._thread)}")
        print(f"  autosave    {_t(self.persistence._autosave_thread)}")
        print()
        self.queue.status()
        self.scheduler.status()

    def submit(self, task: str) -> None:
        """Shorthand for hub.queue.submit(task)."""
        self.queue.submit(task)

    def every(self, minutes: int, task: str) -> str:
        """Shorthand for hub.scheduler.every(minutes, task)."""
        return self.scheduler.every(minutes, task)

    def at(self, time_str: str, task: str) -> str:
        """Shorthand for hub.scheduler.at(time_str, task)."""
        return self.scheduler.at(time_str, task)


# ─────────────────────────────────────────────────────────────────────────────
#  6. autorun() — one call to rule them all
# ─────────────────────────────────────────────────────────────────────────────

def autorun(
    tasks:             list[str] | None = None,
    schedule:          list[tuple]      | None = None,
    autosave_minutes:  int               = 15,
    need_poll_minutes: int               = 10,
    startup_task:      str               = "Check heartbeat, review Need.md, summarise MEMORY.md",
) -> AutomationHub:
    """
    One-call full automation setup. Does everything:

      1. Mounts Drive + restores previous workspace
      2. Sets up API key
      3. Initialises Agent with heartbeat + core evolution
      4. Starts auto-save to Drive
      5. Starts task queue worker
      6. Starts scheduler
      7. Starts need poller
      8. Starts watchdog
      9. Runs startup_task immediately
      10. Submits any tasks you pass in
      11. Registers shutdown hook

    Args:
        tasks:             list of task strings to queue immediately
        schedule:          list of (minutes_or_timestr, task) tuples,
                           e.g. [(30, "summarise logs"), ("09:00", "daily report")]
        autosave_minutes:  how often to save to Drive (default 15)
        need_poll_minutes: how often to poll Need.md (default 10)
        startup_task:      first task run after init

    Returns:
        AutomationHub — interact with hub.submit(), hub.every(), hub.status() etc.

    Example:
        hub = autorun(
            tasks=["Analyse files in /content/agent/files/"],
            schedule=[(60, "Summarise today's logs"), ("18:00", "Daily report")],
        )
    """
    print("═" * 60)
    print("  🤖  FULL AUTOMATION STARTING")
    print("═" * 60)

    # 1. Drive + restore
    p = SessionPersistence()
    if p.mount_drive():
        p.restore()

    # 2. API key
    if not setup_api_key():
        raise RuntimeError(
            "No NVIDIA API key found.\n"
            "  Colab: Left sidebar → 🔑 → add NVIDIA_API_KEY\n"
            "  Local: export NVIDIA_API_KEY=nvapi-xxxx"
        )

    # 3. Agent
    print("\n  Initialising agent…")
    agent = Agent(stream=False)   # stream=False for clean headless output
    agent.start_background_heartbeat()

    # 4. Auto-save
    p.start_autosave(interval=autosave_minutes * 60)
    p.register_shutdown_hook()

    # 5–8. Build hub and start all components
    hub = AutomationHub(agent=agent, persistence=p)
    hub.need_poller._interval = need_poll_minutes * 60
    hub.start_all()

    # 9. Startup task
    hub.queue.submit(startup_task, priority=True)

    # 10. User-provided tasks
    if tasks:
        for t in tasks:
            hub.queue.submit(t)

    # 11. Scheduled jobs
    if schedule:
        for entry in schedule:
            time_or_mins, task = entry
            if isinstance(time_or_mins, int):
                hub.scheduler.every(time_or_mins, task)
            else:
                hub.scheduler.at(str(time_or_mins), task)

    print("\n" + "═" * 60)
    print("  ✅  FULL AUTOMATION RUNNING")
    print("     hub.status()            — live dashboard")
    print("     hub.submit('task...')   — add a task now")
    print("     hub.every(N, 'task...')  — recurring task")
    print("     hub.at('HH:MM', 'task') — daily task")
    print("     hub.stop_all()          — graceful shutdown")
    print("═" * 60 + "\n")

    return hub


print("✓ Automation cell ready")
print()
print("  Minimal usage:")
print("    hub = autorun()")
print()
print("  Full usage:")
print("    hub = autorun(")
print("        tasks=['Analyse logs', 'Update readme'],")
print("        schedule=[(60, 'Summarise work'), ('18:00', 'Daily report')],")
print("    )")