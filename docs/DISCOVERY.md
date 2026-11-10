# Discovery: design (not implemented)

Today the inventory is declared by hand and changed only through plan -> approve -> apply. **Nothing in vnodes
opens a socket, sends a DNS query, or reads a remote file.** This document fixes the design so that discovery,
when it is built, cannot weaken that model.

## The rule that will not change

A discovery source **only proposes**. It returns `add` / `set` ops and `vnodes.discovery.submit()` writes them
as one pending plan with `source: "discovery:<name>"`. An admin approves and applies it like any other plan.
No source writes `nodes.json`, approves or applies. An unapproved machine never enters the inventory.

```python
class Source:                       # vnodes/discovery/__init__.py
    name = "..."
    def propose_ops(self, inventory) -> list[dict]: ...   # pure; no writes

submit(repo, source, reason) -> plan_id | None
```

Stubs that raise `NotImplementedError`: `capabilities.py`, `mdns.py`, `dnssd.py`, `manifest.py`
(tested in `tests/test_discovery.py`).

## One record from every path

Every path emits the same object: a `schema/node.schema.json` record. `vnodes add --from-json` already accepts
one (that is how an enrolment kit run on the node hands its record over). Declared fields (`roles`,
`primary_for`, `role`, `clis`, `mcp_profiles`, `enabled`, `status`, `admin_group`) are **never** proposed by
discovery; an admin sets them.

## The signed node card

Each node that wants to be found publishes a small JSON **node card**, signed with the node's SSH host key:
`node_id`, the node record it proposes, `host_key_sha256`, `created_at`, `owner` principal. A source fetches the
card and verifies the signature against the host key before proposing anything. When an admin applies the plan,
vnodes writes `card.node_id`, `card.host_key_sha256`, `card.approved_at`, `card.approved_by` into the node
(trust on first use, with a human in the loop). After that, a card that verifies against the recorded host key
may propose `addresses.*` refreshes; a card with a different key is a new node, never an update.

## Path 1: mDNS on the LAN (homes, small offices)

- Service type `_vnodes._tcp.local` (DNS-SD over multicast DNS, RFC 6762 / RFC 6763). The TXT record carries the
  card URL and `host_key_sha256`.
- `vnodes discover mdns` would browse, fetch and verify cards, and propose `add` ops with
  `source: "discovered"`, `enrolment: {source: "mdns", at, by: <owner principal from the card>}`.
- Multicast stays on one link; nothing crosses routers. Fine for a flat home or office LAN, not for
  segmented networks.

## Path 2: unicast DNS-SD (enterprise)

- The same service type browsed through ordinary DNS (RFC 6763 section 11) under a zone the IT team controls,
  e.g. `_vnodes._tcp.nodes.corp.example`: PTR records list instances, SRV/TXT point at each card.
- Works across subnets and VPNs, needs no multicast, and the DNS zone itself is an access control: only
  hosts IT registered are browsable.
- Proposes the same `add` ops as mDNS. Needs a new `enrolment.source` value (`dnssd`), which is a schema
  minor version (2.2, additive).

## Path 3: MDM / IT channel with a signed batch manifest

- For fleets enrolled through device management (MDM, configuration management, an asset database), IT exports
  a **batch manifest**: `{manifest_version, issued_at, issuer, nodes: [node records...]}`, signed by an IT key
  the inventory trusts (the trusted public keys would be listed in `vnodes.config.json`, changed only via
  `config-record`, so a key change is itself on the change log).
- `vnodes discover manifest FILE` would verify the signature, check every record against the node schema, drop
  ids already present (or turn differences into `set` ops on non-declared fields), and propose the rest as
  **one** plan, so an admin reviews and applies a whole batch at once.
- Needs `enrolment.source: manifest` and the manifest's `sha256` (like `enrolment.request_sha256` today):
  also part of schema 2.2.

## Capability probe

- Reads each active node's own capability file over the SSH alias already in the inventory (no new ports, no
  sweep) and proposes `set capabilities.*` where measured differs from declared, with `capabilities.basis:
  "probed"`.

## Before any path is built

1. Tests first: the source proposes only and never writes (the contract tests already exist).
2. Settle the `node_id` scheme for cards (stable across reinstalls or not).
3. Schema 2.2 (additive) for the new `enrolment.source` values and manifest hash.
