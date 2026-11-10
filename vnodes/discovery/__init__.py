"""Discovery extension point. DESIGN ONLY: nothing here probes the network (see docs/DISCOVERY.md).

The contract is fixed now, before any source exists:

    A discovery source NEVER writes nodes.json. It returns proposed ops and submits them as a
    pending plan (source="discovery:<name>"). An admin approves and applies it like any other plan.

Planned sources (all stubs that raise NotImplementedError):
  capabilities.py  read a node's own capability file over the existing SSH alias; propose `set capabilities.*`
  mdns.py          small LAN: browse _vnodes._tcp.local, verify the signed node card, propose `add`
  dnssd.py         enterprise: unicast DNS-SD (RFC 6763) under an admin-controlled DNS zone, same card check
  manifest.py      MDM / IT channel: a signed batch manifest of node records, verified, proposed as one plan
"""
from ..repo import Repo


class Source:
    """Interface every discovery source implements."""
    name = "abstract"

    def propose_ops(self, inventory):
        """Return a list of add/set ops (same shape as vnodes plans). Must not mutate anything."""
        raise NotImplementedError


def submit(repo: Repo, source: Source, reason: str):
    """Turn a source's proposals into ONE pending plan. Returns the plan id, or None if nothing changed."""
    ops = source.propose_ops(repo.load())
    if not ops:
        return None
    return repo.propose(ops, reason=reason, source=f"discovery:{source.name}")
