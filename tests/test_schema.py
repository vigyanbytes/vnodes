import copy
import json
import re
import unittest

from tests.helpers import ROOT, sample_doc
from vnodes import schema


class SchemaTests(unittest.TestCase):
    def test_schema_file_is_draft_2020_12(self):
        s = schema.load_schema()
        self.assertEqual(s["$schema"], "https://json-schema.org/draft/2020-12/schema")

    def test_sample_is_valid(self):
        self.assertEqual(schema.validate_inventory(sample_doc()), [])

    def test_empty_inventory_is_valid(self):
        self.assertEqual(schema.validate_inventory({"version": 2, "nodes": []}), [])

    def bad(self, mutate):
        d = sample_doc()
        mutate(d)
        errs = schema.validate_inventory(d)
        self.assertTrue(errs, "expected validation errors")
        return errs

    def test_wrong_version(self):
        self.bad(lambda d: d.__setitem__("version", 1))

    def test_missing_required(self):
        errs = self.bad(lambda d: d["nodes"][0].pop("admin_group"))
        self.assertTrue(any("admin_group" in e for e in errs))

    def test_unknown_property(self):
        errs = self.bad(lambda d: d["nodes"][0].__setitem__("password", "x"))
        self.assertTrue(any("password" in e for e in errs))

    def test_bad_enum(self):
        self.bad(lambda d: d["nodes"][0].__setitem__("os", "beos"))

    def test_bad_role(self):
        self.bad(lambda d: d["nodes"][0]["roles"].append("chief.of.everything"))

    def test_bad_pattern(self):
        self.bad(lambda d: d["nodes"][0].__setitem__("id", "Bad_ID"))

    def test_bad_ipv4(self):
        self.bad(lambda d: d["nodes"][0]["addresses"].__setitem__("lan_ipv4", "300.1.1.1"))

    def test_bad_type(self):
        self.bad(lambda d: d["nodes"][1]["capabilities"].__setitem__("ram_gb", "lots"))

    def test_gpu_null_allowed(self):
        d = sample_doc()
        d["nodes"][0]["capabilities"]["gpu"] = None
        self.assertEqual(schema.validate_inventory(d), [])

    def test_bad_timestamp(self):
        self.bad(lambda d: d["nodes"][0]["provenance"].__setitem__("changed_at", "yesterday"))

    def test_duplicate_ids(self):
        def dup(d):
            n = copy.deepcopy(d["nodes"][0]); n["name"] = "other"; d["nodes"].append(n)
        errs = self.bad(dup)
        self.assertTrue(any("duplicate id" in e for e in errs))

    def test_duplicate_names(self):
        def dup(d):
            n = copy.deepcopy(d["nodes"][0]); n["id"] = "other"; d["nodes"].append(n)
        errs = self.bad(dup)
        self.assertTrue(any("duplicate name" in e for e in errs))

    def test_primary_for_subset_of_roles(self):
        errs = self.bad(lambda d: d["nodes"][1].__setitem__("primary_for", ["gateway"]))
        self.assertTrue(any("primary_for" in e for e in errs))

    def test_retired_needs_retired_at(self):
        self.bad(lambda d: d["nodes"][3].pop("retired_at"))

    def test_examples_use_documentation_values_only(self):
        # names and addresses of real machines are caught by tests/test_leak_scan.py; this is the generic rule
        for f in sorted((ROOT / "examples").rglob("*.json")):
            text = f.read_text()
            for ip in re.findall(r"\b\d+\.\d+\.\d+\.\d+\b", text):
                self.assertTrue(ip.startswith(("192.0.2.", "198.51.100.", "203.0.113.")), (f.name, ip))
            for host in re.findall(r'"(?:lan_name|tailnet_name|public_name|host)":\s*"([^"]+)"', text):
                self.assertTrue(host.endswith((".example", ".example.com", ".example.org", ".example.net")), (f.name, host))


if __name__ == "__main__":
    unittest.main()


class InventoryHoldsNoSecretsTests(unittest.TestCase):
    def test_genesis_inventory_holds_no_secret_looking_values(self):
        from tests.helpers import ALICE, make_repo, seed_doc
        from vnodes.repo import Repo
        root = make_repo()
        Repo(root, identity=ALICE).init_genesis(seed_doc(), reason="seed")
        text = (root / "nodes.json").read_text() + (root / "changes.jsonl").read_text()
        for pat in (r"(?i)\b(password|passwd|token|secret|api[_-]?key|private[_-]?key)\b",
                    r"sk-[A-Za-z0-9]{16,}", r"-----BEGIN", r"ghp_[A-Za-z0-9]{20,}", r"tskey-"):
            self.assertIsNone(re.search(pat, text), pat)
