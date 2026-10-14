# DECISIONS — archived: VDI data-transfer experiments (parked, not a current priority)

This file used to be a long, session-by-session record of getting a mock project's files
out of the lab VDI via a redirected-printer/base64 channel (`pull`, `pull-project`,
`pull-payload`). That effort is **parked** — it was a side exploration in this personal
lab (mock projects only), not the project's goal, and keeping ~450 lines of blow-by-blow
debugging history around just confused later sessions into treating it as current work.

**Current goal of this repo:** monitor the Horizon VDI session and notify the user when a
monitoring event happens that needs human attention. See `CLAUDE.md`.

The code for the parked experiments (`src/pull.py`, `src/pull_payload.py`,
`src/remote_tree.py`, `src/remote_scripts/`, their tests, and the `pull`/`pull-project`/
`pull-payload` CLI commands + Pull/Push UI tabs) is left in place and still works as
last verified — it's just not where development attention should go. Pick it back up only
if asked to.

## What's worth keeping from that work

- `src/remote_tree.py` — remote file-tree discovery; transport-agnostic, reusable for
  anything that needs to enumerate files in the VDI.
- The corroboration / abort-on-ambiguity safety pattern in `src/pull.py` — driving a GUI
  open-loop (no window handles, no return codes, state inferred from OCR) is inherently
  unreliable, and that pattern is what kept failures *loud* (zero silently-wrong files)
  instead of silent.

## Meta-lesson (generic, still applies)

When an approach fails repeatedly and each fix reveals another failure of the same kind,
stop and question the approach — don't ship the next patch. Open-loop GUI automation and
lossy pixel-based data transport both have unbounded failure surfaces; name that early
instead of after the third day.
