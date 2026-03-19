---
name: code-review
description: Review Python code for correctness, safety, and efficiency.
---

# Code Review Skill

Perform a thorough code review focusing on:

1. **Correctness** — Logic errors, edge cases, race conditions
2. **Safety** — SQL injection, path traversal, authentication bypasses
3. **Efficiency** — N+1 queries, unbounded caches, blocking I/O in async context
4. **Reuse** — Duplication, missing abstractions, inconsistent patterns

## Severity Levels
- CRITICAL: Security vulnerabilities or data loss risk
- HIGH: Bugs that will cause failures in production
- MEDIUM: Code smells that degrade reliability
- LOW: Style and minor improvements

## Output Format

For each finding:
```
[SEVERITY] filename.py:line — description
Fix: what to change
```
