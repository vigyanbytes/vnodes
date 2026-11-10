"""A fresh, empty inventory starts from `vnodes init` (no seed, no hand-written files)."""
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tests.helpers import ALICE, BOB, MALLORY
from vnodes import cli
from vnodes.repo import Repo

# git needs an identity for the genesis commit; never use the developer's own
GIT_ENV = {"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com",
           "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.com",
           "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "commit.gpgsign", "GIT_CONFIG_VALUE_0": "false",
           "GIT_CONFIG_KEY_1": "init.defaultBranch", "GIT_CONFIG_VALUE_1": "main"}


def run(root, who, *argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(["--repo", str(root), *argv], identity=who)
    return rc, out.getvalue(), err.getvalue()


@mock.patch.dict(os.environ, GIT_ENV)
class InitTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="vnodes-init-")) / "inventory"

    def test_empty_init_then_first_node(self):
        rc, out, err = run(self.root, ALICE, "init", "--admin-group", "ops-admins", "--reason", "new inventory")
        self.assertEqual(rc, 0, err)
        for f in ("vnodes.config.json", ".gitignore", ".git"):
            self.assertIn(f"created {f}", out)
        doc = json.loads((self.root / "nodes.json").read_text())
        self.assertEqual(doc, {"version": 2, "nodes": []})
        self.assertEqual(json.loads((self.root / "vnodes.config.json").read_text())["admin_group"], "ops-admins")
        self.assertEqual(run(self.root, MALLORY, "verify")[0], 0)
        self.assertEqual(run(self.root, MALLORY, "list")[0], 0)
        tracked = subprocess.run(["git", "-C", str(self.root), "ls-files"], capture_output=True, text=True).stdout.split()
        self.assertEqual(sorted(tracked), [".gitignore", "changes.jsonl", "nodes.json", "vnodes.config.json"])
        # first node through the normal flow
        rc, out, err = run(self.root, ALICE, "add", "ctl-1", "--ssh", "local", "--roles", "control.plane",
                           "--lan-ipv4", "192.0.2.10", "--reason", "the controller")
        self.assertEqual(rc, 0, err)
        pid = out.strip().split()[-1]
        self.assertEqual(run(self.root, BOB, "approve", pid)[0], 0)
        rc, _, err = run(self.root, BOB, "apply", pid)
        self.assertEqual(rc, 0, err)
        self.assertEqual(Repo(self.root, identity=MALLORY).verify(), [])
        self.assertEqual([n["id"] for n in json.loads((self.root / "nodes.json").read_text())["nodes"]], ["ctl-1"])

    def test_init_refused_for_non_admin_and_creates_nothing(self):
        rc, _, err = run(self.root, MALLORY, "init", "--admin-group", "ops-admins", "--reason", "x")
        self.assertEqual(rc, 4, err)
        self.assertFalse(self.root.exists())

    def test_init_only_once(self):
        self.assertEqual(run(self.root, ALICE, "init", "--admin-group", "ops-admins", "--reason", "x")[0], 0)
        rc, _, err = run(self.root, ALICE, "init", "--reason", "again")
        self.assertEqual(rc, 1)
        self.assertIn("already initialised", err)

    def test_init_from_seed(self):
        seed = Path(__file__).resolve().parent.parent / "examples" / "nodes.sample.json"
        rc, out, err = run(self.root, ALICE, "init", "--admin-group", "ops-admins", "--seed", str(seed), "--reason", "x")
        self.assertEqual(rc, 0, err)
        self.assertIn("4 nodes", out)


if __name__ == "__main__":
    unittest.main()
