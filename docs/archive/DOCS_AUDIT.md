# EvoClaw Docs Audit

Last updated: 2026-05-22

This audit summarizes every markdown file under `docs/`, records its role in the repo, and calls out reading risks. It also documents the encoding trap that caused agents to believe valid UTF-8 Chinese documents were corrupted.

## Executive Summary

The `docs/` directory currently contains 20 markdown files. They fall into five groups:

- Architecture and specs: `ARCHITECTURE.md`, `SPEC.md`, `REQUIREMENTS.md`, `SKILLS_ARCHITECTURE.md`, `SDK_DEEP_DIVE.md`
- Operations and maintenance: `DEBUG_CHECKLIST.md`, `SELF_UPDATE.md`, `DEVELOPMENT_WORKFLOW.md`, `APPLE-CONTAINER-NETWORKING.md`, `SECURITY.md`, `test-scenarios.md`
- Planning and strategy: `EXECUTION_PLAN.md`, `FLUIDITY_AND_DAYTONA_PLAN.md`, `PRODUCT_STRATEGY.md`, `SPEED_UP_IMPLEMENTATION_PLAN.md`, `SPEED_UP_TASK_BREAKDOWN.md`
- Analysis and stability: `ANALYSIS_REPORT_2026.md`, `STABILITY_ANALYSIS.md`
- Navigation and history: `README.md`, `CHANGELOG.md`

The files are valid UTF-8 on disk. The garbled text observed in PowerShell output is terminal mojibake, not file corruption. Agents should verify with a UTF-8 reader before rewriting any Chinese document.

## Agent Reading Procedure

For full-directory documentation review, use this process:

1. List all `docs/*.md` files first.
2. Build a checklist with every filename.
3. Read one file at a time.
4. Record purpose, key content, operational relevance, and risks.
5. Mark the file complete before moving to the next one.
6. If terminal output appears garbled, verify using a UTF-8 reader.
7. Do not stop after early files such as `SPEED_UP_IMPLEMENTATION_PLAN.md` and `SPEED_UP_TASK_BREAKDOWN.md`.

Windows-safe UTF-8 check:

```powershell
python -c "from pathlib import Path; s=Path('docs/README.md').read_text(encoding='utf-8'); print(s[:1000])"
```

Corruption evidence check:

```powershell
python -c "from pathlib import Path; s=Path('docs/README.md').read_text(encoding='utf-8', errors='replace'); print(s.count(chr(0xfffd)))"
```

`0` replacement characters means Python did not detect UTF-8 decode corruption.

## File-by-File Analysis

### `ANALYSIS_REPORT_2026.md`

Purpose: Deep technical comparison between EvoClaw and NanoClaw.

Key content:

- root-cause analysis for false responses
- memory pollution and feedback-loop risks
- host stability and container architecture findings
- remaining high-priority fixes after Phase 12-19

Operational relevance: Useful when diagnosing why EvoClaw diverged from NanoClaw or when deciding whether a stability issue is architectural rather than a one-line bug.

Risk: Large report. Agents should read by section instead of trying to summarize it all in one pass.

Status: Valid UTF-8.

### `APPLE-CONTAINER-NETWORKING.md`

Purpose: macOS Apple Container networking guide.

Key content:

- IP forwarding setup
- NAT / vmnet configuration
- IPv6 DNS caveat
- validation commands

Operational relevance: Only relevant for macOS / Apple Container operators.

Risk: Platform-specific. Do not apply blindly to Windows or Linux Docker deployments.

Status: Valid UTF-8.

### `ARCHITECTURE.md`

Purpose: Main architecture overview.

Key content:

- current v1.x host / container architecture
- target v2.x UnifiedClaw direction
- portable agent concept
- agent repository structure
- `agent.yaml` design

Operational relevance: Primary architecture reference before changing host/container boundaries, identity, memory, or runtime shape.

Risk: Contains target architecture as well as implemented architecture. Verify claims against code before treating them as shipped behavior.

Status: Valid UTF-8.

### `CHANGELOG.md`

Purpose: Release history and operational rollout notes.

