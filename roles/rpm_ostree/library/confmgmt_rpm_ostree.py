#!/usr/bin/python

from __future__ import annotations

DOCUMENTATION = r"""
---
module: confmgmt_rpm_ostree
short_description: Manage an rpm-ostree deployment in one transaction sequence
description:
  - Upgrades rpm-ostree, installs declared layered packages, and reconciles kernel arguments.
  - Uses rpm-ostree's machine-readable status and unchanged exit code instead of parsing messages.
  - Batches package installation and kernel argument changes to avoid one transaction per item.
options:
  packages:
    description:
      - Layered packages that must be requested in the default deployment.
      - Packages not listed here are left untouched.
    type: list
    elements: str
    default: []
  kargs:
    description:
      - Kernel arguments to enforce in the default deployment.
      - Each argument key may be declared once. Existing values for declared keys are replaced.
      - Arguments with keys not listed here are left untouched.
    type: list
    elements: str
    default: []
  upgrade:
    description: Stage an operating system upgrade when one is available.
    type: bool
    default: false
  executable:
    description: rpm-ostree executable name or path.
    type: path
    default: rpm-ostree
attributes:
  check_mode:
    support: partial
  diff_mode:
    support: none
author:
  - conf-mgmt
notes:
  - Upgrade availability is not predicted in check mode because rpm-ostree documents its preview as unreliable.
"""

EXAMPLES = r"""
- name: Manage the pending rpm-ostree deployment
  confmgmt_rpm_ostree:
    packages:
      - tailscale
      - zsh
    kargs:
      - quiet
      - ttm.pages_limit=29360128
    upgrade: true
"""

RETURN = r"""
upgrade_changed:
  description: Whether an operating system upgrade was staged.
  returned: always
  type: bool
upgrade_check_skipped:
  description: Whether upgrade prediction was deliberately skipped in check mode.
  returned: always
  type: bool
package_candidates:
  description: Declared packages missing from the default deployment's requested packages.
  returned: always
  type: list
  elements: str
packages_changed:
  description: Whether a package transaction changed the deployment.
  returned: always
  type: bool
kargs_to_remove:
  description: Existing kernel arguments removed because their declared keys needed reconciliation.
  returned: always
  type: list
  elements: str
kargs_to_append:
  description: Declared kernel arguments appended during reconciliation.
  returned: always
  type: list
  elements: str
kargs_changed:
  description: Whether a kernel argument transaction changed the deployment.
  returned: always
  type: bool
reboot_required:
  description: Whether the default deployment is not the currently booted deployment.
  returned: always
  type: bool
failed_command:
  description: Argument list for a failed rpm-ostree command.
  returned: failure
  type: list
  elements: str
msg:
  description: Concise summary of deployment changes and reboot state.
  returned: success
  type: str
"""

import json
import re
import shlex

from ansible.module_utils.basic import AnsibleModule


PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.-]*$")


class RpmOstreeError(Exception):
    def __init__(self, message, command=None, rc=None, stdout="", stderr=""):
        super().__init__(message)
        self.message = message
        self.command = command
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr


def normalize_packages(packages):
    normalized = []
    seen = set()

    for package in packages:
        if package != package.strip() or not PACKAGE_NAME_RE.fullmatch(package):
            raise RpmOstreeError("Invalid rpm-ostree package name: %r" % package)
        if package not in seen:
            normalized.append(package)
            seen.add(package)

    return normalized


def normalize_kargs(kargs):
    normalized = []
    seen_keys = set()

    for raw_argument in kargs:
        if "\0" in raw_argument or "\n" in raw_argument or "\r" in raw_argument:
            raise RpmOstreeError("Invalid kernel argument: %r" % raw_argument)
        try:
            tokens = shlex.split(raw_argument)
        except ValueError as error:
            raise RpmOstreeError(
                "Invalid kernel argument %r: %s" % (raw_argument, error)
            )
        if len(tokens) != 1 or not tokens[0]:
            raise RpmOstreeError(
                "Kernel argument must contain exactly one shell token: %r" % raw_argument
            )

        argument = tokens[0]
        key = argument.split("=", 1)[0]
        if not key or any(character.isspace() for character in key):
            raise RpmOstreeError("Invalid kernel argument key in %r" % raw_argument)
        if '"' in argument or any(
            character.isspace() and character != " " for character in argument
        ):
            raise RpmOstreeError("Unsupported kernel argument quoting in %r" % raw_argument)
        if key == "ostree":
            raise RpmOstreeError("The rpm-ostree managed 'ostree' kernel argument is reserved")
        if key in seen_keys:
            raise RpmOstreeError("Kernel argument key declared more than once: %s" % key)

        normalized.append(argument)
        seen_keys.add(key)

    return normalized


