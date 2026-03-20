# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] - 2026-03-20

    ### Added
    - **Cost Tracking**: APICounter now tracks monetary cost using per-model pricing (MODEL_COSTS) and Config.cost_for().
    - **APICounter Persistence**: New methods persist_stats() and load_historical() to save/load session stats to a JSONL file.
    - **Enhanced Retry Logic**: New _parse_retry_after() function and improved retry_api decorator with jitter and better Retry-After header handling.
    - **Per-Tool Timeouts**: Config.TOOL_TIMEOUTS allows overriding timeouts for individual tools via environment variables.
    - **Config Improvements**: HEARTBEAT_INTERVAL now clamped between 60 and 21600 seconds; added tool_timeout() method.
    - **Testing**: Added comprehensive tests for cost calculation, retry behavior, and APICounter persistence.

    ### Changed
    - Agent initializes APICounter with Config.cost_for as the cost calculator.
    - APICounter summary includes cost_usd and displays in __str__.
    - Improved error handling in APICounter persistence with warnings.

    ### Fixed
    - Fixed Config class indentation issues.
    - Fixed duplicate import warnings.
    - Fixed staticmethod patch errors in HeartbeatManager and Watchdog.
## [0.3.1] - 2026-03-19

### Fixed
- Duplicate import statements in `persistence.py`, `automation.py`, `telegram_bot.py`.
- Incorrect `.__func__` access on staticmethod in HeartbeatManager patch (agent.py) and Watchdog patch (telegram_bot.py).
- Minor typo corrections and import ordering.

### Changed
- Updated README badges to reflect new version.

## [0.3.0] - 2026-03-19

### Added
- **Session Persistence** — Automatic workspace backup to Google Drive with restore capabilities, ideal for Colab environments where `/content/` is ephemeral. Includes auto-save every 15 minutes, manual saves, and a rolling window of backups.
- **Automation Hub** — Comprehensive headless automation system:
  - `TaskQueue`: submit tasks programmatically, view status (pending/running/done), clear queue.
  - `Scheduler`: run tasks at fixed intervals or at specific times (cron-like with daily or one-off).
  - `NeedPoller`: automatically detects pending needs in `Need.md` and feeds them to the agent for autonomous resolution.
  - `Watchdog`: monitors critical threads (task queue, scheduler, need poller, heartbeat) and restarts them if they fail.
  - `autorun()`: one-call setup that wires Drive mounting, API key setup, agent initialization, heartbeat, auto-save, task queue, scheduler, need poller, and watchdog. Also runs an optional startup task.
- **Live Log Streaming** — Real-time log viewing without leaving your workflow:
  - `LogStreamer` widget (Jupyter notebooks) with auto-refresh, level & text filters, and customizable line count.
  - `tail_logs(n, level, text)` for quick snapshots.
  - `watch_logs()` for blocking live tail like `tail -f`.
- **Telegram Bot Integration** — Full remote control of the agent via Telegram:
  - Submit tasks, receive results.
  - Stream logs or request snapshots, set level/text filters.
  - Download any workspace file.
  - View full status dashboard.
  - Manual Drive save trigger.
  - Interactive control panel with inline buttons.
  - Secure by chat-ID whitelist; all communications authenticated.
- Additional configuration options for automation intervals (auto-save, need polling, heartbeat).

### Changed
- Documentation extensively updated to cover new features and usage patterns.
- Dependencies: `requests` already included; `ipywidgets` is optional (only needed for `LogStreamer` widget).

### Fixed
- HeartbeatManager patch typo (`check.__func__` → `check`) for staticmethod compatibility.
- Minor import adjustments and typo corrections.

## [0.2.0] - 2026-03-18

### Added
- **Core Evolution System** — Automatic updates of core identity files:
  - SOUL.md: learned behaviours and patterns
  - USER.md: inferred user preferences
  - MEMORY.md: long-term facts with consolidation
  - HEARTBEAT.md: health check logs
- Cross-platform support: Linux, macOS, Windows
- Conditional resource limits (skipped on Windows)
- `import shutil` fix for `shutil.rmtree` usage
- `IS_WINDOWS` platform detection
- `evolve_now(agent)` helper for manual evolution
- `show_core_files()` helper to inspect core files
- GitHub Actions CI: fixed artifact path to use `dist/`

### Changed
- Updated CI workflow to upload artifacts from `dist/`
- README: added Supported Platforms section
- Agent: `preexec_fn` now only used on non-Windows platforms

### Fixed
- Syntax: moved `resource` import into try/except block

## [0.1.0] - 2026-03-18

### Added
- First public release
- Full agent functionality from UniGent.ipynb
- Debian packaging (unigent_0.1.0-1_all.deb)
- pyproject.toml with setuptools backend
- Documentation and README with SEO optimization
- GitHub repository with issue templates