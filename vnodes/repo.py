"""Inventory repository: read, propose, approve, apply, verify.

Change flow (admin-controlled, no auto-apply anywhere):
  propose  -> plans/pending/<id>.json   anyone with write access (people, later discovery probes)
  approve  -> approval recorded in plan admin-group human; must differ from author unless solo mode
  apply    -> nodes.json + changes.jsonl admin-group human; plan must be approved, unchanged and current
              + git commit with trailers
"""
import copy
import fcntl
import json
import os
import secrets
import shutil
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import SCHEMA_VERSION, chain, schema
from .identity import controller_id, current_identity, is_admin, local_node_id, principal, uid_min_from_login_defs

PROTECTED = {"id", "provenance", "status", "retired_at", "retired_reason"}
WRITE_ONCE = {"enrolment"}  # may be set on a node that has none (backfill), never changed afterwards
DEFAULT_CONFIG = {
    "admin_group": "adm",
    "uid_min": None,
    "solo_mode": {"enabled": False, "reason": ""},
    "git_commit": True,
    "commit_trailers": [],
}


CONFIG_COMMENT = ("Admin policy for vnodes. Changing this file must be followed by `vnodes config-record --reason ...` "
                  "(admin), or verify/apply refuse. solo_mode lets the plan author approve their own plan; it is OFF by "
                  "default and needs a reason when on. Every solo approval is marked in changes.jsonl and in the commit "
                  "trailer (Vnodes-Solo: yes).")
GITIGNORE = ".vnodes.lock\n*.tmp\n__pycache__/\nplans/pending/\nplans/rejected/\n"


class VnodesError(Exception):
    pass


class PermissionDenied(VnodesError):
    pass


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _set_path(obj, dotted, value):
    parts = dotted.split(".")
    for p in parts[:-1]:
        nxt = obj.get(p)
        if nxt is None:
            nxt = obj[p] = {}
        if not isinstance(nxt, dict):
            raise VnodesError(f"cannot set {dotted}: {p} is not an object")
        obj = nxt
    obj[parts[-1]] = value


def _unset_path(obj, dotted):
    parts = dotted.split(".")
    for p in parts[:-1]:
        obj = obj.get(p)
        if not isinstance(obj, dict):
            raise VnodesError(f"cannot unset {dotted}: not present")
    if parts[-1] not in obj:
        raise VnodesError(f"cannot unset {dotted}: not present")
    del obj[parts[-1]]


