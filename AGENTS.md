# Development Space

This workspace is a software-development workshop. It owns reusable engineering
procedures and isolated worktrees. It owns no product source code.

## Repository boundaries

Each tool is an independent Git repository at `~/.agents/tools/<tool-name>/`.
That repository owns its callable behavior, tests, and tool-specific automation.
The Admin repository ignores the whole tools subtree.

This workshop hosts linked worktrees of those repositories. A worktree never
transfers repository ownership to the workshop.
Product and research repositories stay at their own paths.

When a tool's stable invocation, inputs, outputs, and failure behavior must be
defined, read [Small-tool contract](docs/small-tool-contract.md).

## Methodology

Use the installed engineering skills for the current operation.
Read the selected skill before execution.

For agreed work, use `superpowers:subagent-driven-development`.
For a spec with dependent tickets, use `superpowers:writing-plans` then `superpowers:executing-plans`.
For test-first implementation, use `superpowers:test-driven-development`.
For bugs, use `superpowers:systematic-debugging`.
For review, use `superpowers:receiving-code-review`.

Use Superpowers when the user selects it or the owning project requires it.
Before execution, resolve conflicts between the selected skill and project instructions.
Keep publication and integration within the user's authorized scope.

## Worktrees

For substantial change-making work, prefer isolated Git worktrees under:

`~/development-space/worktrees/`

For work requested by Admin, use
`~/development-space/worktrees/admin/<tool-name>_<request-id>/` on branch
`admin/<tool-name>_<request-id>` in the tool repository.
Example: `worktrees/admin/opencode-free-router_008` on `admin/opencode-free-router_008`.

The canonical checkout owns the source. A worktree never transfers repository
ownership to this workshop. When valid isolation already exists, reuse it
instead of creating another worktree. When the task is complete and integrated
or pushed, remove only the current task worktree with `git worktree remove`.
Leave any dirty worktree in place. Leave any worktree with unpushed commits in
place. Leave any other task worktree in place.

Scheduled jobs call the stable canonical checkout, never a temporary worktree.

## Local instructions

Before changing a product, read that repository's own `AGENTS.md`.
Project-specific instructions override generic workshop conventions.
Tool work also reads the tool repository's `AGENTS.md` and any nested contract
in the changed folder.

## Specialist skills

Select the narrowest local skill that owns the current operation.
Executor adapters and specialist references can assist that skill without
replacing its authority.

- `codebase-design`: define module interfaces and seams.
- `impeccable`: shape UI and design quality.
- `swiftui-expert-skill`: write SwiftUI code for iOS or macOS.
- `playwright-cli`: prove web UI behavior in a real browser.
- `gh-fix-ci`: repair a failed check on an existing PR.
- `gh-address-comments`: address review threads on an open GitHub PR.
- `cli-developer`: design commands, flags, help, exit codes, and non-interactive paths.

Only for UI work, load `impeccable`. Only for SwiftUI work, load `swiftui-expert-skill`.
Only for command-line behavior contracts, load `cli-developer`.

## Safety

- For substantial work, branch from the verified base.
- For integration, prefer opening a PR for substantial changes. Never silently merge to `main`.
- For shared history, never force-push or rewrite without explicit authorization.
- For review threads, implement only technically valid requests. Make sure that each change passes verification.
- For CI, never disable a required test or check to produce a green result.
- Do not claim completion without fresh verification evidence.

## Scope

This repository is not a product monorepo, a research repository, a documentation knowledge base, a package registry, or the canonical source of any application or tool. Keep project conventions in the project. Keep framework knowledge in the owning specialist skill.

## Agent skills

Before reading or publishing tickets, read [Issue tracker](docs/agents/issue-tracker.md).

Before triage, read [Triage labels](docs/agents/triage-labels.md).

Before exploring terminology or architecture, read [Domain docs](docs/agents/domain.md).
