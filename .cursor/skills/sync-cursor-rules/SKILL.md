---
name: sync-cursor-rules
description: >-
  Analyzes git changes against `.cursor/rules/*.mdc` and proposes minimal updates
  so project rules stay aligned with the codebase. Use when the user asks to
  sync or refresh Cursor rules, after a large refactor, when stack or run
  commands change, or when integrating a new subsystem (APIs, adapters).
---

# Sync Cursor Rules

## When to apply

- User explicitly asks to sync / refresh / align Cursor rules with the project.
- User finished a feature that changes architecture, entrypoints, stack, or intentional stubs (e.g. Toka search).
- User mentions drift between `.cursor/rules` and actual code.

## What not to do

- Do not silently delete or weaken warnings about **intentional stubs**, **MVP limitations**, or **placeholder** behavior unless `git diff` (or read files) shows the code no longer matches that story.
- Do not paste large chunks of README into rules; prefer one-line pointers (`see README.md`).
- Do not balloon rule files; prefer a **new focused `.mdc`** with `globs` over one giant file.
- Prefer **proposing** edits; apply them only if the user asked to change files.

## Procedure

1. **Scope**
   - If the user gave a branch or commit range, use it. Otherwise use unstaged + staged changes, or `git diff main...HEAD` / `git diff origin/main...HEAD` when on a feature branch (explain which range was used).

2. **Inventory code changes**
   - List paths and nature of changes (new modules, deleted routes, renamed dirs, env vars, new external services).

3. **Inventory rules**
   - Read `.cursor/rules/*.mdc` and note `alwaysApply` and `globs` for each.

4. **Gap analysis**
   - For each significant code change, ask: *Would a new chat without memory misunderstand this?*
   - Match: new/changed areas might need a new `.mdc` or an update to `restaurant-assistant.mdc` if global (run commands, stack, product goal).

5. **Proposed edits**
   - For each proposed change: target file, 1–2 sentence rationale, **minimal** diff (add/remove a short paragraph or bullet).
   - If only a **code comment** near a stub is enough, say so and skip rule churn.

6. **Output format for the user**
   - Summary table: *Code area* → *Rule affected* → *Action* (update / add file / none).
   - Then concrete suggested wording or patches.

## Project anchors (restaurant-assistant)

- Rules live under `.cursor/rules/`; global context in `restaurant-assistant.mdc` (`alwaysApply: true`).
- Toka restaurant search stubs: `backend/app/routers/restaurants.py` — do not "fix" search semantics in rules if code is still stubbed unless the code was actually changed to real search.

## Optional: end with a checklist

Ask whether the user wants these updates applied now, or only the written proposal for manual edit.
