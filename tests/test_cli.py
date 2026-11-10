import io
import json
import shlex
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

from tests.helpers import ALICE, BOB, MALLORY, ROOT, make_repo, seed_doc
from vnodes import cli
from vnodes.repo import Repo


def run(root, who, *argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(["--repo", str(root), *argv], identity=who)
    return rc, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        Repo(self.root, identity=ALICE).init_genesis(seed_doc(), reason="seed")

    def test_read_commands_need_no_privilege(self):
        for argv in (["validate"], ["list"], ["list", "--all"], ["show", "gpu-1"], ["verify"], ["plans"],
                     ["export", "--format", "json"], ["export", "--format", "env"]):
            rc, out, err = run(self.root, MALLORY, *argv)
            self.assertEqual(rc, 0, (argv, err))

    def test_list_hides_retired_by_default(self):
        _, out, _ = run(self.root, MALLORY, "list")
        self.assertIn("gpu-1", out); self.assertNotIn("laptop-1", out)
        _, out, _ = run(self.root, MALLORY, "list", "--all")
        self.assertIn("laptop-1", out)

    def test_show_unknown(self):
        rc, _, err = run(self.root, MALLORY, "show", "nope")
        self.assertNotEqual(rc, 0)

    def test_validate_file_flag(self):
        rc, _, _ = run(self.root, MALLORY, "validate", "--file", str(ROOT / "examples" / "nodes.sample.json"))
        self.assertEqual(rc, 0)

    def test_add_approve_apply_via_cli(self):
        rc, out, err = run(self.root, ALICE, "add", "new-1", "--os", "linux", "--ssh", "new-1",
                           "--roles", "client", "--admin-group", "ops-admins",
                           "--lan-ipv4", "192.0.2.50", "--gpu", "nvidia:test:8", "--ram-gb", "16",
                           "--service", "vllm:8000", "--reason", "new box")
        self.assertEqual(rc, 0, err)
        pid = out.strip().split()[-1]
        rc, _, err = run(self.root, ALICE, "approve", pid)
        self.assertEqual(rc, 4, err)          # self-approval refused
        rc, _, err = run(self.root, MALLORY, "approve", pid)
        self.assertEqual(rc, 4, err)          # non-admin refused
        self.assertEqual(run(self.root, BOB, "approve", pid)[0], 0)
        rc, _, err = run(self.root, BOB, "apply", pid, "--trailer", "X-Test: 1")
        self.assertEqual(rc, 0, err)
        _, out, _ = run(self.root, MALLORY, "show", "new-1", "--json")
        n = json.loads(out)
        self.assertEqual(n["capabilities"]["gpu"], {"vendor": "nvidia", "model": "test", "vram_gb": 8})
        self.assertEqual(n["services"], {"vllm": {"port": 8000}})
        self.assertEqual(n["addresses"]["lan_ipv4"], "192.0.2.50")

    def test_set_and_retire_via_cli(self):
        rc, out, err = run(self.root, ALICE, "set", "gpu-1", "capabilities.ram_gb=96", "display_name=GPU box",
                           "--unset", "addresses.lan_ipv4", "--reason", "upgrade")
        self.assertEqual(rc, 0, err)
        pid = out.strip().split()[-1]
        run(self.root, BOB, "approve", pid); run(self.root, BOB, "apply", pid)
        _, out, _ = run(self.root, MALLORY, "show", "gpu-1", "--json")
        n = json.loads(out)
        self.assertEqual(n["capabilities"]["ram_gb"], 96)
        self.assertNotIn("lan_ipv4", n["addresses"])
        rc, out, err = run(self.root, ALICE, "retire", "gpu-1", "--reason", "dead")
        self.assertEqual(rc, 0, err)

    def test_export_json_active_only_with_v1_compat_fields(self):
        _, out, _ = run(self.root, MALLORY, "export", "--format", "json")
        d = json.loads(out)
        self.assertEqual(d["version"], 2)
        ids = [n["id"] for n in d["nodes"]]
        self.assertIn("ctl-1", ids); self.assertNotIn("laptop-1", ids)

    def test_export_env_is_shell_safe(self):
        _, out, _ = run(self.root, MALLORY, "export", "--format", "env")
        env = {}
        for line in out.splitlines():
            k, v = line.split("=", 1)
            env[k] = shlex.split(v)[0] if v else ""
        self.assertEqual(env["VNODES_SCHEMA_VERSION"], "2")
        self.assertEqual(env["VNODES_ROLE_COMPUTE_GPU"], "gpu-1")
        self.assertEqual(env["VNODES_ROLE_VAULT_ORIGIN"], "ctl-1")
        self.assertEqual(env["VNODES_NODE_GPU_1_LAN_IPV4"], "192.0.2.14")
        self.assertIn("ctl-1", env["VNODES_IDS"].split())
        # sourceable by sh
        r = subprocess.run(["sh", "-c", out + '\necho "$VNODES_ROLE_GATEWAY"'], capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "ctl-1")

    def test_export_version_check(self):
        rc, _, err = run(self.root, MALLORY, "export", "--format", "json", "--require-version", "3")
        self.assertEqual(rc, 3)
        self.assertIn("version", err)

    def test_verify_fails_on_tamper(self):
        p = self.root / "nodes.json"
        d = json.loads(p.read_text()); d["nodes"][0]["display_name"] = "sneaky"
        p.write_text(json.dumps(d))
        rc, _, _ = run(self.root, MALLORY, "verify")
        self.assertNotEqual(rc, 0)

    def test_config_record_clears_drift_admin_only(self):
        cfg = self.root / "vnodes.config.json"
        d = json.loads(cfg.read_text()); d["solo_mode"] = {"enabled": True, "reason": "only one admin today"}
        cfg.write_text(json.dumps(d, indent=2))
        self.assertNotEqual(run(self.root, MALLORY, "verify")[0], 0)
        self.assertEqual(run(self.root, MALLORY, "config-record", "--reason", "x")[0], 4)
        rc, _, err = run(self.root, ALICE, "config-record", "--reason", "solo for now")
        self.assertEqual(rc, 0, err)
        self.assertEqual(run(self.root, MALLORY, "verify")[0], 0)

    def test_bin_shim_runs(self):
        r = subprocess.run([sys.executable, str(ROOT / "bin" / "vnodes"), "--repo", str(self.root), "list"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("gpu-1", r.stdout)


if __name__ == "__main__":
    unittest.main()
