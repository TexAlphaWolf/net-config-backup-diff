# Security

## Model

cfgtrack logs in to network devices, so it holds the most sensitive thing a network has: the ability to read every
configuration. It is a command line program with **no network service of its own**; it opens outgoing SSH or HTTP(S)
connections to the devices you list and runs `git` on a local directory. The Docker image runs as an unprivileged user
with a read-only root filesystem and no capabilities.

What it does to limit damage:

- **No password in a file.** The configuration refuses `password`, `token`, `passphrase` and `secret` keys; credentials
  come from environment variables (`password_env`) or an SSH key file.
- **Host keys are never accepted blindly.** An SSH device needs a pinned fingerprint (`host_key`) or a `known_hosts`
  file. A server with another key is refused before any credential is sent (tested). `cfgtrack hostkey` prints the
  fingerprint to pin.
- **No shell.** `command` devices run an argument list, not a string; a value cannot become a second command.
- **Secrets are replaced before anything is written.** Passwords, community strings and keys in the device output become
  `<secret:...>`, an HMAC with a key that stays outside the repository, so a rotation is still visible as a change but
  the value is in no file, no commit and no report. `cfgtrack verify` re-checks the stored files.
- **Bad input is refused.** Device names and revisions are validated before they reach a path or a `git` command
  (`../x`, `--output=...`, `HEAD..HEAD~3` are errors); the global git configuration, hooks and signing are switched off
  for the repository, so a hostile `~/.gitconfig` or hook cannot change what is stored.
- **Size and time limits** on every fetch (`timeout`, `max_bytes`).
- Errors are short fixed messages, never the credentials or the output; when a device answers with an error instead of a
  configuration, only that one line (at most 80 characters) is shown.
- The HTML report is one page with no scripts and no external files; everything that comes from a configuration is
  escaped.

What it does not protect:

- **The redaction is a safety net, not a guarantee.** It knows the common ways IOS, Junos and RouterOS write passwords
  and keys, and the generic profile guesses by name. A secret in a format nobody wrote a rule for **will be stored in
  clear text**. Keep the repository private, add `redact` patterns for your devices and run `cfgtrack verify`.
- The key file (`cfgtrack.key`) makes the tags unguessable. Anyone with the key and a copy of the repository can test
  guesses for weak passwords. Keep it out of the repository (it is in `.gitignore`) and out of backups of the repository.
- Because secrets are not stored, the repository **cannot be used to restore a device**. It is a record of what changed.
- The repository shows how your network is built (addresses, ACLs, VLANs). Treat it like the configurations themselves.

## Reporting

Please report vulnerabilities privately through GitHub's "Report a vulnerability" (Security tab), not in a public issue.
