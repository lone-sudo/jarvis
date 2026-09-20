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
jarvis write-file --task TSK-XXXXXXXX path/to/file.py --content-file local_draft.py
jarvis git-commit --task TSK-XXXXXXXX --project structural-rcc-suite --message "Fix beam calc"

# V1: dispatch a prompt to your existing ChatGPT/Claude/Gemini subscription
# (copies to clipboard, waits for you to paste the response back — no API, no cost)
jarvis ask-ai --task TSK-XXXXXXXX --provider Claude_Web "Explain this stack trace: ..."
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
