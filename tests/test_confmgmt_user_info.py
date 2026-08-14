import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "roles"
    / "home_files"
    / "library"
    / "confmgmt_user_info.py"
)
SPEC = importlib.util.spec_from_file_location("confmgmt_user_info", MODULE_PATH)
USER_INFO = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(USER_INFO)


def account(name, uid, gid, home):
    return SimpleNamespace(pw_name=name, pw_uid=uid, pw_gid=gid, pw_dir=home)


class UserInfoTests(unittest.TestCase):
    def test_resolves_users_and_preserves_requested_keys(self):
        records = {
            "root": account("root", 0, 0, "/root"),
            "alias": account("canonical", 501, 20, "/Users/canonical"),
        }

        users = USER_INFO.resolve_users(["root", "alias"], lookup=records.__getitem__)

        self.assertEqual(
            users,
            {
                "root": {"name": "root", "uid": 0, "gid": 0, "home": "/root"},
                "alias": {
                    "name": "canonical",
                    "uid": 501,
                    "gid": 20,
                    "home": "/Users/canonical",
                },
            },
        )

    def test_deduplicates_names_without_normalizing_them(self):
        calls = []

        def lookup(name):
            calls.append(name)
            return account(name, 1000, 1000, "/home/%s" % name)

        users = USER_INFO.resolve_users(["user-name", "user.name", "user-name"], lookup=lookup)

        self.assertEqual(calls, ["user-name", "user.name"])
        self.assertEqual(list(users), ["user-name", "user.name"])

    def test_reports_all_missing_users(self):
        def lookup(name):
            raise KeyError(name)

        with self.assertRaises(USER_INFO.UserInfoError) as raised:
            USER_INFO.resolve_users(["missing-one", "missing-two"], lookup=lookup)

        self.assertEqual(raised.exception.missing_users, ["missing-one", "missing-two"])
        self.assertIn("users not found: missing-one, missing-two", str(raised.exception))

    def test_rejects_empty_and_null_containing_names_before_lookup(self):
        def unexpected_lookup(name):
            self.fail("lookup should not run for %r" % name)

        with self.assertRaises(USER_INFO.UserInfoError) as raised:
            USER_INFO.resolve_users(["", "bad\0name"], lookup=unexpected_lookup)

        self.assertEqual(raised.exception.invalid_names, ["''", "'bad\\x00name'"])

    def test_rejects_non_absolute_home_directories(self):
        records = {
            "relative": account("relative", 1000, 1000, "home/relative"),
            "empty": account("empty", 1001, 1001, ""),
        }

        with self.assertRaises(USER_INFO.UserInfoError) as raised:
            USER_INFO.resolve_users(["relative", "empty"], lookup=records.__getitem__)

        self.assertEqual(raised.exception.invalid_home_users, ["relative", "empty"])
        self.assertIn("non-absolute home directories: relative, empty", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