def _write_json_atomic(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


class Repo:
    def __init__(self, root, identity=None):
        self.root = Path(root).resolve()
        self.identity = identity or current_identity()
        self.nodes_path = self.root / "nodes.json"
        self.changes_path = self.root / "changes.jsonl"
        self.config_path = self.root / "vnodes.config.json"
        self.pending = self.root / "plans" / "pending"
        self.applied = self.root / "plans" / "applied"
        self.rejected = self.root / "plans" / "rejected"
        local_schema = self.root / "schema" / "nodes.schema.json"
        self.schema_path = local_schema if local_schema.exists() else schema.SCHEMA_PATH

    # ── config / identity ────────────────────────────────────────────────
    def config(self):
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        if self.config_path.exists():
            cfg.update(json.loads(self.config_path.read_text()))
        if cfg["uid_min"] is None:
            cfg["uid_min"] = uid_min_from_login_defs()
        solo = cfg.get("solo_mode") or {}
        if solo.get("enabled") and not str(solo.get("reason", "")).strip():
            raise VnodesError("vnodes.config.json: solo_mode.enabled needs a non-empty solo_mode.reason")
        return cfg

    def config_hash(self):
        return chain.canonical_hash(json.loads(self.config_path.read_text())) if self.config_path.exists() else None

    def solo(self):
        return bool(self.config()["solo_mode"].get("enabled"))

    def require_admin(self, action):
        cfg = self.config()
        if not is_admin(self.identity, cfg["admin_group"], cfg["uid_min"]):
            why = (f"uid {self.identity.uid} < UID_MIN {cfg['uid_min']} (run as your own login, not root/sudo)"
                   if self.identity.uid < cfg["uid_min"] else f"not in admin group {cfg['admin_group']!r}")
            raise PermissionDenied(f"{action} refused for {self.identity.login}: {why}")

    # ── reads ────────────────────────────────────────────────────────────
    def load(self):
        if not self.nodes_path.exists():
            raise VnodesError(f"{self.nodes_path} does not exist (run `vnodes init --seed FILE`)")
        return json.loads(self.nodes_path.read_text())

    def validate_doc(self, doc):
        return schema.validate_inventory(doc, schema.load_schema(self.schema_path))

    def verify(self):
        errs = []
        try:
            doc = self.load()
        except (VnodesError, json.JSONDecodeError) as e:
            return [str(e)]
        errs += [f"nodes.json {e}" for e in self.validate_doc(doc)]
        errs += chain.verify(self.changes_path)
        entries = chain.read(self.changes_path) if self.changes_path.exists() else []
        if entries:
            if entries[-1].get("after_hash") != chain.canonical_hash(doc):
                errs.append("nodes.json does not match the last applied change (edited outside vnodes)")
            if entries[-1].get("config_hash") != self.config_hash():
                errs.append("vnodes.config.json changed outside vnodes (an admin must run `vnodes config-record`)")
        return errs

    def require_clean(self):
        errs = self.verify()
        if errs:
            raise VnodesError("repository fails verify; fix before changing it:\n  " + "\n  ".join(errs))

    # ── simulate ops ─────────────────────────────────────────────────────
    def _simulate(self, doc, ops, plan_id, author, approver, ts):
        doc = copy.deepcopy(doc)
        by_id = {n["id"]: n for n in doc["nodes"]}
        if not isinstance(ops, list) or not ops:
            raise VnodesError("a plan needs at least one op")
        touched = set()
        for op in ops:
            kind = op.get("op")
            if kind == "add":
                node = copy.deepcopy(op.get("node") or {})
                nid = node.get("id")
                if nid in by_id:
                    state = by_id[nid].get("status")
                    raise VnodesError(f"add {nid}: id already exists ({state}); ids are never reused")
                if "provenance" in node:
                    raise VnodesError(f"add {nid}: provenance is written by vnodes, not by the plan")
                node.setdefault("status", "active")
                node.setdefault("source", "declared")
                if node["status"] != "active":
                    raise VnodesError(f"add {nid}: new nodes start active")
                node["provenance"] = {"created_at": ts, "created_by": author}
                doc["nodes"].append(node)
                by_id[nid] = node
            elif kind == "set":
                nid = op.get("id")
                node = by_id.get(nid)
                if node is None:
                    raise VnodesError(f"set {nid}: no such node")
                if node.get("status") != "active":
                    raise VnodesError(f"set {nid}: node is retired")
                had = {k for k in WRITE_ONCE if k in node}
                for path, value in (op.get("set") or {}).items():
                    if path.split(".")[0] in PROTECTED:
                        raise VnodesError(f"set {nid}: {path} is protected (use add/retire)")
                    if path.split(".")[0] in had:
                        raise VnodesError(f"set {nid}: {path} is write-once (how the node entered never changes)")
                    _set_path(node, path, value)
                for path in op.get("unset") or []:
                    if path.split(".")[0] in PROTECTED | WRITE_ONCE:
                        raise VnodesError(f"set {nid}: {path} is protected")
                    _unset_path(node, path)
            elif kind == "retire":
                nid = op.get("id")
                node = by_id.get(nid)
                if node is None:
                    raise VnodesError(f"retire {nid}: no such node")
                if node.get("status") != "active":
                    raise VnodesError(f"retire {nid}: already retired")
                if not str(op.get("reason", "")).strip():
                    raise VnodesError(f"retire {nid}: a reason is required")
                node["status"] = "retired"
                node["retired_at"] = ts
                node["retired_reason"] = op["reason"]
            else:
                raise VnodesError(f"unknown op {kind!r} (add|set|retire)")
            touched.add(nid)
        for nid in touched:
            prov = by_id[nid]["provenance"]
            prov.update({"changed_at": ts, "changed_by": author, "change": plan_id})
            if approver:
                prov["approved_by"] = approver
            else:
                prov.pop("approved_by", None)
        errs = self.validate_doc(doc)
        if errs:
            raise VnodesError("change would make nodes.json invalid:\n  " + "\n  ".join(errs))
        return doc

    # ── plans ────────────────────────────────────────────────────────────
    @staticmethod
    def plan_hash(plan):
        keys = ("plan_id", "created_at", "author", "source", "reason", "base_hash", "ops")
        return chain.canonical_hash({k: plan.get(k) for k in keys})

    def _plan_path(self, plan_id):
        if "/" in plan_id or not plan_id.startswith("P-"):
            raise VnodesError(f"bad plan id {plan_id!r}")
        return self.pending / f"{plan_id}.json"

    def plan(self, plan_id):
        for d in (self.pending, self.applied, self.rejected):
            p = d / f"{plan_id}.json"
            if p.exists():
                return json.loads(p.read_text())
        raise VnodesError(f"no plan {plan_id}")

    def plans(self, include_closed=False):
        dirs = [("pending", self.pending)] + ([("applied", self.applied), ("rejected", self.rejected)] if include_closed else [])
        out = []
        for state, d in dirs:
            for p in sorted(d.glob("P-*.json")) if d.exists() else []:
                plan = json.loads(p.read_text())
                plan["_state"] = state
                out.append(plan)
        return out

    def principal(self, doc=None):
        """The acting identity as a v2.1 principal: user:<login>@<id of the node this runs on>."""
        doc = doc if doc is not None else self.load()
        return principal(self.identity, local_node_id(doc.get("nodes")))

    def migrate_ops(self, doc=None, rewrite_os=False):
        """Ops that bring active nodes to v2.1 conventions. Pure: derived from the inventory itself.
        - os windows-git-bash: add shell git-bash. Only with rewrite_os also set os windows: v1 readers that read
          nodes.json directly may still test os == "windows-git-bash" to pick Git Bash, so rewriting os before
          they read `shell` would break them. `vnodes export` already maps it.
        - enrolment backfilled from provenance (manual; by = the creator's login on the controller)"""
        doc = doc if doc is not None else self.load()
        ctl = controller_id(doc.get("nodes")) or local_node_id(doc.get("nodes"))
        ops = []
        for n in doc["nodes"]:
            if n.get("status") != "active":
                continue
            sets = {}
            if n.get("os") == "windows-git-bash":
                if rewrite_os:
                    sets["os"] = "windows"
                if "shell" not in n:
                    sets["shell"] = "git-bash"
            prov = n.get("provenance") or {}
            if "enrolment" not in n and prov.get("created_by"):
                sets["enrolment"] = {"source": "manual", "at": prov["created_at"],
                                     "by": f"user:{prov['created_by']['login']}@{ctl}"}
            if sets:
                ops.append({"op": "set", "id": n["id"], "set": sets})
        return ops

    def propose(self, ops, reason, source="cli"):
        if not str(reason).strip():
            raise VnodesError("a reason is required")
        self.config()  # fail early on bad config
        doc = self.load()
        ts = utc_now()
        plan_id = f"P-{ts.replace('-', '').replace(':', '')}-{secrets.token_hex(3)}"
        author = self.identity.actor()
        ops = copy.deepcopy(ops)
        for op in ops if isinstance(ops, list) else []:
            if op.get("op") == "add" and isinstance(op.get("node"), dict) and "enrolment" not in op["node"]:
                op["node"]["enrolment"] = {"source": "manual", "at": ts, "by": self.principal(doc)}
        self._simulate(doc, ops, plan_id, author, None, ts)
        plan = {"plan_version": 1, "plan_id": plan_id, "created_at": ts, "author": author,
                "source": source, "reason": reason, "base_hash": chain.canonical_hash(doc),
                "ops": ops, "approvals": []}
        self.pending.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(self._plan_path(plan_id), plan)
        return plan_id

    def _check_approver(self, plan, approver):
        if approver["uid"] == plan["author"]["uid"] and not self.solo():
            raise PermissionDenied(
                f"self-approval refused: {approver['login']} wrote plan {plan['plan_id']}. Another admin must "
                "approve, or an admin enables the documented solo mode (vnodes.config.json solo_mode + reason).")

    def approve(self, plan_id):
        self.require_admin("approve")
        path = self._plan_path(plan_id)
        if not path.exists():
            raise VnodesError(f"no pending plan {plan_id}")
        plan = json.loads(path.read_text())
        me = self.identity.actor()
        self._check_approver(plan, me)
        if any(a["uid"] == me["uid"] for a in plan["approvals"]):
            raise VnodesError(f"{me['login']} already approved {plan_id}")
        plan["approvals"].append({**me, "at": utc_now(), "plan_hash": self.plan_hash(plan),
                                  "solo": me["uid"] == plan["author"]["uid"]})
        _write_json_atomic(path, plan)
        return plan

    def reject(self, plan_id, reason):
        self.require_admin("reject")
        path = self._plan_path(plan_id)
        if not path.exists():
            raise VnodesError(f"no pending plan {plan_id}")
        plan = json.loads(path.read_text())
        plan["rejected"] = {**self.identity.actor(), "at": utc_now(), "reason": reason}
        self.rejected.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(self.rejected / path.name, plan)
        path.unlink()
        return plan

    @contextmanager
    def _lock(self):
        with open(self.root / ".vnodes.lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def apply(self, plan_id, trailers=()):
        self.require_admin("apply")
        with self._lock():
            self.require_clean()
            self._require_git()
            path = self._plan_path(plan_id)
            if not path.exists():
                raise VnodesError(f"no pending plan {plan_id}")
            plan = json.loads(path.read_text())
            if not plan.get("approvals"):
                raise VnodesError(f"plan {plan_id} is not approved (vnodes approve {plan_id})")
            approval = plan["approvals"][-1]
            if approval.get("plan_hash") != self.plan_hash(plan):
                raise VnodesError(f"plan {plan_id} changed after approval; re-propose it")
            approver = {"login": approval["login"], "uid": approval["uid"]}
            self._check_approver(plan, approver)  # solo mode may have been switched off since
            doc = self.load()
            before = chain.canonical_hash(doc)
            if plan["base_hash"] != before:
                raise VnodesError(f"stale plan {plan_id}: nodes.json changed since it was proposed; re-propose")
            ts = utc_now()
            new = self._simulate(doc, plan["ops"], plan_id, plan["author"], approver, ts)
            solo = approver["uid"] == plan["author"]["uid"]
            _write_json_atomic(self.nodes_path, new)
            entry = chain.append(self.changes_path, {
                "kind": "change", "ts": ts, "plan_id": plan_id, "reason": plan["reason"],
                "source": plan.get("source", "cli"), "author": plan["author"], "approved_by": approver,
                "applied_by": self.identity.actor(), "solo": solo, "ops": plan["ops"],
                "plan_hash": self.plan_hash(plan), "before_hash": before,
                "after_hash": chain.canonical_hash(new), "config_hash": self.config_hash()})
            plan["applied"] = {**self.identity.actor(), "at": ts, "chain_seq": entry["seq"], "chain_hash": entry["hash"]}
            self.applied.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(self.applied / path.name, plan)
            path.unlink()
            summary = ", ".join(f"{o['op']} {o.get('id') or o.get('node', {}).get('id')}" for o in plan["ops"])
            self._commit(f"vnodes: {summary}\n\n{plan['reason']}", {
                "Vnodes-Change": plan_id, "Vnodes-Author": plan["author"]["login"],
                "Vnodes-Approved-By": approver["login"], "Vnodes-Applied-By": self.identity.login,
                "Vnodes-Solo": "yes" if solo else "no", "Vnodes-Chain-Hash": entry["hash"]}, trailers)
            return entry

    def scaffold(self, admin_group=None, git_commit=True):
        """Prepare a new inventory directory for genesis: the directory, vnodes.config.json, .gitignore and a git
        work tree. Leaves anything that already exists alone. Returns the names it created."""
        if not self.config_path.exists():  # refuse before creating anything
            group = admin_group or DEFAULT_CONFIG["admin_group"]
            uid_min = uid_min_from_login_defs()
            if not is_admin(self.identity, group, uid_min):
                raise PermissionDenied(f"init refused for {self.identity.login}: not an admin (group {group!r}, "
                                       f"uid >= {uid_min}); pass --admin-group with a group you are in")
        else:
            self.require_admin("init")
        created = []
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.config_path.exists():
            cfg = {"$comment": CONFIG_COMMENT, **copy.deepcopy(DEFAULT_CONFIG)}
            if admin_group:
                cfg["admin_group"] = admin_group
            cfg["git_commit"] = bool(git_commit)
            _write_json_atomic(self.config_path, cfg)
            created.append("vnodes.config.json")
        gi = self.root / ".gitignore"
        if not gi.exists():
            gi.write_text(GITIGNORE)
            created.append(".gitignore")
        if self.config().get("git_commit", True) and \
                self._git("rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
            r = self._git("init", "-q")
            if r.returncode != 0:
                raise VnodesError(f"git init failed: {r.stderr.strip()}")
            created.append(".git")
        return created

    def init_genesis(self, seed, reason, trailers=()):
        self.require_admin("init")
        if self.changes_path.exists() or self.nodes_path.exists():
            raise VnodesError("already initialised (genesis happens once; use add/set/retire)")
        self._require_git()
        ts = utc_now()
        me = self.identity.actor()
        doc = copy.deepcopy(seed)
        doc["version"] = SCHEMA_VERSION
        for n in doc.get("nodes", []):
            n.setdefault("status", "active")
            n.setdefault("source", "declared")
            n["provenance"] = {"created_at": ts, "created_by": me, "changed_at": ts,
                               "changed_by": me, "change": "genesis"}
        errs = self.validate_doc(doc)
        if errs:
            raise VnodesError("seed is invalid:\n  " + "\n  ".join(errs))
        with self._lock():
            _write_json_atomic(self.nodes_path, doc)
            entry = chain.append(self.changes_path, {
                "kind": "genesis", "ts": ts, "reason": reason, "author": me, "approved_by": None,
                "applied_by": me, "solo": True, "node_ids": [n["id"] for n in doc["nodes"]],
                "before_hash": None, "after_hash": chain.canonical_hash(doc), "config_hash": self.config_hash()})
            self._commit(f"vnodes: genesis ({len(doc['nodes'])} nodes)\n\n{reason}", {
                "Vnodes-Change": "genesis", "Vnodes-Author": me["login"], "Vnodes-Applied-By": me["login"],
                "Vnodes-Chain-Hash": entry["hash"]}, trailers)
        return entry

    def config_record(self, reason, trailers=()):
        """Record the current vnodes.config.json (e.g. solo mode on/off) in the chain. Admin only."""
        self.require_admin("config-record")
        cfg = self.config()
        with self._lock():
            doc = self.load()
            h = chain.canonical_hash(doc)
            entry = chain.append(self.changes_path, {
                "kind": "config", "ts": utc_now(), "reason": reason, "author": self.identity.actor(),
                "approved_by": None, "applied_by": self.identity.actor(),
                "solo": bool(cfg["solo_mode"].get("enabled")),
                "config": json.loads(self.config_path.read_text()),
                "before_hash": h, "after_hash": h, "config_hash": self.config_hash()})
            self._commit(f"vnodes: record config\n\n{reason}", {
                "Vnodes-Change": "config", "Vnodes-Applied-By": self.identity.login,
                "Vnodes-Chain-Hash": entry["hash"]}, trailers)
        return entry

    # ── git ──────────────────────────────────────────────────────────────
    def _git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True)

    def _require_git(self):
        if self.config().get("git_commit", True):
            if self._git("rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
                raise VnodesError(f"{self.root} is not a git work tree (git_commit is on)")

    def _commit(self, message, vtrailers, extra=()):
        if not self.config().get("git_commit", True):
            return
        lines = [f"{k}: {v}" for k, v in vtrailers.items()]
        lines += list(self.config().get("commit_trailers") or []) + list(extra)
        msg = message.rstrip() + "\n\n" + "\n".join(lines) + "\n"
        paths = ["nodes.json", "changes.jsonl", "vnodes.config.json", ".gitignore", "plans/applied"]
        def has_content(p):
            f = self.root / p
            if f.is_file() or self._git("ls-files", "--", p).stdout.strip():
                return True
            return f.is_dir() and any(x.is_file() for x in f.rglob("*"))
        paths = [p for p in paths if has_content(p)]
        r = self._git("add", "-A", "--", *paths)
        if r.returncode == 0:
            r = self._git("commit", "-q", "-m", msg, "--", *paths)
        if r.returncode != 0:
            raise VnodesError(f"state written and chained, but git commit failed: {r.stderr.strip()}")
