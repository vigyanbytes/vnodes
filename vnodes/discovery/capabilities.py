"""Design stub: capability probe. Disabled; raises until implemented (docs/DISCOVERY.md)."""
from . import Source


class CapabilityProbe(Source):
    name = "capabilities"

    def propose_ops(self, inventory):
        raise NotImplementedError("capability probing is not implemented (docs/DISCOVERY.md)")
