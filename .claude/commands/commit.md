Commit and push all pending changes across both repos in this project.

## Repos
- `C:\github\horizon-monitor` — main repo, pushes to `origin main`
- `C:\github\horizon-mcp` — MCP server dependency, pushes to `origin main`

## Steps

1. In each repo, run `git status` and `git diff` in parallel to understand what changed.

2. For **horizon-monitor**: if there are unstaged or untracked changes:
   - Stage all modified tracked files plus any new files in `src/`, root `*.py`, `*.toml` example files, `*.bat`, `requirements.txt`, `.gitignore`, `.claude/commands/`
   - Never stage: `.env`, `config.toml`, `data/`, `.venv/`, `secrets/`, `*.local*`
   - Write a concise commit message (imperative mood, ≤72 chars subject) based on the actual diff
   - Commit with `Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>`
   - Push to `origin main`

3. For **horizon-mcp**: if there are changes:
   - Stage only `src/index.ts` and `dist/index.js`
   - Write a commit message based on the diff
   - Commit with `Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>`
   - Push to `origin main`

4. Report: what was committed in each repo (or "nothing to commit" if clean).

## Safety rules
- If `.env` or `config.toml` (non-example) would be staged, abort and warn.
- If either push fails, show the error and stop — do not force-push.
