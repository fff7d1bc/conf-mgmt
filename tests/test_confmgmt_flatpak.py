import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "roles"
    / "flatpak"
    / "library"
    / "confmgmt_flatpak.py"
)
SPEC = importlib.util.spec_from_file_location("confmgmt_flatpak", MODULE_PATH)
FLATPAK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FLATPAK)


EXECUTABLE = "/usr/bin/flatpak"
REMOTE_URL = "https://dl.flathub.org/repo/flathub.flatpakrepo"


class FakeModule:
    def __init__(self, responses, check_mode=False):
        self.responses = list(responses)
        self.check_mode = check_mode
        self.calls = []

    def run_command(self, command, environ_update=None):
        if not self.responses:
            raise AssertionError("Unexpected command: %r" % command)

        expected_command, response = self.responses.pop(0)
        if command != expected_command:
            raise AssertionError("Expected command %r, got %r" % (expected_command, command))

        environment = dict(environ_update or {})
        self.calls.append((list(command), environment))
        return response

    def assert_complete(self):
        if self.responses:
            raise AssertionError(
                "Commands were not run: %r" % [response[0] for response in self.responses]
            )


def response(stdout="", stderr="", rc=0):
    return rc, stdout, stderr


def installed_output(*refs):
    return "".join(
        "%s\t%s\t%s\n" % (application, ref, commit)
        for application, ref, commit in refs
    )


REMOTES_COMMAND = [
    EXECUTABLE,
    "remotes",
    "--system",
    "--show-disabled",
    "--columns=name,options",
]
LIST_COMMAND = [
    EXECUTABLE,
    "list",
    "--system",
    "--all",
    "--columns=application,ref,active",
]
UPDATE_COMMAND = [EXECUTABLE, "update", "--system", "--noninteractive"]


class InputAndParsingTests(unittest.TestCase):
    def test_package_ids_are_deduplicated(self):
        self.assertEqual(
            FLATPAK.normalize_packages(
                ["org.example.Application", "io.example.Second", "org.example.Application"]
            ),
            ["org.example.Application", "io.example.Second"],
        )

    def test_package_ids_reject_options_whitespace_and_malformed_ids(self):
        for package in (
            "--help",
            " org.example.Application",
            "org.example",
            "org..example.Application",
            "org/example/Application",
            "org.example.Application\n",
        ):
            with self.subTest(package=package), self.assertRaises(FLATPAK.FlatpakError):
                FLATPAK.normalize_packages([package])

    def test_remote_validation_rejects_unsafe_values(self):
        invalid_values = (
            ("--system", REMOTE_URL),
            ("flathub bad", REMOTE_URL),
            ("flathub", "--no-gpg-verify"),
            ("flathub", ""),
            ("flathub", "https://example.invalid/repo\nother"),
        )
        for remote, remote_url in invalid_values:
            with self.subTest(remote=remote, remote_url=remote_url), self.assertRaises(
                FLATPAK.FlatpakError
            ):
                FLATPAK.validate_remote(remote, remote_url)

    def test_remote_listing_tracks_disabled_option(self):
        self.assertEqual(
            FLATPAK.parse_remotes("flathub\t\nnightly\tdisabled,no-enumerate\n"),
            {
                "flathub": {"enabled": True},
                "nightly": {"enabled": False},
            },
        )

    def test_remote_listing_rejects_duplicate_names(self):
        with self.assertRaises(FLATPAK.FlatpakError):
            FLATPAK.parse_remotes("flathub\t\nflathub\tdisabled\n")

    def test_installed_ref_listing_is_exact_and_structured(self):
        stdout = installed_output(
            ("org.example.App", "app/org.example.App/x86_64/stable", "commit-one"),
            ("org.example.Platform", "runtime/org.example.Platform/x86_64/1", "commit-two"),
        )

        self.assertEqual(
            FLATPAK.parse_installed_refs(stdout),
            {
                "app/org.example.App/x86_64/stable": {
                    "application": "org.example.App",
                    "active_commit": "commit-one",
                },
                "runtime/org.example.Platform/x86_64/1": {
                    "application": "org.example.Platform",
                    "active_commit": "commit-two",
                },
            },
        )

    def test_installed_ref_listing_rejects_malformed_rows(self):
        for stdout in ("org.example.App\n", "app\tref\n", "app\tref\t\n"):
            with self.subTest(stdout=stdout), self.assertRaises(FLATPAK.FlatpakError):
                FLATPAK.parse_installed_refs(stdout)

    def test_changed_refs_detects_added_removed_and_updated_refs(self):
        before = {
            "same": {"application": "same", "active_commit": "one"},
            "updated": {"application": "updated", "active_commit": "one"},
            "removed": {"application": "removed", "active_commit": "one"},
        }
        after = {
            "same": {"application": "same", "active_commit": "one"},
            "updated": {"application": "updated", "active_commit": "two"},
            "added": {"application": "added", "active_commit": "one"},
        }

        self.assertEqual(
            FLATPAK.changed_refs(before, after),
            ["added", "removed", "updated"],
        )

    def test_only_application_refs_satisfy_declared_application_ids(self):
        refs = {
            "runtime/org.example.Shared/x86_64/stable": {
                "application": "org.example.Shared",
                "active_commit": "runtime-commit",
            },
            "app/org.example.App/x86_64/stable": {
                "application": "org.example.App",
                "active_commit": "app-commit",
            },
        }

        self.assertEqual(
            FLATPAK.installed_application_ids(refs),
            {"org.example.App"},
        )


