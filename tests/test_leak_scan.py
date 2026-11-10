"""Leak scan: fails when the repository (working tree AND git history) mentions anything specific to a real
deployment.

The forbidden list is DERIVED AT TEST TIME from private sources on the machine running the test and is never
written to the repository:
  - this machine: login name, host name, home directory
  - /etc/hosts and ~/.ssh/config (Host, HostName, User)
  - every inventory file named in VNODES_LEAK_SOURCES (os.pathsep-separated; a real nodes.json, a
    changes.jsonl, plan files): node ids and names, hosts, SSH targets, LAN/tailnet/public names and their
    domain suffixes, IPv4 and MAC addresses, logins (user, provenance, approvals), owner/enrolment principals
On top of that, generic rules that need no private input: no private-range IPv4 addresses, no tailnet
suffixes, no token-shaped strings, every URL host must be a documentation or well-known public host.

VNODES_LEAK_STRICT=1 fails when VNODES_LEAK_SOURCES names no readable file (use it before every commit and
before publishing). Git history (every line ever added, commit messages included) is scanned whenever the
repository has commits.

Run: VNODES_LEAK_STRICT=1 VNODES_LEAK_SOURCES=/path/to/private/nodes.json python3 -m unittest tests.test_leak_scan
"""
import getpass
import json
import os
import re
import socket
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "__pycache__"}
BINARY = re.compile(r"\.(png|jpg|jpeg|gif|ico|pdf|zip|gz|tgz|pyc)$", re.I)

# The published schema identifiers are fixed by the schema contract and are the only allowed occurrences of
# the project's original name. They are removed before scanning; nothing else is exempt.
ALLOWED_LITERALS = ("urn:vigyan-nodes:schema:node:2.1", "urn:vigyan-nodes:schema:nodes:2.1")

IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
MAC = re.compile(r"\b[0-9a-f]{2}(?::[0-9a-f]{2}){5}\b", re.I)
URL_HOST = re.compile(r"\b[a-z][a-z0-9+.-]*://(?:[^/@\s\"'`]+@)?([a-z0-9.-]+|\[[0-9a-f:]+\])", re.I)
EMAIL = re.compile(r"\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b", re.I)
DOC_IP = re.compile(r"^(127\.|0\.0\.0\.0$|255\.|192\.0\.2\.|198\.51\.100\.|203\.0\.113\.)")
PRIVATE_IP = re.compile(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.|169\.254\.)")
TAILNET = re.compile(r"\.ts\.net\b|\.beta\.tailscale\.net\b", re.I)
SECRET_SHAPES = [re.compile(p) for p in (
    r"\bsk-[A-Za-z0-9_-]{20,}", r"\bghp_[A-Za-z0-9]{20,}", r"\bgithub_pat_[A-Za-z0-9_]{20,}",
    r"\bxox[abpr]-[A-Za-z0-9-]{10,}", r"\bAKIA[0-9A-Z]{16}\b", r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.", r"\bhf_[A-Za-z0-9]{30,}", r"\btskey-[A-Za-z0-9-]{10,}")]
PUBLIC_HOSTS = {"github.com", "json-schema.org", "www.apache.org", "apache.org", "www.rfc-editor.org",
                "datatracker.ietf.org", "spdx.org", "docs.python.org", "www.python.org", "git-scm.com"}
DOC_MACS = re.compile(r"^02:00:5e:00:53:", re.I)   # RFC 7042 documentation range
# ordinary words that show up as host labels / logins everywhere; never distinctive on their own
GENERIC = {"localhost", "local", "localdomain", "ip6-localhost", "ip6-loopback", "ip6-allnodes", "ip6-allrouters",
           "broadcasthost", "root", "user", "admin", "ubuntu", "git", "host", "hosts", "node", "nodes", "gateway",
           "server", "servers", "router", "switch", "desktop", "laptop", "home", "test", "tests", "none", "true",
           "false", "null", "default", "config", "build", "http", "https", "mail", "cloud", "proxy", "files",
           "example.com", "github.com", "ops"}


def _is_ip(s):
    return bool(re.fullmatch(r"(\d{1,3}\.){3}\d{1,3}", s))


class Forbidden:
    def __init__(self):
        self.words, self.subs, self.ips, self.macs, self._hosts, self.sources = set(), set(), set(), set(), [], []

    def ip(self, v):
        if v and not DOC_IP.match(v):
            self.ips.add(v)

    def word(self, w):
        w = str(w or "").strip().lower()
        if len(w) >= 4 and w not in GENERIC:
            self.words.add(w)
        for part in re.split(r"[\s@]+", w):  # "First Last" -> first, last
            if part != w and len(part) >= 4 and part not in GENERIC:
                self.words.add(part)

    def host(self, h, stem=True):
        h = str(h or "").strip().lower().rstrip(".")
        if not h or h in GENERIC or "*" in h:
            return
        if _is_ip(h):
            return self.ip(h)
        if len(h) >= 4:
            self.words.add(h)
            if stem:
                self._hosts.append(h)
        labels = h.split(".")
        if len(labels) >= 3:                       # a.b.c -> its private suffix b.c (tailnet / LAN domain)
            self.words.add(".".join(labels[1:]))
            for lab in labels[1:-1]:
                if len(lab) >= 4 and lab not in GENERIC:
                    self.words.add(lab)
        if labels[0] != h and len(labels[0]) >= 4:
            self.words.add(labels[0])

    def principal(self, p):
        m = re.fullmatch(r"user:(.+)@([a-z0-9-]+)", str(p or ""))
        if m:
            self.word(m.group(1)); self.host(m.group(2))
            self.words.add(str(p).lower())

    def text(self, t):
        for m in IPV4.finditer(t):
            self.ip(m.group(0))
        for m in MAC.finditer(t):
            if not DOC_MACS.match(m.group(0)):
                self.macs.add(m.group(0).lower())
        for m in URL_HOST.finditer(t):
            self.host(m.group(1), stem=False)
        for m in EMAIL.finditer(t):
            self.word(m.group(0)); self.word(m.group(0).split("@")[0])
        for m in re.finditer(r"/home/([a-z0-9._-]+)", t, re.I):
            self.word(f"/home/{m.group(1)}")

    def inventory_node(self, n):
        if not isinstance(n, dict):
            return
        for k in ("id", "name", "host"):
            self.host(n.get(k))
        ssh = n.get("ssh")
        if ssh not in (None, "local", "none"):
            user, _, target = str(ssh).rpartition("@")
            self.host(target); self.word(user)
        self.word(n.get("user"))
        for k, v in (n.get("addresses") or {}).items():
            (self.ip if k.endswith("ipv4") else self.host)(v)
        wol = n.get("wol") or {}
        if wol.get("mac"):
            self.macs.add(wol["mac"].lower())
        self.ip(wol.get("broadcast"))
        self.principal(n.get("owner"))
        self.principal((n.get("enrolment") or {}).get("by"))
        for v in (n.get("provenance") or {}).values():
            if isinstance(v, dict):
                self.word(v.get("login"))
        card = n.get("card") or {}
        for k in ("node_id", "host_key_sha256"):
            if card.get(k):
                self.words.add(str(card[k]).lower())
        self.text(json.dumps(n.get("services") or {}))

    def json_doc(self, v):
        """Any vnodes document: an inventory, a plan, a chain entry. Walks for node records and actors."""
        if isinstance(v, dict):
            if "id" in v and ("ssh" in v or "addresses" in v or "os" in v):
                self.inventory_node(v)
            if isinstance(v.get("login"), str) and "uid" in v:
                self.word(v["login"])
            for x in v.values():
                self.json_doc(x)
        elif isinstance(v, list):
            for x in v:
                self.json_doc(x)
        elif isinstance(v, str):
            self.text(v)

    def source_file(self, path):
        t = path.read_text(errors="replace")
        self.sources.append(str(path))
        try:
            self.json_doc(json.loads(t))
            return
        except json.JSONDecodeError:
            pass
        lines = [l for l in t.splitlines() if l.strip()]
        try:
            for l in lines:  # JSON Lines (changes.jsonl)
                self.json_doc(json.loads(l))
        except json.JSONDecodeError:
            self.text(t)

    def finish(self):
        # a leading label shared by several private host names ("acme-a", "acme-b" -> "acme") is a substring match
        stems = {}
        for h in self._hosts:
            s = re.split(r"[-.]", h)[0]
            if len(s) >= 4:
                stems[s] = stems.get(s, 0) + 1
        self.subs |= {s for s, c in stems.items() if c >= 2 and s not in GENERIC}
        return self


def derive_forbidden():
    f = Forbidden()
    try:
        login = getpass.getuser().lower()
    except Exception:  # noqa: BLE001 - no login name available
        login = ""
    if len(login) >= 3 and login not in GENERIC:
        f.subs.add(login)
    f.host(socket.gethostname()); f.host(socket.gethostname().split(".")[0])
    f.word(str(Path.home()))
    try:
        for line in Path("/etc/hosts").read_text().splitlines():
            fields = line.split("#")[0].split()
            if len(fields) < 2 or re.match(r"^(127\.|::1|fe00|ff0)", fields[0]):
                continue
            if _is_ip(fields[0]):
                f.ip(fields[0])
            for h in fields[1:]:
                f.host(h)
    except OSError:
        pass
    try:
        for line in (Path.home() / ".ssh" / "config").read_text().splitlines():
            m = re.match(r"^\s*(Host|HostName|User)\s+(.+)$", line, re.I)
            if m:
                for v in m.group(2).split():
                    (f.word if m.group(1).lower() == "user" else f.host)(v)
    except OSError:
        pass
    for p in filter(None, os.environ.get("VNODES_LEAK_SOURCES", "").split(os.pathsep)):
        p = Path(p).expanduser()
        if p.is_file():
            f.source_file(p)
    return f.finish()


def host_allowed(h):
    h = h.lower().strip("[]")
    if h in PUBLIC_HOSTS or h == "localhost" or h == "::1":
        return True
    if re.search(r"(^|\.)example(\.(com|org|net))?$", h) or re.search(r"\.(test|invalid)$", h):
        return True
    if _is_ip(h):
        return bool(DOC_IP.match(h))
    return h in ("host", "node", "gateway")   # bare placeholders in docs, e.g. http://gateway:4000


def scan_text(name, text, forb):
    for lit in ALLOWED_LITERALS:
        text = text.replace(lit, "")
    problems = []
    lower = text.lower()
    for w in sorted(forb.words):
        if re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", lower):
            problems.append(f"{name}: private name/path {w!r}")
    for s in sorted(forb.subs):
        if s in lower:
            problems.append(f"{name}: private name {s!r}")
    for m in IPV4.finditer(text):
        ip = m.group(0)
        if ip in forb.ips:
            problems.append(f"{name}: private address {ip}")
        elif PRIVATE_IP.match(ip):
            problems.append(f"{name}: private-range address {ip}")
    for m in MAC.finditer(text):
        if m.group(0).lower() in forb.macs or not DOC_MACS.match(m.group(0)):
            problems.append(f"{name}: MAC address outside the documentation range {m.group(0)}")
    if TAILNET.search(text):
        problems.append(f"{name}: tailnet suffix")
    for rx in SECRET_SHAPES:
        if rx.search(text):
            problems.append(f"{name}: token-shaped string {rx.pattern}")
    for m in URL_HOST.finditer(text):
        if not host_allowed(m.group(1)):
            problems.append(f"{name}: URL host not allowed: {m.group(1)}")
    return problems


def repo_files():
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        out += [Path(dirpath) / f for f in filenames if not BINARY.search(f)]
    return sorted(out)


# attribution trailers the project's commit policy requires; reviewed by hand before publishing
TRAILER = re.compile(r"^\s*(Co-Authored-By|Claude-Session):", re.I)
FORB = derive_forbidden()


class LeakScanTests(unittest.TestCase):
    def test_private_sources_were_read(self):
        self.assertTrue(FORB.words, "at least this machine's host name and home directory")
        if os.environ.get("VNODES_LEAK_STRICT") == "1":
            self.assertTrue(FORB.sources, "VNODES_LEAK_STRICT=1 needs VNODES_LEAK_SOURCES naming a readable inventory")

    def test_working_tree(self):
        problems = []
        files = repo_files()
        for f in files:
            problems += scan_text(str(f.relative_to(ROOT)), f.read_text(errors="replace"), FORB)
        problems += scan_text("(paths)", "\n".join(str(f.relative_to(ROOT)) for f in files), FORB)
        self.assertEqual(sorted(set(problems)), [])

    def test_git_history(self):
        r = subprocess.run(["git", "-C", str(ROOT), "log", "--all", "-p", "--no-color", "--format=commit %H%n%B"],
                           capture_output=True, text=True, errors="replace")
        if r.returncode != 0 or not r.stdout.strip():
            self.skipTest("no git history yet")
        # removed lines ("-") are in no tree but stay in history, so they ARE scanned; only "---" headers and
        # the required attribution trailers are skipped
        lines = [l for l in r.stdout.splitlines() if not l.startswith("--- ") and not TRAILER.match(l)]
        self.assertEqual(sorted(set(scan_text("(history)", "\n".join(lines), FORB))), [])

    def test_rules_catch_synthetic_leaks(self):
        f = Forbidden()
        f.inventory_node({"id": "zorb-alpha", "name": "zorb-alpha", "ssh": "ops@zorb-alpha", "user": "Pat Quill",
                          "addresses": {"lan_ipv4": "198.18.7.7", "tailnet_name": "zorb-alpha.blue-heron.corp"},
                          "wol": {"mac": ":".join(["ae"] * 6)}, "owner": "user:pquill@zorb-alpha"})
        f.inventory_node({"id": "zorb-beta", "name": "zorb-beta", "ssh": "zorb-beta"})
        f.finish()
        hit = lambda t: bool(scan_text("x", t, f))
        self.assertTrue(hit("ssh zorb-alpha"))
        self.assertTrue(hit("brand Zorbcorp"))                    # shared stem, substring
        self.assertTrue(hit("domain blue-heron.corp"))
        self.assertTrue(hit("addr 198.18.7.7"))
        self.assertTrue(hit("by Pat Quill"))
        self.assertTrue(hit("owner user:pquill@x"))
        self.assertTrue(hit("mac " + ":".join(["ae"] * 6)))
        self.assertTrue(hit(f"addr {'.'.join(['10', '1', '2', '3'])}"))
        self.assertTrue(hit(f"host box{'.ts'}.net"))
        self.assertTrue(hit(f"key {'sk-'}{'a' * 24}"))
        self.assertTrue(hit(f"url https://{'intranet'}.corp.internal/x"))
        self.assertFalse(hit("url https://www.example.com and http://127.0.0.1:4000 and 192.0.2.10 mac 02:00:5e:00:53:11"))
        self.assertFalse(hit(ALLOWED_LITERALS[0]))


if __name__ == "__main__":
    unittest.main()
