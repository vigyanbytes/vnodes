import json
import unittest

from tests.helpers import ALICE, BOB, MALLORY, ROOTUSER, git_log, make_repo, new_node, seed_doc
from vnodes import chain
from vnodes.repo import PermissionDenied, Repo, VnodesError


class FlowBase(unittest.TestCase):
    solo = False

    def setUp(self):
        self.root = make_repo(solo=self.solo)
        Repo(self.root, identity=ALICE).init_genesis(seed_doc(), reason="seed")

    def repo(self, who):
        return Repo(self.root, identity=who)

    def nodes(self):
        return {n["id"]: n for n in json.loads((self.root / "nodes.json").read_text())["nodes"]}


class GenesisTests(FlowBase):
    def test_genesis_writes_valid_inventory_chain_and_commit(self):
        r = self.repo(ALICE)
        self.assertEqual(r.verify(), [])
        entries = chain.read(self.root / "changes.jsonl")
        self.assertEqual(entries[0]["kind"], "genesis")
        self.assertEqual(entries[0]["applied_by"], {"login": "alice", "uid": 1001})
        prov = self.nodes()["ctl-1"]["provenance"]
        self.assertEqual(prov["created_by"]["login"], "alice")
        self.assertEqual(prov["change"], "genesis")
        self.assertIn("Vnodes-Change: genesis", git_log(self.root))

    def test_genesis_only_once(self):
        with self.assertRaises(VnodesError):
            self.repo(ALICE).init_genesis(seed_doc(), reason="again")

    def test_genesis_needs_admin(self):
        root = make_repo()
        with self.assertRaises(PermissionDenied):
            Repo(root, identity=MALLORY).init_genesis(seed_doc(), reason="x")

    def test_genesis_rejects_invalid_seed(self):
        root = make_repo()
        bad = seed_doc(); bad["nodes"][0]["os"] = "beos"
        with self.assertRaises(VnodesError):
            Repo(root, identity=ALICE).init_genesis(bad, reason="x")
        self.assertFalse((root / "nodes.json").exists())


class PlanApproveApplyTests(FlowBase):
    def propose_add(self, who=ALICE, node_id="new-1"):
        return self.repo(who).propose([{"op": "add", "node": new_node(node_id)}], reason="add test node")

    def test_plan_is_pending_and_does_not_change_inventory(self):
        before = (self.root / "nodes.json").read_text()
        pid = self.propose_add()
        self.assertTrue((self.root / "plans" / "pending" / f"{pid}.json").exists())
        self.assertEqual((self.root / "nodes.json").read_text(), before)
        plan = self.repo(MALLORY).plan(pid)
        self.assertEqual(plan["author"], {"login": "alice", "uid": 1001})

    def test_non_admin_may_propose_but_not_approve_or_apply(self):
        pid = self.propose_add(who=MALLORY)
        with self.assertRaises(PermissionDenied):
            self.repo(MALLORY).approve(pid)
        self.repo(ALICE).approve(pid)
        with self.assertRaises(PermissionDenied):
            self.repo(MALLORY).apply(pid)
        self.assertNotIn("new-1", self.nodes())

    def test_self_approval_refused(self):
        pid = self.propose_add(who=ALICE)
        with self.assertRaises(PermissionDenied) as cm:
            self.repo(ALICE).approve(pid)
        self.assertIn("solo", str(cm.exception))

    def test_root_refused(self):
        pid = self.propose_add(who=ALICE)
        with self.assertRaises(PermissionDenied):
            self.repo(ROOTUSER).approve(pid)

    def test_apply_requires_approval(self):
        pid = self.propose_add()
        with self.assertRaises(VnodesError):
            self.repo(BOB).apply(pid)

    def test_full_flow_add(self):
        pid = self.propose_add(who=ALICE)
        self.repo(BOB).approve(pid)
        entry = self.repo(BOB).apply(pid, trailers=["Co-Authored-By: Test <t@example.com>"])
        n = self.nodes()["new-1"]
        self.assertEqual(n["provenance"]["created_by"]["login"], "alice")
        self.assertEqual(n["provenance"]["approved_by"]["login"], "bob")
        self.assertEqual(n["provenance"]["change"], pid)
        self.assertEqual(entry["author"]["login"], "alice")
        self.assertEqual(entry["approved_by"]["login"], "bob")
        self.assertFalse(entry["solo"])
        self.assertEqual(self.repo(MALLORY).verify(), [])
        self.assertTrue((self.root / "plans" / "applied" / f"{pid}.json").exists())
        self.assertFalse((self.root / "plans" / "pending" / f"{pid}.json").exists())
        log = git_log(self.root)
        for t in (f"Vnodes-Change: {pid}", "Vnodes-Author: alice", "Vnodes-Approved-By: bob",
                  f"Vnodes-Chain-Hash: {entry['hash']}", "Co-Authored-By: Test <t@example.com>"):
            self.assertIn(t, log)

    def test_set_and_retire(self):
        pid = self.repo(ALICE).propose([
            {"op": "set", "id": "gpu-1", "set": {"capabilities.ram_gb": 128, "display_name": "Big GPU"},
             "unset": ["addresses.tailnet_name"]},
            {"op": "retire", "id": "web-1", "reason": "moved host"}], reason="update")
        self.repo(BOB).approve(pid)
        self.repo(ALICE).apply(pid)
        nodes = self.nodes()
        self.assertEqual(nodes["gpu-1"]["capabilities"]["ram_gb"], 128)
        self.assertEqual(nodes["gpu-1"]["display_name"], "Big GPU")
        self.assertNotIn("tailnet_name", nodes["gpu-1"]["addresses"])
        self.assertEqual(nodes["gpu-1"]["provenance"]["created_by"]["login"], "alice")
        self.assertEqual(nodes["web-1"]["status"], "retired")
        self.assertEqual(nodes["web-1"]["retired_reason"], "moved host")
        self.assertEqual(self.repo(ALICE).verify(), [])

    def test_set_cannot_touch_protected_fields(self):
        for path in ("id", "provenance.changed_by", "status"):
            with self.assertRaises(VnodesError):
                self.repo(ALICE).propose([{"op": "set", "id": "gpu-1", "set": {path: "x"}}], reason="x")

    def test_invalid_change_refused_at_plan_time(self):
        with self.assertRaises(VnodesError):
            self.repo(ALICE).propose([{"op": "set", "id": "gpu-1", "set": {"os": "beos"}}], reason="x")
        with self.assertRaises(VnodesError):
            self.repo(ALICE).propose([{"op": "add", "node": new_node("ctl-1")}], reason="dup")
        with self.assertRaises(VnodesError):
            self.repo(ALICE).propose([{"op": "retire", "id": "nope", "reason": "x"}], reason="x")

    def test_retired_id_cannot_be_reused(self):
        pid = self.repo(ALICE).propose([{"op": "retire", "id": "web-1", "reason": "gone"}], reason="x")
        self.repo(BOB).approve(pid); self.repo(BOB).apply(pid)
        with self.assertRaises(VnodesError):
            self.repo(ALICE).propose([{"op": "add", "node": new_node("web-1")}], reason="reuse")

    def test_plan_edited_after_approval_refused(self):
        pid = self.propose_add()
        self.repo(BOB).approve(pid)
        p = self.root / "plans" / "pending" / f"{pid}.json"
        plan = json.loads(p.read_text())
        plan["ops"][0]["node"]["roles"] = ["gateway"]
        p.write_text(json.dumps(plan))
        with self.assertRaises(VnodesError) as cm:
            self.repo(BOB).apply(pid)
        self.assertIn("changed after approval", str(cm.exception))

    def test_stale_plan_refused(self):
        p1 = self.propose_add(node_id="new-1")
        p2 = self.propose_add(node_id="new-2")
        for p in (p1, p2):
            self.repo(BOB).approve(p)
        self.repo(BOB).apply(p1)
        with self.assertRaises(VnodesError) as cm:
            self.repo(BOB).apply(p2)
        self.assertIn("stale", str(cm.exception))

    def test_reject_plan(self):
        pid = self.propose_add()
        self.repo(BOB).reject(pid, reason="no")
        self.assertFalse((self.root / "plans" / "pending" / f"{pid}.json").exists())
        self.assertTrue((self.root / "plans" / "rejected" / f"{pid}.json").exists())


