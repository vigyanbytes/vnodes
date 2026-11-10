# Security

## What vnodes protects, and what it does not

- The inventory holds names, addresses and declared capabilities. The schema has no field for keys, tokens or
  passwords (`additionalProperties: false` everywhere), so a secret cannot be stored by accident through vnodes.
- Every change is a plan; approval and apply need an admin (OS group + human uid), and approver != author unless
  solo mode is on and recorded.
- `changes.jsonl` is a hash chain and every apply is a git commit, so out-of-band edits are **detected**
  (`vnodes verify`) and apply refuses until they are fixed.
- Filesystem permissions are the write barrier. Anyone who can write the inventory directory can bypass the CLI;
  vnodes makes that visible, it does not prevent it. Keep the directory owned by the admin group.
- No network access of any kind. Discovery is a design document, not code.

## Reporting a vulnerability

Please report privately through the repository's security advisories ("Report a vulnerability"), not in a public
issue. Include the vnodes version (`vnodes/__init__.py`), what you ran, and what you expected. We aim to
acknowledge within a week.
