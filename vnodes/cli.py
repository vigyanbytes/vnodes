"""vnodes CLI. Read commands need no privilege; changes go plan -> approve -> apply.

Exit codes: 0 ok · 1 error / verify failed · 2 usage · 3 schema version mismatch · 4 permission denied
"""
import argparse
import json
import os
import re
import shlex
import sys
from pathlib import Path

from . import SCHEMA_VERSION, schema
from .repo import PermissionDenied, Repo, VnodesError



def _csv(v):
    return [x.strip() for x in v.split(",") if x.strip()] if v else []


def _value(raw):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _env_key(s):
    return re.sub(r"[^A-Z0-9]", "_", s.upper())


def _host(n):
    return n.get("host") or n["name"]


def _role_holders(nodes, role):
    holders = [n for n in nodes if role in (n.get("roles") or [])]
    return sorted(holders, key=lambda n: role not in (n.get("primary_for") or []))  # stable: primary first


def _gpu(v):
    if v in ("none", "null"):
        return None
    parts = v.split(":")
    g = {"vendor": parts[0]}
    if len(parts) > 1 and parts[1]:
        g["model"] = parts[1]
    if len(parts) > 2 and parts[2]:
        g["vram_gb"] = _value(parts[2])
    return g


OS_CHOICES = ["linux", "windows", "macos", "windows-git-bash"]
SHELL_CHOICES = ["bash", "zsh", "powershell", "git-bash"]


def build_node(a, cfg, repo=None):
    if a.from_json:
        n = json.loads(Path(a.from_json).read_text())
        if not isinstance(n, dict) or n.get("id") != a.id:
            raise VnodesError(f"--from-json: node id {n.get('id') if isinstance(n, dict) else None!r} is not {a.id!r}")
        n.setdefault("admin_group", a.admin_group or cfg["admin_group"])
        return n
    n = {"id": a.id, "name": a.name or a.id, "ssh": a.ssh, "os": a.os,
         "admin_group": a.admin_group or cfg["admin_group"], "status": "active", "source": "declared"}
    if a.owner_self:
        n["owner"] = repo.principal()
    for key, val in (("shell", a.shell), ("display_name", a.display_name), ("user", a.user), ("role", a.role),
                     ("host", a.host), ("os_detail", a.os_detail), ("notes", a.notes)):
        if val:
            n[key] = val
    if a.roles:
        n["roles"] = _csv(a.roles)
    if a.primary_for:
        n["primary_for"] = _csv(a.primary_for)
    if a.mcp_profiles:
        n["mcp_profiles"] = _csv(a.mcp_profiles)
    addr = {k: getattr(a, k) for k in ("lan_name", "lan_ipv4", "tailnet_name", "tailnet_ipv4", "public_name")
            if getattr(a, k)}
    if addr:
        n["addresses"] = addr
    caps = {}
    if a.gpu is not None:
        caps["gpu"] = _gpu(a.gpu)
    if a.ram_gb is not None:
        caps["ram_gb"] = _value(a.ram_gb)
    if a.cpu_cores is not None:
        caps["cpu_cores"] = a.cpu_cores
    if a.zfs is not None:
        caps["zfs"] = a.zfs
    if caps:
        caps["basis"] = "declared"
        n["capabilities"] = caps
    if a.service:
        svcs = {}
        for s in a.service:
            name, _, rest = s.partition(":")
            svcs[name] = {"port": int(rest)} if rest.isdigit() else ({"path": rest} if rest else {})
        n["services"] = svcs
    return n