class TamperTests(FlowBase):
    def test_out_of_band_inventory_edit_detected(self):
        p = self.root / "nodes.json"
        d = json.loads(p.read_text()); d["nodes"][0]["roles"].append("compute.gpu")
        p.write_text(json.dumps(d, indent=2))
        errs = self.repo(MALLORY).verify()
        self.assertTrue(any("does not match" in e for e in errs), errs)

    def test_out_of_band_edit_blocks_apply(self):
        p = self.root / "nodes.json"
        pid = self.repo(ALICE).propose([{"op": "add", "node": new_node()}], reason="x")
        self.repo(BOB).approve(pid)
        d = json.loads(p.read_text()); d["nodes"][0]["display_name"] = "sneaky"
        p.write_text(json.dumps(d, indent=2))
        with self.assertRaises(VnodesError):
            self.repo(BOB).apply(pid)

    def test_chain_tamper_detected_by_repo_verify(self):
        c = self.root / "changes.jsonl"
        e = json.loads(c.read_text().splitlines()[0]); e["reason"] = "forged"
        c.write_text(json.dumps(e) + "\n")
        self.assertTrue(self.repo(ALICE).verify())

    def test_config_change_outside_vnodes_detected(self):
        cfg = self.root / "vnodes.config.json"
        d = json.loads(cfg.read_text()); d["solo_mode"] = {"enabled": True, "reason": "sneaky"}
        cfg.write_text(json.dumps(d))
        errs = self.repo(ALICE).verify()
        self.assertTrue(any("config" in e for e in errs), errs)


class SoloModeTests(FlowBase):
    solo = True

    def test_solo_mode_allows_self_approval_and_records_it(self):
        pid = self.repo(ALICE).propose([{"op": "add", "node": new_node()}], reason="solo")
        self.repo(ALICE).approve(pid)
        entry = self.repo(ALICE).apply(pid)
        self.assertTrue(entry["solo"])
        self.assertIn("Vnodes-Solo: yes", git_log(self.root))

    def test_solo_mode_still_refuses_non_admin(self):
        pid = self.repo(MALLORY).propose([{"op": "add", "node": new_node()}], reason="x")
        with self.assertRaises(PermissionDenied):
            self.repo(MALLORY).approve(pid)

    def test_solo_mode_requires_reason(self):
        cfg = self.root / "vnodes.config.json"
        d = json.loads(cfg.read_text()); d["solo_mode"]["reason"] = ""
        cfg.write_text(json.dumps(d))
        with self.assertRaises(VnodesError):
            self.repo(ALICE).propose([{"op": "add", "node": new_node()}], reason="x")


if __name__ == "__main__":
    unittest.main()


class EmptyPlanDirsTests(unittest.TestCase):
    def test_genesis_commits_with_empty_untracked_plan_dirs(self):
        root = make_repo()
        (root / "plans" / "applied").mkdir(parents=True)
        (root / "plans" / "pending").mkdir(parents=True)
        Repo(root, identity=ALICE).init_genesis(seed_doc(), reason="seed")
        self.assertIn("Vnodes-Change: genesis", git_log(root))
