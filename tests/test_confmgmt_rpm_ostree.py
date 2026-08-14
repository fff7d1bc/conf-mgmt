import importlib.util
import json
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "roles"
    / "rpm_ostree"
    / "library"
    / "confmgmt_rpm_ostree.py"
)
SPEC = importlib.util.spec_from_file_location("confmgmt_rpm_ostree", MODULE_PATH)
RPM_OSTREE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RPM_OSTREE)


EXECUTABLE = "/usr/bin/rpm-ostree"


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


def status_response(requested_packages=None, booted=True):
    return (
        0,
        json.dumps(
            {
                "deployments": [
                    {
                        "booted": booted,
                        "requested-packages": requested_packages or [],
                    }
                ]
            }
        ),
        "",
    )


class InputTests(unittest.TestCase):
    def test_packages_are_deduplicated_and_malformed_names_are_rejected(self):
        self.assertEqual(
            RPM_OSTREE.normalize_packages(["tailscale", "zsh", "tailscale"]),
            ["tailscale", "zsh"],
        )

        for package in ("--help", " zsh", "repo/package", "bad\nname", ""):
            with self.subTest(package=package), self.assertRaises(RPM_OSTREE.RpmOstreeError):
                RPM_OSTREE.normalize_packages([package])

    def test_kargs_accept_flags_and_quoted_single_tokens(self):
        self.assertEqual(
            RPM_OSTREE.normalize_kargs(["quiet", '"loglevel=3 quiet"']),
            ["quiet", "loglevel=3 quiet"],
        )
        self.assertEqual(
            RPM_OSTREE.format_karg_argument("loglevel=3 quiet"),
            'loglevel="3 quiet"',
        )

    def test_kargs_reject_duplicate_keys_reserved_key_and_multiple_tokens(self):
        invalid_kargs = (
            ["quiet", "quiet=1"],
            ["ostree=/ostree/example"],
            ["foo=bar baz=quux"],
            ["unterminated='value"],
            ["=value"],
        )
        for kargs in invalid_kargs:
            with self.subTest(kargs=kargs), self.assertRaises(RPM_OSTREE.RpmOstreeError):
                RPM_OSTREE.normalize_kargs(kargs)


class ParsingAndPlanningTests(unittest.TestCase):
    def test_status_uses_first_deployment_and_requested_packages(self):
        parsed = RPM_OSTREE.parse_status(
            json.dumps(
                {
                    "deployments": [
                        {"booted": False, "requested-packages": ["zsh"]},
                        {"booted": True, "requested-packages": ["old"]},
                    ]
                }
            )
        )

        self.assertEqual(parsed["requested_packages"], {"zsh"})
        self.assertTrue(parsed["reboot_required"])

    def test_status_rejects_malformed_or_empty_data(self):
        for stdout in ("not json", "{}", '{"deployments": []}', '{"deployments": [1]}'):
            with self.subTest(stdout=stdout), self.assertRaises(RPM_OSTREE.RpmOstreeError):
                RPM_OSTREE.parse_status(stdout)

    def test_karg_plan_replaces_managed_keys_and_leaves_others_alone(self):
        current = [
            "ostree=/ostree/example",
            "quiet",
            "ttm.pages_limit=1",
            "mitigations=off",
            "ttm.pages_limit=2",
        ]

        to_remove, to_append = RPM_OSTREE.plan_karg_changes(
            current,
            ["quiet", "ttm.pages_limit=29360128", "amd_iommu=on"],
        )

        self.assertEqual(to_remove, ["ttm.pages_limit=1", "ttm.pages_limit=2"])
        self.assertEqual(
            to_append,
            ["ttm.pages_limit=29360128", "amd_iommu=on"],
        )

    def test_karg_plan_collapses_duplicate_current_values(self):
        self.assertEqual(
            RPM_OSTREE.plan_karg_changes(["quiet", "quiet"], ["quiet"]),
            (["quiet", "quiet"], ["quiet"]),
        )

    def test_result_summary_describes_changes_and_reboot_state(self):
        result = {
            "changed": True,
            "upgrade_changed": True,
            "upgrade_check_skipped": False,
            "package_candidates": ["zsh", "tmux"],
            "packages_changed": True,
            "kargs_to_append": ["quiet"],
            "kargs_changed": True,
            "reboot_required": True,
        }

        self.assertEqual(
            RPM_OSTREE.summarize_result(result),
            "rpm-ostree: staged operating system upgrade; requested 2 layered packages; "
            "reconciled 1 kernel argument key; reboot required",
        )


