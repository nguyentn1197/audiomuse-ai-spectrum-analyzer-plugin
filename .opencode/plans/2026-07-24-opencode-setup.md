# OpenCode Setup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Set up the audiomuse-ai-spectrum-analyzer-plugin repo for OpenCode by adding bash-permissions config, agent instructions, and updating `.gitignore`.

**Architecture:** Three small deliverables — an `.opencode/opencode.json` (bash allow/ask/deny rules for this Python/DSP project), an `AGENTS.md` (agent conventions for the plugin), and a `.gitignore` update. No code changes, no tests needed.

**Tech Stack:** OpenCode JSON config, Markdown.

---

## File Map

| Action | Path | Purpose |
|--------|------|---------|
| Create | `.opencode/opencode.json` | Bash permission rules (allow read-only git, allow pytest, deny everything else without asking) |
| Create | `AGENTS.md` | Authoritative agent instructions — verbatim copy of `CLAUDE.md` with header updated |
| Modify | `CLAUDE.md` | Replace with a one-line redirect to `AGENTS.md` |
| Modify | `.gitignore` | Suppress OpenCode tool-cache noise |

> **Note:** `.opencode/plans/` already exists (it holds this file).

---

## Task 1: Create `.opencode/opencode.json`

**Files:**
- Create: `.opencode/opencode.json`

- [ ] **Step 1: Create the file**

Write `.opencode/opencode.json` with the following content exactly:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "autoshare": false,
  "permissions": {
    "bash": [
      { "permission": "allow", "pattern": "git status*" },
      { "permission": "allow", "pattern": "git diff*" },
      { "permission": "allow", "pattern": "git log*" },
      { "permission": "allow", "pattern": "git show*" },
      { "permission": "allow", "pattern": "git branch*" },
      { "permission": "allow", "pattern": "git remote*" },
      { "permission": "allow", "pattern": "git stash list*" },
      { "permission": "ask",   "pattern": "git add*" },
      { "permission": "ask",   "pattern": "git commit*" },
      { "permission": "ask",   "pattern": "git stash*" },
      { "permission": "ask",   "pattern": "git checkout*" },
      { "permission": "ask",   "pattern": "git switch*" },
      { "permission": "deny",  "pattern": "git push*" },
      { "permission": "deny",  "pattern": "git push --force*" },
      { "permission": "allow", "pattern": "python3 -m unittest*" },
      { "permission": "allow", "pattern": "python3 tests/run_verdicts.py*" },
      { "permission": "allow", "pattern": "python3 tests/generate_adversarial_fixtures.py*" },
      { "permission": "allow", "pattern": "python3 -c \"import*" },
      { "permission": "allow", "pattern": "cat *" },
      { "permission": "allow", "pattern": "head *" },
      { "permission": "allow", "pattern": "tail *" },
      { "permission": "allow", "pattern": "grep *" },
      { "permission": "allow", "pattern": "rg *" },
      { "permission": "allow", "pattern": "ls *" },
      { "permission": "allow", "pattern": "find *" },
      { "permission": "allow", "pattern": "ffprobe *" },
      { "permission": "allow", "pattern": "ffmpeg -i *" },
      { "permission": "ask",   "pattern": "ffmpeg *" },
      { "permission": "ask",   "pattern": "./build.sh*" },
      { "permission": "ask",   "pattern": "pip install*" },
      { "permission": "ask",   "pattern": "pip3 install*" },
      { "permission": "deny",  "pattern": "*" }
    ]
  }
}
```

**Rationale for each group:**
- **Git read-only → allow:** `git status/diff/log/show/branch/remote/stash list` — safe inspection; no state mutation.
- **Git write → ask:** `add/commit/stash/checkout/switch` — mutate working tree or history; require confirmation.
- **`git push` → deny:** Publishing is irreversible; must be done manually or explicitly unlocked.
- **Python test/run → allow:** `python3 -m unittest discover tests -v` and `run_verdicts.py` are the standard dev loop; frequent, safe.
- **`generate_adversarial_fixtures.py` → allow:** Reads only committed files + numpy/soundfile; safe to run unattended.
- **Shell inspection → allow:** `cat/head/tail/grep/rg/ls/find` are read-only; needed constantly.
- **`ffprobe` → allow:** Read-only metadata probe; no output written.
- **`ffmpeg -i` → allow:** The `-i`-only form is the probe/info invocation; no decode output.
- **`ffmpeg *` (other) → ask:** ffmpeg can transcode large files; ask before spending CPU/disk unexpectedly.
- **`./build.sh` → ask:** Rewrites `dist/` and potentially `plugin.json`; confirm before running.
- **`pip install` → ask:** Mutates the environment; require confirmation.
- **Catch-all → deny:** Anything not listed above is denied by default.

- [ ] **Step 2: Verify the file is valid JSON**

Run:
```
python3 -c "import json, sys; json.load(open('.opencode/opencode.json')); print('OK')"
```
Expected output: `OK`

---

## Task 2: Create `AGENTS.md` and update `CLAUDE.md`

**Files:**
- Create: `AGENTS.md` (repo root) — verbatim copy of `CLAUDE.md`, with the title line updated
- Modify: `CLAUDE.md` — replace all content with a one-line redirect

The goal is a single source of truth that all agent toolchains (OpenCode via `AGENTS.md`, Claude Code via `CLAUDE.md`) can find. `AGENTS.md` becomes the canonical document; `CLAUDE.md` simply points to it.

- [ ] **Step 1: Create `AGENTS.md`**

Copy the full content of `CLAUDE.md` into `AGENTS.md`, replacing only the opening title line. The new file must start with:

```markdown
# AGENTS.md
```

…followed by the rest of `CLAUDE.md` verbatim (starting from the line `This file provides guidance to Claude Code...`).

The quickest way:

```bash
cp CLAUDE.md AGENTS.md
```

Then open `AGENTS.md` and change line 1 from:

```
# CLAUDE.md
```

to:

```
# AGENTS.md
```

Everything else stays byte-for-byte identical to `CLAUDE.md`.

- [ ] **Step 2: Verify `AGENTS.md` has the full content**

```bash
wc -l AGENTS.md CLAUDE.md
```

Both files should report the same line count. Also spot-check the title:

```bash
head -1 AGENTS.md
```

Expected: `# AGENTS.md`

