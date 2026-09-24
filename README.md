# Jarvis — Personal AI Operating Layer

Prototype Milestone 001. See `docs/adrs/` for frozen architecture decisions and
`docs/build-logs/` for the implementation journey.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -e .
```

## First run

```bash
# Point Jarvis at a workspace (optional; defaults to ~/jarvis-workspace)
set JARVIS_WORKSPACE_ROOT=C:\Users\you\jarvis-workspace     # Windows (cmd)
$env:JARVIS_WORKSPACE_ROOT = "C:\Users\you\jarvis-workspace" # Windows (PowerShell)

# Register a project you already have under the workspace root
jarvis init-project structural-rcc-suite structural-rcc-suite --name "Structural RCC Suite"

# Start working
jarvis new-session "Fix beam calculation bug"
jarvis new-task --session SES-XXXXXXXX --project structural-rcc-suite --title "Fix beam calc"
jarvis note structural-rcc-suite "Found root cause: unhandled None in load_factor()"

# Later, in a new terminal / after a reboot:
jarvis resume

# V1: confirmed writes (shows a diff, asks [y/N] before touching disk)
# --content-file must itself be inside the workspace (policy-checked before it's read)
jarvis write-file --task TSK-XXXXXXXX path/to/file.py --content-file structural-rcc-suite/local_draft.py
jarvis git-commit --task TSK-XXXXXXXX --project structural-rcc-suite --message "Fix beam calc"

# V1: dispatch a prompt to your existing ChatGPT/Claude/Gemini subscription
# (copies to clipboard, waits for you to paste the response back — no API, no cost)
jarvis ask-ai --task TSK-XXXXXXXX --provider Claude_Web "Explain this stack trace: ..."

# V2: content inbox (manual capture only — Jarvis never fetches a URL on its own)
jarvis save --url "https://example.com/some-article" --title "Article title" --note "why you saved it"
jarvis save --text "raw text or a transcript you pasted in yourself" --title "quick note"
jarvis inbox-process --provider Claude_Web   # classifies UNPROCESSED items via manual paste round-trip
jarvis inbox                                  # list everything
jarvis inbox --status PROCESSED               # list just what's been classified
jarvis inbox-link INB-XXXXXXXX data-engineering  # the ONLY way an item gets linked to a project
jarvis inbox-archive INB-XXXXXXXX

# V3: controlled URL fetch (deny-by-default — nothing is fetchable until allowlisted)
jarvis network-list                          # see what's allowed (starts empty)
jarvis network-allow example.com             # explicitly allow a domain
jarvis save --url "https://example.com/article" --title "..." --fetch  # single HTTPS request, confirmed, bounded
# plain `jarvis save --url ...` WITHOUT --fetch never makes a network request — that stays the default

# V2 completion: consolidation (Jaccard tag similarity, preview-first, manual only)
jarvis inbox-consolidate
# Finds clusters of PROCESSED items sharing enough tags (Jaccard >= 0.5), shows a preview
# and proposed merged note per cluster, and asks [y/N] before writing anything.
# - Items confirmed-linked to the SAME project -> merged note written to that project's memory
# - Unlinked items (including anything with only a suggested_project_key) -> merged into a
#   new plain inbox item instead — an AI's suggestion never grants write access to project memory
# - Originals are archived (never deleted) only AFTER a successful write — a failed write
#   leaves the originals untouched
```

## Tests

```bash
pip install pytest
pytest tests/ -v
```

## Layout

```
src/jarvis/
├── core/         # (not yet built — orchestration)
├── state/        # SQLite: sessions, tasks, executions, checkpoints, projects
├── policy/       # $0 spend rule, workspace boundary, action tiers — gates everything
├── memory/       # Markdown per-project notes
├── tools/        # Read-only git + filesystem inspection, policy-gated
├── providers/    # Manual clipboard-based AI dispatch (no API, no cost)
└── interface/    # CLI
```
