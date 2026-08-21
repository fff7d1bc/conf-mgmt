import importlib.util
import json
import os
import stat
import subprocess
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "roles"
    / "homebrew"
    / "library"
    / "confmgmt_homebrew.py"
)
SPEC = importlib.util.spec_from_file_location("confmgmt_homebrew", MODULE_PATH)
HOMEBREW = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HOMEBREW)


BREW = "/opt/homebrew/bin/brew"


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
        if callable(response):
            return response(command, environment)
        return response

    def assert_complete(self):
        if self.responses:
            raise AssertionError("Commands were not run: %r" % [response[0] for response in self.responses])


def formula(name, installed=True, **extra):
    value = {
        "name": name,
        "full_name": name,
        "tap": "homebrew/core",
        "installed": [{"version": "1.0"}] if installed else [],
    }
    value.update(extra)
    return value


def cask(name, installed=True, **extra):
    value = {
        "token": name,
        "full_token": name,
        "tap": "homebrew/cask",
        "installed": "1.0" if installed else None,
    }
    value.update(extra)
    return value


def json_response(formulae=None, casks=None):
    return (0, json.dumps({"formulae": formulae or [], "casks": casks or []}), "")


class PackageMetadataTests(unittest.TestCase):
    def test_command_summary_omits_package_names(self):
        self.assertEqual(
            HOMEBREW.summarize_command(
                [BREW, "info", "--json=v2", "--cask", "firefox", "signal"]
            ),
            "brew info --json=v2 --cask",
        )

    def test_normalize_names_lowercases_and_deduplicates(self):
        self.assertEqual(
            HOMEBREW.normalize_package_names(["JQ", "user/tap/Thing", "jq"], "formula"),
            ["jq", "user/tap/thing"],
        )

    def test_normalize_names_rejects_option_and_malformed_names(self):
        for name in ("--debug", " name", "tap//name", "tap/name/", "name\nother"):
            with self.subTest(name=name), self.assertRaises(HOMEBREW.HomebrewError):
                HOMEBREW.normalize_package_names([name], "formula")

    def test_installed_info_resolves_aliases_old_names_and_taps(self):
        data = {
            "formulae": [
                formula(
                    "current",
                    aliases=["alias"],
                    oldnames=["old"],
                    tap="owner/repository",
                )
            ],
            "casks": [
                cask(
                    "current-app",
                    old_tokens=["old-app"],
                    tap="owner/casks",
                )
            ],
        }
        self.assertEqual(
            HOMEBREW.installed_packages_from_info(
                data,
                ["owner/repository/alias", "owner/repository/old"],
                "formula",
            ),
            {"owner/repository/alias", "owner/repository/old"},
        )
        self.assertEqual(
            HOMEBREW.installed_packages_from_info(data, ["owner/casks/old-app"], "cask"),
            {"owner/casks/old-app"},
        )

    def test_installed_info_rejects_unresolved_metadata(self):
        with self.assertRaisesRegex(HOMEBREW.HomebrewError, "0 metadata matches"):
            HOMEBREW.installed_packages_from_info(
                {"formulae": [formula("jq")]},
                ["ripgrep"],
                "formula",
            )

    def test_generic_info_prefers_formula_but_accepts_cask_migrations(self):
        data = {
            "formulae": [formula("shared")],
            "casks": [cask("shared"), cask("codex")],
        }
        states = HOMEBREW.resolved_packages_from_info(
            data,
            ["shared", "codex"],
            preferred_type="formula",
        )
        self.assertEqual(states["shared"]["type"], "formula")
        self.assertEqual(states["codex"]["type"], "cask")

    def test_outdated_info_ignores_pinned_formulae(self):
        formulas, casks = HOMEBREW.outdated_packages_from_json(
            {
                "formulae": [
                    {"name": "jq", "pinned": True},
                    {"name": "ripgrep", "pinned": False},
                ],
                "casks": [
                    {"name": "firefox", "pinned": False},
                    {"name": "signal", "pinned": True},
                ],
            }
        )
        self.assertEqual(formulas, ["ripgrep"])
        self.assertEqual(casks, ["firefox"])


