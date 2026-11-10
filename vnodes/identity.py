"""Who is acting: derived from the OS login (pwd/grp through NSS), never typed, never from env vars.

Local users, SSSD, authd and Kanidm all surface through NSS, so this works unchanged when the
directory moves off /etc/passwd. $USER/$LOGNAME/$SUDO_USER are deliberately ignored.
"""
import grp
import os
import pwd
from dataclasses import dataclass


@dataclass(frozen=True)
class Identity:
    login: str
    uid: int
    groups: frozenset

    def actor(self):
        return {"login": self.login, "uid": self.uid}

    def principal(self, node_id):
        return principal(self, node_id)


def principal(identity, node_id):
    """Schema v2.1 principal: user:<login>@<node-id>. The login comes from the derived identity, never typed."""
    return f"user:{identity.login}@{node_id}"


def local_node_id(nodes, hostname=None):
    """Inventory id of the machine this runs on: the node whose name/id is the short host name, else the
    controller (ssh == "local"), else the short host name itself (lower-case, slug characters only)."""
    import re
    import socket
    short = (hostname or socket.gethostname()).split(".")[0].lower()
    for n in nodes or []:
        if short in (n.get("name"), n.get("id")):
            return n["id"]
    return controller_id(nodes) or re.sub(r"[^a-z0-9-]+", "-", short).strip("-") or "localhost"


def current_identity():
    pw = pwd.getpwuid(os.getuid())
    names = set()
    for gid in os.getgrouplist(pw.pw_name, pw.pw_gid):
        try:
            names.add(grp.getgrgid(gid).gr_name)
        except KeyError:
            pass
    return Identity(pw.pw_name, pw.pw_uid, frozenset(names))


def uid_min_from_login_defs(path="/etc/login.defs", default=1000):
    try:
        with open(path) as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "UID_MIN":
                    return int(parts[1])
    except (OSError, ValueError):
        pass
    return default


def is_admin(identity, admin_group, uid_min):
    """Admin = a human login (uid >= UID_MIN, so never root/sudo or system accounts) in admin_group."""
    return identity.uid >= uid_min and admin_group in identity.groups


def controller_id(nodes):
    """The controller: the active node reached as ssh == "local" (where vnodes runs). None if not declared."""
    for n in nodes or []:
        if n.get("ssh") == "local" and n.get("status", "active") == "active":
            return n["id"]
    return None
