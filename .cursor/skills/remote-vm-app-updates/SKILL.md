---
name: remote-vm-app-updates
description: Safely installs application updates on a remote virtual machine over SSH with prechecks, backup/rollback, deployment, smoke tests, and post-deploy verification. Use when the user asks to update an app on a remote VM, deploy a new build to a server, restart services after release, or run hotfix rollout over SSH.
---

# Remote VM App Updates

## When to Use

Use this skill when the task includes:
- updating an application on a remote VM;
- deploying a new build via SSH;
- restarting or reloading backend services after release;
- applying a hotfix with minimal downtime.

If the user asks only for a plan, provide the plan first and do not execute commands until they confirm.

## Required Inputs

Collect these before running commands:
- SSH target: `user@host`, port, and auth method;
- deployment path and service name (`systemd` unit, docker compose service, etc.);
- artifact source (git branch/tag, image tag, archive URL/path);
- health endpoint or smoke-test command;
- rollback target (previous release path, previous tag/image, or backup archive).

If any required input is missing, ask concise clarifying questions.

## Safety Rules

1. Never run destructive commands without explicit confirmation (for example, data migrations with irreversible changes).
2. Keep rollback assets before switching to the new version.
3. Prefer idempotent commands and explicit paths.
4. Stop after failed health checks; do rollback before further retries.

## Standard Workflow

Copy this checklist and update status as you work:

```text
Update Progress:
- [ ] 1) Confirm scope and maintenance constraints
- [ ] 2) SSH connectivity and environment prechecks
- [ ] 3) Create backup/rollback point
- [ ] 4) Upload/pull new artifact
- [ ] 5) Install/switch release
- [ ] 6) Restart/reload service
- [ ] 7) Run smoke tests and health checks
- [ ] 8) Roll back if needed, otherwise finalize
```

### 1) Confirm Scope

Confirm:
- expected version/build;
- allowed downtime window;
- whether DB migrations are required;
- success criteria.

### 2) Prechecks on VM

Run minimally:

```bash
ssh user@host "set -e; uname -a; whoami; date; df -h; free -m"
```

Validate:
- enough disk space;
- service currently healthy before change;
- config files and secrets are present.

### 3) Backup / Rollback Point

Use one of:
- symlink-based releases: keep current symlink target as rollback;
- container deployment: capture current image tag and compose file;
- in-place deploy: archive current app directory.

Example:

```bash
ssh user@host "set -e; ts=\$(date +%Y%m%d-%H%M%S); tar -czf /var/backups/app-\$ts.tgz /opt/app"
```

### 4) Deliver Artifact

Preferred order:
1. Pull immutable tag/commit on VM.
2. Or upload built artifact with checksum validation.

Examples:

```bash
ssh user@host "set -e; cd /opt/app && git fetch --all --tags && git checkout <tag-or-commit>"
```

```bash
scp ./build.tar.gz user@host:/tmp/build.tar.gz
ssh user@host "set -e; sha256sum /tmp/build.tar.gz"
```

### 5) Install / Switch Release

Prefer atomic switch (release dir + symlink):

```bash
ssh user@host "set -e; mkdir -p /opt/releases/<release-id>; tar -xzf /tmp/build.tar.gz -C /opt/releases/<release-id>; ln -sfn /opt/releases/<release-id> /opt/app/current"
```

### 6) Restart / Reload

Use the platform-appropriate command:

```bash
ssh user@host "sudo systemctl daemon-reload || true; sudo systemctl restart <service>; sudo systemctl --no-pager --full status <service>"
```

For compose-based apps:

```bash
ssh user@host "set -e; cd /opt/app/current; docker compose pull && docker compose up -d"
```

### 7) Verify

Run both technical and functional checks:
- process/service status;
- health endpoint (`curl -f`);
- key user flow smoke test;
- logs scan for new errors.

Example:

```bash
ssh user@host "set -e; curl -fsS http://localhost:<port>/health"
```

### 8) Rollback Decision

If any critical check fails:
1. switch back to previous release/image;
2. restart service;
3. re-run health checks;
4. report root cause hypothesis and next safe action.

## Response Format to User

When finishing, provide:
- what was updated (host, app, version/tag);
- commands/actions performed at high level;
- verification results;
- rollback status (not needed / executed);
- recommended next steps (monitoring window, follow-up fixes).

## Escalation / Stop Conditions

Stop and ask user before proceeding if:
- SSH auth fails repeatedly;
- host fingerprint mismatch appears;
- unexpected production drift is detected;
- rollback path is unavailable;
- migration risk is unclear.