- [ ] **Step 3: Replace `CLAUDE.md` with a redirect**

Overwrite `CLAUDE.md` with exactly this content (3 lines):

```markdown
# CLAUDE.md

See [AGENTS.md](AGENTS.md) for all project guidance and agent instructions.
```

This keeps `CLAUDE.md` present (so Claude Code finds it) while making `AGENTS.md` the single source of truth. Do **not** delete `CLAUDE.md` — Claude Code requires it at the repo root.

- [ ] **Step 4: Verify `CLAUDE.md` is now short**

```bash
wc -l CLAUDE.md
```

Expected: `3` (or `4` if your editor adds a trailing newline — both are fine)

```bash
cat CLAUDE.md
```

Expected output:
```
# CLAUDE.md

See [AGENTS.md](AGENTS.md) for all project guidance and agent instructions.
```

---

## Task 3: Update `.gitignore`

**Files:**
- Modify: `.gitignore`

Current content:
```
# Python
__pycache__/
*.py[cod]

# Local Claude Code settings (machine-specific)
.claude/settings.local.json

# Editors / OS
.DS_Store
.idea/
.vscode/
```

- [ ] **Step 1: Add OpenCode entries**

Append the following section to `.gitignore`:

```
# OpenCode tool cache (machine-specific, auto-generated)
.opencode/node_modules/
.opencode/package-lock.json
```

The `.opencode/opencode.json` and `.opencode/plans/` **should** be committed — they are project-level config and shared plans, not machine-specific.

- [ ] **Step 2: Commit all changes together**

```bash
git add .opencode/opencode.json .opencode/plans/2026-07-24-opencode-setup.md AGENTS.md CLAUDE.md .gitignore
git commit -m "chore: set up OpenCode config, AGENTS.md as single source of truth, redirect CLAUDE.md"
```

---

## Self-review

| Requirement | Covered by |
|-------------|-----------|
| `.opencode/opencode.json` with bash permissions | Task 1 |
| Python test runner allowed without prompting | Task 1 — `python3 -m unittest*` and `run_verdicts.py*` → allow |
| Git push denied | Task 1 — explicit deny before catch-all |
| `AGENTS.md` contains full project guidance | Task 2, Step 1 — verbatim copy of `CLAUDE.md` |
| `CLAUDE.md` redirects to `AGENTS.md` | Task 2, Step 3 — 3-line redirect file |
| Single source of truth for all agent toolchains | Task 2 — both `AGENTS.md` (OpenCode) and `CLAUDE.md` (Claude Code) point to same content |
| `.gitignore` updated | Task 3 |
| `.opencode/plans/` committed (not ignored) | Task 3 — only `node_modules` and `package-lock.json` ignored |

No placeholders. No TODOs. All steps are concrete and self-contained.