class FlatpakManagerTests(unittest.TestCase):
    def manager(self, module, **kwargs):
        defaults = {
            "module": module,
            "executable": EXECUTABLE,
            "packages": [],
            "remote": "flathub",
            "remote_url": REMOTE_URL,
            "method": "system",
            "upgrade_all": False,
        }
        defaults.update(kwargs)
        return FLATPAK.FlatpakManager(**defaults)

    def test_noop_update_uses_commit_snapshots_not_command_output(self):
        installed = installed_output(
            ("org.example.App", "app/org.example.App/x86_64/stable", "same-commit"),
        )
        module = FakeModule(
            [
                (REMOTES_COMMAND, response("flathub\t\n")),
                (LIST_COMMAND, response(installed)),
                (UPDATE_COMMAND, response("Human-facing output in any language\n")),
                (LIST_COMMAND, response(installed)),
            ]
        )

        result = self.manager(
            module,
            packages=["org.example.App"],
            upgrade_all=True,
        ).run()

        module.assert_complete()
        self.assertFalse(result["changed"])
        self.assertFalse(result["updates_changed"])
        self.assertFalse(result["packages_changed"])
        self.assertEqual(result["updated_refs"], [])
        self.assertEqual(result["install_candidates"], [])
        self.assertEqual(result["msg"], "Flatpak: installation is current")
        for unused_command, environment in module.calls:
            self.assertEqual(environment, {"LANGUAGE": "C", "LC_ALL": "C"})

    def test_remote_update_and_install_are_each_single_transactions(self):
        before = installed_output(
            ("it.mijorus.gearlever", "app/it.mijorus.gearlever/x86_64/stable", "old-app"),
            ("org.example.Platform", "runtime/org.example.Platform/x86_64/1", "old-runtime"),
        )
        after = installed_output(
            ("it.mijorus.gearlever", "app/it.mijorus.gearlever/x86_64/stable", "new-app"),
            ("org.example.Platform", "runtime/org.example.Platform/x86_64/1", "new-runtime"),
        )
        module = FakeModule(
            [
                (REMOTES_COMMAND, response()),
                (LIST_COMMAND, response(before)),
                (
                    [
                        EXECUTABLE,
                        "remote-add",
                        "--system",
                        "--if-not-exists",
                        "flathub",
                        REMOTE_URL,
                    ],
                    response(),
                ),
                (UPDATE_COMMAND, response("Updates complete\n")),
                (LIST_COMMAND, response(after)),
                (
                    [
                        EXECUTABLE,
                        "install",
                        "--system",
                        "--noninteractive",
                        "flathub",
                        "io.github.ilya_zlobintsev.LACT",
                    ],
                    response(),
                ),
            ]
        )

        result = self.manager(
            module,
            packages=["it.mijorus.gearlever", "io.github.ilya_zlobintsev.LACT"],
            upgrade_all=True,
        ).run()

        module.assert_complete()
        self.assertTrue(result["changed"])
        self.assertTrue(result["remote_added"])
        self.assertTrue(result["updates_changed"])
        self.assertTrue(result["packages_changed"])
        self.assertEqual(
            result["updated_refs"],
            [
                "app/it.mijorus.gearlever/x86_64/stable",
                "runtime/org.example.Platform/x86_64/1",
            ],
        )
        self.assertEqual(
            result["install_candidates"],
            ["io.github.ilya_zlobintsev.LACT"],
        )
        self.assertEqual(
            result["msg"],
            "Flatpak: added remote flathub; updated 2 Flatpak refs; installed 1 application",
        )

    def test_existing_disabled_remote_is_enabled(self):
        module = FakeModule(
            [
                (REMOTES_COMMAND, response("flathub\tdisabled,no-enumerate\n")),
                (LIST_COMMAND, response()),
                (
                    [EXECUTABLE, "remote-modify", "--system", "--enable", "flathub"],
                    response(),
                ),
            ]
        )

        result = self.manager(module).run()

        module.assert_complete()
        self.assertTrue(result["changed"])
        self.assertFalse(result["remote_added"])
        self.assertTrue(result["remote_enable_changed"])
        self.assertEqual(result["msg"], "Flatpak: enabled remote flathub")

    def test_check_mode_reports_remote_updates_and_packages_without_mutation(self):
        module = FakeModule(
            [
                (REMOTES_COMMAND, response()),
                (LIST_COMMAND, response()),
                (
                    [
                        EXECUTABLE,
                        "remote-ls",
                        "--system",
                        "--updates",
                        "--columns=ref",
                    ],
                    response(
                        "app/org.example.Existing/x86_64/stable\n"
                        "runtime/org.example.Platform/x86_64/1\n"
                    ),
                ),
            ],
            check_mode=True,
        )

        result = self.manager(
            module,
            packages=["org.example.NewApplication"],
            upgrade_all=True,
        ).run()

        module.assert_complete()
        self.assertTrue(result["changed"])
        self.assertTrue(result["remote_added"])
        self.assertEqual(
            result["update_candidates"],
            [
                "app/org.example.Existing/x86_64/stable",
                "runtime/org.example.Platform/x86_64/1",
            ],
        )
        self.assertEqual(result["install_candidates"], ["org.example.NewApplication"])
        self.assertEqual(
            result["msg"],
            "Flatpak check mode: would add remote flathub; would update 2 Flatpak refs; "
            "would install 1 application",
        )

    def test_upgrade_can_be_disabled_without_extra_queries(self):
        installed = installed_output(
            ("org.example.App", "app/org.example.App/x86_64/stable", "commit"),
        )
        module = FakeModule(
            [
                (REMOTES_COMMAND, response("flathub\t\n")),
                (LIST_COMMAND, response(installed)),
            ]
        )

        result = self.manager(module, packages=["org.example.App"]).run()

        module.assert_complete()
        self.assertFalse(result["changed"])
        self.assertEqual(result["msg"], "Flatpak: installation is current")

    def test_user_method_is_applied_to_every_command(self):
        user_remotes_command = [
            EXECUTABLE,
            "remotes",
            "--user",
            "--show-disabled",
            "--columns=name,options",
        ]
        user_list_command = [
            EXECUTABLE,
            "list",
            "--user",
            "--all",
            "--columns=application,ref,active",
        ]
        module = FakeModule(
            [
                (user_remotes_command, response("flathub\t\n")),
                (user_list_command, response()),
                (
                    [
                        EXECUTABLE,
                        "install",
                        "--user",
                        "--noninteractive",
                        "flathub",
                        "org.example.App",
                    ],
                    response(),
                ),
            ]
        )

        result = self.manager(
            module,
            packages=["org.example.App"],
            method="user",
        ).run()

        module.assert_complete()
        self.assertTrue(result["packages_changed"])

    def test_remote_add_failure_does_not_claim_a_change(self):
        module = FakeModule(
            [
                (REMOTES_COMMAND, response()),
                (LIST_COMMAND, response()),
                (
                    [
                        EXECUTABLE,
                        "remote-add",
                        "--system",
                        "--if-not-exists",
                        "flathub",
                        REMOTE_URL,
                    ],
                    response(stderr="network failure", rc=1),
                ),
            ]
        )
        manager = self.manager(module)

        with self.assertRaises(FLATPAK.FlatpakError) as raised:
            manager.run()

        module.assert_complete()
        self.assertEqual(raised.exception.rc, 1)
        self.assertFalse(manager.result["changed"])
        self.assertTrue(manager.result["remote_added"])

    def test_failed_snapshot_after_update_preserves_possible_change(self):
        module = FakeModule(
            [
                (REMOTES_COMMAND, response("flathub\t\n")),
                (LIST_COMMAND, response()),
                (UPDATE_COMMAND, response()),
                (LIST_COMMAND, response("malformed\n")),
            ]
        )
        manager = self.manager(module, upgrade_all=True)

        with self.assertRaises(FLATPAK.FlatpakError) as raised:
            manager.run()

        module.assert_complete()
        self.assertEqual(raised.exception.command, LIST_COMMAND)
        self.assertTrue(manager.result["changed"])


if __name__ == "__main__":
    unittest.main()
