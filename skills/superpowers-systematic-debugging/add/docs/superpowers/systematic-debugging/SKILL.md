---
name: systematic-debugging
description: Use when encountering any bug, test failure, or unexpected behavior, before proposing fixes
---

# Systematic Debugging

*Core principle:* ALWAYS find root cause before attempting fixes. Symptom fixes are failure.

## The Iron Law

```
NO FIXES WITHOUT ROOT CAUSE INVESTIGATION FIRST
```

## The Four Phases

### Phase 1: Root Cause Investigation

BEFORE attempting ANY fix:
1. Read Error Messages Carefully
2. Reproduce Consistently
3. Check Recent Changes (git log, git diff)
4. Gather Evidence (add diagnostic instrumentation)
5. Trace Data Flow (trace backward through call stack)

### Phase 2: Pattern Analysis

1. Find Working Examples
2. Compare Against References
3. Identify Differences
4. Understand Dependencies

### Phase 3: Hypothesis and Testing

1. Form Single Hypothesis — "I think X is the root cause because Y"
2. Test Minimally — smallest possible change
3. Verify Before Continuing
4. When You Don't Know — say so explicitly

### Phase 4: Implementation

1. Create Failing Test Case (use test-driven-development skill)
2. Implement Single Fix
3. Verify Fix
4. If Fix Doesn't Work: if < 3 fixes tried, return to Phase 1; if >= 3 fixes failed, STOP and question the architecture

## Defense in Depth

Add validation at multiple layers:
- Layer 1: Entry point validation
- Layer 2: Business logic validation
- Layer 3: Environment guards (refuse dangerous operations in test env)
- Layer 4: Debug instrumentation/logging

Make the bug structurally impossible, not just fixed in one place.
