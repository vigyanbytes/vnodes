# Reading the inventory from other tools

Consumers **read** `nodes.json`; they never write it. Changes go through `vnodes` (plan -> approve -> apply).

## Rules every consumer follows

| Rule | Why |
|---|---|
| Open read-only. Never write or "fix up" the file. | Writes bypass approval and break the hash chain (`vnodes verify` flags them). |
| Check `version == 2`; refuse anything else with a clear message. | A schema bump must fail loudly, not half-work. |
| Skip `status: "retired"` nodes. Skip `enabled: false` nodes for push/sync work. | Retired ids stay in the file for history and are never reused. |
| Resolve a role: holders = active nodes whose `roles` contain it; `primary_for` holders first, then file order; host = `host`, else `name`. | One rule everywhere; `vnodes export` implements it. |
| Don't cache past one run. | The file can change under an applied plan. |
| Expect no secrets. Credentials live in your secret store / SSH config. | The inventory holds names and addresses only. |

Read `os` and `shell` through `vnodes export`, which maps the deprecated `os: windows-git-bash` to
`os: windows` + `shell: git-bash`.

The node record is `schema/node.schema.json` (`$id urn:vigyan-nodes:schema:node:2.1`). To validate with a standard
JSON Schema library, register that file under its `$id` before validating against `nodes.schema.json`.

## The export helper (no parsing needed)

```sh
vnodes export --format json [--all] [--require-version 2] [--file PATH]
vnodes export --format env  [--all] [--require-version 2] [--file PATH]
```

- Read-only, no privileges. Validates the file first and refuses (exit 1) if it is invalid.
- `--require-version N`: exit **3** if the file has another schema version.
- `json`: `{ "version": 2, "nodes": [ ...active nodes... ] }`.
- `env`: sourceable shell, every value quoted:

```sh
VNODES_SCHEMA_VERSION=2
VNODES_IDS='ctl-1 gpu-1 web-1'
VNODES_NODE_GPU_1_HOST=gpu-1
VNODES_NODE_GPU_1_LAN_IPV4=192.0.2.14
VNODES_NODE_GPU_1_TAILNET_NAME=gpu-1.tailnet.example
VNODES_NODE_<ID>_SHELL=git-bash                # when known; OS is then windows
VNODES_NODE_<ID>_OWNER='user:<login>@<node-id>' # when set
VNODES_ROLE_COMPUTE_GPU=gpu-1                  # first holder (primary_for first)
VNODES_ROLE_COMPUTE_GPU_ALL='gpu-1'            # every holder
```

From a shell script: `eval "$(vnodes export --format env --require-version 2)" || exit`.

## Examples

- **Secret sync / config push:** targets = active nodes with `ssh` not `local`/`none` and `enabled != false`; the
  origin is the holder of role `vault.origin`.
- **Model endpoint:** `http://$VNODES_ROLE_COMPUTE_GPU:<services.vllm.port>/v1` instead of a hard-coded host.
- **Audit trail:** when another system records a decision about a node, cite the node `id` and the current chain
  head (`tail -1 changes.jsonl` -> `hash`), so the decision is tied to an exact inventory state.

## Integrity check before acting

A consumer that acts on the fleet (push, sync, backup) should run `vnodes verify` first and stop on a non-zero exit.
