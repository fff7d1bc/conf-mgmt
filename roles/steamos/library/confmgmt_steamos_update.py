#!/usr/bin/python

from __future__ import annotations

DOCUMENTATION = r"""
---
module: confmgmt_steamos_update
short_description: Apply SteamOS atomic updates
description:
  - Checks for and applies SteamOS atomic operating-system updates.
  - Uses the current C(atomupd-manager) interface rather than the deprecated C(steamos-update) wrapper.
  - Detects updates that have already been applied and are waiting for a reboot.
options:
  path:
    description: Directories searched for C(atomupd-manager).
    type: list
    elements: path
    default:
      - /usr/bin
attributes:
  check_mode:
    support: full
  diff_mode:
    support: none
author:
  - conf-mgmt
"""

EXAMPLES = r"""
- name: Apply an available SteamOS update
  confmgmt_steamos_update:
"""

RETURN = r"""
initial_status:
  description: Atomic updater status observed before checking for updates.
  returned: always
  type: str
update_available:
  description: Whether the update check found a newer build.
  returned: always
  type: bool
target_build_id:
  description: Build identifier returned by the update check.
  returned: when an update is available
  type: str
update_applied:
  description: Whether this invocation successfully applied an update.
  returned: always
  type: bool
update_in_progress:
  description: Whether an update was already in progress or paused.
  returned: always
  type: bool
reboot_required:
  description: Whether an applied update is waiting for a reboot.
  returned: always
  type: bool
failed_command:
  description: Argument list for a failed command.
  returned: failure
  type: list
  elements: str
"""

import re

from ansible.module_utils.basic import AnsibleModule


BUILD_ID_RE = re.compile(r"(?m)^\s*ID:\s*([0-9]+(?:\.[0-9]+)+)(?:\s|$)")
KNOWN_STATUSES = {
    "idle",
    "in-progress",
    "paused",
    "successful",
    "failed",
    "cancelled",
}


class SteamOSUpdateError(Exception):
    def __init__(self, message, command=None, rc=None, stdout="", stderr=""):
        super().__init__(message)
        self.message = message
        self.command = command
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr


def parse_update_status(stdout):
    status = stdout.strip()
    if status not in KNOWN_STATUSES:
        raise SteamOSUpdateError("atomupd-manager returned unknown update status %r" % status)
    return status


def parse_update_check(stdout):
    output = stdout.strip()
    if output == "No update available":
        return None

    match = BUILD_ID_RE.search(output)
    if not match:
        raise SteamOSUpdateError("atomupd-manager returned unrecognized update check output")
    return match.group(1)


class SteamOSUpdateManager:
    def __init__(self, module, executable):
        self.module = module
        self.executable = executable
        self.changed = False
        self.initial_status = "unknown"
        self.update_available = False
        self.target_build_id = None
        self.update_applied = False
        self.update_in_progress = False
        self.reboot_required = False

    def _run_command(self, arguments):
        command = [self.executable] + arguments
        rc, stdout, stderr = self.module.run_command(command)
        if rc != 0:
            raise SteamOSUpdateError(
                stderr.strip()
                or stdout.strip()
                or "Command failed with exit code %s" % rc,
                command=command,
                rc=rc,
                stdout=stdout,
                stderr=stderr,
            )
        return stdout

    def _run_parsed_command(self, arguments, parser):
        stdout = self._run_command(arguments)
        try:
            return parser(stdout)
        except SteamOSUpdateError as error:
            error.command = [self.executable] + arguments
            error.rc = 0
            error.stdout = stdout
            raise

    def _message(self):
        if self.reboot_required:
            if self.update_applied:
                return "SteamOS update %s applied; reboot required" % self.target_build_id
            return "SteamOS update already applied; reboot required"
        if self.update_in_progress:
            return "SteamOS update is %s" % self.initial_status
        if self.update_available:
            if self.module.check_mode:
                return "SteamOS update %s is available" % self.target_build_id
            return "SteamOS update %s was not applied" % self.target_build_id
        return "SteamOS is current"

    def result(self):
        result = {
            "changed": self.changed,
            "msg": self._message(),
            "initial_status": self.initial_status,
            "update_available": self.update_available,
            "update_applied": self.update_applied,
            "update_in_progress": self.update_in_progress,
            "reboot_required": self.reboot_required,
        }
        if self.target_build_id is not None:
            result["target_build_id"] = self.target_build_id
        return result

    def run(self):
        self.initial_status = self._run_parsed_command(
            ["get-update-status"],
            parse_update_status,
        )
        if self.initial_status == "successful":
            self.reboot_required = True
            return self.result()
        if self.initial_status in ("in-progress", "paused"):
            self.update_in_progress = True
            return self.result()

        self.target_build_id = self._run_parsed_command(["check"], parse_update_check)
        if self.target_build_id is None:
            return self.result()

        self.update_available = True
        if self.module.check_mode:
            self.changed = True
            return self.result()

        # A failed update may already have modified the inactive slot, so preserve possible partial
        # change even when atomupd-manager ultimately returns an error.
        self.changed = True
        self._run_command(["update", self.target_build_id])
        self.update_applied = True
        self.reboot_required = True
        return self.result()


def main():
    module = AnsibleModule(
        argument_spec={
            "path": {
                "type": "list",
                "elements": "path",
                "default": ["/usr/bin"],
            },
        },
        supports_check_mode=True,
    )

    executable = module.get_bin_path(
        "atomupd-manager",
        required=False,
        opt_dirs=module.params["path"],
    )
    if not executable:
        module.fail_json(
            changed=False,
            msg="Unable to locate atomupd-manager",
            update_available=False,
            update_applied=False,
            update_in_progress=False,
            reboot_required=False,
        )

    manager = SteamOSUpdateManager(module, executable)
    try:
        module.exit_json(**manager.run())
    except SteamOSUpdateError as error:
        result = manager.result()
        result["msg"] = error.message
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