Key content:

- versioned fixes and features
- issue / PR linkage
- technical details per change
- image rebuild and breaking-change notes

Operational relevance: Required for PR delivery unless `skip-changelog` is valid. Also useful for reconstructing why a behavior exists.

Risk: Very large. Read latest entries first unless historical context is needed.

Status: Valid UTF-8.

### `DEBUG_CHECKLIST.md`

Purpose: Runtime troubleshooting checklist.

Key content:

- known issues
- service health checks
- log checks
- database and scheduler triage
- Docker / queue investigation steps

Operational relevance: First operational reference when EvoClaw is running but behaving incorrectly.

Risk: Some checks may become stale as architecture changes. Confirm command paths before using in production.

Status: Valid UTF-8.

### `DEVELOPMENT_WORKFLOW.md`

Purpose: Central development and delivery workflow.

Key content:

- issue-first workflow
- branch / PR / merge process
- host-side vs container image changes
- verification expectations
- restart and deployment model

Operational relevance: Source of truth for normal contributor behavior and remote GitHub flow.

Risk: Must stay aligned with `.github/PULL_REQUEST_TEMPLATE.md` and `docs/SELF_UPDATE.md`.

Status: Valid UTF-8.

### `EXECUTION_PLAN.md`

Purpose: Six-month execution roadmap.

Key content:

- one-sentence goal
- constraints that affect estimates
- sprint plan
- early issue list
- risk register

Operational relevance: Planning reference for prioritizing work across product, runtime, setup, and solution-pack goals.

Risk: Roadmap content can become stale quickly. Treat as planning context, not a guarantee.

Status: Valid UTF-8.

### `FLUIDITY_AND_DAYTONA_PLAN.md`

Purpose: Responsiveness and runtime migration analysis.

Key content:

- why EvoClaw feels slow
- per-turn cold path problem
- file polling / IPC overhead
- GroupQueue and background-task pressure
- phased path toward runtime abstraction and Daytona

Operational relevance: Main strategic reference for speed, runtime, and Daytona-related changes.

Risk: Some sections are proposals. Check current implementation before assuming the migration has landed.

Status: Valid UTF-8.

### `PRODUCT_STRATEGY.md`

Purpose: Product positioning strategy.

Key content:

- why EvoClaw should not compete head-on with generic coding agents
- positioning as a persistent multi-agent operations platform
- solution-pack strategy
- claims mapped to implementation maturity

Operational relevance: Useful when deciding product scope, messaging, and what not to build.

Risk: Strategic claims can outrun code maturity. Keep README and marketing claims grounded.

Status: Valid UTF-8.

### `README.md`

Purpose: Index for the `docs/` directory.

Key content:

- core document list
- analysis document list
- system design document list
- operations document list
- archived / merged document notes

Operational relevance: Entry point for navigating the docs directory.

Risk: Must be kept current when adding new docs.

Status: Valid UTF-8.

### `REQUIREMENTS.md`

Purpose: Requirements and design philosophy.

Key content:

- why EvoClaw exists
- design philosophy
- safety through isolation
- single-user origin
- customization through code
- AI-native development assumptions

Operational relevance: Helps evaluate whether a proposed feature fits the original product constraints.

Risk: Historical requirements may differ from current product direction. Compare with `PRODUCT_STRATEGY.md`.

Status: Valid UTF-8.

### `SDK_DEEP_DIVE.md`

Purpose: Deep technical analysis of Claude Agent SDK behavior.

Key content:

- SDK architecture
- `query()` options
- permission modes
- agent definitions
- MCP server config
- SDK message types
- sub-agent behavior and integration implications

Operational relevance: Reference for SDK integration, permissions, and agent-loop behavior.

Risk: SDK APIs can change. Verify against current official SDK docs before implementing new SDK behavior.

Status: Valid UTF-8.

### `SECURITY.md`

Purpose: Security model.

Key content:

- trust model
- container isolation
- mount security
- session isolation
- IPC authorization
- credential handling
- RBAC and audit posture

Operational relevance: Required reading before changing permissions, mounts, secrets, IPC tools, or container runtime behavior.

