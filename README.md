<!--
UniGent AI Agent
SEO: autonomous AI agent, NVIDIA NIM, streaming responses, persistent memory, file operations, shell commands, web search, parallel execution, Debian package, Ubuntu, Python, openai, LLM agent, developer tools, AI assistant, local AI, chat AI, code assistant, task automation
-->
<div align="center">

# 🤖 UniGent AI Agent

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Debian Package](https://img.shields.io/badge/Debian-0.1.0-blue?logo=debian)](https://github.com/unn-Known1/unigent/releases)
[![GitHub issues](https://img.shields.io/github/issues/unn-Known1/unigent.svg)](https://github.com/unn-Known1/unigent/issues)
[![GitHub last commit](https://img.shields.io/github/last-commit/unn-Known1/unigent.svg)](https://github.com/unn-Known1/unigent/commits)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

**A universal autonomous AI agent powered by NVIDIA NIM API with streaming responses, persistent memory, file operations, shell commands, web search, parallel tool execution, and robust logging.** Designed for developers who need a powerful local AI assistant that integrates seamlessly into their workflow.

---

</div>

## 🚀 Quick Start

### Installation

#### Option 1: Debian/Ubuntu Package (Recommended)

Download and install the `.deb` package from the [latest release](https://github.com/unn-Known1/unigent/releases).

```bash
# Install dependencies
sudo apt-get update
sudo apt-get install -y python3-requests

# Install the deb package
sudo dpkg -i unigent_0.1.0-1_all.deb
```

#### Option 2: Python Package

```bash
# Clone and install
git clone https://github.com/unn-Known1/unigent.git
cd unigent
pip3 install .
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

## ✨ Features

- **🔊 Streaming Responses** – Real-time token streaming with live status updates.
- **💾 Persistent Memory** – JSON-line based key-value store with tagging and fuzzy search.
- **📁 File Operations** – Read, write, delete, search files within a sandboxed workspace.
- **⚡ Shell Commands** – Safe execution of shell commands with blocklist and timeout.
- **🌐 Web Tools** – Built-in web search (`web_search`) and fetch (`web_fetch`).
- **🔧 Sub-tool System** – Create and run custom Python tools dynamically.
- **⚙️ Skills System** – Extensible skill architecture with validation and auto-reload.
- **📊 Live Status Bar** – Real-time API usage stats and context token monitoring.
- **🛡️ Security Hardening** – Secret management, code sandboxing, and secure defaults.
- **🔄 Parallel API Calls** – Execute multiple tasks concurrently for speed.
- **💓 Heartbeat Manager** – Periodic health checks and log rotation.
- **📈 Structured Logging** – JSON-structured logs with rotating file handlers and color console output.
- **🔀 Retry & Rate Limiting** – Exponential backoff and sliding-window rate limiter.
- **🤝 Todo & Needs** – Built-in todo list management and user need tracking.

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
| `AGENT_LOG_LEVEL` | `DEBUG` | Log level (DEBUG, INFO, WARNING, ERROR) |
| `AGENT_CTX_BUDGET` | `245000` | Token budget for context window |

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
└── CHANGELOG.md
```

## 🔧 Building the Debian Package

From the repository root:

```bash
# Install build dependencies
sudo apt-get update
sudo apt-get install -y devscripts build-essential debhelper dh-python python3-all python3-setuptools

# Build the package
dpkg-buildpackage -b -us -uc

# The .deb package will be in the parent directory
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