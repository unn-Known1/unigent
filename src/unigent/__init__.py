"""UniGent AI Agent - Autonomous AI agent for local usage."""

# Core agent
from .agent import (
    Agent,
    main,
    Config,
    logger,
    FileManager,
    evolve_now,
    show_core_files,
    check_needs,
    setup_api_key,
)

# Persistence
from .persistence import (
    SessionPersistence,
    session_start,
    persistence,
    save_session,
    restore_session,
    list_backups,
)

# Automation
from .automation import (
    TaskQueue,
    Scheduler,
    NeedPoller,
    Watchdog,
    AutomationHub,
    autorun,
)

# Log streaming
from .log_streamer import LogStreamer, tail_logs, watch_logs

# Telegram
from .telegram_bot import TelegramBot, start_telegram

__all__ = [
    # Agent
    "Agent",
    "main",
    "Config",
    "logger",
    "FileManager",
    "evolve_now",
    "show_core_files",
    "check_needs",
    "setup_api_key",
    # Persistence
    "SessionPersistence",
    "session_start",
    "persistence",
    "save_session",
    "restore_session",
    "list_backups",
    # Automation
    "TaskQueue",
    "Scheduler",
    "NeedPoller",
    "Watchdog",
    "AutomationHub",
    "autorun",
    # Log
    "LogStreamer",
    "tail_logs",
    "watch_logs",
    # Telegram
    "TelegramBot",
    "start_telegram",
]