Risk: Security docs must match code. If behavior changes, update this file in the same PR.

Status: Valid UTF-8.

### `SELF_UPDATE.md`

Purpose: Self-update and restart design.

Key content:

- restart policy
- `/update`, auto-update, IPC self-update, and restart entry points
- worktree-based update flow
- AI auto-fix flow
- restart flags and notification files
- manual host-only vs container-image deployment

Operational relevance: Source of truth for update and restart behavior.

Risk: Must stay synchronized with `host/auto_update.py`, `host/ipc_watcher.py`, and `host/main.py`.

Status: Valid UTF-8.

### `SKILLS_ARCHITECTURE.md`

Purpose: Skills system architecture.

Key content:

- what Skills are
- why the design uses git-native composition
- shared foundation model
- backup / restore safety
- code merge vs structured operations
- user customization model

Operational relevance: Main reference before modifying skills, merge flow, or user customization behavior.

Risk: Large architecture document. Read in sections and validate implementation status.

Status: Valid UTF-8.

### `SPEC.md`

Purpose: Broad technical specification.

Key content:

- system architecture
- channel system
- triggers
- conversation context
- commands
- scheduling
- storage
- service launch behavior

Operational relevance: General technical contract for EvoClaw behavior.

Risk: Some sections may be older than implementation. Verify against code and changelog for current behavior.

Status: Valid UTF-8.

### `SPEED_UP_IMPLEMENTATION_PLAN.md`

Purpose: Implementation plan for improving responsiveness.

Key content:

- target metrics such as TTFT, TTR, queue wait, prompt size, and tool turns
- problem definition around Docker cold path, IPC, queues, prompt bloat, and tool loops
- principles for phased improvement
- observability, queue, prompt, runtime, IPC, and Daytona sequence

Operational relevance: Main planning reference for speed work.

Risk: Terminal output may look garbled in PowerShell, but the file is valid UTF-8. Agents should not stop here; this is only one file in the docs set.

Status: Valid UTF-8.

### `SPEED_UP_TASK_BREAKDOWN.md`

Purpose: Task breakdown for the speed-up implementation plan.

Key content:

- execution principles
- work streams for observability, prompt / agent loop, queue / scheduling, runtime / IPC
- Epic A measurement baseline
- later epics for queue priority, prompt slimming, runtime abstraction, and session runtime

Operational relevance: Converts the speed-up plan into actionable issue / PR slices.

Risk: Terminal output may look garbled in PowerShell, but the file is valid UTF-8. Agents should use it as one checklist input, not the whole task.

Status: Valid UTF-8.

### `STABILITY_ANALYSIS.md`

Purpose: Stability analysis report.

Key content:

- architecture-layer comparison
- container runner risks
- group queue risks
- IPC watcher risks
- scheduler and memory issues
- fixed vs remaining stability items

Operational relevance: Useful when diagnosing crashes, hangs, false responses, queue stalls, or retry loops.

Risk: Some findings are historical. Check changelog for fixes that landed after the report.

Status: Valid UTF-8.

### `test-scenarios.md`

Purpose: System test scenario catalog.

Key content:

- basic conversation scenarios
- trigger matching
- multi-turn context
- scheduled tasks
- queue behavior
- memory behavior
- security / injection checks
- recovery cases

Operational relevance: Manual and design-level testing reference.

Risk: Not all scenarios are automated tests. Do not confuse this with `tests/`.

Status: Valid UTF-8.

## Encoding Finding

The files that appeared garbled through `Get-Content` are readable as UTF-8 through Python. This means the stored markdown is not the primary problem. The agent-loop failure mode came from how the files were read and tracked.

Primary fix:

- keep a full checklist
- read files with UTF-8 aware tooling
- distinguish terminal rendering from stored-file corruption
- continue past files that merely display poorly in the terminal

## Follow-Up Recommendations

- Keep this audit updated when docs are added or retired.
- Consider adding a small script later to check replacement characters and suspicious mojibake sequences across docs.
- Keep `docs/README.md` as the navigation entry point and link this audit there.
