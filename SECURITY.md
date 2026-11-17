# Security policy

Restic Console runs as root (Linux) or SYSTEM (Windows) and handles backup passwords, so security reports are taken
seriously.

## Reporting a vulnerability

Please **do not open a public issue** for a security problem. Instead, use GitHub's private reporting:
**Security → Report a vulnerability** on this repository
([direct link](https://github.com/king-networks/Restic-Console/security/advisories/new)).

Include what an attacker needs (for example: a local unprivileged user, a web page in the user's browser, network
access), the steps to reproduce, and the version or commit. You should get a reply within a week. Once a fix is
released, you are credited in the advisory unless you prefer not to be.

## Supported versions

Only the latest release gets security fixes.

## Scope

The design is described in [docs/how-it-works.md](docs/how-it-works.md#security-model). In particular, a user who can
read the sign-in token is *by design* an administrator of the backups (they can back up and download any file). That
is not a vulnerability, but any way to get that access **without** the token is one. Examples of in-scope problems:

- reaching the API without a valid session or token (from another local user, or from a web page),
- reading or writing files outside the allowed restore folders,
- revealing repository, share or SMTP passwords to someone who is not signed in,
- command or argument injection into restic, systemd or Task Scheduler.