def parse_status(stdout):
    try:
        status = json.loads(stdout)
    except (TypeError, ValueError) as error:
        raise RpmOstreeError("Could not parse rpm-ostree status JSON: %s" % error)

    deployments = status.get("deployments") if isinstance(status, dict) else None
    if not isinstance(deployments, list) or not deployments or not isinstance(deployments[0], dict):
        raise RpmOstreeError("rpm-ostree status JSON did not contain a default deployment")

    default_deployment = deployments[0]
    requested_packages = default_deployment.get("requested-packages", [])
    if requested_packages is None:
        requested_packages = []
    if not isinstance(requested_packages, list) or not all(
        isinstance(package, str) for package in requested_packages
    ):
        raise RpmOstreeError(
            "rpm-ostree status JSON contained invalid requested-packages"
        )

    return {
        "requested_packages": set(requested_packages),
        "reboot_required": not bool(default_deployment.get("booted", False)),
    }


def parse_kargs(stdout):
    try:
        return shlex.split(stdout)
    except ValueError as error:
        raise RpmOstreeError("Could not parse rpm-ostree kernel arguments: %s" % error)


def karg_key(argument):
    return argument.split("=", 1)[0]


def format_karg_argument(argument):
    if " " not in argument:
        return argument

    key, separator, value = argument.partition("=")
    if not separator:
        raise RpmOstreeError("Kernel argument flags cannot contain spaces: %r" % argument)
    return '%s="%s"' % (key, value)


def plan_karg_changes(current_kargs, desired_kargs):
    current_by_key = {}
    for argument in current_kargs:
        current_by_key.setdefault(karg_key(argument), []).append(argument)

    to_remove = []
    to_append = []
    for desired in desired_kargs:
        current = current_by_key.get(karg_key(desired), [])
        if current == [desired]:
            continue
        to_remove.extend(current)
        to_append.append(desired)

    return to_remove, to_append


def count_phrase(count, singular, plural=None):
    return "%d %s" % (count, singular if count == 1 else (plural or singular + "s"))


def summarize_result(result, check_mode=False):
    messages = []

    if check_mode:
        if result["package_candidates"]:
            messages.append(
                "would request %s"
                % count_phrase(len(result["package_candidates"]), "layered package")
            )
        if result["kargs_to_append"]:
            messages.append(
                "would reconcile %s"
                % count_phrase(len(result["kargs_to_append"]), "kernel argument key")
            )
        if not result["changed"]:
            messages.append("declared deployment state is current")
        if result["upgrade_check_skipped"]:
            messages.append("upgrade availability not checked")
        prefix = "rpm-ostree check mode"
    else:
        if result["upgrade_changed"]:
            messages.append("staged operating system upgrade")
        if result["packages_changed"]:
            messages.append(
                "requested %s"
                % count_phrase(len(result["package_candidates"]), "layered package")
            )
        if result["kargs_changed"]:
            messages.append(
                "reconciled %s"
                % count_phrase(len(result["kargs_to_append"]), "kernel argument key")
            )
        if not messages:
            messages.append("deployment is current")
        prefix = "rpm-ostree"

    messages.append("reboot required" if result["reboot_required"] else "no reboot required")
    return "%s: %s" % (prefix, "; ".join(messages))


