<!--
UniGent AI Agent
SEO: autonomous AI agent, NVIDIA NIM, streaming responses, persistent memory, file operations, shell commands, web search, parallel execution, Debian package, Ubuntu, Python, openai, LLM agent, developer tools, AI assistant, local AI, chat AI, code assistant, task automation, core evolution, auto-updating identity, agent learning, cross-platform, arm64, amd64
-->
<div align="center">

# 🤖 UniGent AI Agent

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Debian Package](https://img.shields.io/badge/Debian-0.2.0-blue?logo=debian)](https://github.com/unn-Known1/unigent/releases)
[![GitHub issues](https://img.shields.io/github/issues/unn-Known1/unigent.svg)](https://github.com/unn-Known1/unigent/issues)
[![GitHub last commit](https://img.shields.io/github/last-commit/unn-Known1/unigent.svg)](https://github.com/unn-Known1/unigent/commits)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

**A universal autonomous AI agent powered by NVIDIA NIM API with streaming responses, persistent memory, file operations, shell commands, web search, parallel tool execution, robust logging, and a self-improving Core Evolution System.** Designed for developers who need a powerful local AI assistant that integrates seamlessly into their workflow.

---

</div>

## 🚀 Quick Start

### Installation

#### Option 1: Debian/Ubuntu Package (Recommended)

Download and install the `.deb` package from the [latest release](https://github.com/unn-Known1/unigent/releases).

```bash
# Install dependencies
sudo apt-get update
sudo apt-get install -y python3-requests python3-dotenv

# Install the deb package
sudo dpkg -i unigent_0.2.0_all.deb
```

#### Option 2: Python Package

```bash
# Clone and install
git clone https://github.com/unn-Known1/unigent.git
cd unigent
pip3 install -e .   # or: pip3 install .
```

> **Note:** The Python package requires the `openai` library (`pip3 install openai`).

### Usage

```bash
# Start interactive chat mode
unigent

# Run a demo to see capabilities
unigent demo
```

Set your NVIDIA API key before running:

```bash
export NVIDIA_API_KEY="your_nvidia_api_key_here"
```

You can obtain a free API key from [NVIDIA Build](https://build.nvidia.com).

## 🖥️ Supported Platforms

UniGent runs on:

- **Linux** (Ubuntu, Debian, Fedora, etc.) – amd64 & arm64
- **macOS** (10.15+)
- **Windows** (10+ with Python 3.10+)

> Note: On Windows, resource limits are not applied; all other features work identically.

## ✨ Features

- **🔊 Streaming Responses** – Real-time token streaming with live, in-place status bar showing tokens/second and accumulated counts.
- **🧬 Core Evolution System** – SOUL.md, USER.md, MEMORY.md, and HEARTBEAT.md evolve automatically as you use the agent, capturing learned behaviours, user preferences, long-term facts, and health logs.
- **📊 Live Status Bar** – In-terminal progress display with request number, thinking/response token counts, and throughput.
- **📈 Structured Logging** – JSON‑line log files with rotation, daily logs, and colour‑coded console output.
- **💾 Persistent Memory** – JSON‑line based key‑value store with tagging and fuzzy search.
- **📁 File Operations** – Read, write, delete, search files within a sandboxed workspace.
- **⚡ Shell Commands** – Safe execution of shell commands with blocklist and timeout.
- **🌐 Web Tools** – Built‑in web search (`web_search`) and fetch (`web_fetch`).
- **🔧 Sub‑tool System** – Create and run custom Python tools dynamically; cached results with TTL.
- **⚙️ Skills System** – Extensible skill architecture with validation and auto‑reload.
- **🛡️ Security Hardening** – Secret management, code sandboxing, and secure defaults.
- **🔄 Parallel API Calls** – Execute multiple tasks concurrently for speed.
- **💓 Heartbeat Manager** – Periodic health checks, resource monitoring, and log rotation.
- **🔀 Retry & Rate Limiting** – Exponential back‑off and sliding‑window rate limiter for robust API usage.
- **🤝 Todo & Needs** – Built‑in todo list management and user need tracking.

## ⚙️ Configuration

The agent reads configuration from environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `NVIDIA_API_KEY` | *(required)* | Your NVIDIA API key |
| `NVIDIA_MODEL` | `stepfun-ai/step-3.5-flash` | Model name to use |
| `NVIDIA_BASE_URL` | `https://integrate.api.nvidia.com/v1` | API base URL |
| `AGENT_WORKSPACE` | `~/agent_workspace` | Workspace root directory |
| `AGENT_MAX_TOKENS` | `131072` | Max tokens per response |
| `AGENT_MAX_ITERS` | `1000` | Max conversation iterations |
| `AGENT_CODE_TIMEOUT` | `60` | Python code execution timeout (seconds) |
| `AGENT_SHELL_TIMEOUT` | `120` | Shell command timeout (seconds) |
| `AGENT_WEB_TIMEOUT` | `20` | Web request timeout (seconds) |
| `AGENT_LOG_LEVEL` | `DEBUG` | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `AGENT_CTX_BUDGET` | `245000` | Token budget for context window |
| `AGENT_HEARTBEAT_INTERVAL` | `1800` | Health check interval (seconds) |

## 📁 Core Files

The agent maintains a set of core identity files in `<workspace>/core/` that evolve during use:

| File | Purpose |
|------|---------|
| `SOUL.md` | Learned behaviours, preferred patterns, and successful strategies |
| `USER.md` | Inferred user preferences, tech stack, and communication style |
| `MEMORY.md` | Long‑term facts: project details, decisions, errors & fixes |
| `HEARTBEAT.md` | Health‑check logs and system status history |
| `Need.md` | Agent → User requests (managed automatically) |
| `Tools.md` | Skill registry and tool definitions |

You can trigger an immediate evolution cycle:

```python
from unigent.agent import evolve_now, show_core_files
evolve_now(agent)
show_core_files()
```

## 📁 Project Structure

```
unigent/
├── src/unigent/
│   ├── __init__.py
│   └── agent.py         # Main agent code (combined module)
├── debian/              # Debian packaging metadata
├── pyproject.toml       # Python project metadata
├── README.md
├── LICENSE
├── CHANGELOG.md
└── dist/                # Built packages (deb, wheel, sdist)
```

## 🔧 Building the Debian Package

From the repository root:

```bash
# Install build dependencies
sudo apt-get update
sudo apt-get install -y devscripts build-essential debhelper dh-python python3-all python3-setuptools

# Build the package (creates ../unigent_0.2.0_all.deb)
dpkg-buildpackage -b -us -uc
```

## 🧪 Python Packages

Built artifacts are placed in `dist/`:

```bash
# Build wheel and sdist
python3 setup.py sdist bdist_wheel

# Install locally for development
pip3 install -e .
```

## 📚 Documentation

- **Full API Reference** – Coming soon.
- **Developer Guide** – See `docs/` directory for architecture and extensibility.
- **Changelog** – See [CHANGELOG.md](CHANGELOG.md).

## 🤝 Contributing

Contributions are welcome! Please open an issue or submit a pull request.

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

## 📄 License

This project is licensed under the MIT License – see the [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

- Built with the [OpenAI Python client](https://github.com/openai/openai-python) for NIM compatibility.
- Inspired by agent-zero and the broader AI agent community.

---

<div align="center">
Made with ❤️ for developers worldwide
</div>
