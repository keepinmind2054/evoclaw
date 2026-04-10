# Contributing

## Source Code Changes

**Accepted:** Bug fixes, security fixes, simplifications, reducing code.

**Not accepted:** Features, capabilities, compatibility, enhancements. These should be skills.

## How to propose changes

All source changes flow through **issue → PR → merge → CHANGELOG → README**. CI enforces the issue-link and CHANGELOG parts; the README part is a human judgement call.

1. **Open an issue first.** Use `gh issue create` (or the GitHub UI) with one of the templates in `.github/ISSUE_TEMPLATE/`. Describe the problem, not the fix. If an issue already exists, skip this step.
2. **Branch from `main`.** Use a short descriptive prefix: `fix/<slug>`, `chore/<slug>`, `docs/<slug>`, `skill/<slug>`.
3. **Implement + commit.** Small, focused commits are fine; they'll be squashed on merge. Conventional commit prefixes (`fix:`, `chore:`, `docs:`, `feat:`) are encouraged.
4. **Update `docs/CHANGELOG.md`** in the *same* PR. Add a new `## [MAJOR.MINOR.PATCH] — YYYY-MM-DD` block at the top with `### Fixed` / `### Added` / `### Changed` entries that reference the issue/PR number. Patch bumps for fixes, minor for features, major for breaking changes.
   - **Exception:** if the PR touches none of `host/`, `container/`, `scripts/`, `Makefile`, or `.env.example`, CI will not require a CHANGELOG entry. Add the `skip-changelog` label if you want to make the exemption explicit.
5. **Update `README.md` only if user-visible behavior changed** — new env vars, new commands, new install steps, architectural shifts. Pure internal fixes don't need README changes. Keep `README_en.md` in sync if you do touch `README.md`.
6. **Push the branch and open a PR.** The PR body **must** contain `Closes #N`, `Fixes #N`, `Resolves #N`, or `Refs #N`. The `.github/PULL_REQUEST_TEMPLATE.md` has the slot. If this PR genuinely has no tracking issue, add the `no-issue` label to bypass the CI gate.
7. **Squash merge.** Use `gh pr merge <N> --squash --delete-branch`. The squashed commit message automatically gets `(#PR)` appended, which matches the existing history style.

### Escape hatch labels

| Label | Effect |
|---|---|
| `no-issue` | Skips the "PR body must link an issue" CI check |
| `skip-changelog` | Skips the CHANGELOG update requirement (only valid for PRs that don't touch runtime source paths) |

Use sparingly — they exist for legitimate exceptions (tiny typos, CI-only housekeeping), not for shortcutting the flow.

## Skills

A [skill](https://code.claude.com/docs/en/skills) is a markdown file in `skills/` that teaches Claude Code how to transform a EvoClaw installation.

A PR that contributes a skill should not modify any source files.

Your skill should contain the **instructions** Claude follows to add the feature—not pre-built code. See `/add-telegram` for a good example.

### Why?

Every user should have clean and minimal code that does exactly what they need. Skills let users selectively add features to their fork without inheriting code for features they don't want.

### Testing

Test your skill by running it on a fresh clone before submitting.
