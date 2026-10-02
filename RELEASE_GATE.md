# Release Gate — v0.1.0 (2026-10-02)

## Checked commands and results

- `git diff --cached --check`: clean.
- Secret scan over tracked set (`sk-`, `ghp_`, `gho_`, `API_KEY`, `TOKEN`,
  `PASSWORD`, `SECRET`, bearer/JWT patterns): no credential values.
- Filename scan (`.env`, databases, keys, `private`, `secret`): none tracked.
- Mojibake scan: clean (text files).
- `git status`: branch in sync with `origin/master` before tagging.

## Intentional exceptions

- The vendored `impeccable` Mach-O binary contains the generic field names
  `API_KEY` and `TOKEN` and base64-encoded asset descriptors. These are
  identifier names and config data, not credential values. Upstream skill
  asset, kept as shipped.
- `.agent/` holds a second impeccable variant from another harness install.
  Pre-existing, kept pending owner decision.
- No CHANGELOG, SECURITY, CONTRIBUTING, CODE_OF_CONDUCT, `llms.txt`, banner,
  or i18n: deliberately oversized for a workshop repo. README (EN only) plus
  LICENSE, description, and topics cover public basics.
- No CI: nothing builds. No test suite: no code to smoke beyond scans above.

## Release

- Tag `v0.1.0`, initial public snapshot of the workshop.
