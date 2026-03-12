# EvoClaw Known Issues & PR Plan

This document tracks known issues, feature requests, and the plan for Pull Requests (PRs) to address them.

## Critical Issues (P0)

| # | Issue | Status | PR | Notes |
|---|-------|--------|-----|-------|
| 1 | Docker image missing system libraries for PPT/PDF generation | ✅ Fixed | #1 | Added `libfreetype6`, `libpng16-16`, `zlib1g`, `fonts-wqy-zenhei` |
| 2 | Missing `CONTAINER_IMAGE` environment variable | ✅ Fixed | #1 | Added to `host/config.py` |
| 3 | Missing release documentation | ✅ Fixed | #1 | Added `RELEASE.md`, `CHANGELOG.md` |
| 4 | Missing issue tracking system | ✅ Fixed | #1 | Added `ISSUES.md` and GitHub templates |

## High Priority Issues (P1)

| # | Issue | Status | PR | Notes |
|---|-------|--------|-----|-------|
| 5 | File sending race condition | Open | - | Container may write file after host tries to send |
| 6 | Dashboard missing container image version display | Open | - | Need to show current `CONTAINER_IMAGE` in UI |
| 7 | Missing CI/CD automation | Open | - | Need automated testing and building on push |

## Medium Priority Issues (P2)

| # | Issue | Status | PR | Notes |
|---|-------|--------|-----|-------|
| 8 | Missing container resource limits | Open | - | Prevent OOM kills with `CONTAINER_MEMORY` and `CONTAINER_CPUS` |
| 9 | Evolution engine persistence issues | Open | - | Genome updates may be lost on restart |
| 10 | Web portal missing file upload | Open | - | Cannot upload files via web interface |

## PR Plan

### PR #1: Fix Docker Dependencies and Add Documentation (Merged)
- **Issues Fixed**: #1, #2, #3, #4
- **Changes**:
  - Updated `Dockerfile` to include necessary system libraries and fonts.
  - Added `CONTAINER_IMAGE` environment variable support.
  - Added `CHANGELOG.md`, `RELEASE.md`, `ISSUES.md`.
  - Updated `README.md`.
  - Added GitHub Issue and PR templates.

### Future PRs
- **PR #2**: Fix file sending race condition (Issue #5).
- **PR #3**: Add container image version to dashboard (Issue #6).
- **PR #4**: Implement CI/CD pipeline (Issue #7).
