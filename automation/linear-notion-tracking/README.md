# Linear → Notion Issue Mirror

One-way mirror that synchronizes Linear issues into an existing Notion database (`Linear Tracking`).

- **Linear source**: Workspace `tai-xuann`, Team `Development Space` (`TAI`, ID: `6359be10-ea15-4972-80bf-7eac98a64835`).
- **Notion destination**: Database `Linear Tracking` (`4000a859-ed1f-8051-b991-f76de0549b2b`).
- **Architecture**: Zero-cost, 100% Python standard library, uses local authentication tokens from `linear` and `ntn` CLIs.

---

## Mappings

| Notion Property | Type | Linear Source / Mapping Rule |
|---|---|---|
| **Issues** | Title | Issue title |
| **Linear ID** | Rich Text | Issue identifier (e.g. `TAI-78`) |
| **Linear URL** | URL | Issue web URL |
| **Project** | Select | Project name |
| **Status** | Status | Mapped status (`Backlog`, `Todo`, `In Progress`, `In Review`, `Done`, `Canceled`, `Duplicate`) |
| **State Machine** | Select | Labels prefixed with `agent:` or `human:` matching the 10 state options (cleared if removed) |
| **Infrastructure Type** | Select | `Build`, `Refactor`, `Integration`, `Evaluation`, `Migration`, `Maintenance` (cleared if removed) |
| **Priority** | Select | `Urgent` (1), `High` (2), `Medium` (3), `Low` (4), `No priority` (0) |
| **Labels** | Multi-select | Remaining general labels |
| **Last Synced** | Date | ISO timestamp of successful sync |

---

## Safety Guarantees

1. **One-way sync**: Never modifies Linear from Notion.
2. **Body preservation**: Only updates Notion page properties; never overwrites page body, content blocks, or user notes.
3. **Idempotent**: Matches on stable `Linear ID`. If fields have not changed, Notion API is not called.
4. **Zero credential leakage**: Obtains tokens dynamically via `linear auth token` and `ntn auth token` (or `LINEAR_API_KEY` / `NOTION_API_TOKEN` environment variables).

---

## Project Registry (`projects.json`)

Only issues belonging to registered active projects in `projects.json` are synchronized:

```json
{
  "projects": {
    "Raw Viewer": {
      "linear_name": "Raw Viewer",
      "canonical_path": "/Users/tai/research-projects/tools/raw-viewer",
      "workshop_path": "/Users/tai/agent-development",
      "worktrees_dir": "/Users/tai/agent-development/worktrees",
      "tech_stack": "swift-macos",
      "models": {
        "context": "gpt-6.1-sol",
        "planning": "gpt-6.1-sol",
        "executing": "deepseek-v4.1-flash"
      },
      "active": true
    }
  }
}
```

---

## Forge State Machine Driver (`forge_driver.py`)

Autonomous orchestrator implementing the **Forge / Superpowers** contract:

1. **Zero Idle Overhead**: Scans for `agent:*` labels. If none found, exits in <1s.
2. **Phase Routing & Model Assignment**:
   - `agent:context`: Codex (`gpt-6.1-sol`) on canonical repo (read-only). Analyzes codebase knowledge graph via Graphify, checks for team issue overlaps, and scans for in-flight worktree conflicts.
   - `agent:planning`: Codex (`gpt-6.1-sol`). Provisions isolated Git worktree (`~/agent-development/worktrees/<ident>/`), drafts TDD plan into `docs/plans/`, runs Decision Engine / `/audit-writing-plans` quality audit, certifies code seams via Graphify AST, commits to branch `forge/<ident>`, pushes to GitHub (no PR), and posts concise review link to Linear.
   - `agent:executing`: OpenCode Free Router in isolated git worktree with pre/post canonical isolation assertions and automated test gates.
3. **Execution Trail in Notion**: Records `Phase`, `Engine & Model`, `Session ID`, `Worktree & Branch`, `Base Commit`, and `Gate / Status` directly in the Notion page body under `## Agent Execution Trail` without mutating database schema.
4. **Human Review Gates**: Upon phase completion, posts a structured comment to Linear, transitions to matching `human:*` review gate (`human:context-review`, `human:plan-review`, `human:result-review`), and **STOPS** for human review.

```bash
# Preview what forge_driver would do without modifying anything
./forge_driver.py --dry-run

# Test drive a specific issue
./forge_driver.py --issue TAI-78 --dry-run
```

---

## Usage

```bash
# Preview changes without modifying Notion (dry-run)
./sync.py --dry-run

# Sync all issues in Team TAI
./sync.py

# Sync a specific issue
./sync.py --issue TAI-78

# Force update all fields even if unchanged
./sync.py --force

# Sync / refresh page body markdown from Linear description
./sync.py --sync-content

# Run via wrapper script with network check & logging
./run-sync.sh
```

---

## Automation (macOS launchd)

The sync is configured to run automatically at **00:00 every day** via `launchd`:

- **Plist**: `~/Library/LaunchAgents/com.tai.linear-notion-sync.plist`
- **Runner**: `assets/linear-notion-tracking/run-sync.sh`
- **Logs**: `assets/linear-notion-tracking/logs/launchd.stdout.log`

```bash
# Check service status
launchctl list | grep linear-notion-sync

# Unload / reload service
launchctl unload ~/Library/LaunchAgents/com.tai.linear-notion-sync.plist
launchctl load ~/Library/LaunchAgents/com.tai.linear-notion-sync.plist
```