class SudoHandlingTests(unittest.TestCase):
    def test_sudo_failure_classification_is_conservative(self):
        error = HOMEBREW.HomebrewError(
            "sudo failed",
            stderr="sudo: a terminal is required to read the password",
            sudo_capable=True,
        )
        self.assertTrue(HOMEBREW.is_sudo_password_failure(error, password_was_supplied=False))
        self.assertFalse(HOMEBREW.is_sudo_password_failure(error, password_was_supplied=True))

        error.sudo_capable = False
        self.assertFalse(HOMEBREW.is_sudo_password_failure(error, password_was_supplied=False))

        unrelated = HOMEBREW.HomebrewError(
            "download failed",
            stderr="curl: checksum mismatch",
            sudo_capable=True,
        )
        self.assertFalse(HOMEBREW.is_sudo_password_failure(unrelated, password_was_supplied=False))

    def test_askpass_is_executable_quotes_password_and_is_removed(self):
        password = "spaces and 'quotes'"
        with HOMEBREW.sudo_askpass(password) as path:
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o700)
            result = subprocess.run([path], check=True, capture_output=True, text=True)
            self.assertEqual(result.stdout, password + "\n")
        self.assertFalse(os.path.exists(path))

    def test_sudo_failure_result_is_compact_and_structured(self):
        manager = HOMEBREW.HomebrewManager(
            module=FakeModule([]),
            brew_path=BREW,
            formulas=[],
            casks=["tailscale-app"],
        )
        manager.changed = True
        error = HOMEBREW.HomebrewError(
            "full Homebrew transcript",
            command=[BREW, "upgrade", "--quiet", "--no-ask", "--greedy"],
            rc=1,
            stdout="many successful upgrades\n",
            stderr="sudo: a password is required\n",
            sudo_capable=True,
        )

        result = HOMEBREW.failure_result(manager, error)

        self.assertTrue(result["changed"])
        self.assertTrue(result["needs_sudo"])
        self.assertEqual(result["rc"], 1)
        self.assertEqual(
            result["msg"],
            "Homebrew requires a sudo password to continue brew upgrade --quiet --no-ask --greedy",
        )
        self.assertNotIn("stdout", result)
        self.assertNotIn("stderr", result)
        self.assertNotIn("transcript", result["msg"])

    def test_unrelated_failure_result_preserves_diagnostics(self):
        manager = HOMEBREW.HomebrewManager(
            module=FakeModule([]),
            brew_path=BREW,
            formulas=["jq"],
            casks=[],
        )
        error = HOMEBREW.HomebrewError(
            "checksum mismatch",
            command=[BREW, "install", "--quiet", "--formula", "--no-ask", "jq"],
            rc=1,
            stdout="download output\n",
            stderr="checksum mismatch\n",
        )

        result = HOMEBREW.failure_result(manager, error)

        self.assertFalse(result["needs_sudo"])
        self.assertEqual(result["msg"], "checksum mismatch")
        self.assertEqual(result["stdout"], "download output\n")
        self.assertEqual(result["stderr"], "checksum mismatch\n")

    def test_failure_after_password_retry_preserves_diagnostics(self):
        manager = HOMEBREW.HomebrewManager(
            module=FakeModule([]),
            brew_path=BREW,
            formulas=[],
            casks=["tailscale-app"],
            sudo_password="incorrect password",
        )
        error = HOMEBREW.HomebrewError(
            "sudo: authentication failed",
            command=[BREW, "upgrade", "--quiet", "--no-ask", "--greedy"],
            rc=1,
            stderr="sudo: authentication failed\n",
            sudo_capable=True,
        )

        result = HOMEBREW.failure_result(manager, error)

        self.assertFalse(result["needs_sudo"])
        self.assertEqual(result["stderr"], "sudo: authentication failed\n")


