import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "roles"
    / "steamos"
    / "library"
    / "confmgmt_steamos_update.py"
)
SPEC = importlib.util.spec_from_file_location("confmgmt_steamos_update", MODULE_PATH)
STEAMOS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(STEAMOS)


EXECUTABLE = "/usr/bin/atomupd-manager"


class FakeModule:
    def __init__(self, responses, check_mode=False):
        self.responses = list(responses)
        self.check_mode = check_mode
        self.calls = []

    def run_command(self, command):
        if not self.responses:
            raise AssertionError("Unexpected command: %r" % command)

        expected_command, response = self.responses.pop(0)
        if command != expected_command:
            raise AssertionError("Expected command %r, got %r" % (expected_command, command))

        self.calls.append(list(command))
        return response

    def assert_complete(self):
        if self.responses:
            raise AssertionError(
                "Commands were not run: %r" % [response[0] for response in self.responses]
            )


def response(stdout="", stderr="", rc=0):
    return rc, stdout, stderr


STATUS_COMMAND = [EXECUTABLE, "get-update-status"]
CHECK_COMMAND = [EXECUTABLE, "check"]


class ParsingTests(unittest.TestCase):
    def test_known_update_statuses_are_accepted(self):
        for status in STEAMOS.KNOWN_STATUSES:
            with self.subTest(status=status):
                self.assertEqual(STEAMOS.parse_update_status(status + "\n"), status)

    def test_unknown_update_status_is_rejected(self):
        with self.assertRaisesRegex(STEAMOS.SteamOSUpdateError, "unknown update status"):
            STEAMOS.parse_update_status("unexpected\n")

    def test_update_check_distinguishes_no_update_and_build_id(self):
        self.assertIsNone(STEAMOS.parse_update_check("No update available\n"))
        self.assertEqual(
            STEAMOS.parse_update_check("Update available\nVersion: 3.8.26\nID: 20260815.1\n"),
            "20260815.1",
        )

    def test_unrecognized_update_check_output_is_rejected(self):
        with self.assertRaisesRegex(STEAMOS.SteamOSUpdateError, "unrecognized update check"):
            STEAMOS.parse_update_check("Something changed\n")


class SteamOSUpdateManagerTests(unittest.TestCase):
    def manager(self, module):
        return STEAMOS.SteamOSUpdateManager(module, EXECUTABLE)

    def test_no_update_is_unchanged(self):
        module = FakeModule(
            [
                (STATUS_COMMAND, response("idle\n")),
                (CHECK_COMMAND, response("No update available\n")),
            ]
        )
        result = self.manager(module).run()

        module.assert_complete()
        self.assertFalse(result["changed"])
        self.assertFalse(result["update_available"])
        self.assertFalse(result["reboot_required"])
        self.assertEqual(result["msg"], "SteamOS is current")

    def test_applied_update_waiting_for_reboot_is_not_reapplied(self):
        module = FakeModule([(STATUS_COMMAND, response("successful\n"))])
        result = self.manager(module).run()

        module.assert_complete()
        self.assertFalse(result["changed"])
        self.assertTrue(result["reboot_required"])
        self.assertFalse(result["update_applied"])

    def test_existing_update_in_progress_is_reported_without_commands(self):
        for status in ("in-progress", "paused"):
            with self.subTest(status=status):
                module = FakeModule([(STATUS_COMMAND, response(status + "\n"))])
                result = self.manager(module).run()

                module.assert_complete()
                self.assertFalse(result["changed"])
                self.assertTrue(result["update_in_progress"])
                self.assertEqual(result["initial_status"], status)

    def test_check_mode_reports_available_build_without_applying(self):
        module = FakeModule(
            [
                (STATUS_COMMAND, response("idle\n")),
                (CHECK_COMMAND, response("Update available\nID: 20260815.1\n")),
            ],
            check_mode=True,
        )
        result = self.manager(module).run()

        module.assert_complete()
        self.assertTrue(result["changed"])
        self.assertTrue(result["update_available"])
        self.assertFalse(result["update_applied"])
        self.assertEqual(result["target_build_id"], "20260815.1")

    def test_available_update_is_applied_by_build_id(self):
        build_id = "20260815.1"
        module = FakeModule(
            [
                (STATUS_COMMAND, response("idle\n")),
                (CHECK_COMMAND, response("Update available\nID: %s\n" % build_id)),
                ([EXECUTABLE, "update", build_id], response("Update applied\n")),
            ]
        )
        result = self.manager(module).run()

        module.assert_complete()
        self.assertTrue(result["changed"])
        self.assertTrue(result["update_applied"])
        self.assertTrue(result["reboot_required"])
        self.assertEqual(result["target_build_id"], build_id)

    def test_failed_update_preserves_possible_partial_change(self):
        build_id = "20260815.1"
        module = FakeModule(
            [
                (STATUS_COMMAND, response("idle\n")),
                (CHECK_COMMAND, response("Update available\nID: %s\n" % build_id)),
                (
                    [EXECUTABLE, "update", build_id],
                    response(stderr="Atomic update failed\n", rc=1),
                ),
            ]
        )
        manager = self.manager(module)

        with self.assertRaisesRegex(STEAMOS.SteamOSUpdateError, "Atomic update failed") as raised:
            manager.run()

        module.assert_complete()
        self.assertTrue(manager.changed)
        self.assertEqual(raised.exception.command, [EXECUTABLE, "update", build_id])

    def test_malformed_successful_check_reports_command_and_output(self):
        output = "Unexpected successful response\n"
        module = FakeModule(
            [
                (STATUS_COMMAND, response("idle\n")),
                (CHECK_COMMAND, response(output)),
            ]
        )
        manager = self.manager(module)

        with self.assertRaisesRegex(STEAMOS.SteamOSUpdateError, "unrecognized") as raised:
            manager.run()

        module.assert_complete()
        self.assertFalse(manager.changed)
        self.assertEqual(raised.exception.command, CHECK_COMMAND)
        self.assertEqual(raised.exception.rc, 0)
        self.assertEqual(raised.exception.stdout, output)


if __name__ == "__main__":
    unittest.main()
