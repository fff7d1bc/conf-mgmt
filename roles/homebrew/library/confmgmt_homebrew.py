#!/usr/bin/python

from __future__ import annotations

DOCUMENTATION = r"""
---
module: confmgmt_homebrew
short_description: Manage this repository's Homebrew packages in bulk
description:
  - Updates Homebrew, installs declared formulae and casks, and upgrades installed packages.
  - Uses bulk Homebrew commands rather than invoking Homebrew once per package.
  - Supports Homebrew's nested sudo calls for cask operations through SUDO_ASKPASS.
options:
  formulas:
    description:
      - Formulae that must be installed.
      - A name migrated by Homebrew from a formula to a cask is accepted for compatibility.
    type: list
    elements: str
    default: []
  casks:
    description: Casks that must be installed.
    type: list
    elements: str
    default: []
  update_homebrew:
    description: Run C(brew update) before managing packages.
    type: bool
    default: false
  upgrade_all:
    description: Upgrade installed packages in each configured namespace.
    type: bool
    default: false
  greedy:
    description: Include auto-updating and latest-version casks during upgrades.
    type: bool
    default: false
  cleanup:
    description:
      - Controls when to run C(brew cleanup --prune=all) after package management.
      - C(never) leaves cleanup to Homebrew's default behavior.
      - C(on_package_change) runs cleanup only after this invocation installs or upgrades packages.
      - C(always) checks for cleanup candidates on every invocation.
      - Homebrew's update-maintained command indexes are preserved to avoid perpetual cleanup churn.
    type: str
    choices:
      - never
      - on_package_change
      - always
    default: never
  sudo_password:
    description: Password exposed to Homebrew's internal sudo calls through SUDO_ASKPASS.
    type: str
  path:
    description: Directories searched for the Homebrew executable.
    type: list
    elements: path
    default:
      - /usr/local/bin
      - /opt/homebrew/bin
      - /home/linuxbrew/.linuxbrew/bin
attributes:
  check_mode:
    details: >-
      Package mutations are predicted using current metadata, but C(brew update) is skipped and
      cleanup cannot predict artifacts that a planned package mutation would create.
    support: partial
  diff_mode:
    support: none
author:
  - conf-mgmt
"""

EXAMPLES = r"""
- name: Manage Homebrew packages
  confmgmt_homebrew:
    formulas:
      - jq
      - ripgrep
    casks:
      - firefox
    update_homebrew: true
    upgrade_all: true
    greedy: true
    cleanup: on_package_change

- name: Retry cask operations with a sudo password
  confmgmt_homebrew:
    formulas: "{{ confmgmt.homebrew.formulas | default([]) }}"
    casks: "{{ confmgmt.homebrew.casks | default([]) }}"
    upgrade_all: true
    greedy: true
    sudo_password: "{{ homebrew_sudo_password.user_input }}"
"""

RETURN = r"""
homebrew_updated:
  description: Whether C(brew update) changed Homebrew metadata.
  returned: always
  type: bool
install_candidates:
  description: Declared packages that were missing before mutation.
  returned: always
  type: dict
upgrade_candidates:
  description: Installed packages that were outdated before mutation.
  returned: always
  type: dict
needs_sudo:
  description: Whether a failed cask-capable command needs a sudo password retry.
  returned: always
  type: bool
cleanup_candidates:
  description:
    - Whether Homebrew found non-ephemeral files to remove during cleanup.
    - In check mode this describes the current pre-mutation filesystem.
  returned: always
  type: bool
cleanup_checked:
  description:
    - Whether Homebrew was queried for cleanup candidates.
    - False for C(never), and for C(on_package_change) when no package mutation was made or predicted.
  returned: always
  type: bool
cleanup_performed:
  description: Whether cleanup removed files; always false in check mode.
  returned: always
  type: bool
command_timings:
  description: Elapsed wall-clock time for each Homebrew subprocess, with package names omitted.
  returned: always
  type: list
  elements: dict
  contains:
    command:
      description: Sanitized command name and options.
      type: str
    seconds:
      description: Elapsed wall-clock seconds.
      type: float
failed_command:
  description: Argument list for a failed Homebrew command.
  returned: failure
  type: list
  elements: str
"""