class RpmOstreeManager:
    def __init__(self, module, executable, packages=None, kargs=None, upgrade=False):
        self.module = module
        self.executable = executable
        self.packages = normalize_packages(packages or [])
        self.kargs = normalize_kargs(kargs or [])
        self.upgrade = upgrade
        self.result = {
            "changed": False,
            "upgrade_changed": False,
            "upgrade_check_skipped": False,
            "package_candidates": [],
            "packages_changed": False,
            "kargs_to_remove": [],
            "kargs_to_append": [],
            "kargs_changed": False,
            "reboot_required": False,
        }

    def command(self, arguments, accepted_rcs=(0,)):
        command = [self.executable] + list(arguments)
        rc, stdout, stderr = self.module.run_command(
            command,
            environ_update={"LANGUAGE": "C", "LC_ALL": "C"},
        )
        if rc not in accepted_rcs:
            detail = stderr.strip() or stdout.strip() or "no command output"
            raise RpmOstreeError(
                "rpm-ostree command failed with exit code %s: %s" % (rc, detail),
                command=command,
                rc=rc,
                stdout=stdout,
                stderr=stderr,
            )
        return rc, stdout, stderr

    def status(self):
        command = [self.executable, "status", "--json"]
        unused_rc, stdout, stderr = self.command(["status", "--json"])
        try:
            return parse_status(stdout)
        except RpmOstreeError as error:
            error.command = command
            error.stdout = stdout
            error.stderr = stderr
            raise

    def current_kargs(self):
        command = [self.executable, "kargs"]
        unused_rc, stdout, stderr = self.command(["kargs"])
        try:
            return parse_kargs(stdout)
        except RpmOstreeError as error:
            error.command = command
            error.stdout = stdout
            error.stderr = stderr
            raise

    def run(self):
        initial_status = self.status()
        self.result["reboot_required"] = initial_status["reboot_required"]
        self.result["package_candidates"] = [
            package
            for package in self.packages
            if package not in initial_status["requested_packages"]
        ]

        if self.module.check_mode:
            self.result["upgrade_check_skipped"] = self.upgrade
            self.plan_kargs()
            self.result["changed"] = bool(
                self.result["package_candidates"] or self.result["kargs_to_append"]
            )
            self.result["msg"] = summarize_result(self.result, check_mode=True)
            return self.result

        if self.upgrade:
            rc, unused_stdout, unused_stderr = self.command(
                ["upgrade", "--unchanged-exit-77"], accepted_rcs=(0, 77)
            )
            self.result["upgrade_changed"] = rc == 0
            self.result["changed"] |= self.result["upgrade_changed"]

        if self.result["package_candidates"]:
            rc, unused_stdout, unused_stderr = self.command(
                [
                    "install",
                    "--allow-inactive",
                    "--idempotent",
                    "--unchanged-exit-77",
                ]
                + self.result["package_candidates"],
                accepted_rcs=(0, 77),
            )
            self.result["packages_changed"] = rc == 0
            self.result["changed"] |= self.result["packages_changed"]

        self.plan_kargs()
        if self.result["kargs_to_remove"] or self.result["kargs_to_append"]:
            arguments = ["kargs", "--unchanged-exit-77"]
            # rpm-ostree applies strict deletes before strict appends. Its conditional
            # variants run in the opposite order and can remove an already-desired value.
            arguments.extend(
                "--delete=%s" % format_karg_argument(argument)
                for argument in self.result["kargs_to_remove"]
            )
            arguments.extend(
                "--append=%s" % format_karg_argument(argument)
                for argument in self.result["kargs_to_append"]
            )
            rc, unused_stdout, unused_stderr = self.command(
                arguments, accepted_rcs=(0, 77)
            )
            self.result["kargs_changed"] = rc == 0
            self.result["changed"] |= self.result["kargs_changed"]

        final_status = self.status()
        self.result["reboot_required"] = final_status["reboot_required"]
        self.result["msg"] = summarize_result(self.result)
        return self.result

    def plan_kargs(self):
        if not self.kargs:
            return

        current_kargs = self.current_kargs()
        to_remove, to_append = plan_karg_changes(current_kargs, self.kargs)
        self.result["kargs_to_remove"] = to_remove
        self.result["kargs_to_append"] = to_append


def main():
    module = AnsibleModule(
        argument_spec={
            "packages": {"type": "list", "elements": "str", "default": []},
            "kargs": {"type": "list", "elements": "str", "default": []},
            "upgrade": {"type": "bool", "default": False},
            "executable": {"type": "path", "default": "rpm-ostree"},
        },
        supports_check_mode=True,
    )

    manager = None
    try:
        manager = RpmOstreeManager(
            module=module,
            executable=module.params["executable"],
            packages=module.params["packages"],
            kargs=module.params["kargs"],
            upgrade=module.params["upgrade"],
        )
        module.exit_json(**manager.run())
    except RpmOstreeError as error:
        result = manager.result if manager is not None else {"changed": False}
        module.fail_json(
            msg=error.message,
            failed_command=error.command,
            rc=error.rc,
            stdout=error.stdout,
            stderr=error.stderr,
            **result,
        )


if __name__ == "__main__":
    main()
