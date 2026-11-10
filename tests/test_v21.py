"""Schema v2.1: standalone node schema, plain windows + shell,
owner principal, enrolment block, card slot. Backward compatible with v2 files and v1 readers."""
import copy
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout

from tests.helpers import ALICE, BOB, MALLORY, ROOT, make_repo, new_node, sample_doc, seed_doc
from vnodes import cli, schema
from vnodes.identity import Identity, principal
from vnodes.repo import Repo, VnodesError

NODE_SCHEMA = ROOT / "schema" / "node.schema.json"


def run(root, who, *argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(["--repo", str(root), *argv], identity=who)
    return rc, out.getvalue(), err.getvalue()


def doc_with(**over):
    d = sample_doc()
    d["nodes"][0].update(over)
    return d


class NodeSchemaFileTests(unittest.TestCase):
    def test_standalone_node_schema_is_versioned_2020_12(self):
        s = json.loads(NODE_SCHEMA.read_text())
        self.assertEqual(s["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(s["x-schema-version"], "2.1")
        self.assertIn("2.1", s["$id"])
        self.assertEqual(s["type"], "object")

    def test_inventory_schema_references_the_node_schema(self):
        inv = schema.load_schema()
        node_id = json.loads(NODE_SCHEMA.read_text())["$id"]
        self.assertIn(node_id, json.dumps(inv))
        self.assertEqual(inv["properties"]["nodes"]["items"], {"$ref": "#/$defs/inventory_node"})
        self.assertNotIn("node", inv.get("$defs", {}), "node definition lives in node.schema.json only")

    def test_node_record_without_inventory_fields_is_a_valid_node(self):
        # what the kit / mDNS emit: no provenance/status/admin_group yet (vnodes adds them)
        rec = {"id": "box-1", "name": "box-1", "ssh": "none", "os": "windows", "shell": "powershell",
               "owner": "user:alice@box-1", "enrolment": {"source": "kit", "at": "2026-11-08T10:00:00Z", "by": "user:alice@box-1"}}
        self.assertEqual(schema.validate_node(rec), [])
        inv = sample_doc(); inv["nodes"].append(rec)
        errs = schema.validate_inventory(inv)
        for field in ("provenance", "status", "admin_group"):
            self.assertTrue(any(field in e for e in errs), field)

    def test_card_is_documented_as_the_future_mdns_slot(self):
        s = json.loads(NODE_SCHEMA.read_text())
        self.assertIn("mDNS", s["properties"]["card"]["description"])
        self.assertIn("owner", s["properties"])
        self.assertIn("user:<login>@<node-id>", s["$defs"]["principal"]["description"])


class OsShellTests(unittest.TestCase):
    def test_plain_windows_is_valid(self):
        self.assertEqual(schema.validate_inventory(doc_with(os="windows", shell="powershell")), [])
        self.assertFalse([w for w in schema.warnings(doc_with(os="windows", shell="powershell")) if "nodes[0]" in w])

    def test_windows_git_bash_still_valid_but_deprecated(self):
        d = doc_with(os="windows-git-bash")
        self.assertEqual(schema.validate_inventory(d), [])
        w = [x for x in schema.warnings(d) if "nodes[0]" in x]
        self.assertEqual(len(w), 1)
        self.assertIn("windows-git-bash", w[0])
        self.assertIn("os=windows", w[0])

    def test_windows_git_bash_with_another_shell_is_an_error(self):
        self.assertTrue(schema.validate_inventory(doc_with(os="windows-git-bash", shell="powershell")))

    def test_shell_enum(self):
        for sh in ("bash", "zsh", "powershell", "git-bash"):
            self.assertEqual(schema.validate_inventory(doc_with(shell=sh)), [], sh)
        self.assertTrue(schema.validate_inventory(doc_with(shell="fish")))

    def test_validate_cli_warns_but_passes(self):
        root = make_repo()
        Repo(root, identity=ALICE).init_genesis(seed_doc(), reason="seed")   # sample's retired laptop-1 is windows-git-bash
        rc, out, err = run(root, MALLORY, "validate")
        self.assertEqual(rc, 0, err)
        self.assertIn("deprecated", err)
        rc, out, err = run(root, MALLORY, "verify")
        self.assertEqual(rc, 0, err)
        self.assertIn("deprecated", err)

    def test_export_maps_git_bash_to_windows_plus_shell(self):
        root = make_repo()
        Repo(root, identity=ALICE).init_genesis(seed_doc(), reason="seed")
        rc, out, err = run(root, MALLORY, "export", "--format", "json", "--all")
        self.assertEqual(rc, 0, err)
        lap = next(n for n in json.loads(out)["nodes"] if n["id"] == "laptop-1")
        self.assertEqual((lap["os"], lap["shell"]), ("windows", "git-bash"))
        rc, out, _ = run(root, MALLORY, "export", "--format", "env", "--all")
        self.assertIn("VNODES_NODE_LAPTOP_1_OS=windows\n", out)
        self.assertIn("VNODES_NODE_LAPTOP_1_SHELL=git-bash\n", out)
        self.assertNotIn("windows-git-bash", out)


class OwnerTests(unittest.TestCase):
    def test_principal_is_derived_from_identity(self):
        self.assertEqual(principal(ALICE, "ctl-1"), "user:alice@ctl-1")
        self.assertEqual(ALICE.principal("ctl-1"), "user:alice@ctl-1")

    def test_owner_format(self):
        for ok in ("user:alice@ctl-1", "user:Alex Example@alex-pc", "user:a.b_c-d@n1"):
            self.assertEqual(schema.validate_inventory(doc_with(owner=ok)), [], ok)
        for bad in ("alice", "user:alice", "user:@ctl-1", "user:alice@", "user:alice@Bad_Host", "group:ops@ctl-1",
                    "user:a@b@c", "user: alice@ctl-1", "user:dom\\alice@ctl-1", "user:alice @ctl-1"):
            self.assertTrue(schema.validate_inventory(doc_with(owner=bad)), bad)


class EnrolmentTests(unittest.TestCase):
    E = {"source": "manual", "at": "2026-11-08T10:00:00Z", "by": "user:alice@ctl-1"}

    def test_enrolment_sources(self):
        for src in ("manual", "kit", "probe", "mdns"):
            self.assertEqual(schema.validate_inventory(doc_with(enrolment={**self.E, "source": src})), [], src)

    def test_enrolment_bad_values(self):
        for bad in ({**self.E, "source": "magic"}, {**self.E, "at": "today"}, {**self.E, "by": "alice"},
                    {k: v for k, v in self.E.items() if k != "at"}, {**self.E, "extra": 1}):
            self.assertTrue(schema.validate_inventory(doc_with(enrolment=bad)), bad)

    def test_provenance_still_required_and_kept(self):
        d = doc_with(enrolment=self.E)
        self.assertEqual(schema.validate_inventory(d), [])
        d["nodes"][0].pop("provenance")
        self.assertTrue(schema.validate_inventory(d))


class FlowV21Tests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        Repo(self.root, identity=ALICE).init_genesis(seed_doc(), reason="seed")

    def nodes(self):
        return {n["id"]: n for n in json.loads((self.root / "nodes.json").read_text())["nodes"]}

    def apply(self, pid):
        Repo(self.root, identity=BOB).approve(pid)
        Repo(self.root, identity=BOB).apply(pid)

    def test_manual_add_records_enrolment_from_the_proposer(self):
        pid = Repo(self.root, identity=ALICE).propose([{"op": "add", "node": new_node("new-1")}], reason="r")
        enr = Repo(self.root, identity=ALICE).plan(pid)["ops"][0]["node"]["enrolment"]
        self.assertEqual(enr["source"], "manual")
        self.assertEqual(enr["by"], "user:alice@ctl-1")   # ctl-1 = the controller (ssh local) in the sample
        self.apply(pid)
        self.assertEqual(self.nodes()["new-1"]["enrolment"], enr)
        self.assertIn("provenance", self.nodes()["new-1"])

    def test_kit_enrolment_is_kept(self):
        enr = {"source": "kit", "at": "2026-11-08T09:00:00Z", "by": "user:zoe@zoe-pc"}
        n = new_node("zoe-pc", os="windows", shell="powershell", owner="user:zoe@zoe-pc", enrolment=enr)
        pid = Repo(self.root, identity=ALICE).propose([{"op": "add", "node": n}], reason="kit")
        self.apply(pid)
        self.assertEqual(self.nodes()["zoe-pc"]["enrolment"], enr)
        self.assertEqual(self.nodes()["zoe-pc"]["owner"], "user:zoe@zoe-pc")

    def test_enrolment_is_write_once(self):
        enr = {"source": "manual", "at": "2026-11-08T09:00:00Z", "by": "user:alice@ctl-1"}
        pid = Repo(self.root, identity=ALICE).propose([{"op": "set", "id": "gpu-1", "set": {"enrolment": enr}}], reason="backfill")
        self.apply(pid)
        with self.assertRaises(VnodesError):
            Repo(self.root, identity=ALICE).propose([{"op": "set", "id": "gpu-1", "set": {"enrolment.source": "kit"}}], reason="x")

    def test_add_cli_windows_shell_and_owner_self(self):
        rc, out, err = run(self.root, ALICE, "add", "win-1", "--os", "windows", "--shell", "powershell",
                           "--owner-self", "--admin-group", "ops-admins", "--reason", "win box")
        self.assertEqual(rc, 0, err)
        pid = out.strip().split()[-1]
        node = Repo(self.root, identity=ALICE).plan(pid)["ops"][0]["node"]
        self.assertEqual((node["os"], node["shell"], node["owner"]), ("windows", "powershell", "user:alice@ctl-1"))

    def test_add_cli_deprecated_os_warns(self):
        rc, out, err = run(self.root, ALICE, "add", "old-1", "--os", "windows-git-bash", "--reason", "x")
        self.assertEqual(rc, 0, err)
        self.assertIn("deprecated", err)

    def test_add_from_json_fills_admin_group(self):
        p = self.root / "rec.json"
        p.write_text(json.dumps({"id": "rec-1", "name": "rec-1", "ssh": "none", "os": "linux", "shell": "bash",
                                 "enrolment": {"source": "kit", "at": "2026-11-08T09:00:00Z", "by": "user:zoe@rec-1"}}))
        rc, out, err = run(self.root, ALICE, "add", "rec-1", "--from-json", str(p), "--reason", "kit")
        self.assertEqual(rc, 0, err)
        node = Repo(self.root, identity=ALICE).plan(out.strip().split()[-1])["ops"][0]["node"]
        self.assertEqual(node["admin_group"], "ops-admins")
        self.assertEqual(node["enrolment"]["source"], "kit")

    def test_add_from_json_id_must_match(self):
        p = self.root / "rec.json"
        p.write_text(json.dumps({"id": "rec-1", "name": "rec-1", "ssh": "none", "os": "linux"}))
        rc, _, err = run(self.root, ALICE, "add", "other", "--from-json", str(p), "--reason", "kit")
        self.assertNotEqual(rc, 0)

    def test_migrate_proposes_os_mapping_and_enrolment_backfill(self):
        pid = Repo(self.root, identity=ALICE).propose(
            [{"op": "add", "node": new_node("lap-2", os="windows-git-bash")}], reason="old style")
        self.apply(pid)
        # pretend lap-2 predates v2.1: genesis nodes have no enrolment either
        rc, out, err = run(self.root, ALICE, "migrate", "--reason", "adopt 2.1")
        self.assertEqual(rc, 0, err)
        safe = Repo(self.root, identity=ALICE).plan(out.strip().split()[-1])["ops"]
        lap = next(o for o in safe if o["id"] == "lap-2")
        self.assertEqual(lap["set"], {"shell": "git-bash"}, "default keeps os for v1 readers that test os == windows-git-bash")
        Repo(self.root, identity=ALICE).reject(out.strip().split()[-1], "test")
        rc, out, err = run(self.root, ALICE, "migrate", "--rewrite-os", "--reason", "adopt 2.1")
        self.assertEqual(rc, 0, err)
        mpid = out.strip().split()[-1]
        ops = {o["id"]: o for o in Repo(self.root, identity=ALICE).plan(mpid)["ops"]}
        self.assertEqual(ops["lap-2"]["set"]["os"], "windows")
        self.assertEqual(ops["lap-2"]["set"]["shell"], "git-bash")
        self.assertNotIn("enrolment", ops["lap-2"]["set"], "lap-2 already has an enrolment")
        self.assertEqual(ops["gpu-1"]["set"]["enrolment"],
                         {"source": "manual", "at": self.nodes()["gpu-1"]["provenance"]["created_at"], "by": "user:alice@ctl-1"})
        self.assertNotIn("laptop-1", ops, "retired nodes are left as they are")
        self.apply(mpid)
        self.assertEqual(schema.warnings(json.loads((self.root / "nodes.json").read_text()))[0].count("lap-2"), 0)
        self.assertEqual(Repo(self.root, identity=MALLORY).verify(), [])
        rc, out, _ = run(self.root, ALICE, "migrate", "--reason", "again")
        self.assertEqual(rc, 0)
        self.assertIn("nothing to migrate", out)


class V2CompatTests(unittest.TestCase):
    def test_v2_genesis_repo_verifies(self):
        root = make_repo()
        Repo(root, identity=ALICE).init_genesis(seed_doc(), reason="seed")
        self.assertEqual(Repo(root, identity=MALLORY).verify(), [])

    def test_v2_document_version_unchanged(self):
        self.assertTrue(schema.validate_inventory({**sample_doc(), "version": 3}))
        self.assertEqual(schema.load_schema()["properties"]["version"], {"const": 2})


if __name__ == "__main__":
    unittest.main()
