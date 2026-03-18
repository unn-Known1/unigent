# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
