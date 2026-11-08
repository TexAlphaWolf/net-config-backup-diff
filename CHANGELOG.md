# Changelog

## 1.0.0

First release.

- Fetch a configuration over SSH (one command, or an interactive session), HTTP, any program, or from a file.
- Remove the lines that change on every run, replace passwords and keys by keyed tags, store the rest in Git:
  one file per device, one commit per real change.
- A diff that knows sections (`interface ...`, `ip access-list ...`, Junos blocks, RouterOS paths), and a report as
  Markdown, JSON or one HTML page.
- Fetches that are not configurations (empty, an error message, cut off, far smaller than before) are not stored.
- `cfgtrack noise` suggests `ignore` patterns for lines that change on every backup; `cfgtrack verify` re-checks what
  was stored; `cfgtrack status` and exit codes for monitoring.
- Profiles for Cisco IOS, Junos, MikroTik RouterOS and a generic one.