class RpmOstreeManagerTests(unittest.TestCase):
    def manager(self, module, **kwargs):
        defaults = {
            "module": module,
            "executable": EXECUTABLE,
            "packages": [],
            "kargs": [],
            "upgrade": False,
        }
        defaults.update(kwargs)
        return RPM_OSTREE.RpmOstreeManager(**defaults)

    def test_noop_uses_machine_readable_state_and_exit_77(self):
        module = FakeModule(
            [
                ([EXECUTABLE, "status", "--json"], status_response(["zsh"])),
                ([EXECUTABLE, "upgrade", "--unchanged-exit-77"], (77, "", "")),
                ([EXECUTABLE, "kargs"], (0, "quiet ttm.pages_limit=29360128\n", "")),
                ([EXECUTABLE, "status", "--json"], status_response(["zsh"])),
            ]
        )

        result = self.manager(
            module,
            packages=["zsh"],
            kargs=["quiet", "ttm.pages_limit=29360128"],
            upgrade=True,
        ).run()

        module.assert_complete()
        self.assertFalse(result["changed"])
        self.assertFalse(result["upgrade_changed"])
        self.assertFalse(result["packages_changed"])
        self.assertFalse(result["kargs_changed"])
        self.assertEqual(result["package_candidates"], [])
        self.assertEqual(result["kargs_to_remove"], [])
        self.assertEqual(result["kargs_to_append"], [])
        self.assertEqual(
            result["msg"],
            "rpm-ostree: deployment is current; no reboot required",
        )
        for unused_command, environment in module.calls:
            self.assertEqual(environment, {"LANGUAGE": "C", "LC_ALL": "C"})

    def test_packages_and_kargs_are_each_batched_after_upgrade(self):
        kargs_command = [
            EXECUTABLE,
            "kargs",
            "--unchanged-exit-77",
            "--delete=ttm.pages_limit=1",
            "--delete=ttm.pages_limit=2",
            "--append=ttm.pages_limit=29360128",
            "--append=amd_iommu=on",
        ]
        module = FakeModule(
            [
                ([EXECUTABLE, "status", "--json"], status_response(["jq"])),
                ([EXECUTABLE, "upgrade", "--unchanged-exit-77"], (0, "", "")),
                (
                    [
                        EXECUTABLE,
                        "install",
                        "--allow-inactive",
                        "--idempotent",
                        "--unchanged-exit-77",
                        "ripgrep",
                        "zsh",
                    ],
                    (0, "", ""),
                ),
                (
                    [EXECUTABLE, "kargs"],
                    (
                        0,
                        "ostree=/ostree/example quiet ttm.pages_limit=1 "
                        "mitigations=off ttm.pages_limit=2\n",
                        "",
                    ),
                ),
                (kargs_command, (0, "", "")),
                (
                    [EXECUTABLE, "status", "--json"],
                    status_response(["jq", "ripgrep", "zsh"], booted=False),
                ),
            ]
        )

        result = self.manager(
            module,
            packages=["jq", "ripgrep", "zsh"],
            kargs=["quiet", "ttm.pages_limit=29360128", "amd_iommu=on"],
            upgrade=True,
        ).run()

        module.assert_complete()
        self.assertTrue(result["changed"])
        self.assertTrue(result["upgrade_changed"])
        self.assertTrue(result["packages_changed"])
        self.assertTrue(result["kargs_changed"])
        self.assertEqual(result["package_candidates"], ["ripgrep", "zsh"])
        self.assertEqual(
            result["kargs_to_remove"],
            ["ttm.pages_limit=1", "ttm.pages_limit=2"],
        )
        self.assertEqual(
            result["kargs_to_append"],
            ["ttm.pages_limit=29360128", "amd_iommu=on"],
        )
        self.assertTrue(result["reboot_required"])

    def test_karg_transaction_deletes_before_reappending_desired_value(self):
        module = FakeModule(
            [
                ([EXECUTABLE, "status", "--json"], status_response()),
                ([EXECUTABLE, "kargs"], (0, "quiet quiet=verbose\n", "")),
                (
                    [
                        EXECUTABLE,
                        "kargs",
                        "--unchanged-exit-77",
                        "--delete=quiet",
                        "--delete=quiet=verbose",
                        "--append=quiet",
                    ],
                    (0, "", ""),
                ),
                ([EXECUTABLE, "status", "--json"], status_response(booted=False)),
            ]
        )

        result = self.manager(module, kargs=["quiet"]).run()

        module.assert_complete()
        self.assertTrue(result["changed"])
        self.assertTrue(result["kargs_changed"])
        self.assertEqual(result["kargs_to_remove"], ["quiet", "quiet=verbose"])
        self.assertEqual(result["kargs_to_append"], ["quiet"])

    def test_check_mode_predicts_declared_state_without_mutations(self):
        module = FakeModule(
            [
                ([EXECUTABLE, "status", "--json"], status_response(["jq"], booted=False)),
                ([EXECUTABLE, "kargs"], (0, "quiet old.value=1\n", "")),
            ],
            check_mode=True,
        )

        result = self.manager(
            module,
            packages=["jq", "zsh"],
            kargs=["quiet", "old.value=2"],
            upgrade=True,
        ).run()

        module.assert_complete()
        self.assertTrue(result["changed"])
        self.assertTrue(result["upgrade_check_skipped"])
        self.assertEqual(result["package_candidates"], ["zsh"])
        self.assertEqual(result["kargs_to_remove"], ["old.value=1"])
        self.assertEqual(result["kargs_to_append"], ["old.value=2"])
        self.assertFalse(result["upgrade_changed"])
        self.assertFalse(result["packages_changed"])
        self.assertFalse(result["kargs_changed"])
        self.assertTrue(result["reboot_required"])
        self.assertEqual(
            result["msg"],
            "rpm-ostree check mode: would request 1 layered package; would reconcile 1 "
            "kernel argument key; upgrade availability not checked; reboot required",
        )

    def test_rc_77_is_accepted_if_package_state_changes_between_queries(self):
        module = FakeModule(
            [
                ([EXECUTABLE, "status", "--json"], status_response()),
                (
                    [
                        EXECUTABLE,
                        "install",
                        "--allow-inactive",
                        "--idempotent",
                        "--unchanged-exit-77",
                        "zsh",
                    ],
                    (77, "", ""),
                ),
                ([EXECUTABLE, "status", "--json"], status_response(["zsh"])),
            ]
        )

        result = self.manager(module, packages=["zsh"]).run()

        module.assert_complete()
        self.assertFalse(result["changed"])
        self.assertFalse(result["packages_changed"])
        self.assertEqual(result["package_candidates"], ["zsh"])

    def test_failure_after_upgrade_preserves_partial_changed_state(self):
        module = FakeModule(
            [
                ([EXECUTABLE, "status", "--json"], status_response()),
                ([EXECUTABLE, "upgrade", "--unchanged-exit-77"], (0, "", "")),
                (
                    [
                        EXECUTABLE,
                        "install",
                        "--allow-inactive",
                        "--idempotent",
                        "--unchanged-exit-77",
                        "zsh",
                    ],
                    (1, "", "package not found"),
                ),
            ]
        )
        manager = self.manager(module, packages=["zsh"], upgrade=True)

        with self.assertRaises(RPM_OSTREE.RpmOstreeError) as raised:
            manager.run()

        module.assert_complete()
        self.assertEqual(raised.exception.rc, 1)
        self.assertEqual(raised.exception.command[-1], "zsh")
        self.assertTrue(manager.result["changed"])
        self.assertTrue(manager.result["upgrade_changed"])
        self.assertFalse(manager.result["packages_changed"])

    def test_malformed_status_reports_the_status_command(self):
        module = FakeModule(
            [([EXECUTABLE, "status", "--json"], (0, "not json", ""))]
        )

        with self.assertRaises(RPM_OSTREE.RpmOstreeError) as raised:
            self.manager(module).run()

        self.assertEqual(raised.exception.command, [EXECUTABLE, "status", "--json"])


if __name__ == "__main__":
    unittest.main()