class HomebrewManagerTests(unittest.TestCase):
    def manager(self, module, **kwargs):
        defaults = {
            "module": module,
            "brew_path": BREW,
            "formulas": [],
            "casks": [],
        }
        defaults.update(kwargs)
        return HOMEBREW.HomebrewManager(**defaults)

    def test_noop_uses_one_discovery_command_per_package_type(self):
        module = FakeModule(
            [
                ([BREW, "update"], (0, "Already up-to-date.\n", "")),
                (
                    [BREW, "info", "--json=v2", "jq"],
                    json_response(formulae=[formula("jq")]),
                ),
                (
                    [BREW, "info", "--json=v2", "--cask", "firefox"],
                    json_response(casks=[cask("firefox")]),
                ),
                ([BREW, "outdated", "--json=v2", "--greedy"], json_response()),
            ]
        )
        result = self.manager(
            module,
            formulas=["jq"],
            casks=["firefox"],
            update_homebrew=True,
            upgrade_all=True,
            greedy=True,
        ).run()

        module.assert_complete()
        self.assertFalse(result["changed"])
        self.assertEqual(result["install_candidates"], {"formulas": [], "casks": []})
        self.assertFalse(result["cleanup_candidates"])
        self.assertFalse(result["cleanup_checked"])
        self.assertFalse(result["cleanup_performed"])
        self.assertEqual(len(module.calls), 4)
        self.assertNotIn("HOMEBREW_NO_INSTALL_CLEANUP", module.calls[0][1])
        self.assertEqual(
            [timing["command"] for timing in result["command_timings"]],
            [
                "brew update",
                "brew info --json=v2",
                "brew info --json=v2 --cask",
                "brew outdated --json=v2 --greedy",
            ],
        )
        self.assertTrue(all(timing["seconds"] >= 0 for timing in result["command_timings"]))
        for unused_command, environment in module.calls:
            self.assertEqual(environment["HOMEBREW_NO_COLOR"], "1")
            self.assertEqual(environment["HOMEBREW_NO_EMOJI"], "1")
            self.assertEqual(environment["HOMEBREW_NO_ENV_HINTS"], "1")

    def test_missing_packages_and_upgrades_are_batched(self):
        password = "sudo password"

        def sudo_response(command, environment):
            self.assertIn("SUDO_ASKPASS", environment)
            askpass_path = environment["SUDO_ASKPASS"]
            self.assertTrue(os.path.exists(askpass_path))
            result = subprocess.run([askpass_path], check=True, capture_output=True, text=True)
            self.assertEqual(result.stdout, password + "\n")
            return (0, "changed\n", "")

        module = FakeModule(
            [
                ([BREW, "update"], (0, "Already up-to-date.\n", "")),
                (
                    [BREW, "info", "--json=v2", "jq", "ripgrep"],
                    json_response(formulae=[formula("jq"), formula("ripgrep", installed=False)]),
                ),
                (
                    [BREW, "info", "--json=v2", "--cask", "firefox", "signal"],
                    json_response(casks=[cask("firefox"), cask("signal", installed=False)]),
                ),
                (
                    [BREW, "install", "--quiet", "--formula", "--no-ask", "ripgrep"],
                    (0, "installed\n", ""),
                ),
                ([BREW, "install", "--quiet", "--cask", "--no-ask", "signal"], sudo_response),
                (
                    [BREW, "outdated", "--json=v2", "--greedy"],
                    json_response(
                        formulae=[{"name": "jq", "full_name": "jq", "pinned": False}],
                        casks=[{"name": "firefox", "pinned": False}],
                    ),
                ),
                ([BREW, "upgrade", "--quiet", "--no-ask", "--greedy"], sudo_response),
            ]
        )
        result = self.manager(
            module,
            formulas=["jq", "ripgrep"],
            casks=["firefox", "signal"],
            update_homebrew=True,
            upgrade_all=True,
            greedy=True,
            sudo_password=password,
        ).run()

        module.assert_complete()
        self.assertTrue(result["changed"])
        self.assertEqual(result["install_candidates"], {"formulas": ["ripgrep"], "casks": ["signal"]})
        self.assertEqual(result["upgrade_candidates"], {"formulas": ["jq"], "casks": ["firefox"]})
        formula_install_environment = module.calls[3][1]
        self.assertNotIn("SUDO_ASKPASS", formula_install_environment)
        for command, environment in module.calls:
            if "SUDO_ASKPASS" in environment:
                self.assertFalse(os.path.exists(environment["SUDO_ASKPASS"]))

    def test_check_mode_reports_changes_without_mutating_commands(self):
        module = FakeModule(
            [
                (
                    [BREW, "info", "--json=v2", "jq"],
                    json_response(formulae=[formula("jq", installed=False)]),
                ),
                (
                    [BREW, "info", "--json=v2", "--cask", "firefox"],
                    json_response(casks=[cask("firefox")]),
                ),
                (
                    [BREW, "outdated", "--json=v2", "--greedy"],
                    json_response(casks=[{"name": "firefox", "pinned": False}]),
                ),
            ],
            check_mode=True,
        )
        result = self.manager(
            module,
            formulas=["jq"],
            casks=["firefox"],
            update_homebrew=True,
            upgrade_all=True,
            greedy=True,
        ).run()

        module.assert_complete()
        self.assertTrue(result["changed"])
        self.assertFalse(result["homebrew_updated"])
        self.assertEqual(result["install_candidates"]["formulas"], ["jq"])
        self.assertNotIn("update", [command[1] for command, unused in module.calls])
        self.assertNotIn("install", [command[1] for command, unused in module.calls])
        self.assertNotIn("upgrade", [command[1] for command, unused in module.calls])

    def test_formula_only_linux_upgrade_uses_formula_namespace_without_askpass(self):
        module = FakeModule(
            [
                (
                    [BREW, "info", "--json=v2", "jq"],
                    json_response(formulae=[formula("jq")]),
                ),
                (
                    [BREW, "outdated", "--json=v2", "--formula"],
                    json_response(formulae=[{"name": "jq", "pinned": False}]),
                ),
                ([BREW, "upgrade", "--quiet", "--no-ask", "--formula"], (0, "upgraded\n", "")),
            ]
        )
        result = self.manager(
            module,
            formulas=["jq"],
            upgrade_all=True,
            greedy=True,
            sudo_password="unused",
        ).run()

        module.assert_complete()
        self.assertTrue(result["changed"])
        self.assertNotIn("SUDO_ASKPASS", module.calls[-1][1])

    def test_formula_input_migrated_to_cross_platform_cask_is_managed(self):
        module = FakeModule(
            [
                (
                    [BREW, "info", "--json=v2", "codex"],
                    json_response(casks=[cask("codex")]),
                ),
                ([BREW, "outdated", "--json=v2", "--greedy"], json_response()),
            ]
        )
        result = self.manager(
            module,
            formulas=["codex"],
            upgrade_all=True,
            greedy=True,
        ).run()

        module.assert_complete()
        self.assertFalse(result["changed"])

    def test_partial_change_is_preserved_on_sudo_failure(self):
        module = FakeModule(
            [
                ([BREW, "update"], (0, "Updated Homebrew.\n", "")),
                (
                    [BREW, "info", "--json=v2", "--cask", "firefox"],
                    json_response(casks=[cask("firefox", installed=False)]),
                ),
                (
                    [BREW, "install", "--quiet", "--cask", "--no-ask", "firefox"],
                    (1, "", "sudo: a terminal is required to read the password"),
                ),
            ]
        )
        manager = self.manager(
            module,
            casks=["firefox"],
            update_homebrew=True,
        )

        with self.assertRaises(HOMEBREW.HomebrewError) as raised:
            manager.run()

        module.assert_complete()
        self.assertTrue(manager.changed)
        self.assertTrue(HOMEBREW.is_sudo_password_failure(raised.exception, password_was_supplied=False))

    def test_cleanup_on_package_change_skips_noop(self):
        module = FakeModule(
            [
                (
                    [BREW, "info", "--json=v2", "jq"],
                    json_response(formulae=[formula("jq")]),
                ),
                ([BREW, "outdated", "--json=v2", "--formula"], json_response()),
            ]
        )
        result = self.manager(
            module,
            formulas=["jq"],
            upgrade_all=True,
            cleanup="on_package_change",
        ).run()

        module.assert_complete()
        self.assertFalse(result["changed"])
        self.assertFalse(result["cleanup_checked"])
        self.assertFalse(result["cleanup_performed"])
        self.assertEqual(module.calls[0][1]["HOMEBREW_NO_INSTALL_CLEANUP"], "1")

    def test_cleanup_on_package_change_ignores_metadata_update(self):
        module = FakeModule(
            [
                ([BREW, "update"], (0, "Updated Homebrew.\n", "")),
                (
                    [BREW, "info", "--json=v2", "jq"],
                    json_response(formulae=[formula("jq")]),
                ),
            ]
        )
        result = self.manager(
            module,
            formulas=["jq"],
            update_homebrew=True,
            cleanup="on_package_change",
        ).run()

        module.assert_complete()
        self.assertTrue(result["changed"])
        self.assertTrue(result["homebrew_updated"])
        self.assertFalse(result["cleanup_checked"])
        self.assertIn("cleanup=skipped", result["msg"])

    def test_cleanup_on_package_change_runs_after_install(self):
        module = FakeModule(
            [
                (
                    [BREW, "info", "--json=v2", "jq"],
                    json_response(formulae=[formula("jq", installed=False)]),
                ),
                (
                    [BREW, "install", "--quiet", "--formula", "--no-ask", "jq"],
                    (0, "installed\n", ""),
                ),
                (
                    [BREW, "cleanup", "--prune=all", "--dry-run"],
                    (0, "Would remove: /cache/archive (1GB)\n", ""),
                ),
                ([BREW, "cleanup", "--prune=all"], (0, "Removing: /cache/archive... (1GB)\n", "")),
            ]
        )
        result = self.manager(
            module,
            formulas=["jq"],
            cleanup="on_package_change",
        ).run()

        module.assert_complete()
        self.assertTrue(result["changed"])
        self.assertTrue(result["cleanup_checked"])
        self.assertTrue(result["cleanup_performed"])

    def test_cleanup_on_package_change_runs_after_upgrade(self):
        module = FakeModule(
            [
                (
                    [BREW, "info", "--json=v2", "jq"],
                    json_response(formulae=[formula("jq")]),
                ),
                (
                    [BREW, "outdated", "--json=v2", "--formula"],
                    json_response(formulae=[{"name": "jq", "pinned": False}]),
                ),
                ([BREW, "upgrade", "--quiet", "--no-ask", "--formula"], (0, "upgraded\n", "")),
                ([BREW, "cleanup", "--prune=all", "--dry-run"], (0, "", "")),
            ]
        )
        result = self.manager(
            module,
            formulas=["jq"],
            upgrade_all=True,
            cleanup="on_package_change",
        ).run()

        module.assert_complete()
        self.assertTrue(result["changed"])
        self.assertTrue(result["cleanup_checked"])
        self.assertFalse(result["cleanup_performed"])

    def test_cleanup_on_package_change_check_mode_uses_current_dry_run(self):
        module = FakeModule(
            [
                (
                    [BREW, "info", "--json=v2", "jq"],
                    json_response(formulae=[formula("jq", installed=False)]),
                ),
                (
                    [BREW, "cleanup", "--prune=all", "--dry-run"],
                    (0, "Would remove: /cache/archive (1GB)\n", ""),
                ),
            ],
            check_mode=True,
        )
        result = self.manager(
            module,
            formulas=["jq"],
            cleanup="on_package_change",
        ).run()

        module.assert_complete()
        self.assertTrue(result["changed"])
        self.assertTrue(result["cleanup_checked"])
        self.assertTrue(result["cleanup_candidates"])
        self.assertFalse(result["cleanup_performed"])

    def test_cleanup_prunes_all_and_reports_change(self):
        module = FakeModule(
            [
                (
                    [BREW, "cleanup", "--prune=all", "--dry-run"],
                    (0, "Would remove: /cache/archive (1GB)\n", ""),
                ),
                ([BREW, "cleanup", "--prune=all"], (0, "Removing: /cache/archive... (1GB)\n", "")),
            ]
        )
        result = self.manager(module, cleanup="always").run()

        module.assert_complete()
        self.assertTrue(result["changed"])
        self.assertTrue(result["cleanup_candidates"])
        self.assertTrue(result["cleanup_checked"])
        self.assertTrue(result["cleanup_performed"])
        self.assertEqual(module.calls[0][1]["HOMEBREW_NO_INSTALL_CLEANUP"], "1")

    def test_cleanup_noop_is_unchanged(self):
        module = FakeModule(
            [
                ([BREW, "cleanup", "--prune=all", "--dry-run"], (0, "", "")),
            ]
        )
        result = self.manager(module, cleanup="always").run()

        module.assert_complete()
        self.assertFalse(result["changed"])
        self.assertFalse(result["cleanup_candidates"])
        self.assertTrue(result["cleanup_checked"])
        self.assertFalse(result["cleanup_performed"])

    def test_cleanup_informational_output_is_unchanged(self):
        module = FakeModule(
            [
                (
                    [BREW, "cleanup", "--prune=all", "--dry-run"],
                    (0, "Cleanup skipped a protected path.\n", "Warning: nothing was removed.\n"),
                ),
            ]
        )
        result = self.manager(module, cleanup="always").run()

        module.assert_complete()
        self.assertFalse(result["changed"])
        self.assertFalse(result["cleanup_candidates"])
        self.assertTrue(result["cleanup_checked"])
        self.assertFalse(result["cleanup_performed"])

    def test_cleanup_change_on_stderr_is_detected(self):
        module = FakeModule(
            [
                (
                    [BREW, "cleanup", "--prune=all", "--dry-run"],
                    (0, "", "Would remove: /cache/archive (1GB)\n"),
                ),
                ([BREW, "cleanup", "--prune=all"], (0, "Removing: /cache/archive... (1GB)\n", "")),
            ]
        )
        result = self.manager(module, cleanup="always").run()

        module.assert_complete()
        self.assertTrue(result["changed"])
        self.assertTrue(result["cleanup_candidates"])
        self.assertTrue(result["cleanup_checked"])
        self.assertTrue(result["cleanup_performed"])

    def test_cleanup_ignores_command_indexes_recreated_by_update(self):
        module = FakeModule(
            [
                (
                    [BREW, "cleanup", "--prune=all", "--dry-run"],
                    (
                        0,
                        "Would remove: /Users/test/Library/Caches/Homebrew/external_commands_list.txt (1B)\n"
                        "Would remove: /Users/test/Library/Caches/Homebrew/all_commands_list.txt (1KB)\n"
                        "==> This operation would free approximately 1KB of disk space.\n",
                        "",
                    ),
                ),
            ]
        )
        result = self.manager(module, cleanup="always").run()

        module.assert_complete()
        self.assertFalse(result["changed"])
        self.assertFalse(result["cleanup_candidates"])
        self.assertTrue(result["cleanup_checked"])
        self.assertFalse(result["cleanup_performed"])

    def test_cleanup_check_mode_uses_dry_run(self):
        module = FakeModule(
            [
                (
                    [BREW, "cleanup", "--prune=all", "--dry-run"],
                    (0, "Would remove: /cache/archive (1GB)\n", ""),
                ),
            ],
            check_mode=True,
        )
        result = self.manager(module, cleanup="always").run()

        module.assert_complete()
        self.assertTrue(result["changed"])
        self.assertTrue(result["cleanup_candidates"])
        self.assertTrue(result["cleanup_checked"])
        self.assertFalse(result["cleanup_performed"])
        self.assertIn("cleanup=True", result["msg"])


if __name__ == "__main__":
    unittest.main()
