# Changelog

All notable changes to EvoClaw will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.10.6] - 2026-03-11

### Fixed (Code Review Findings)
- CRASH: .env shadow mount no longer double-prefixes `-v` flag (containers were failing to start on Linux/macOS)
- ERROR: run_container_agent now catches asyncio.CancelledError and calls _stop_container (outer timeout no longer creates zombie containers)
- ERROR: /api/dev/resume now writes IPC file to correct group folder path (DevEngine resume was silently broken)
- WARNING: cleanup_orphans now awaits proc.wait() after docker rm
- Minor: send_file tool schema — chat_jid removed from required[] (auto-detected from input)
- Minor: _resolve_container_path guards against empty group_folder
- Minor: TelegramChannel.send_file removes redundant filename parameter

## [1.10.5] - 2026-03-11

### Added
- **Comprehensive Container Agent Logging**: Added `_log(tag, msg)` helper with millisecond timestamps to `container/agent-runner/agent.py` for structured stderr logging throughout the agent lifecycle.
  - Startup: process ID logged at container boot (`🚀 START`).
  - Input parsed: JID, group folder, and message count (`📥 INPUT`).
  - Last message preview for quick debugging (`💬 MSG`).
  - Model/provider selection before first LLM call (`🤖 MODEL`).
  - Per-turn LLM call and response with stop reason (`🧠 LLM →/←`).
  - Tool dispatch with name and truncated args (`🔧 TOOL`).
  - Tool result preview (`🔧 RESULT`).
  - IPC file writes for messages, tasks, and files (`📨 IPC`).
  - File send path and existence check (`📎 FILE`).
  - Output size in chars before emit (`📤 OUTPUT`).
  - Exception type and message with full traceback to stderr (`❌ ERROR`).
  - Completion with success flag (`🏁 DONE`).
- **Noisy SDK log suppression**: `httpx`, `httpcore`, `google`, and `urllib3` loggers clamped to WARNING level.
- **Host stderr elevation**: `host/container_runner.py` `_stream_stderr()` now promotes emoji-tagged agent log lines from DEBUG to INFO so they appear in production logs without `--debug`.

## [1.10.1] - 2026-03-11

### Fixed
- **Telegram Channel**: Fixed critical bug in `send_file()` where binary files (e.g., `.pptx`, `.pdf`, `.jpg`) would fail to send due to incorrect encoding handling (`cp950 codec can't decode` error).
  - Changed file reading to explicitly use binary mode (`rb`) and read entire content before sending.
  - Now uses `telegram.InputFile` to ensure binary data is properly transmitted.
  - Added MIME type detection with fallback to `application/octet-stream`.
  - Set `disable_content_type_detection=True` to prevent Telegram from re-encoding files.
  - Improved error logging for file sending failures.

## [1.10.0] - 2026-03-10

### Added
- **Evolution Engine**: Full genome evolution with formality, technical_depth, and responsiveness genes.
- **Health Monitor**: Real-time system health tracking with automatic alerts.
- **DevEngine**: 7-stage automated development pipeline (Analyze → Design → Implement → Test → Review → Document → Deploy).
- **Web Dashboard**: 7-tab monitoring interface with Subagent hierarchy visualization.
- **Superpowers Integration**: 12 workflow skill packages from Superpowers methodology.

### Changed
- Replaced `threading.Lock` with `asyncio.Lock` for better async compatibility.
- GroupQueue now serializes container execution per group.
- WebPortal session timeout reduced to 1 hour.

### Fixed
- `_stop_container` now properly waits for `proc.wait()` to complete.
- `/api/env` now uses key whitelist for security.
- DevEngine JID fallback now provides clear error messages.
- macOS compatibility fixes for `.env` file handling.

## [1.9.0] - 2026-02-15

### Added
- **Immune System Enhancement**: 22 injection pattern detections.
- **Adaptive Evolution**: Epigenetic adaptation based on system load and time of day.
- **Evolution Log**: Complete history of genome changes in `evolution_log` table.

### Changed
- Improved container isolation and security.
- Enhanced error reporting in dashboard.

## [1.8.0] - 2026-02-01

### Added
- **Skills Engine**: Plugin system for adding new capabilities.
- **WhatsApp Support**: Optional skill for WhatsApp integration.
- **Multi-model Support**: Gemini, OpenAI-compatible, and Claude.

### Changed
- Refactored channel architecture for better modularity.

---

## Version History Summary

| Version | Date | Key Changes |
|---------|------|-------------|
| 1.10.1 | 2026-03-11 | Fixed Telegram binary file sending bug |
| 1.10.0 | 2026-03-10 | Full evolution engine, DevEngine, Health Monitor |
| 1.9.0 | 2026-02-15 | Enhanced immune system, adaptive evolution |
| 1.8.0 | 2026-02-01 | Skills engine, WhatsApp support |
