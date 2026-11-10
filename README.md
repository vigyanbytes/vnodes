# vnodes

A small, admin-controlled inventory of the machines you run: which nodes exist, what they do, and how to
reach them. One JSON file (`nodes.json`), one JSON Schema, one CLI. Every change goes **plan -> approve ->
apply**, every applied change is appended to a **hash-chained log**, and who did it comes from the **OS
login**, never from a typed name.

- Python 3.9+ standard library only (no pip, no venv). Linux and macOS. Needs `git`.
- Holds names, addresses and declared capabilities. Never keys, tokens or passwords.
- No network discovery. Discovery paths (mDNS, unicast DNS-SD, a signed MDM manifest) are **designed, not
  implemented**: see [docs/DISCOVERY.md](docs/DISCOVERY.md).

Apache-2.0.

## Quick start

```sh
git clone <this repository> vnodes
export PATH="$PWD/vnodes/bin:$PATH"

# the inventory lives in its own directory (its own git repo), not in this checkout
mkdir -p ~/inventory && cd ~/inventory          # or: export VNODES_REPO=~/inventory
vnodes init --reason "new inventory"            # empty nodes.json, vnodes.config.json, change log, git commit
vnodes verify

# propose a node (anyone who can write the directory)
vnodes add ctl-1 --ssh local --roles control.plane,gateway --lan-ipv4 192.0.2.10 \
  --owner-self --reason "the machine I run vnodes on"
# -> plan written: plans/pending/P-...

# approve and apply (members of the admin group; approver must differ from the author)
vnodes approve P-...
vnodes apply P-...
vnodes list
```

`vnodes init` makes you the first admin only if your login is in the admin group (default `adm`, the group
the first user is in on Ubuntu/Debian). Pass `--admin-group <group>` to use another group you belong to.
Working alone? See *solo mode* in [docs/CONCEPTS.md](docs/CONCEPTS.md).

Starting from an existing list instead: `vnodes init --seed examples/nodes.sample.json --reason "..."`.

## Commands

```text
read (no privilege)   list [--all] [--json] · show ID · validate [--file F] · verify · plans [--all] · show-plan P
                      export --format json|env [--all] [--file F] [--require-version 2]
propose               add ID [flags | --from-json FILE] · set ID key=value... [--unset key] · retire ID
                      migrate [--rewrite-os]                                   (all need --reason)
admin                 approve P · reject P --reason · apply P [--trailer "K: V"]
                      init [--seed F] [--admin-group G] · config-record --reason
```

`vnodes <command> --help` lists every flag. Exit codes: 0 ok, 1 error or verify failed, 2 usage,
3 schema version mismatch (`export --require-version`), 4 permission denied.

## Layout of an inventory directory

| Path | What |
|---|---|
| `nodes.json` | the inventory, schema v2.1 |
| `changes.jsonl` | append-only, hash-chained log of every applied change |
| `vnodes.config.json` | admin policy: admin group, solo mode, git commits, extra commit trailers |
| `plans/pending/` | proposed changes (git-ignored) |
| `plans/applied/` | applied plans, committed with the change |
| `plans/rejected/` | rejected plans (git-ignored) |

## This repository

| Path | What |
|---|---|
| `bin/vnodes`, `vnodes/` | the CLI |
| `schema/node.schema.json` | the node record, JSON Schema 2020-12, `$id urn:vigyan-nodes:schema:node:2.1` |
| `schema/nodes.schema.json` | the inventory document: `version: 2` + `nodes[]` (node record + `status`, `admin_group`, `provenance`) |
| `examples/` | fictional data only (RFC 5737 addresses, `.example` names) |
| `docs/CONCEPTS.md` | plan -> approve -> apply, admin from OS login, the change log, integrity |
| `docs/CONSUMERS.md` | how other tools read the inventory |
| `docs/DISCOVERY.md` | discovery design (not implemented) |

The schema `$id` keeps the URN it was first published under, so records written by other tools against
`urn:vigyan-nodes:schema:node:2.1` stay valid. Vendor `schema/node.schema.json` unchanged and reference it by
`$id`.

## Tests

```sh
python3 -m unittest
```

The suite includes a leak scan (`tests/test_leak_scan.py`). It derives a list of names from the machine
running it (login, host name, `/etc/hosts`, `~/.ssh/config`) plus any private inventory files named in
`VNODES_LEAK_SOURCES`, and fails if the working tree **or the git history** mentions any of them, any
private-range address, any token-shaped string, or a URL host that is not a documentation or well-known host.
Before publishing a fork that was ever pointed at a real inventory:

```sh
VNODES_LEAK_STRICT=1 VNODES_LEAK_SOURCES=/path/to/real/nodes.json:/path/to/real/changes.jsonl \
  python3 -m unittest tests.test_leak_scan
```

## License

Apache License 2.0, see [LICENSE](LICENSE) and [NOTICE](NOTICE).
