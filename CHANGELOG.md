# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial release of UniGent AI Agent
- Streaming responses with live status bar
- Persistent memory (JSON-line store) with tagging and fuzzy search
- File manager: read, write, delete, search within sandboxed workspace
- Shell runner with safety blocklist and timeouts
- Web search and fetch tools
- Sub-tool system for dynamic Python tool creation
- Skills system with validation and auto-reload
- Parallel API handler for concurrent tasks
- Heartbeat manager with periodic health checks
- Structured JSON logging with rotation
- Retry decorator with exponential backoff
- Rate limiter for API calls
- LRU cache for tool results
- Comprehensive toolset: memory, files, shell, web, subtools, skills, todo, needs, etc.
- Debian package for easy installation on Ubuntu/Debian

## [0.1.0] - 2026-03-18

### Added
- First public release
- Full agent functionality from UniGent.ipynb
- Debian packaging (unigent_0.1.0-1_all.deb)
- pyproject.toml with setuptools backend
- Documentation and README with SEO optimization
- GitHub repository with issue templates
