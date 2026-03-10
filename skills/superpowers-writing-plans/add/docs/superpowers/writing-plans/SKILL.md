---
name: writing-plans
description: Use when you have a spec or requirements for a multi-step task, before touching code
---

# Writing Plans

Write comprehensive implementation plans assuming the engineer has zero context for the codebase and questionable taste. Document everything: which files to touch, code snippets, testing, docs. Give them the whole plan as bite-sized tasks. DRY. YAGNI. TDD. Frequent commits.

*Save plans to:* docs/superpowers/plans/YYYY-MM-DD-<feature-name>.md

## Bite-Sized Task Granularity

Each step is one action (2-5 minutes):
- "Write the failing test" — step
- "Run it to make sure it fails" — step
- "Implement the minimal code to make the test pass" — step
- "Run the tests and make sure they pass" — step
- "Commit" — step

## Plan Document Header

Every plan MUST start with:

```
# [Feature Name] Implementation Plan

Goal: [One sentence describing what this builds]
Architecture: [2-3 sentences about approach]
Tech Stack: [Key technologies/libraries]
```

## Execution Handoff

- If subagents available: Use subagent-driven-development
- If no subagents: Use executing-plans
