# development-space

A software-development workshop. It owns reusable agent skills and isolated Git
worktrees. It owns no product source code.

## Layout

```text
AGENTS.md                 workshop policy and routing
.agents/skills/           reusable specialist skills
docs/                     workshop references and agent docs
worktrees/                temporary linked Git worktrees (never committed)
```

Tool repositories live at `~/.agents/tools/<tool-name>/`. Each one owns its own
source. This workshop only hosts linked worktrees of those repositories.

## Skills

Methodology (planning, TDD, debugging, review, integration) comes from the
harness plugin. Local specialists:

- `codebase-design`, `prototype`, `impeccable`, `swiftui-expert-skill`, `playwright-cli`
- `gh-fix-ci`, `gh-address-comments` (GitHub PR loops)
- `cli-developer` (command-line behavior contracts)

Read `AGENTS.md` for routing, worktree policy, and safety rules. Product
rules always live in the product repository, never here.
Read [Small-tool contract](docs/small-tool-contract.md) when defining a tool's
callable surface.

## License

MIT. See `LICENSE`.
