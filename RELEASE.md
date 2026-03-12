# EvoClaw Release Procedure

This document outlines the standard procedure for releasing new versions of EvoClaw.

## Pre-release Checklist

- [ ] All issues for the milestone are closed or moved.
- [ ] `CHANGELOG.md` is updated with the latest changes.
- [ ] `README.md` is updated with the new version number and release notes.
- [ ] All tests pass locally and in CI (if available).
- [ ] Docker image is built and tagged correctly (e.g., `evoclaw-agent:v1.10.23`).
- [ ] Docker image is pushed to the registry (if applicable).

## Release Steps

1. **Create a Release Branch**:
   ```bash
   git checkout -b release/v1.10.23
   ```

2. **Update Version Numbers**:
   - Update version in `README.md`, `CHANGELOG.md`, and any other relevant files.

3. **Create a Pull Request**:
   - Title: `Release v1.10.23`
   - Description: Summarize changes, link to issues, and list breaking changes.

4. **Review and Merge**:
   - Ensure at least one approval from a maintainer.
   - Merge the PR into `main`.

5. **Create a Git Tag**:
   ```bash
   git tag -a v1.10.23 -m "Release v1.10.23"
   git push origin v1.10.23
   ```

6. **Create a GitHub Release**:
   - Go to the "Releases" page.
   - Click "Draft a new release".
   - Select the tag `v1.10.23`.
   - Copy the changelog entry into the release description.
   - Publish the release.

7. **Post-release**:
   - Deploy the new Docker image to production (if applicable).
   - Announce the release on relevant channels (e.g., Telegram, Discord).

## Hotfix Procedure

For critical bug fixes:
1. Create a branch from the latest release tag (e.g., `hotfix/v1.10.24`).
2. Apply the fix.
3. Follow the release steps above, incrementing the patch version.
