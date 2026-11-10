"""Design stub: MDM / IT channel, a signed batch manifest of node records. Disabled; nothing is read."""
from . import Source


class SignedManifestSource(Source):
    name = "manifest"

    def propose_ops(self, inventory):
        raise NotImplementedError("signed batch manifests are not implemented (docs/DISCOVERY.md)")
