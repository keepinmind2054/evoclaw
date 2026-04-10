## Linked Issue

<!--
Every PR should reference a tracking issue. Use one of:
  Closes #N      Fixes #N      Resolves #N      Refs #N
If there is genuinely no issue (e.g. a tiny typo fix), add the `no-issue`
label to this PR to bypass the CI check.
-->

Closes #

## Type of Change

- [ ] **Fix** — bug fix or security fix to source code
- [ ] **Simplification** — reduces or simplifies source code
- [ ] **Skill** — adds a new skill in `.claude/skills/`
- [ ] **Chore / meta** — tooling, CI, docs, or repo housekeeping

## Description

<!-- What changed and why. Call out any migration steps or breaking changes. -->

## Checklist

- [ ] PR body links an issue (or I added the `no-issue` label)
- [ ] `docs/CHANGELOG.md` has an entry for this change (or I added the `skip-changelog` label — only valid for non-runtime PRs)
- [ ] `README.md` updated if user-visible behavior changed (install steps, env vars, commands, architecture)
- [ ] Tests added or updated where relevant, and `pytest tests/` passes locally

## For Skills

- [ ] I have not made any changes to source code
- [ ] My skill contains instructions for Claude to follow (not pre-built code)
- [ ] I tested this skill on a fresh clone
