import importlib.util
import plistlib
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "roles"
    / "macos"
    / "library"
    / "confmgmt_macos_defaults.py"
)
SPEC = importlib.util.spec_from_file_location("confmgmt_macos_defaults", MODULE_PATH)
MACOS_DEFAULTS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MACOS_DEFAULTS)


DEFAULTS = "/usr/bin/defaults"


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
        if callable(response):
            return response(command)
        return response

    def assert_complete(self):
        if self.responses:
            raise AssertionError(
                "Commands were not run: %r" % [response[0] for response in self.responses]
            )


def export_command(domain, host="anyHost"):
    command = [DEFAULTS]
    if host == "currentHost":
        command.append("-currentHost")
    return command + ["export", domain, "-"]


def export_response(preferences):
    return (0, plistlib.dumps(preferences).decode("utf-8"), "")


def plist_fragment(value):
    payload = plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=True).decode("utf-8")
    return payload.split('<plist version="1.0">', 1)[1].split("</plist>", 1)[0].strip()


def write_command(domain, key, value, host="anyHost"):
    command = [DEFAULTS]
    if host == "currentHost":
        command.append("-currentHost")
    return command + ["write", domain, key, plist_fragment(value)]


def delete_command(domain, key, host="anyHost"):
    command = [DEFAULTS]
    if host == "currentHost":
        command.append("-currentHost")
    return command + ["delete", domain, key]


def hotkey(parameters, enabled=True):
    return {
        "enabled": enabled,
        "value": {
            "type": "standard",
            "parameters": parameters,
        },
    }


class InputTests(unittest.TestCase):
    def test_rejects_duplicate_preferences(self):
        preferences = [
            {"domain": "com.example", "key": "Setting", "value": True},
            {"domain": "com.example", "key": "Setting", "value": False},
        ]

        with self.assertRaisesRegex(MACOS_DEFAULTS.MacOSDefaultsError, "Duplicate"):
            MACOS_DEFAULTS.normalize_preferences(preferences)

    def test_current_host_and_any_host_are_distinct_targets(self):
        preferences = MACOS_DEFAULTS.normalize_preferences(
            [
                {"domain": "com.example", "key": "Setting", "value": True},
                {
                    "domain": "com.example",
                    "key": "Setting",
                    "host": "currentHost",
                    "value": False,
                },
            ]
        )

        self.assertEqual(
            [MACOS_DEFAULTS.preference_label(preference) for preference in preferences],
            ["com.example:Setting", "currentHost:com.example:Setting"],
        )

    def test_rejects_invalid_names_states_and_values(self):
        invalid_preferences = [
            {"domain": "", "key": "Setting", "value": True},
            {"domain": " com.example", "key": "Setting", "value": True},
            {"domain": "com.example", "key": "", "value": True},
            {"domain": "com.example", "key": "Setting", "state": "unknown", "value": True},
            {"domain": "com.example", "key": "Setting", "host": "other", "value": True},
            {"domain": "com.example", "key": "Setting", "value": None},
            {
                "domain": "com.example",
                "key": "Setting",
                "dict_mode": "merge",
                "value": "not a dictionary",
            },
            {
                "domain": "com.example",
                "key": "Setting",
                "state": "absent",
                "dict_mode": "merge",
            },
        ]

        for preference in invalid_preferences:
            with self.subTest(preference=preference), self.assertRaises(
                MACOS_DEFAULTS.MacOSDefaultsError
            ):
                MACOS_DEFAULTS.normalize_preferences([preference])

    def test_type_preserving_comparison_distinguishes_plist_scalar_types(self):
        self.assertFalse(MACOS_DEFAULTS.values_equal(True, 1))
        self.assertFalse(MACOS_DEFAULTS.values_equal(45, 45.0))
        self.assertFalse(MACOS_DEFAULTS.values_equal({"value": True}, {"value": 1}))
        self.assertTrue(
            MACOS_DEFAULTS.values_equal(
                {"values": [True, 1, 45.0]},
                {"values": [True, 1, 45.0]},
            )
        )


