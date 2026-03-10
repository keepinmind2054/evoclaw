---
name: test-driven-development
description: Use when implementing any feature or bugfix, before writing implementation code
---

# Test-Driven Development (TDD)

*Core principle:* If you didn't watch the test fail, you don't know if it tests the right thing.

## The Iron Law

```
NO PRODUCTION CODE WITHOUT A FAILING TEST FIRST
```

Write code before the test? Delete it. Start over. No exceptions.

## Red-Green-Refactor

### RED — Write Failing Test
Write one minimal test showing what should happen. One behavior, clear name, real code (no mocks unless unavoidable).

### Verify RED — Watch It Fail
MANDATORY. Never skip. Confirm test fails (not errors), failure message is expected, fails because feature missing.

### GREEN — Minimal Code
Write simplest code to pass the test. Don't add features, refactor other code, or "improve" beyond the test.

### Verify GREEN — Watch It Pass
MANDATORY. Confirm test passes, other tests still pass.

### REFACTOR — Clean Up
After green only: remove duplication, improve names, extract helpers. Keep tests green. Don't add behavior.

## Common Rationalizations (all mean: Delete code. Start over.)

- "Too simple to test"
- "I'll test after"
- "Already manually tested"
- "TDD will slow me down"
- "I'm being pragmatic"

## Testing Anti-Patterns to Avoid

- Testing mock behavior instead of real behavior
- Test-only methods in production classes
- Mocking without understanding dependencies
- Incomplete mocks missing fields real code uses
