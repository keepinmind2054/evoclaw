---
name: dispatching-parallel-agents
description: Use when facing 2+ independent tasks that can be worked on without shared state or sequential dependencies
---

# Dispatching Parallel Agents

## Overview

When you have multiple unrelated failures or tasks, investigating them sequentially wastes time. Dispatch one agent per independent problem domain and let them work concurrently.

*Core principle:* Dispatch one agent per independent problem domain. Let them work concurrently.

## When to Use

Use when:
- 3+ test files failing with different root causes
- Multiple subsystems broken independently
- Each problem can be understood without context from others
- No shared state between investigations

Don't use when:
- Failures are related (fix one might fix others)
- Need to understand full system state
- Agents would interfere with each other

## The Pattern

1. *Identify Independent Domains* — Group failures by what's broken
2. *Create Focused Agent Tasks* — Each agent gets specific scope, clear goal, constraints, expected output
3. *Dispatch in Parallel* — All agents run concurrently
4. *Review and Integrate* — Read each summary, verify fixes don't conflict, run full test suite

## Agent Prompt Structure

Good agent prompts are:
- *Focused* - One clear problem domain
- *Self-contained* - All context needed to understand the problem
- *Specific about output* - What should the agent return?

## Common Mistakes

- Too broad: "Fix all the tests"
- No context → paste the error messages and test names
- No constraints → "Do NOT change production code"
- Vague output → "Return summary of root cause and changes"

## Verification

After agents return:
1. Review each summary
2. Check for conflicts
3. Run full suite
4. Spot check