def parser():
    p = argparse.ArgumentParser(prog="vnodes", description="Layer-0 node inventory (admin-controlled).")
    p.add_argument("--repo", default=os.environ.get("VNODES_REPO") or ".",
                   help="inventory directory (default: $VNODES_REPO, else the current directory)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("validate", help="schema-check nodes.json (or --file)")
    s.add_argument("--file")
    s = sub.add_parser("list", help="list nodes (active only unless --all)")
    s.add_argument("--all", action="store_true"); s.add_argument("--json", action="store_true")
    s = sub.add_parser("show", help="show one node"); s.add_argument("id"); s.add_argument("--json", action="store_true")
    sub.add_parser("verify", help="schema + hash chain + nodes.json/config match the chain head")
    s = sub.add_parser("plans", help="list plans"); s.add_argument("--all", action="store_true")
    s = sub.add_parser("show-plan", help="print a plan"); s.add_argument("plan_id")
    s = sub.add_parser("export", help="read-only export for consumers")
    s.add_argument("--format", choices=["json", "env"], required=True)
    s.add_argument("--all", action="store_true", help="include retired nodes")
    s.add_argument("--file", help="read this inventory file instead of the repo's nodes.json")
    s.add_argument("--require-version", type=int, help="exit 3 unless the file's schema version is this")

    s = sub.add_parser("add", help="propose adding a node (writes a pending plan)")
    s.add_argument("id")
    s.add_argument("--from-json", help="node object from a JSON file (other flags ignored)")
    s.add_argument("--name"); s.add_argument("--display-name"); s.add_argument("--host")
    s.add_argument("--ssh", default="none"); s.add_argument("--user")
    s.add_argument("--os", choices=OS_CHOICES, default="linux", help="windows-git-bash is deprecated: --os windows --shell git-bash")
    s.add_argument("--shell", choices=SHELL_CHOICES)
    s.add_argument("--owner-self", action="store_true", help="owner = you (user:<your OS login>@<this node>), derived, never typed")
    s.add_argument("--os-detail"); s.add_argument("--role", choices=["dev", "server"])
    s.add_argument("--roles"); s.add_argument("--primary-for"); s.add_argument("--mcp-profiles")
    s.add_argument("--admin-group")
    for k in ("lan-name", "lan-ipv4", "tailnet-name", "tailnet-ipv4", "public-name"):
        s.add_argument(f"--{k}")
    s.add_argument("--gpu", help="vendor:model:vram_gb or none"); s.add_argument("--ram-gb")
    s.add_argument("--cpu-cores", type=int)
    s.add_argument("--zfs", dest="zfs", action="store_true", default=None)
    s.add_argument("--no-zfs", dest="zfs", action="store_false")
    s.add_argument("--service", action="append", help="name:port or name:/path (repeatable)")
    s.add_argument("--notes"); s.add_argument("--reason", required=True)

    s = sub.add_parser("set", help="propose field changes: key=value (dotted keys, JSON values)")
    s.add_argument("id"); s.add_argument("assign", nargs="*")
    s.add_argument("--unset", action="append", default=[]); s.add_argument("--reason", required=True)
    s = sub.add_parser("retire", help="propose retiring a node"); s.add_argument("id"); s.add_argument("--reason", required=True)
    s = sub.add_parser("migrate", help="propose the v2.1 migration (deprecated os -> os + shell, enrolment backfill)")
    s.add_argument("--rewrite-os", action="store_true",
                   help="also rewrite os windows-git-bash -> windows (only once v1 readers use `shell`)")
    s.add_argument("--reason", required=True)

    s = sub.add_parser("approve", help="approve a pending plan (admin; not the author unless solo mode)")
    s.add_argument("plan_id")
    s = sub.add_parser("reject", help="reject a pending plan (admin)"); s.add_argument("plan_id"); s.add_argument("--reason", required=True)
    s = sub.add_parser("apply", help="apply an approved plan (admin): nodes.json + chain + git commit")
    s.add_argument("plan_id"); s.add_argument("--trailer", action="append", default=[])
    s = sub.add_parser("init", help="one-time genesis: an empty inventory, or --seed FILE (admin)")
    s.add_argument("--seed", help="inventory document to start from (default: no nodes)")
    s.add_argument("--admin-group", help="admin group for a new vnodes.config.json (default: adm)")
    s.add_argument("--no-git", action="store_true", help="new config with git_commit off (not recommended)")
    s.add_argument("--reason", required=True)
    s.add_argument("--trailer", action="append", default=[])
    s = sub.add_parser("config-record", help="record vnodes.config.json in the chain (admin)")
    s.add_argument("--reason", required=True); s.add_argument("--trailer", action="append", default=[])
    return p


def _print_table(nodes):
    rows = [("id", "status", "os", "roles", "lan", "tailnet")]
    for n in nodes:
        a = n.get("addresses") or {}
        rows.append((n["id"], n["status"], n["os"], ",".join(n.get("roles") or []) or "-",
                     a.get("lan_ipv4") or a.get("lan_name") or "-", a.get("tailnet_name") or "-"))
    w = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(c.ljust(w[i]) for i, c in enumerate(r)).rstrip())


