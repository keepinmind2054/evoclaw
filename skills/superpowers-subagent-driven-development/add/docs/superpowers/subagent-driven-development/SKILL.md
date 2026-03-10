---
name: subagent-driven-development
description: Use when executing implementation plans with independent tasks in the current session
---

# Subagent-Driven Development

Execute plan by dispatching fresh subagent per task, with two-stage review after each: spec compliance review first, then code quality review.

*Core principle:* Fresh subagent per task + two-stage review (spec then quality) = high quality, fast iteration

## The Process

1. Read plan, extract all tasks with full text, note context, create todo list
2. Per task:
   - Dispatch implementer subagent
   - If subagent asks questions → answer them
   - Implementer implements, tests, commits, self-reviews
   - Dispatch spec reviewer → fix if issues
   - Dispatch code quality reviewer → fix if issues
   - Mark task complete in todo list
3. After all tasks: Dispatch final code reviewer for entire implementation
4. Use finishing-a-development-branch

## Model Selection

- *Mechanical implementation tasks* (1-2 files, clear specs): fast model
- *Integration and judgment tasks* (multi-file): standard model
- *Architecture, design, review tasks*: most capable model

## Handling Implementer Status

- *DONE*: Proceed to spec compliance review
- *DONE_WITH_CONCERNS*: Read concerns before proceeding
- *NEEDS_CONTEXT*: Provide missing context and re-dispatch
- *BLOCKED*: Assess blocker — more context, smaller task, or escalate

## Red Flags

Never:
- Start implementation on main/master without explicit user consent
- Skip reviews
- Proceed with unfixed issues
- Dispatch multiple implementation subagents in parallel (conflicts)
