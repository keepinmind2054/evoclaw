# EvoClaw Development Workflow

Last updated: 2026-05-21

This document centralizes the practical development workflow for EvoClaw: how to start work, implement changes, validate them, open a PR, and deploy updates safely.

## 1. Source of truth

This workflow is distilled from:

- `README.md` for setup / startup / pm2 usage
- `docs/SELF_UPDATE.md` for update / restart behavior
- `.github/PULL_REQUEST_TEMPLATE.md` for delivery requirements

If these documents disagree, use this order:

1. `docs/SELF_UPDATE.md` for restart / update behavior
2. `.github/PULL_REQUEST_TEMPLATE.md` for PR requirements
3. `README.md` for operator commands and local setup

## 2. Normal development flow

Use this sequence for most work:

1. Sync `main`
2. Create or confirm the tracking issue
3. Create a branch from the latest `main`
4. Implement one focused change set
5. Run relevant local verification
6. Update docs where required
7. Commit with a clear message
8. Push branch and open PR
9. Merge after review / checks
10. Deploy with the correct restart path

## 3. Before you start coding

Recommended checks:

```bash
git fetch origin main
git status
git switch main
git pull --ff-only origin main
```

Then create a working branch:

```bash
git switch -c <topic-branch>
```

Guidelines:

- Do not pile unrelated work into one branch.
- Do not start from a stale or dirty `main`.
- If local uncommitted work exists, either commit it, stash it, or move it out of the way before switching tasks.

## 4. Issue-first workflow

Most non-trivial changes should have a GitHub issue first.

An issue should state:

- what is broken or missing
- why it matters
- acceptance criteria
- whether rollout requires only host restart or also Docker image rebuild

PRs in this repo are expected to link an issue unless the change is truly trivial.

## 5. Change classification

Before implementation, classify the change.

### Host-side change

Examples:

- `host/*.py`
- `scripts/*`
- `docs/*`
- `.github/*`
- `.env.example`

Effect:

- usually requires only host restart after deploy

### Agent image change

Examples:

- `container/agent-runner/*`
- `container/Dockerfile`

Effect:

- requires rebuilding `evoclaw-agent:latest`
- host restart alone is not enough

This distinction is operationally important. Host self-update updates the host process, but code baked into the agent image does not change until the image is rebuilt.

## 6. Local development and startup

Basic local start:

```bash
python run.py
```

If the agent image must exist or be refreshed:

```bash
docker build -t evoclaw-agent:latest container/
```

pm2-based host startup:

```bash
pm2 start ecosystem.config.js
pm2 save
```

Useful local surfaces:

- dashboard: `http://localhost:8765`
- portal: `http://localhost:8766`

## 7. Verification before commit

Run verification proportional to the change.

Common examples:

```bash
python -m py_compile host/main.py host/container_runner.py host/group_queue.py
pytest tests/
docker build -t evoclaw-agent:latest container/
```

Expectations:

- syntax checks for touched Python modules
- tests for behavior changes where relevant
- image build verification for `container/` changes

Do not claim verification you did not actually run.

## 8. Required documentation updates

Check documentation as part of implementation, not as an afterthought.

### Always consider `docs/CHANGELOG.md`

PR template expectation:

- add a changelog entry unless the change is explicitly exempt

### Update `README.md` when user-visible behavior changes

Examples:

- startup steps
- environment variables
- commands
- architecture-visible behavior
- observability operators should know about

### Update focused docs when process or architecture changes

Examples:

- `docs/SELF_UPDATE.md`
- architecture / security / execution plan docs

## 9. Commit and PR workflow

Stage only the intended files:

```bash
git add <files>
git commit -m "<clear message>"
git push -u origin <topic-branch>
```

PR expectations from the repo template:

- link an issue
- classify the change type
- update `docs/CHANGELOG.md`
- update `README.md` if user-visible behavior changed
- add or update tests where relevant

A good PR body should state:

- what changed
- why it changed
- how it was verified
- whether Docker image rebuild is required

## 10. GitHub workflow

The remote GitHub flow should be treated as a distinct stage, not an afterthought.

Standard sequence:

1. open or confirm the tracking issue
2. branch from the latest `main`
3. implement and verify locally
4. push the topic branch to GitHub
5. open a PR linked to the issue
6. merge after review / checks
7. update the deployed machine separately

Typical commands:

```bash
git fetch origin main
git switch main
git pull --ff-only origin main
git switch -c <topic-branch>
git push -u origin <topic-branch>
```

Default rule:

- open or confirm the tracking issue before pushing work through the normal GitHub flow
- do not push feature or documentation work directly to `main`
- the normal path is `topic branch -> PR -> merge -> sync local main`
- direct `push origin main` should be treated as an exception, not the default workflow

Recommended order:

1. issue
2. topic branch
3. local implementation and verification
4. push branch
5. PR linked to the issue
6. merge
7. sync local `main`

Remote expectations:

- the PR should have a real issue behind it unless the change is genuinely trivial
- the PR should link its issue
- the PR should describe verification actually performed
- the PR should call out whether `container/` changed
- merge to GitHub does not by itself deploy the running EvoClaw host

Important distinction:

- GitHub merge updates repository history
- deployment still requires a separate `git pull` on the machine that runs EvoClaw

## 11. Restart and update model

For internal programmatic restart, EvoClaw prefers the host-managed restart path described in `docs/SELF_UPDATE.md`.

Key rule:

- in-process restart logic uses `os.execv(...)`
- `pm2 restart evoclaw` remains valid for manual operator maintenance

Programmatic update entry points include:

- `/update`
- auto-update loop
- legacy IPC self-update
- restart-only IPC / slash flows

The detailed design and gates live in `docs/SELF_UPDATE.md`.

## 12. Manual deployment workflow

### Host-only deployment

Use when only host-side files changed:

```bash
git pull
pm2 restart evoclaw
```

### Deployment with agent image changes

Use when `container/` changed:

```bash
git pull
docker build -t evoclaw-agent:latest container/
pm2 restart evoclaw
```

Rule:

- if `container/agent-runner/` or `container/Dockerfile` changed, rebuild the image

## 13. Self-update limitations

Current limitation:

- self-update does not automatically rebuild the agent image

Operational consequence:

- a successful self-update may refresh host code while agents still run the old image

Mitigation:

- use changelog notes such as `Image rebuild required: Yes`
- manually run `docker build -t evoclaw-agent:latest container/` when `container/` changes are included

## 14. Practical checklist

Use this as the short version:

1. `git fetch origin main`
2. confirm or open issue
3. branch from latest `main`
4. implement one focused change
5. run verification
6. update changelog / README / focused docs
7. commit
8. push and open PR
9. merge
10. deploy with either host restart only or Docker rebuild + restart

## 15. Related files

- `README.md`
- `docs/SELF_UPDATE.md`
- `docs/CHANGELOG.md`
- `.github/PULL_REQUEST_TEMPLATE.md`