def _warn(doc):
    for w in schema.warnings(doc):
        print(f"warning: {w}", file=sys.stderr)


def export(doc, fmt, include_retired, src):
    nodes = [schema.export_node(n) for n in doc["nodes"] if include_retired or n.get("status") == "active"]
    if fmt == "json":
        print(json.dumps({"version": doc["version"], "nodes": nodes}, indent=2, ensure_ascii=False))
        return
    q = shlex.quote
    out = [f"VNODES_SCHEMA_VERSION={doc['version']}", f"VNODES_FILE={q(str(src))}",
           f"VNODES_IDS={q(' '.join(n['id'] for n in nodes))}"]
    for n in nodes:
        k = f"VNODES_NODE_{_env_key(n['id'])}"
        a = n.get("addresses") or {}
        vals = {"NAME": n["name"], "HOST": _host(n), "SSH": n["ssh"], "OS": n["os"], "STATUS": n["status"],
                "ROLES": " ".join(n.get("roles") or []), "ADMIN_GROUP": n["admin_group"]}
        for f in ("shell", "owner"):
            if n.get(f):
                vals[f.upper()] = n[f]
        for f in ("lan_name", "lan_ipv4", "tailnet_name", "tailnet_ipv4", "public_name"):
            if a.get(f):
                vals[f.upper()] = a[f]
        out += [f"{k}_{name}={q(str(v))}" for name, v in vals.items()]
    active = [n for n in nodes if n.get("status") == "active"]
    roles = sorted({r for n in active for r in (n.get("roles") or [])})
    for r in roles:
        holders = _role_holders(active, r)
        out.append(f"VNODES_ROLE_{_env_key(r)}={q(_host(holders[0]))}")
        out.append(f"VNODES_ROLE_{_env_key(r)}_ALL={q(' '.join(_host(h) for h in holders))}")
    print("\n".join(out))


