---
name: using-git-worktrees
description: Use when starting feature work that needs isolation from current workspace or before executing implementation plans
---

# Using Git Worktrees

*Core principle:* Systematic directory selection + safety verification = reliable isolation.

## Directory Selection Priority

1. Check for existing .worktrees/ or worktrees/ directory
2. Check CLAUDE.md for preference
3. Ask user (options: .worktrees/ in project or ~/.config/superpowers/worktrees/<project-name>/)

## Safety Verification

MUST verify the worktree directory is gitignored:
```bash
git check-ignore -q .worktrees
```
If NOT ignored: add to .gitignore and commit first.

## Creation Steps

1. Detect project name: `basename $(git rev-parse --show-toplevel)`
2. Create worktree: `git worktree add "$path" -b "$BRANCH_NAME"`
3. Run project setup (auto-detect: npm install, cargo build, pip install, etc.)
4. Verify Clean Baseline (run tests; if fail, report and ask)
5. Report location to user

## Cleanup

After work is complete (via finishing-a-development-branch):
```bash
git worktree remove "$path"
git branch -d "$BRANCH_NAME"
```