class MacOSDefaultsManagerTests(unittest.TestCase):
    def test_first_preference_in_empty_domain_is_written_and_verified(self):
        domain = "com.example"
        expected = {"Setting": True}
        module = FakeModule(
            [
                (export_command(domain), export_response({})),
                (write_command(domain, "Setting", True), (0, "", "")),
                (export_command(domain), export_response(expected)),
            ]
        )
        manager = MACOS_DEFAULTS.MacOSDefaultsManager(
            module,
            DEFAULTS,
            [{"domain": domain, "key": "Setting", "value": True}],
        )

        result = manager.run()

        self.assertTrue(result["changed"])
        self.assertEqual(result["applied_preferences"], ["com.example:Setting"])
        module.assert_complete()

    def test_noop_batches_discovery_by_domain(self):
        domain = "com.example"
        current = {"First": True, "Second": 2}
        module = FakeModule([(export_command(domain), export_response(current))])
        manager = MACOS_DEFAULTS.MacOSDefaultsManager(
            module,
            DEFAULTS,
            [
                {"domain": domain, "key": "First", "value": True},
                {"domain": domain, "key": "Second", "value": 2},
            ],
        )

        result = manager.run()

        self.assertFalse(result["changed"])
        self.assertFalse(result["relogin_required"])
        self.assertEqual(module.calls, [export_command(domain)])
        module.assert_complete()

    def test_bulk_write_normalizes_types_and_merges_dictionaries(self):
        domain = "com.example"
        current = {
            "LegacyBoolean": "YES",
            "AppleSymbolicHotKeys": {
                "32": hotkey([65535, 53, 1572864]),
                "999": hotkey([1, 2, 3], enabled=False),
            },
            "Unmanaged": "preserve me",
        }
        merged_hotkeys = {
            "32": hotkey([65535, 53, 524288]),
            "118": hotkey([49, 18, 524288]),
            "999": hotkey([1, 2, 3], enabled=False),
        }
        expected = {
            "LegacyBoolean": True,
            "AppleSymbolicHotKeys": merged_hotkeys,
            "Unmanaged": "preserve me",
        }
        write_boolean = write_command(domain, "LegacyBoolean", True)
        write_hotkeys = write_command(domain, "AppleSymbolicHotKeys", merged_hotkeys)
        module = FakeModule(
            [
                (export_command(domain), export_response(current)),
                (write_boolean, (0, "", "")),
                (write_hotkeys, (0, "", "")),
                (export_command(domain), export_response(expected)),
            ]
        )
        manager = MACOS_DEFAULTS.MacOSDefaultsManager(
            module,
            DEFAULTS,
            [
                {"domain": domain, "key": "LegacyBoolean", "value": True},
                {
                    "domain": domain,
                    "key": "AppleSymbolicHotKeys",
                    "dict_mode": "merge",
                    "value": {
                        "32": hotkey([65535, 53, 524288]),
                        "118": hotkey([49, 18, 524288]),
                    },
                },
            ],
        )

        result = manager.run()

        self.assertTrue(result["changed"])
        self.assertTrue(result["relogin_required"])
        self.assertEqual(
            result["changed_preferences"],
            ["com.example:LegacyBoolean", "com.example:AppleSymbolicHotKeys"],
        )
        self.assertEqual(result["applied_preferences"], result["changed_preferences"])
        module.assert_complete()

    def test_absent_preference_is_deleted_and_verified(self):
        domain = "com.example"
        current = {"Remove": 1, "Keep": 2}
        expected = {"Keep": 2}
        module = FakeModule(
            [
                (export_command(domain), export_response(current)),
                (delete_command(domain, "Remove"), (0, "", "")),
                (export_command(domain), export_response(expected)),
            ]
        )
        manager = MACOS_DEFAULTS.MacOSDefaultsManager(
            module,
            DEFAULTS,
            [{"domain": domain, "key": "Remove", "state": "absent"}],
        )

        result = manager.run()

        self.assertTrue(result["changed"])
        self.assertEqual(result["applied_preferences"], ["com.example:Remove"])
        module.assert_complete()

    def test_check_mode_predicts_current_host_change_without_writes(self):
        domain = "com.example"
        export = export_command(domain, host="currentHost")
        module = FakeModule([(export, export_response({"Setting": False}))], check_mode=True)
        manager = MACOS_DEFAULTS.MacOSDefaultsManager(
            module,
            DEFAULTS,
            [
                {
                    "domain": domain,
                    "key": "Setting",
                    "host": "currentHost",
                    "value": True,
                }
            ],
        )

        result = manager.run()

        self.assertTrue(result["changed"])
        self.assertEqual(result["applied_preferences"], [])
        self.assertFalse(manager.changed)
        self.assertEqual(module.calls, [export])
        module.assert_complete()

    def test_write_failure_before_success_does_not_claim_a_change(self):
        domain = "com.example"
        write = write_command(domain, "Setting", True)
        module = FakeModule(
            [
                (export_command(domain), export_response({"Setting": False})),
                (write, (1, "", "write failed")),
            ]
        )
        manager = MACOS_DEFAULTS.MacOSDefaultsManager(
            module,
            DEFAULTS,
            [{"domain": domain, "key": "Setting", "value": True}],
        )

        with self.assertRaisesRegex(MACOS_DEFAULTS.MacOSDefaultsError, "Unable to manage"):
            manager.run()

        self.assertFalse(manager.changed)
        self.assertEqual(manager.changed_preferences, ["com.example:Setting"])
        self.assertEqual(manager.applied_preferences, [])
        module.assert_complete()

    def test_later_failure_preserves_partial_changed_state(self):
        domain = "com.example"
        first_write = write_command(domain, "First", True)
        second_write = write_command(domain, "Second", True)
        module = FakeModule(
            [
                (export_command(domain), export_response({"First": False, "Second": False})),
                (first_write, (0, "", "")),
                (second_write, (1, "", "second write failed")),
            ]
        )
        manager = MACOS_DEFAULTS.MacOSDefaultsManager(
            module,
            DEFAULTS,
            [
                {"domain": domain, "key": "First", "value": True},
                {"domain": domain, "key": "Second", "value": True},
            ],
        )

        with self.assertRaisesRegex(MACOS_DEFAULTS.MacOSDefaultsError, "Second"):
            manager.run()

        self.assertTrue(manager.changed)
        self.assertEqual(manager.applied_preferences, ["com.example:First"])
        module.assert_complete()

    def test_failed_verification_preserves_changed_state(self):
        domain = "com.example"
        current = {"Setting": False}
        write = write_command(domain, "Setting", True)
        module = FakeModule(
            [
                (export_command(domain), export_response(current)),
                (write, (0, "", "")),
                (export_command(domain), export_response(current)),
            ]
        )
        manager = MACOS_DEFAULTS.MacOSDefaultsManager(
            module,
            DEFAULTS,
            [{"domain": domain, "key": "Setting", "value": True}],
        )

        with self.assertRaisesRegex(MACOS_DEFAULTS.MacOSDefaultsError, "did not retain"):
            manager.run()

        self.assertTrue(manager.changed)
        self.assertEqual(manager.applied_preferences, ["com.example:Setting"])
        module.assert_complete()


if __name__ == "__main__":
    unittest.main()
