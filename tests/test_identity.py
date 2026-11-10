import os
import pwd
import unittest

from tests.helpers import ALICE, MALLORY, ROOTUSER, SYSACCT
from vnodes.identity import current_identity, is_admin, uid_min_from_login_defs


class IdentityTests(unittest.TestCase):
    def test_current_identity_comes_from_os_login(self):
        me = current_identity()
        self.assertEqual(me.uid, os.getuid())
        self.assertEqual(me.login, pwd.getpwuid(os.getuid()).pw_name)
        self.assertIn(me.login, me.groups | {me.login})

    def test_env_cannot_spoof_identity(self):
        old = os.environ.get("USER"), os.environ.get("LOGNAME")
        os.environ["USER"] = os.environ["LOGNAME"] = "someone-else"
        try:
            self.assertEqual(current_identity().login, pwd.getpwuid(os.getuid()).pw_name)
        finally:
            for k, v in zip(("USER", "LOGNAME"), old):
                if v is None: os.environ.pop(k, None)
                else: os.environ[k] = v

    def test_admin_rules(self):
        self.assertTrue(is_admin(ALICE, "ops-admins", 1000))
        self.assertFalse(is_admin(MALLORY, "ops-admins", 1000))
        self.assertFalse(is_admin(ROOTUSER, "ops-admins", 1000), "root/sudo is refused: run as yourself")
        self.assertFalse(is_admin(SYSACCT, "ops-admins", 1000), "system accounts are not admins")

    def test_uid_min_parse(self):
        self.assertGreaterEqual(uid_min_from_login_defs(), 1)


if __name__ == "__main__":
    unittest.main()
