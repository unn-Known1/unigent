"""Test suite for v0.20.0 enhancements: Config refactor, APICounter persistence/costing, retry_api jitter."""

import os
import sys
import json
import time
import tempfile
import threading
import random
import re
import warnings
from pathlib import Path
from datetime import datetime

# ---------------------------------------------------------------------------
# Import the agent module robustly
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import unigent.agent as agent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def override_env(monkeypatch, **kwargs):
    for k, v in kwargs.items():
        monkeypatch.setenv(k, v)


# ---------------------------------------------------------------------------
# Tests (rest unchanged)
# ---------------------------------------------------------------------------
def test_config_has_all_attributes():
    """Config class must expose all expected attributes."""
    required = [
        "BASE_DIR", "FILES_DIR", "SUBTOOL_DIR", "SKILLS_DIR", "CORE_DIR",
        "WORK_DIR", "MEMORY_DIR", "MEM_FILE", "STATS_FILE",
        "MODEL", "BASE_URL", "MAX_TOKENS", "MAX_ITERS",
        "CODE_TIMEOUT", "SHELL_TIMEOUT", "WEB_TIMEOUT",
        "MAX_FILE_SIZE_MB", "MAX_WEB_CONTENT", "MAX_PYTHON_MEMORY_MB",
        "SYSTEM_PROMPT_MEM_LIMIT", "SYSTEM_PROMPT_TOOL_LIMIT",
        "MAX_PARALLEL_REQUESTS", "CONTEXT_TOKEN_BUDGET", "TOOL_RESULT_CHARS_MAX",
        "TOOL_CACHE_SIZE", "API_RETRY_MAX", "API_RETRY_BACKOFF", "HEARTBEAT_INTERVAL",
    ]
    for attr in required:
        assert hasattr(agent.Config, attr), f"Missing Config.{attr}"


def test_config_setup_dirs_creates_directories(tmp_path, monkeypatch):
    """Config.setup_dirs() should create all required directories in a custom workspace."""
    override_env(monkeypatch, AGENT_WORKSPACE=str(tmp_path / "custom_workspace"))
    agent.Config.setup_dirs()
    for d in (agent.Config.FILES_DIR, agent.Config.SUBTOOL_DIR, agent.Config.SKILLS_DIR,
              agent.Config.CORE_DIR, agent.Config.WORK_DIR, agent.Config.MEMORY_DIR):
        assert d.exists(), f"Directory {d} was not created"


def test_config_validate_raises_on_invalid():
    """Config.validate() should raise on invalid values."""
    original = agent.Config.MAX_TOKENS
    try:
        agent.Config.MAX_TOKENS = 0
        try:
            agent.Config.validate()
            raise AssertionError("Expected ValueError")
        except ValueError as e:
            assert "MAX_TOKENS must be positive" in str(e)
    finally:
        agent.Config.MAX_TOKENS = original


def test_cost_for_known_models():
    """Config.cost_for() should compute cost for known models."""
    cost = agent.Config.cost_for("stepfun-ai/step-3.5-flash", tokens_in=1000, tokens_out=500)
    expected = (1000 * 0.15 + 500 * 0.60) / 1_000_000
    assert abs(cost - expected) < 1e-9


def test_cost_for_unknown_model_returns_zero():
    assert agent.Config.cost_for("unknown-model", 100, 200) == 0.0


def test_api_counter_basic():
    """APICounter should track requests, tokens, and errors accurately."""
    counter = agent.APICounter()
    counter.record(tokens_in=100, tokens_out=50)
    counter.record(tokens_in=200, tokens_out=80, error=True)
    summary = counter.summary()
    assert summary["total_requests"] == 2
    assert summary["errors"] == 1
    assert summary["tokens_prompt"] == 300
    assert summary["tokens_response"] == 130


def test_api_counter_thread_safety():
    """APICounter should handle concurrent record() calls without data races."""
    counter = agent.APICounter()
    errors = []

    def worker(n):
        try:
            for _ in range(n):
                counter.record(tokens_in=10, tokens_out=5)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(1000,)) for _ in range(5)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert not errors, f"Concurrency errors: {errors}"
    assert counter.total == 5000


def test_api_counter_cost_tracking():
    """APICounter should accumulate cost when configured with cost_calculator."""
    def my_cost(model, in_tok, out_tok):
        return (in_tok + out_tok) / 1_000_000  # Simple: 1 unit per million tokens

    counter = agent.APICounter(cost_calculator=my_cost)
    counter.record(model="test-model", tokens_in=1000, tokens_out=500)
    counter.record(model="test-model", tokens_in=500, tokens_out=250)
    summary = counter.summary()
    assert abs(summary["cost_usd"] - 0.0015) < 1e-9


