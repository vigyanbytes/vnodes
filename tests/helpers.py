"""Shared fixtures: throwaway repos and fake identities (injected, never read from env)."""
import copy
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from vnodes.identity import Identity

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "examples" / "nodes.sample.json"

ALICE = Identity("alice", 1001, frozenset({"alice", "ops-admins"}))
BOB = Identity("bob", 1002, frozenset({"bob", "ops-admins"}))
MALLORY = Identity("mallory", 1003, frozenset({"mallory", "users"}))
ROOTUSER = Identity("root", 0, frozenset({"root", "ops-admins"}))
SYSACCT = Identity("syslog", 104, frozenset({"syslog", "ops-admins"}))


def sample_doc():
    return json.loads(SAMPLE.read_text())


def seed_doc():
    """Sample without provenance (genesis adds it)."""
    d = copy.deepcopy(sample_doc())
    for n in d["nodes"]:
        n.pop("provenance", None)
    return d


def new_node(node_id="new-1", **over):
    n = {"id": node_id, "name": node_id, "ssh": node_id, "os": "linux",
         "admin_group": "ops-admins", "status": "active", "roles": ["client"]}
    n.update(over)
    return n


def make_repo(solo=False, git=True):
    tmp = Path(tempfile.mkdtemp(prefix="vnodes-test-"))
    (tmp / "schema").mkdir()
    for f in ("nodes.schema.json", "node.schema.json"):
        shutil.copy(ROOT / "schema" / f, tmp / "schema" / f)
    cfg = {"admin_group": "ops-admins", "uid_min": 1000,
           "solo_mode": {"enabled": solo, "reason": "test solo" if solo else ""},
           "git_commit": git}
    (tmp / "vnodes.config.json").write_text(json.dumps(cfg, indent=2) + "\n")
    if git:
        run = lambda *a: subprocess.run(["git", "-C", str(tmp), *a], check=True, capture_output=True)
        run("init", "-q", "-b", "main")
        run("config", "user.name", "test")
        run("config", "user.email", "test@example.com")
        run("config", "commit.gpgsign", "false")
        run("add", "-A")
        run("commit", "-q", "-m", "fixture")
    return tmp


def git_log(root):
    return subprocess.run(["git", "-C", str(root), "log", "--format=%B%x00"],
                          check=True, capture_output=True, text=True).stdout
