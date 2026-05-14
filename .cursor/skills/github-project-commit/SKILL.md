---
name: github-project-commit
description: >-
  Creates safe, focused git commits for this repository with pre-checks, clear
  commit messages, and post-commit verification. Use when the user asks to
  commit changes, prepare a commit before opening a PR, or organize local
  changes for GitHub.
disable-model-invocation: true
---

# GitHub Project Commit

## When to apply

- User asks to create a commit in this repository.
- User asks to prepare changes before pushing to GitHub.
- User asks to group work into one or more meaningful commits.

## Goals

- Keep commits focused on one task.
- Avoid committing secrets or unrelated changes.
- Use commit messages that explain intent, not only file changes.

## Commit workflow

1. Inspect current state:
   - `git status --short`
   - `git diff -- .`
   - `git diff --cached -- .`
   - `git log --oneline -n 10`

2. Define commit scope:
   - Include only files related to the user task.
   - If unrelated local changes exist, leave them untouched.
   - Split into multiple commits only when that improves reviewability.

3. Validate before commit:
   - Run targeted checks for changed area (tests/lint) when feasible.
   - Do not stage `.env`, credential files, API keys, or tokens.

4. Stage explicitly:
   - Use path-based add commands (for example: `git add backend/app/...`).
   - Re-check staged set with `git diff --cached --name-only`.

5. Write commit message:
   - Structure: short imperative title + optional body.
   - Explain "why" and effect on behavior.
   - Keep title concise and project-consistent.

6. Commit via HEREDOC:

```bash
git commit -m "$(cat <<'EOF'
<title>

<optional body>
EOF
)"
```

7. Verify result:
   - `git status`
   - `git show --stat --name-only --oneline HEAD`

## Commit message guidelines

- Prefer verbs: `add`, `update`, `fix`, `refactor`, `test`, `docs`.
- Good title examples:
  - `fix: handle missing Afisha city slug fallback`
  - `test: cover specific restaurant ambiguity flow`
  - `refactor: isolate Toka binding lookup logic`
- Add body only when context is not obvious from diff.

## Safety constraints

- Never run destructive git commands unless explicitly requested.
- Never amend commits unless explicitly requested.
- Never push automatically unless user explicitly asked to push.
- If hooks fail, fix issues and create a new commit attempt.

## Output to user

After commit, report:
- Commit hash and title.
- Included files (high level).
- Checks run and their status.
- Whether branch is ahead of remote and next suggested command (`git push` if requested).

### VM update commands (always append)

After the items above, **always** add a short block the user can run **on the VM** to refresh the deployed app. Assume this repository: production-style update is `deploy/update-vm.sh` (git pull `main`, `docker compose -f docker-compose.prod.yml` rebuild/up; DB restore from dump only with `--restore-db`).

Use **placeholders** the user can replace (`USER`, `HOST`, `REPO_DIR`). Do not invent real hosts or paths from the user’s machine unless they were stated in the chat.

Suggested copy-paste shape (adapt wording if the user’s deployment differs, e.g. no Docker):

```text
# On your laptop (if local main is ahead of origin):
git push origin main

# On the VM (SSH, then project root — update-vm.sh pulls main itself):
ssh USER@HOST
cd REPO_DIR
bash deploy/update-vm.sh

# Same script but also restore PostgreSQL from the repo dump:
bash deploy/update-vm.sh --restore-db
```

If the user has no VM or uses another rollout, say so in one line and still give the closest equivalent (e.g. only `docker compose` up) or point to skill `remote-vm-app-updates` for a custom SSH checklist.
