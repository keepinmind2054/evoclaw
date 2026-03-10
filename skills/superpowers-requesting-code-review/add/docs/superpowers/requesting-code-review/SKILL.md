---
name: requesting-code-review
description: Use when completing tasks, implementing major features, or before merging to verify work meets requirements
---

# Requesting Code Review

Dispatch a code-reviewer subagent to catch issues before they cascade.

*Core principle:* Review early, review often.

## When to Request Review

Mandatory:
- After each task in subagent-driven development
- After completing major feature
- Before merge to main

## How to Request

1. Get git SHAs (BASE_SHA and HEAD_SHA)
2. Dispatch code-reviewer subagent with: diff, requirements, success criteria
3. Act on feedback: Fix Critical immediately, Important before proceeding, note Minor

## Code Reviewer Checks

The reviewer should check:
- All stated requirements implemented (nothing more, nothing less)
- Code quality: naming, structure, single responsibility
- Error handling coverage
- Test coverage
- Production readiness (no debug code, no hardcoded values)

Verdict: Ready to merge: Yes / No / With fixes

## Red Flags

Never:
- Skip review because "it's simple"
- Ignore Critical issues
- Proceed with unfixed Important issues
