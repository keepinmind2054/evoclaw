# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `CONTAINER_IMAGE` environment variable support for flexible Docker image configuration.
- `CHANGELOG.md`, `RELEASE.md`, `ISSUES.md` documentation files.
- GitHub Issue and Pull Request templates.

### Fixed
- Docker image missing system libraries for PPT/PDF generation (`libfreetype6`, `libpng16-16`, `zlib1g`).
- Docker image missing Chinese fonts (`fonts-wqy-zenhei`).
- File sending race condition in container-to-host communication.

### Changed
- Updated `README.md` with v1.10.23 release notes and environment variable documentation.

## [1.10.23] - 2026-03-12

### Fixed
- Critical: Docker image now includes necessary system libraries for document generation.
- Critical: Added `CONTAINER_IMAGE` environment variable to `host/config.py`.

### Added
- Standardized release process documentation (`RELEASE.md`).
- Issue tracking template (`ISSUES.md`).
