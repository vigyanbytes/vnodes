import unittest

from tests.helpers import ALICE, BOB, make_repo, new_node, seed_doc
from vnodes import discovery
from vnodes.discovery.capabilities import CapabilityProbe
from vnodes.discovery.dnssd import DnsSdSource
from vnodes.discovery.manifest import SignedManifestSource
from vnodes.discovery.mdns import MdnsSource
from vnodes.repo import Repo


class FakeSource(discovery.Source):
    name = "fake"

    def __init__(self, ops):
        self.ops = ops

    def propose_ops(self, inventory):
        return self.ops


class DiscoveryContractTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        Repo(self.root, identity=ALICE).init_genesis(seed_doc(), reason="seed")

    def test_discovery_only_proposes(self):
        before = (self.root / "nodes.json").read_text()
        pid = discovery.submit(Repo(self.root, identity=ALICE), FakeSource([{"op": "add", "node": new_node()}]), "probe")
        self.assertEqual((self.root / "nodes.json").read_text(), before)
        plan = Repo(self.root, identity=BOB).plan(pid)
        self.assertEqual(plan["source"], "discovery:fake")
        self.assertEqual(plan["approvals"], [])

    def test_nothing_to_propose(self):
        self.assertIsNone(discovery.submit(Repo(self.root, identity=ALICE), FakeSource([]), "probe"))

    def test_later_phases_are_disabled(self):
        for src in (CapabilityProbe(), MdnsSource(), DnsSdSource(), SignedManifestSource()):
            with self.assertRaises(NotImplementedError):
                src.propose_ops({})


if __name__ == "__main__":
    unittest.main()
