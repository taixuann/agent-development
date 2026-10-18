# Agent Development

This workspace is a temporary software-development workshop. It owns reusable procedures and isolated worktrees. It owns no product source code.

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

`~/agent-development/worktrees/`

The canonical repository (the product home checkout) owns the product source. A worktree never transfers repository ownership to this workshop. When valid isolation already exists, reuse it instead of creating another worktree. When the task is complete and integrated or pushed, remove only the current task worktree with `git worktree remove`. Leave any dirty worktree in place. Leave any worktree with unpushed commits in place. Leave any other task worktree in place.

## Local instructions

Before changing a product, read that repository's own `AGENTS.md`. Project-specific instructions override generic workshop conventions.

## Specialist skills

Select the narrowest local skill that owns the current operation.
Executor adapters and specialist references can assist that skill without replacing its authority.

- `codebase-design`: define module interfaces and seams.
- `impeccable`: shape UI and design quality.
- `swiftui-expert-skill`: write SwiftUI code for iOS or macOS.
- `playwright-cli`: prove web UI behavior in a real browser.
- `gh-fix-ci`: repair a failed check on an existing PR.
- `gh-address-comments`: address review threads on an open GitHub PR.

Only for UI work, load `impeccable`. Only for SwiftUI work, load `swiftui-expert-skill`.

## Safety

- For substantial work, branch from the verified base.
- For integration, prefer opening a PR for substantial changes. Never silently merge to `main`.
- For shared history, never force-push or rewrite without explicit authorization.
- For review threads, implement only technically valid requests. Make sure that each change passes verification.
- For CI, never disable a required test or check to produce a green result.
- Do not claim completion without fresh verification evidence.

## Scope

This repository is not a product monorepo, a research repository, a documentation knowledge base, a package registry, or the canonical source of any application. Keep project conventions in the project. Keep framework knowledge in the owning specialist skill.

## Agent skills

Before reading or publishing tickets, read [Issue tracker](docs/agents/issue-tracker.md).

Before triage, read [Triage labels](docs/agents/triage-labels.md).

Before exploring terminology or architecture, read [Domain docs](docs/agents/domain.md).
