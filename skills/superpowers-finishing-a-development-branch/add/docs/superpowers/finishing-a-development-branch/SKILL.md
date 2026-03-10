---
name: finishing-a-development-branch
description: Use when implementation is complete, all tests pass, and you need to decide how to integrate the work
---

# Finishing a Development Branch

## Overview

Guide completion of development work by presenting clear options and handling chosen workflow.

*Core principle:* Verify tests → Present options → Execute choice → Clean up.

## The Process

### Step 1: Verify Tests
Run the full test suite. If failing, STOP and fix before proceeding.

### Step 2: Determine Base Branch
Check what branch this work should merge back to.

### Step 3: Present Options

```
Implementation complete. What would you like to do?

1. Merge back to <base-branch> locally
2. Push and create a Pull Request
3. Keep the branch as-is (I'll handle it later)
4. Discard this work

Which option?
```

### Step 4: Execute Choice
- Option 1: checkout base, pull, merge feature, verify tests, delete branch
- Option 2: git push -u origin, create PR
- Option 3: Report "Keeping branch. Worktree preserved."
- Option 4: Confirm with user, then delete

## Red Flags

Never:
- Proceed with failing tests
- Merge without verifying tests on result
- Delete work without confirmation
- Force-push without explicit request
