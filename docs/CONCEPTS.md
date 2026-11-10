# Concepts

## Plan -> approve -> apply

Nothing writes `nodes.json` directly. Every change is a **plan** first.

```text
 propose   vnodes add | set | retire | migrate   -> plans/pending/P-<time>-<rand>.json
           anyone who can write the directory; the plan is simulated and schema-checked now,
           so an invalid change is refused before anyone is asked to approve it
 approve   vnodes approve P-...                  -> approval (login, uid, time, plan hash) recorded in the plan
           an admin; not the author (unless solo mode)
 apply     vnodes apply P-...                    -> nodes.json + changes.jsonl + plans/applied/ + git commit
           an admin; only if the approval's hash matches the plan as it is now, the plan was proposed against
           the current nodes.json (otherwise "stale": re-propose), and `vnodes verify` passes
 reject    vnodes reject P-... --reason "..."    -> plans/rejected/
```

A plan holds a list of ops:

| op | effect |
|---|---|
| `add` | a new node record. Ids are never reused, even after retirement. `provenance` is written by vnodes, never by the plan. |
| `set` | dotted keys with JSON values (`capabilities.ram_gb=128`), plus optional `unset`. `id`, `status`, `provenance`, `retired_*` are protected; `enrolment` is write-once. |
| `retire` | `status: retired` with a time and a reason. The record stays for history. |

Each applied node carries `provenance`: who created it, who last changed it, who approved, and which plan.

`vnodes migrate --reason "..."` proposes the schema 2.1 conventions for active nodes (`shell: git-bash` for the
deprecated `os: windows-git-bash`, and an `enrolment` backfilled from `provenance`). It is a plan like any other.

## Who may do what: identity from the OS login

Identity is the login of the process, read through NSS (`pwd`/`grp`): local accounts, SSSD, authd, Kanidm or any
other NSS directory work unchanged. It is **never** a typed name and never `$USER`, `$LOGNAME` or `$SUDO_USER`.

- **Admin** = member of `admin_group` (in `vnodes.config.json`, default `adm`) **with uid >= `UID_MIN`** from
  `/etc/login.defs`. So root, `sudo vnodes ...` and system accounts are refused: run as yourself.
- **Approver != author.** Self-approval is refused unless **solo mode** is on:
  `"solo_mode": {"enabled": true, "reason": "<why>"}` in `vnodes.config.json`, recorded with
  `vnodes config-record --reason "<why>"`. Solo approvals are marked `"solo": true` in the change log and
  `Vnodes-Solo: yes` in the commit.
- Reading (`list`, `show`, `export`, `verify`, ...) needs no privilege.

People are written as **principals**: `user:<login>@<node-id>`, e.g. `user:alice@ctl-1`. `vnodes add --owner-self`
sets `owner` to you on the node vnodes runs on; `enrolment.by` records who proposed the node.

## The hash-chained change log

`changes.jsonl` is append-only. Each line is one applied change (`genesis`, `change` or `config`) with:

- `seq`, and `prev_hash` = the previous entry's `hash` (`000...0` for genesis)
- `hash` = SHA-256 of the canonical JSON of the entry without `hash` (sorted keys, no whitespace)
- `author`, `approved_by`, `applied_by`, `solo`, `ops`, `plan_hash`
- `before_hash` / `after_hash` = canonical hash of `nodes.json` before and after
- `config_hash` = canonical hash of `vnodes.config.json` at that moment

Editing, reordering or deleting an entry breaks the chain. Truncating the tail is caught because the last entry's
`after_hash` must match `nodes.json`, and git history keeps every committed head.

Every apply also makes a git commit of exactly the inventory files, with trailers:
`Vnodes-Change`, `Vnodes-Author`, `Vnodes-Approved-By`, `Vnodes-Applied-By`, `Vnodes-Solo`, `Vnodes-Chain-Hash`,
plus `commit_trailers` from the config and any `--trailer "Key: value"`.

## Integrity: `vnodes verify`

Checks the schema, recomputes every chain hash and link, and checks that `nodes.json` and `vnodes.config.json`
equal what the last chain entry recorded. Hand edits, edited or deleted log entries and unrecorded policy changes
all fail verify, and `apply` refuses until they are fixed.

What this is and is not: filesystem permissions are the real write barrier. For anyone who can write the
directory, the flow is a procedural gate with a tamper-evident record, not cryptographic access control. Keep the
inventory directory owned by the admin group and push it somewhere the admins review.

## Schema versions

- Document `version` is `2`. Readers must check it and refuse anything else.
- The node record is `schema/node.schema.json`, `x-schema-version 2.1`. 2.1 only added optional fields (`shell`,
  `owner`, `enrolment`, `card`, plain `os: windows`), so every 2.0 file is valid and `version` stayed `2`.
- `os: windows-git-bash` is accepted but deprecated: `validate`/`verify` warn, and `export` emits
  `os=windows` + `shell=git-bash`.
