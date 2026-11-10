"""Design stub: mDNS on the LAN (_vnodes._tcp.local) + signed node card. Disabled; no sockets are opened."""
from . import Source


class MdnsSource(Source):
    name = "mdns"

    def propose_ops(self, inventory):
        raise NotImplementedError("mDNS discovery is not implemented (docs/DISCOVERY.md)")
