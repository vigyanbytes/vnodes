"""Design stub: unicast DNS-SD (RFC 6763) for enterprise networks. Disabled; no DNS queries are made."""
from . import Source


class DnsSdSource(Source):
    name = "dnssd"

    def propose_ops(self, inventory):
        raise NotImplementedError("unicast DNS-SD discovery is not implemented (docs/DISCOVERY.md)")