def test_api_counter_persistence_roundtrip(tmp_path):
    """persist_stats should write JSONL, and load_historical should read it back."""
    stats_file = tmp_path / "stats.jsonl"
    original = agent.Config.STATS_FILE
    agent.Config.STATS_FILE = stats_file

    try:
        counter = agent.APICounter()
        counter.record(tokens_in=123, tokens_out=456)
        counter.persist_stats()

        counter2 = agent.APICounter()
        historical = counter2.load_historical()
        assert len(historical) == 1
        last = historical[-1]
        assert last["tokens_prompt"] == 123
        assert last["tokens_response"] == 456
    finally:
        agent.Config.STATS_FILE = original


def test_retry_api_retries_transient_error():
    """retry_api should retry on a simulated transient call failure."""
    attempts = {"count": 0}

    @agent.retry_api(max_retries=3)
    def flaky():
        attempts["count"] += 1
        if attempts["count"] < 2:
            raise Exception("rate limit exceeded")
        return "ok"

    result = flaky()
    assert result == "ok"
    assert attempts["count"] == 2


def test_retry_api_raises_after_max_attempts():
    """retry_api should give up after max_retries and raise the last exception."""
    attempts = {"count": 0}

    @agent.retry_api(max_retries=2)
    def always_fails():
        attempts["count"] += 1
        raise Exception("service unavailable")

    try:
        always_fails()
        raise AssertionError("Expected exception")
    except Exception as e:
        assert "service unavailable" in str(e)
    assert attempts["count"] == 2


def test_retry_api_honors_retry_after_header(monkeypatch):
    """retry_api should use Retry-After header value instead of backoff if present."""
    attempts = {"count": 0}
    waits = []

    class DummyException(Exception):
        def __init__(self):
            self.response = None

    @agent.retry_api(max_retries=2)
    def with_retry_after():
        attempts["count"] += 1
        if attempts["count"] == 1:
            exc = DummyException()
            exc.response = type("Resp", (), {"headers": {"Retry-After": "2"}})
            raise exc
        return "ok"

    orig_sleep = time.sleep
    def fake_sleep(sec):
        waits.append(sec)
    monkeypatch.setattr(time, "sleep", fake_sleep)

    result = with_retry_after()
    assert result == "ok"
    assert len(waits) == 1 and waits[0] == 2

    monkeypatch.setattr(time, "sleep", orig_sleep)


def test_agent_counter_uses_cost_calculator():
    """Agent should create APICounter with Config.cost_for as cost_calculator."""
    agent_obj = agent.Agent()
    assert agent_obj.counter._cost_calculator is agent.Config.cost_for


def test_end_to_end_metrics():
    """Simulate a few annotated API calls and verify summary metrics."""
    counter = agent.APICounter(cost_calculator=agent.Config.cost_for)

    calls = [
        ("stepfun-ai/step-3.5-flash", 1000, 500),
        ("stepfun-ai/step-3.5-flash", 800, 300),
        ("gpt-4o", 2000, 1000),
    ]

    for model, inp, out in calls:
        counter.record(model=model, tokens_in=inp, tokens_out=out)

    s = counter.summary()
    assert s["total_requests"] == 3
    assert s["tokens_prompt"] == 3800
    assert s["tokens_response"] == 1800

    expected_cost = 0.0
    expected_cost += (1000 * 0.15 + 500 * 0.60) / 1_000_000
    expected_cost += (800 * 0.15 + 300 * 0.60) / 1_000_000
    expected_cost += (2000 * 2.50 + 1000 * 10.00) / 1_000_000
    assert abs(s["cost_usd"] - expected_cost) < 1e-9


def test_heartbeat_internal_invocation(monkeypatch):
    """trigger_heartbeat should call system_heartbeat and log the event."""
    called = {}

    def fake_heartbeat():
        called["ran"] = True

    from unittest.mock import MagicMock
    fake_manager = MagicMock()
    fake_manager.system_heartbeat = fake_heartbeat

    original_agent_manager = agent.Agent.manager if hasattr(agent.Agent, 'manager') else None
    agent.Agent.manager = fake_manager

    try:
        ag = agent.Agent()
        ag.trigger_heartbeat()
        assert called.get("ran") is True
    finally:
        if original_agent_manager is not None:
            agent.Agent.manager = original_agent_manager
        elif hasattr(agent.Agent, 'manager'):
            delattr(agent.Agent, 'manager')
