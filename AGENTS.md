# Agent Development

This workspace is a temporary software-development workshop. It owns reusable procedures and isolated worktrees. It owns no product source code.

## Methodology

Use the harness plugin as the default software-development methodology. It owns isolation, planning, test-first implementation, debugging, verification, review, and branch integration.

## Worktrees

For substantial change-making work, prefer isolated Git worktrees under:

`/Users/tai/agent-development/worktrees/`

The canonical repository (the product home checkout) owns the product source. A worktree never transfers repository ownership to this workshop. When valid isolation already exists, reuse it instead of creating another worktree. When the task is complete and integrated or pushed, remove only the current task worktree with `git worktree remove`. Leave any dirty worktree in place. Leave any worktree with unpushed commits in place. Leave any other task worktree in place.

## Local instructions

Before changing a product, read that repository's own `AGENTS.md`. Project-specific instructions override generic workshop conventions.

## Specialist skills

Use the narrowest local skill that owns the current operation. Use one skill per operation.

- `codebase-design`: define module interfaces and seams.
- `prototype`: answer an open design question with throwaway code.
- `impeccable`: shape UI and design quality.
- `swiftui-expert-skill`: write SwiftUI code for iOS or macOS.
- `playwright-cli`: prove web UI behavior in a real browser.
- `gh-fix-ci`: repair a failed check on an existing PR.
- `gh-address-comments`: address review threads on an existing PR.

Only when requirements are unclear, load `prototype`. Only for UI work, load `impeccable`. Only for SwiftUI work, load `swiftui-expert-skill`.

## Safety

- For substantial work, branch from the verified base.
- For integration, prefer opening a PR for substantial changes. Never silently merge to `main`.
- For shared history, never force-push or rewrite without explicit authorization.
- For review threads, implement only technically valid requests. Make sure that each change passes verification.
- For CI, never disable a required test or check to produce a green result.
- Do not claim completion without fresh verification evidence.

## Scope

This repository is not a product monorepo, a research repository, a documentation knowledge base, a package registry, or the canonical source of any application. Keep project conventions in the project. Keep framework knowledge in the owning specialist skill.
