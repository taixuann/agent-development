# Small-tool contract

This page defines the minimum behavior a tool must document and test before
Admin automation calls it. Each tool stays an independent repository. The
contract describes its public surface, not its internal structure.

## Layout

```text
<tool-name>/
  README.md            # Human runbook: purpose, invocation, recovery
  AGENTS.md            # Agent boundaries for this repository
  backend/             # CLI entrypoint and implementation
  tests/               # Behavior checks
  docs/                # Supporting docs when needed
  automation/          # Tool-owned jobs, when the tool has one
  pyproject.toml       # Or the language-native manifest when needed
```

A one-file tool may keep one entrypoint in `backend/`. Add modules only when
responsibility requires them. Existing package conventions take precedence when
a new wrapper would add indirection. Do not create an empty folder to satisfy the
tree.

## Required documentation

Record these in the tool's `README.md`:

- Stable invocation and how a scheduled job reaches it by absolute path.
- Configuration source and precedence.
- Inputs, outputs, and exit codes.
- Timeout and interruption behavior.
- Rerun semantics: whether a repeat call is safe, and how a partial failure is
  recovered.
- State location, and which parts are credentials or mutable runtime state.
- The last valid state preserved when an update fails.

## Required behavior

- `--help` works and a non-interactive path exists for every operation a
  scheduler calls. No prompt may block an unattended run.
- Structured results go to stdout for automation. Diagnostics go to stderr.
- An invalid input exits nonzero with a stated reason and changes no state.
- A failed update preserves the prior valid state.
- Concurrent calls cannot corrupt shared state. Use one lock per mutable resource.
- Credentials and mutable runtime state stay outside tracked source.

Tests cover success, invalid input, and the main failure path. Add a case when a
new failure mode becomes reachable.

## Branch and worktree for Admin-origin work

| Field | Value |
| --- | --- |
| Branch | `admin/<tool-name>_<request-id>` |
| Worktree | `~/development-space/worktrees/admin/<tool-name>_<request-id>/` |
| Automation branch | Same tool task branch, under the tool repository's `automation/<job-name>/` |

The request ID identifies Admin intent and acceptance criteria. It is not the
implementation plan number. Link derived design, implementation plans, and
verification evidence back to the request.

A job that coordinates several independently owned tools, or that applies Space
policy, lives in Admin under `automation/<job-name>/` on branch
`automation/<job-name>`. A shared incident reporter dependency alone does not
move a dedicated job out of its tool repository.

## Verification before integration

Run the tool's own tests in the worktree with temporary state and report roots.
Keep the canonical checkout unchanged while the work is in review. Record:

- Exact base and final revision, or the uncommitted diff.
- Changed-file summary.
- Commands run and their observed results.
- Unresolved issues.

Integration into the canonical checkout and any automation restart need their
own authorization. Verify the automation consumer after an authorized
integration changes the tool.

## Out of scope by default

Do not add dependencies, package publishing, shell completions, or cross-platform
support without a consumer. Do not point scheduled jobs at a development
worktree.