import json
import os
import re
import shlex
import tempfile
import time
from contextlib import contextmanager

from ansible.module_utils.basic import AnsibleModule


PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+@/-]*$")
SUDO_FAILURE_PATTERNS = (
    re.compile(r"sudo:.*a terminal is required", re.IGNORECASE),
    re.compile(r"sudo:.*no tty present", re.IGNORECASE),
    re.compile(r"sudo:.*no askpass", re.IGNORECASE),
    re.compile(r"sudo:.*askpass program", re.IGNORECASE),
    re.compile(r"sudo:.*password is required", re.IGNORECASE),
    re.compile(r"sudo:.*no password was provided", re.IGNORECASE),
)


class HomebrewError(Exception):
    def __init__(
        self,
        message,
        command=None,
        rc=None,
        stdout="",
        stderr="",
        sudo_capable=False,
    ):
        super().__init__(message)
        self.message = message
        self.command = command
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr
        self.sudo_capable = sudo_capable


def normalize_package_names(names, package_type):
    normalized = []
    seen = set()

    for name in names:
        if name != name.strip():
            raise HomebrewError("Invalid %s name with surrounding whitespace: %r" % (package_type, name))

        normalized_name = name.lower()
        if (
            not PACKAGE_NAME_RE.fullmatch(normalized_name)
            or "//" in normalized_name
            or normalized_name.endswith("/")
        ):
            raise HomebrewError("Invalid %s name: %r" % (package_type, name))

        if normalized_name not in seen:
            normalized.append(normalized_name)
            seen.add(normalized_name)

    return normalized


def package_identifiers(package, package_type):
    identifiers = set()
    if package_type == "formula":
        scalar_fields = ("name", "full_name")
        list_fields = ("aliases", "oldnames")
    else:
        scalar_fields = ("token", "full_token")
        list_fields = ("old_tokens",)

    for field in scalar_fields:
        value = package.get(field)
        if value:
            identifiers.add(value.lower())

    for field in list_fields:
        identifiers.update(value.lower() for value in package.get(field, []) if value)

    tap = package.get("tap")
    if tap:
        tap = tap.lower()
        identifiers.update("%s/%s" % (tap, value) for value in list(identifiers) if "/" not in value)

    return identifiers


def installed_packages_from_info(data, requested_names, package_type):
    states = resolved_packages_from_info(data, requested_names, preferred_type=package_type)
    return {name for name, state in states.items() if state["installed"]}


def resolved_packages_from_info(data, requested_names, preferred_type="formula"):
    formulae = data.get("formulae", [])
    casks = data.get("casks", [])
    states = {}

    for requested_name in requested_names:
        formula_matches = [
            package for package in formulae if requested_name in package_identifiers(package, "formula")
        ]
        cask_matches = [package for package in casks if requested_name in package_identifiers(package, "cask")]
        if preferred_type == "cask":
            package_type = "cask"
            matches = cask_matches
        elif formula_matches:
            package_type = "formula"
            matches = formula_matches
        else:
            package_type = "cask"
            matches = cask_matches

        if len(matches) != 1:
            raise HomebrewError(
                "Homebrew returned %d metadata matches for %s %r"
                % (len(matches), package_type, requested_name)
            )
        states[requested_name] = {
            "installed": bool(matches[0].get("installed")),
            "type": package_type,
        }

    return states


def outdated_packages_from_json(data):
    formulas = []
    casks = []

    for formula in data.get("formulae", []):
        if formula.get("pinned"):
            continue
        name = formula.get("full_name") or formula.get("name")
        if name:
            formulas.append(name.lower())

    for cask in data.get("casks", []):
        if cask.get("pinned"):
            continue
        name = cask.get("full_token") or cask.get("token") or cask.get("name")
        if name:
            casks.append(name.lower())

    return sorted(set(formulas)), sorted(set(casks))


def cleanup_dry_run_has_changes(stdout, stderr=""):
    for raw_line in ("%s\n%s" % (stdout, stderr)).splitlines():
        line = raw_line.strip()
        if line.startswith("Would remove: "):
            path = re.sub(r"\s+\([^()]*\)$", "", line[len("Would remove: ") :])
            # `brew update` needs and recreates these command indexes. Deleting them after every
            # update would make an otherwise idempotent update/cleanup pair change forever.
            if path.endswith(("/Homebrew/all_commands_list.txt", "/Homebrew/external_commands_list.txt")):
                continue
            return True
        if line.startswith("Would remove (broken link):") or "Would autoremove" in line:
            return True
    return False