def run(a, identity):
    repo = Repo(a.repo, identity=identity)
    c = a.cmd
    if c == "validate":
        path = Path(a.file) if a.file else repo.nodes_path
        doc = json.loads(path.read_text())
        errs = schema.validate_inventory(doc, schema.load_schema(repo.schema_path))
        for e in errs:
            print(e, file=sys.stderr)
        _warn(doc)
        print(f"{path}: {'INVALID' if errs else 'valid'} ({len(errs)} errors)")
        return 1 if errs else 0
    if c == "verify":
        errs = repo.verify()
        for e in errs:
            print(e, file=sys.stderr)
        try:
            _warn(repo.load())
        except (VnodesError, json.JSONDecodeError):
            pass
        print("verify: FAILED" if errs else "verify: ok (schema, chain, inventory and config match head)")
        return 1 if errs else 0
    if c == "list":
        nodes = [n for n in repo.load()["nodes"] if a.all or n["status"] == "active"]
        if a.json:
            print(json.dumps(nodes, indent=2, ensure_ascii=False))
        else:
            _print_table(nodes)
        return 0
    if c == "show":
        n = next((n for n in repo.load()["nodes"] if n["id"] == a.id or n["name"] == a.id), None)
        if n is None:
            print(f"no node {a.id}", file=sys.stderr)
            return 1
        print(json.dumps(n, indent=2, ensure_ascii=False))
        return 0
    if c == "plans":
        for p in repo.plans(include_closed=a.all):
            ops = ", ".join(f"{o['op']} {o.get('id') or o.get('node', {}).get('id')}" for o in p["ops"])
            appr = ",".join(x["login"] for x in p.get("approvals", [])) or "-"
            print(f"{p['plan_id']}  {p['_state']:8}  by {p['author']['login']:10} approved:{appr:10} {ops}  — {p['reason']}")
        return 0
    if c == "show-plan":
        print(json.dumps(repo.plan(a.plan_id), indent=2, ensure_ascii=False))
        return 0
    if c == "export":
        path = Path(a.file) if a.file else repo.nodes_path
        doc = json.loads(path.read_text())
        want = a.require_version
        if want is not None and doc.get("version") != want:
            print(f"vnodes export: {path} has schema version {doc.get('version')}, consumer requires {want}", file=sys.stderr)
            return 3
        errs = schema.validate_inventory(doc, schema.load_schema(repo.schema_path))
        if errs:
            print(f"vnodes export: {path} is invalid; refusing to export:\n  " + "\n  ".join(errs), file=sys.stderr)
            return 1
        export(doc, a.format, a.all, path)
        return 0
    if c == "add":
        node = build_node(a, repo.config(), repo)
        if node.get("os") == "windows-git-bash":
            print("warning: os 'windows-git-bash' is deprecated: use --os windows --shell git-bash", file=sys.stderr)
        pid = repo.propose([{"op": "add", "node": node}], reason=a.reason)
    elif c == "migrate":
        ops = repo.migrate_ops(rewrite_os=a.rewrite_os)
        if not ops:
            print("nothing to migrate: active nodes already follow schema v2.1")
            return 0
        pid = repo.propose(ops, reason=a.reason, source="migrate:2.1")
    elif c == "set":
        sets = {}
        for kv in a.assign:
            k, eq, v = kv.partition("=")
            if not eq:
                print(f"set: expected key=value, got {kv!r}", file=sys.stderr)
                return 2
            sets[k] = _value(v)
        op = {"op": "set", "id": a.id, "set": sets}
        if a.unset:
            op["unset"] = a.unset
        pid = repo.propose([op], reason=a.reason)
    elif c == "retire":
        pid = repo.propose([{"op": "retire", "id": a.id, "reason": a.reason}], reason=a.reason)
    elif c == "approve":
        repo.approve(a.plan_id)
        print(f"approved {a.plan_id} as {repo.identity.login}; apply with: vnodes apply {a.plan_id}")
        return 0
    elif c == "reject":
        repo.reject(a.plan_id, a.reason)
        print(f"rejected {a.plan_id}")
        return 0
    elif c == "apply":
        e = repo.apply(a.plan_id, trailers=a.trailer)
        print(f"applied {a.plan_id}: chain seq {e['seq']} {e['hash'][:16]}…")
        return 0
    elif c == "init":
        seed = json.loads(Path(a.seed).read_text()) if a.seed else {"version": SCHEMA_VERSION, "nodes": []}
        created = repo.scaffold(admin_group=a.admin_group, git_commit=not a.no_git)
        for f in created:
            print(f"created {f}")
        e = repo.init_genesis(seed, reason=a.reason, trailers=a.trailer)
        print(f"genesis written: {len(e['node_ids'])} nodes, chain {e['hash'][:16]}…")
        return 0
    elif c == "config-record":
        e = repo.config_record(a.reason, trailers=a.trailer)
        print(f"config recorded: chain seq {e['seq']}")
        return 0
    print(f"plan written: plans/pending/{pid}.json\nnext: another admin runs `vnodes approve {pid}` then `vnodes apply {pid}`\n{pid}")
    return 0


def main(argv=None, identity=None):
    a = parser().parse_args(argv)
    try:
        return run(a, identity)
    except PermissionDenied as e:
        print(f"vnodes: {e}", file=sys.stderr)
        return 4
    except (VnodesError, OSError, json.JSONDecodeError) as e:
        print(f"vnodes: {e}", file=sys.stderr)
        return 1
