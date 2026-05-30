Commit and push pending changes in the **horizon-monitor** repo only.

This repo and `C:\github\horizon-mcp` are committed separately and on purpose. This skill
NEVER touches horizon-mcp — use the horizon-mcp repo's own commit skill for that.

## Repo
- `C:\github\horizon-monitor` — pushes to `origin main`

## Steps

1. Run `git status` and `git diff` to understand what changed.
2. If there are unstaged or untracked changes:
   - Stage modified tracked files plus any new files in `src/`, root `*.py`, `*.toml`
     example files, `*.md`, `*.bat`, `requirements.txt`, `.gitignore`, `.claude/commands/`
   - Never stage: `.env`, `config.toml`, `data/`, `.venv/`, `secrets/`, `*.local*`
   - Write a concise commit message (imperative mood, ≤72-char subject) from the actual diff
   - Commit with a `Co-Authored-By: Claude <model> <noreply@anthropic.com>` trailer naming
     the model actually running this session (e.g. `Claude Opus 4.8 (1M context)`)
   - Push to `origin main`
3. Report what was committed (or "nothing to commit" if clean).

## Safety rules
- If `.env` or a non-example `config.toml` would be staged, abort and warn.
- If the push fails, show the error and stop — do not force-push.
- Do not run any git command against `C:\github\horizon-mcp`.