def summarize_command(command):
    parts = [os.path.basename(command[0]), command[1]]
    parts.extend(argument for argument in command[2:] if argument.startswith("-"))
    return " ".join(parts)


def is_sudo_password_failure(error, password_was_supplied):
    if password_was_supplied or not error.sudo_capable:
        return False

    output = "%s\n%s" % (error.stdout, error.stderr)
    return any(pattern.search(output) for pattern in SUDO_FAILURE_PATTERNS)


@contextmanager
def sudo_askpass(password):
    descriptor = None
    path = None
    try:
        descriptor, path = tempfile.mkstemp(prefix="confmgmt-homebrew-askpass-")
        os.fchmod(descriptor, 0o700)
        with os.fdopen(descriptor, "w") as stream:
            descriptor = None
            stream.write("#!/bin/sh\nprintf '%s\\n' %s\n" % ("%s", shlex.quote(password)))
        yield path
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if path is not None:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


class HomebrewManager:
    def __init__(
        self,
        module,
        brew_path,
        formulas,
        casks,
        update_homebrew=False,
        upgrade_all=False,
        greedy=False,
        cleanup="never",
        sudo_password=None,
    ):
        self.module = module
        self.brew_path = brew_path
        self.formulas = normalize_package_names(formulas, "formula")
        self.casks = normalize_package_names(casks, "cask")
        self.update_homebrew = update_homebrew
        self.upgrade_all = upgrade_all
        self.greedy = greedy
        self.cleanup = cleanup
        self.sudo_password = sudo_password
        self.changed = False
        self.package_changed = False
        self.homebrew_updated = False
        self.cleanup_candidates = False
        self.cleanup_checked = False
        self.cleanup_performed = False
        self.command_timings = []
        self.install_candidates = {"formulas": [], "casks": []}
        self.upgrade_candidates = {"formulas": [], "casks": []}
        self.manage_formula_namespace = bool(self.formulas)
        self.manage_cask_namespace = bool(self.casks)
        self.environment = {
            "HOMEBREW_NO_ASK": "1",
            "HOMEBREW_NO_AUTO_UPDATE": "1",
            "LANGUAGE": "C",
            "LC_ALL": "C",
        }
        if self.cleanup != "never":
            # Avoid redundant implicit cleanup after install/upgrade; the explicit final cleanup is
            # more aggressive and reports its own changed state.
            self.environment["HOMEBREW_NO_INSTALL_CLEANUP"] = "1"

    def _run_command(self, command, sudo_capable=False):
        environment = self.environment.copy()
        started_at = time.monotonic()
        try:
            if sudo_capable and self.sudo_password is not None:
                with sudo_askpass(self.sudo_password) as askpass_path:
                    environment["SUDO_ASKPASS"] = askpass_path
                    rc, stdout, stderr = self.module.run_command(command, environ_update=environment)
            else:
                rc, stdout, stderr = self.module.run_command(command, environ_update=environment)
        finally:
            self.command_timings.append(
                {
                    "command": summarize_command(command),
                    "seconds": round(time.monotonic() - started_at, 3),
                }
            )

        if rc != 0:
            raise HomebrewError(
                stderr.strip() or stdout.strip() or "Homebrew command failed with exit code %s" % rc,
                command=command,
                rc=rc,
                stdout=stdout,
                stderr=stderr,
                sudo_capable=sudo_capable,
            )
        return stdout, stderr

    def _run_json_command(self, command, sudo_capable=False):
        stdout, stderr = self._run_command(command, sudo_capable=sudo_capable)
        try:
            return json.loads(stdout)
        except (TypeError, ValueError) as error:
            raise HomebrewError(
                "Homebrew returned invalid JSON: %s" % error,
                command=command,
                rc=0,
                stdout=stdout,
                stderr=stderr,
                sudo_capable=sudo_capable,
            )

    def _update(self):
        if not self.update_homebrew or self.module.check_mode or not (self.formulas or self.casks):
            return

        stdout, stderr = self._run_command([self.brew_path, "update"])
        output = "%s\n%s" % (stdout, stderr)
        if output.strip() and "already up-to-date." not in output.lower():
            self.homebrew_updated = True
            self.changed = True

    def _package_states(self, package_type, names, force_type=False):
        if not names:
            return {}

        command = [self.brew_path, "info", "--json=v2"]
        if force_type:
            command.append("--%s" % package_type)
        command.extend(names)
        data = self._run_json_command(command)
        return resolved_packages_from_info(data, names, preferred_type=package_type)

    def _discover_install_candidates(self):
        # The existing formulas input historically went through Homebrew without --formula. Preserve
        # that behavior so a package migrated by Homebrew from a formula to a cask, such as codex,
        # does not require an immediate host-playbook migration. Formulae still win name collisions.
        formula_states = self._package_states("formula", self.formulas)
        cask_states = self._package_states("cask", self.casks, force_type=True)
        implicit_casks = [name for name, state in formula_states.items() if state["type"] == "cask"]
        if implicit_casks:
            self.manage_cask_namespace = True

        missing_formulas = [
            name
            for name, state in formula_states.items()
            if state["type"] == "formula" and not state["installed"]
        ]
        missing_casks = [
            name
            for name, state in formula_states.items()
            if state["type"] == "cask" and not state["installed"]
        ]
        missing_casks.extend(name for name, state in cask_states.items() if not state["installed"])
        self.install_candidates = {
            "formulas": sorted(set(missing_formulas)),
            "casks": sorted(set(missing_casks)),
        }

    def _install(self):
        if self.module.check_mode:
            if self.install_candidates["formulas"] or self.install_candidates["casks"]:
                self.package_changed = True
                self.changed = True
            return

        if self.install_candidates["formulas"]:
            command = [self.brew_path, "install", "--formula", "--no-ask"]
            self._run_command(command + self.install_candidates["formulas"])
            self.package_changed = True
            self.changed = True

        if self.install_candidates["casks"]:
            command = [self.brew_path, "install", "--cask", "--no-ask"]
            self._run_command(command + self.install_candidates["casks"], sudo_capable=True)
            self.package_changed = True
            self.changed = True

    def _outdated_command(self):
        command = [self.brew_path, "outdated", "--json=v2"]
        if self.manage_formula_namespace and not self.manage_cask_namespace:
            command.append("--formula")
        elif self.manage_cask_namespace and not self.manage_formula_namespace:
            command.append("--cask")
        if self.manage_cask_namespace and self.greedy:
            command.append("--greedy")
        return command

    def _upgrade_command(self):
        command = [self.brew_path, "upgrade", "--no-ask"]
        if self.manage_formula_namespace and not self.manage_cask_namespace:
            command.append("--formula")
        elif self.manage_cask_namespace and not self.manage_formula_namespace:
            command.append("--cask")
        if self.manage_cask_namespace and self.greedy:
            command.append("--greedy")
        return command

    def _upgrade(self):
        if not self.upgrade_all or not (self.formulas or self.casks):
            return

        data = self._run_json_command(self._outdated_command())
        outdated_formulas, outdated_casks = outdated_packages_from_json(data)
        if not self.manage_formula_namespace:
            outdated_formulas = []
        if not self.manage_cask_namespace:
            outdated_casks = []
        self.upgrade_candidates = {
            "formulas": outdated_formulas,
            "casks": outdated_casks,
        }

        if not (outdated_formulas or outdated_casks):
            return
        if self.module.check_mode:
            self.package_changed = True
            self.changed = True
            return

        self._run_command(self._upgrade_command(), sudo_capable=self.manage_cask_namespace)
        self.package_changed = True
        self.changed = True

    def _cleanup(self):
        if self.cleanup == "never":
            return
        if self.cleanup == "on_package_change" and not self.package_changed:
            return

        command = [self.brew_path, "cleanup", "--prune=all"]
        self.cleanup_checked = True
        stdout, stderr = self._run_command(command + ["--dry-run"])
        self.cleanup_candidates = cleanup_dry_run_has_changes(stdout, stderr)
        if not self.cleanup_candidates:
            return

        self.changed = True
        if self.module.check_mode:
            return

        self._run_command(command)
        self.cleanup_performed = True

    def _message(self):
        install_count = len(self.install_candidates["formulas"]) + len(self.install_candidates["casks"])
        upgrade_count = len(self.upgrade_candidates["formulas"]) + len(self.upgrade_candidates["casks"])
        if self.module.check_mode:
            if self.changed:
                return "Homebrew would install %d, upgrade %d, cleanup=%s using current metadata" % (
                    install_count,
                    upgrade_count,
                    self.cleanup_candidates,
                )
            return "Homebrew packages are current using current metadata"
        if not self.changed:
            return "Homebrew packages are current"
        return "Homebrew updated=%s, installed=%d, upgraded=%d" % (
            self.homebrew_updated,
            install_count,
            upgrade_count,
        ) + (
            ", cleanup=%s" % (self.cleanup_performed if self.cleanup_checked else "skipped")
            if self.cleanup != "never"
            else ""
        )

    def result(self):
        return {
            "changed": self.changed,
            "msg": self._message(),
            "homebrew_updated": self.homebrew_updated,
            "cleanup_candidates": self.cleanup_candidates,
            "cleanup_checked": self.cleanup_checked,
            "cleanup_performed": self.cleanup_performed,
            "command_timings": self.command_timings,
            "install_candidates": self.install_candidates,
            "upgrade_candidates": self.upgrade_candidates,
            "needs_sudo": False,
        }

    def run(self):
        self._update()
        self._discover_install_candidates()
        self._install()
        self._upgrade()
        self._cleanup()
        return self.result()


