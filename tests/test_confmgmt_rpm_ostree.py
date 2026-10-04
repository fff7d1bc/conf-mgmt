import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch


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
STATUS_COMMAND = [EXECUTABLE, "status", "--json"]
UPGRADE_COMMAND = [EXECUTABLE, "upgrade", "--unchanged-exit-77"]
INSTALL_COMMAND = [
    EXECUTABLE, "install", "--allow-inactive", "--idempotent", "--unchanged-exit-77"
]
BUSY_STDERR = (
    "error: Transaction in progress: update --check\n"
    " You can cancel the current transaction with `rpm-ostree cancel`\n"
)
BUSY_RESPONSE = (1, "", BUSY_STDERR)


class FakeClock:
    def __init__(self):
        self.elapsed = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.elapsed += seconds


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
    def test_busy_timeout_rejects_invalid_values_before_running_commands(self):
        for timeout in (-1, 1.5, True, "120", None):
            module = FakeModule([])
            with self.subTest(timeout=timeout), self.assertRaisesRegex(
                RPM_OSTREE.RpmOstreeError, "busy_timeout must be a non-negative integer"
            ):
                RPM_OSTREE.RpmOstreeManager(module, EXECUTABLE, busy_timeout=timeout)
            self.assertEqual(module.calls, [])

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
    def setUp(self):
        self.clock = FakeClock()
        clock_patch = patch.object(RPM_OSTREE, "time", self.clock)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)

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
        self.assertEqual(result["busy_retries"], 0)
        self.assertEqual(result["busy_wait_seconds"], 0.0)
        self.assertEqual(self.clock.sleeps, [])
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

    def test_busy_upgrade_recovers_with_capped_backoff_and_refreshes_packages(self):
        for upgrade_rc in (0, 77):
            with self.subTest(upgrade_rc=upgrade_rc):
                self.clock.sleeps.clear()
                module = FakeModule(
                    [(STATUS_COMMAND, status_response())]
                    + [(UPGRADE_COMMAND, BUSY_RESPONSE)] * 5
                    + [
                        (UPGRADE_COMMAND, (upgrade_rc, "", "")),
                        (STATUS_COMMAND, status_response(["zsh"], booted=False)),
                        (STATUS_COMMAND, status_response(["zsh"], booted=False)),
                    ]
                )

                result = self.manager(module, packages=["zsh"], upgrade=True).run()

                module.assert_complete()
                self.assertEqual(self.clock.sleeps, [2, 4, 8, 10, 10])
                self.assertEqual(result["busy_retries"], 5)
                self.assertEqual(result["busy_wait_seconds"], 34.0)
                self.assertEqual(result["changed"], upgrade_rc == 0)
                self.assertEqual(result["upgrade_changed"], upgrade_rc == 0)
                self.assertFalse(result["packages_changed"])
                self.assertEqual(result["package_candidates"], [])
                self.assertTrue(result["reboot_required"])

    def test_busy_timeout_caps_final_sleep_and_preserves_error_details(self):
        module = FakeModule(
            [(STATUS_COMMAND, status_response())]
            + [(UPGRADE_COMMAND, BUSY_RESPONSE)] * 5
        )
        manager = self.manager(module, upgrade=True, busy_timeout=17)

        with self.assertRaisesRegex(
            RPM_OSTREE.RpmOstreeError, "remained busy for 17 seconds.*update --check"
        ) as raised:
            manager.run()

        module.assert_complete()
        self.assertEqual(self.clock.sleeps, [2, 4, 8, 3])
        self.assertEqual(manager.result["busy_retries"], 4)
        self.assertEqual(manager.result["busy_wait_seconds"], 17.0)
        self.assertFalse(manager.result["changed"])
        self.assertEqual(raised.exception.command, UPGRADE_COMMAND)
        self.assertEqual(raised.exception.rc, 1)
        self.assertEqual(raised.exception.stdout, "")
        self.assertEqual(raised.exception.stderr, BUSY_STDERR)

    def test_busy_timeout_includes_time_spent_retrying_commands(self):
        manager = self.manager(FakeModule([]), busy_timeout=10)

        def slow_busy_operation():
            self.clock.elapsed += 6
            raise RPM_OSTREE.RpmOstreeError(
                "busy", command=UPGRADE_COMMAND, rc=1, stderr=BUSY_STDERR
            )

        with self.assertRaisesRegex(RPM_OSTREE.RpmOstreeError, "remained busy for 10 seconds"):
            manager.retry_busy(slow_busy_operation)

        self.assertEqual(self.clock.sleeps, [2, 2])
        self.assertEqual(manager.result["busy_wait_seconds"], 4.0)
        self.assertEqual(manager.result["busy_retries"], 2)

    def test_zero_busy_timeout_disables_retries(self):
        module = FakeModule(
            [
                (STATUS_COMMAND, status_response()),
                (UPGRADE_COMMAND, BUSY_RESPONSE),
            ]
        )
        manager = self.manager(module, upgrade=True, busy_timeout=0)

        with self.assertRaises(RPM_OSTREE.RpmOstreeError) as raised:
            manager.run()

        module.assert_complete()
        self.assertNotIn("remained busy", raised.exception.message)
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(manager.result["busy_retries"], 0)

    def test_unrelated_failures_are_not_retried(self):
        for response in (
            (1, "", "error: Package not found"),
            (1, "", "error: Download failed"),
            (1, BUSY_STDERR, ""),
            (1, "", "error: Other failure mentioning Transaction in progress: update --check"),
            (2, "", BUSY_STDERR),
        ):
            with self.subTest(response=response):
                module = FakeModule(
                    [
                        (STATUS_COMMAND, status_response()),
                        (UPGRADE_COMMAND, response),
                    ]
                )
                manager = self.manager(module, upgrade=True)

                with self.assertRaises(RPM_OSTREE.RpmOstreeError) as raised:
                    manager.run()

                module.assert_complete()
                self.assertEqual(raised.exception.rc, response[0])
                self.assertEqual(raised.exception.stderr, response[2])
                self.assertEqual(manager.result["busy_retries"], 0)
                self.assertEqual(self.clock.sleeps, [])

    def test_unrelated_failure_after_busy_is_reported_immediately(self):
        module = FakeModule(
            [
                (STATUS_COMMAND, status_response()),
                (UPGRADE_COMMAND, BUSY_RESPONSE),
                (UPGRADE_COMMAND, (1, "download output", "error: Download failed")),
            ]
        )
        manager = self.manager(module, upgrade=True)

        with self.assertRaisesRegex(RPM_OSTREE.RpmOstreeError, "Download failed") as raised:
            manager.run()

        module.assert_complete()
        self.assertEqual(raised.exception.stdout, "download output")
        self.assertEqual(self.clock.sleeps, [2])
        self.assertEqual(manager.result["busy_retries"], 1)
        self.assertFalse(manager.result["changed"])

    def test_busy_install_refreshes_candidates_without_repeating_upgrade(self):
        module = FakeModule(
            [
                (STATUS_COMMAND, status_response()),
                (UPGRADE_COMMAND, (0, "", "")),
                (INSTALL_COMMAND + ["zsh", "tmux"], BUSY_RESPONSE),
                (STATUS_COMMAND, status_response(["zsh"], booted=False)),
                (INSTALL_COMMAND + ["tmux"], (0, "", "")),
                (STATUS_COMMAND, status_response(["zsh", "tmux"], booted=False)),
            ]
        )

        result = self.manager(module, packages=["zsh", "tmux"], upgrade=True).run()

        module.assert_complete()
        self.assertTrue(result["upgrade_changed"])
        self.assertTrue(result["packages_changed"])
        self.assertTrue(result["changed"])
        self.assertEqual(result["package_candidates"], ["tmux"])
        self.assertEqual(self.clock.sleeps, [2])

    def test_busy_install_becomes_noop_if_another_client_installed_packages(self):
        module = FakeModule(
            [
                (STATUS_COMMAND, status_response()),
                (INSTALL_COMMAND + ["zsh"], BUSY_RESPONSE),
                (STATUS_COMMAND, status_response(["zsh"], booted=False)),
                (STATUS_COMMAND, status_response(["zsh"], booted=False)),
            ]
        )

        result = self.manager(module, packages=["zsh"]).run()

        module.assert_complete()
        self.assertFalse(result["changed"])
        self.assertFalse(result["packages_changed"])
        self.assertEqual(result["package_candidates"], [])
        self.assertTrue(result["reboot_required"])
        self.assertEqual(result["busy_retries"], 1)

    def test_busy_refresh_shares_timeout_and_preserves_prior_upgrade_change(self):
        module = FakeModule(
            [
                (STATUS_COMMAND, status_response()),
                (UPGRADE_COMMAND, (0, "", "")),
                (INSTALL_COMMAND + ["zsh"], BUSY_RESPONSE),
                (STATUS_COMMAND, BUSY_RESPONSE),
                (STATUS_COMMAND, BUSY_RESPONSE),
            ]
        )
        manager = self.manager(module, packages=["zsh"], upgrade=True, busy_timeout=3)

        with self.assertRaisesRegex(RPM_OSTREE.RpmOstreeError, "remained busy for 3 seconds") as raised:
            manager.run()

        module.assert_complete()
        self.assertEqual(self.clock.sleeps, [2, 1])
        self.assertEqual(raised.exception.command, STATUS_COMMAND)
        self.assertTrue(manager.result["changed"])
        self.assertTrue(manager.result["upgrade_changed"])
        self.assertFalse(manager.result["packages_changed"])

    def test_busy_kargs_recomputes_strict_deletes_from_fresh_state(self):
        module = FakeModule(
            [
                (STATUS_COMMAND, status_response()),
                ([EXECUTABLE, "kargs"], (0, "quiet setting=1\n", "")),
                (
                    [EXECUTABLE, "kargs", "--unchanged-exit-77", "--delete=setting=1", "--append=setting=3"],
                    BUSY_RESPONSE,
                ),
                ([EXECUTABLE, "kargs"], (0, "quiet setting=2\n", "")),
                (
                    [EXECUTABLE, "kargs", "--unchanged-exit-77", "--delete=setting=2", "--append=setting=3"],
                    (0, "", ""),
                ),
                (STATUS_COMMAND, status_response(booted=False)),
            ]
        )

        result = self.manager(module, kargs=["setting=3"]).run()

        module.assert_complete()
        self.assertTrue(result["changed"])
        self.assertTrue(result["kargs_changed"])
        self.assertEqual(result["kargs_to_remove"], ["setting=2"])
        self.assertEqual(result["kargs_to_append"], ["setting=3"])
        self.assertEqual(result["busy_retries"], 1)

    def test_busy_kargs_becomes_noop_if_another_client_applied_desired_state(self):
        module = FakeModule(
            [
                (STATUS_COMMAND, status_response()),
                ([EXECUTABLE, "kargs"], (0, "quiet\n", "")),
                ([EXECUTABLE, "kargs", "--unchanged-exit-77", "--append=setting=3"], BUSY_RESPONSE),
                ([EXECUTABLE, "kargs"], (0, "quiet setting=3\n", "")),
                (STATUS_COMMAND, status_response(booted=False)),
            ]
        )

        result = self.manager(module, kargs=["setting=3"]).run()

        module.assert_complete()
        self.assertFalse(result["changed"])
        self.assertFalse(result["kargs_changed"])
        self.assertEqual(result["kargs_to_remove"], [])
        self.assertEqual(result["kargs_to_append"], [])

    def test_busy_kargs_timeout_preserves_prior_package_change(self):
        kargs_command = [EXECUTABLE, "kargs", "--unchanged-exit-77", "--append=setting=3"]
        module = FakeModule(
            [
                (STATUS_COMMAND, status_response()),
                (INSTALL_COMMAND + ["zsh"], (0, "", "")),
                ([EXECUTABLE, "kargs"], (0, "quiet\n", "")),
                (kargs_command, BUSY_RESPONSE),
                ([EXECUTABLE, "kargs"], (0, "quiet\n", "")),
                (kargs_command, BUSY_RESPONSE),
            ]
        )
        manager = self.manager(module, packages=["zsh"], kargs=["setting=3"], busy_timeout=2)

        with self.assertRaisesRegex(RPM_OSTREE.RpmOstreeError, "remained busy for 2 seconds") as raised:
            manager.run()

        module.assert_complete()
        self.assertEqual(raised.exception.command, kargs_command)
        self.assertTrue(manager.result["changed"])
        self.assertTrue(manager.result["packages_changed"])
        self.assertFalse(manager.result["kargs_changed"])

    def test_check_mode_retries_only_read_queries_without_claiming_changes(self):
        module = FakeModule(
            [
                (STATUS_COMMAND, BUSY_RESPONSE),
                (STATUS_COMMAND, status_response(["zsh"])),
                ([EXECUTABLE, "kargs"], BUSY_RESPONSE),
                ([EXECUTABLE, "kargs"], (0, "quiet\n", "")),
            ],
            check_mode=True,
        )

        result = self.manager(module, packages=["zsh"], kargs=["quiet"], upgrade=True).run()

        module.assert_complete()
        self.assertFalse(result["changed"])
        self.assertTrue(result["upgrade_check_skipped"])
        self.assertFalse(result["upgrade_changed"])
        self.assertFalse(result["packages_changed"])
        self.assertFalse(result["kargs_changed"])
        self.assertEqual(result["busy_retries"], 2)
        self.assertEqual(result["busy_wait_seconds"], 4.0)
        self.assertEqual(self.clock.sleeps, [2, 2])

    def test_final_status_retries_preserve_successful_changes(self):
        module = FakeModule(
            [
                (STATUS_COMMAND, status_response()),
                (UPGRADE_COMMAND, (0, "", "")),
                (STATUS_COMMAND, BUSY_RESPONSE),
                (STATUS_COMMAND, status_response(booted=False)),
            ]
        )

        result = self.manager(module, upgrade=True).run()

        module.assert_complete()
        self.assertTrue(result["changed"])
        self.assertTrue(result["upgrade_changed"])
        self.assertTrue(result["reboot_required"])
        self.assertEqual(result["busy_retries"], 1)


if __name__ == "__main__":
    unittest.main()