def main():
    module = AnsibleModule(
        argument_spec={
            "formulas": {"type": "list", "elements": "str", "default": []},
            "casks": {"type": "list", "elements": "str", "default": []},
            "update_homebrew": {"type": "bool", "default": False},
            "upgrade_all": {"type": "bool", "default": False},
            "greedy": {"type": "bool", "default": False},
            "cleanup": {
                "type": "str",
                "choices": ["never", "on_package_change", "always"],
                "default": "never",
            },
            "sudo_password": {"type": "str", "no_log": True},
            "path": {
                "type": "list",
                "elements": "path",
                "default": [
                    "/usr/local/bin",
                    "/opt/homebrew/bin",
                    "/home/linuxbrew/.linuxbrew/bin",
                ],
            },
        },
        supports_check_mode=True,
    )

    brew_path = module.get_bin_path("brew", required=False, opt_dirs=module.params["path"])
    if not brew_path:
        module.fail_json(changed=False, needs_sudo=False, msg="Unable to locate the Homebrew executable")

    manager = None
    try:
        manager = HomebrewManager(
            module=module,
            brew_path=brew_path,
            formulas=module.params["formulas"],
            casks=module.params["casks"],
            update_homebrew=module.params["update_homebrew"],
            upgrade_all=module.params["upgrade_all"],
            greedy=module.params["greedy"],
            cleanup=module.params["cleanup"],
            sudo_password=module.params["sudo_password"],
        )
        module.exit_json(**manager.run())
    except HomebrewError as error:
        result = {
            "changed": manager.changed if manager else False,
            "msg": error.message,
            "homebrew_updated": manager.homebrew_updated if manager else False,
            "cleanup_candidates": manager.cleanup_candidates if manager else False,
            "cleanup_checked": manager.cleanup_checked if manager else False,
            "cleanup_performed": manager.cleanup_performed if manager else False,
            "command_timings": manager.command_timings if manager else [],
            "install_candidates": manager.install_candidates if manager else {"formulas": [], "casks": []},
            "upgrade_candidates": manager.upgrade_candidates if manager else {"formulas": [], "casks": []},
            "needs_sudo": is_sudo_password_failure(
                error,
                password_was_supplied=bool(manager and manager.sudo_password is not None),
            ),
        }
        if error.command is not None:
            result.update(
                {
                    "failed_command": error.command,
                    "rc": error.rc,
                    "stdout": error.stdout,
                    "stderr": error.stderr,
                }
            )
        module.fail_json(**result)


if __name__ == "__main__":
    main()
