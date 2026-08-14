#!/usr/bin/python

from __future__ import annotations

DOCUMENTATION = r"""
---
module: confmgmt_flatpak
short_description: Manage this repository's Flatpak installation in bulk
description:
  - Ensures a Flatpak remote, updates installed refs, and installs declared applications.
  - Uses column-oriented Flatpak queries and installed commit snapshots instead of parsing messages.
  - Batches application installation into one Flatpak transaction.
options:
  packages:
    description:
      - Application IDs that must be installed.
      - Applications not listed here are left untouched.
    type: list
    elements: str
    default: []
  remote:
    description: Remote used to install declared applications.
    type: str
    default: flathub
  remote_url:
    description: Repository URL used when the remote is missing.
    type: str
    default: https://dl.flathub.org/repo/flathub.flatpakrepo
  method:
    description: Manage the system-wide or current user's Flatpak installation.
    type: str
    choices: [system, user]
    default: system
  upgrade_all:
    description: Update all installed applications and runtimes.
    type: bool
    default: false
  executable:
    description: Flatpak executable name or path.
    type: path
    default: flatpak
attributes:
  check_mode:
    support: full
  diff_mode:
    support: none
author:
  - conf-mgmt
"""

EXAMPLES = r"""
- name: Manage system Flatpaks
  confmgmt_flatpak:
    packages:
      - io.github.ilya_zlobintsev.LACT
      - it.mijorus.gearlever
    upgrade_all: true
"""

RETURN = r"""
remote_added:
  description: Whether the configured remote was added.
  returned: always
  type: bool
remote_enable_changed:
  description: Whether an existing disabled remote was enabled.
  returned: always
  type: bool
install_candidates:
  description: Declared application IDs missing before installation.
  returned: always
  type: list
  elements: str
packages_changed:
  description: Whether an application installation transaction ran successfully.
  returned: always
  type: bool
update_candidates:
  description:
    - Refs reported as updatable in check mode.
    - Empty during normal execution because normal change detection compares installed commits.
  returned: always
  type: list
  elements: str
updated_refs:
  description: Installed refs added, removed, or moved to another commit by the update transaction.
  returned: always
  type: list
  elements: str
updates_changed:
  description: Whether the update transaction changed installed refs.
  returned: always
  type: bool
msg:
  description: Concise summary of Flatpak changes.
  returned: success
  type: str
failed_command:
  description: Argument list for a failed Flatpak command.
  returned: failure
  type: list
  elements: str
"""

import re

from ansible.module_utils.basic import AnsibleModule


PACKAGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.[A-Za-z0-9_.-]+$")
REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class FlatpakError(Exception):
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
        if (
            package != package.strip()
            or not PACKAGE_ID_RE.fullmatch(package)
            or package.count(".") < 2
            or ".." in package
        ):
            raise FlatpakError("Invalid Flatpak application ID: %r" % package)
        if package not in seen:
            normalized.append(package)
            seen.add(package)

    return normalized


def validate_remote(remote, remote_url):
    if remote != remote.strip() or not REMOTE_NAME_RE.fullmatch(remote):
        raise FlatpakError("Invalid Flatpak remote name: %r" % remote)
    if (
        not remote_url
        or remote_url != remote_url.strip()
        or remote_url.startswith("-")
        or "\0" in remote_url
        or "\n" in remote_url
        or "\r" in remote_url
    ):
        raise FlatpakError("Invalid Flatpak remote URL: %r" % remote_url)


def parse_remotes(stdout):
    remotes = {}

    for line in stdout.splitlines():
        if not line:
            continue
        fields = line.split("\t", 1)
        name = fields[0]
        options = fields[1].split(",") if len(fields) == 2 and fields[1] else []
        if not name or name in remotes:
            raise FlatpakError("Could not parse Flatpak remote listing")
        remotes[name] = {"enabled": "disabled" not in options}

    return remotes


def parse_installed_refs(stdout):
    refs = {}

    for line in stdout.splitlines():
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) != 3 or not all(fields):
            raise FlatpakError("Could not parse installed Flatpak ref listing")
        application, ref, active_commit = fields
        if ref in refs:
            raise FlatpakError("Flatpak listed installed ref more than once: %s" % ref)
        refs[ref] = {
            "application": application,
            "active_commit": active_commit,
        }

    return refs


def parse_lines(stdout):
    return list(dict.fromkeys(line.strip() for line in stdout.splitlines() if line.strip()))


def changed_refs(before, after):
    return sorted(
        ref
        for ref in set(before) | set(after)
        if before.get(ref) != after.get(ref)
    )


def installed_application_ids(refs):
    return {
        state["application"]
        for ref, state in refs.items()
        if ref.startswith("app/")
    }


def count_phrase(count, singular):
    return "%d %s" % (count, singular if count == 1 else singular + "s")


def summarize_result(result, remote, check_mode=False):
    messages = []

    if result["remote_added"]:
        messages.append(("would add" if check_mode else "added") + " remote %s" % remote)
    if result["remote_enable_changed"]:
        messages.append(("would enable" if check_mode else "enabled") + " remote %s" % remote)
    if check_mode and result["update_candidates"]:
        messages.append(
            "would update %s"
            % count_phrase(len(result["update_candidates"]), "Flatpak ref")
        )
    if not check_mode and result["updates_changed"]:
        messages.append(
            "updated %s" % count_phrase(len(result["updated_refs"]), "Flatpak ref")
        )
    if result["install_candidates"]:
        verb = "would install" if check_mode else "installed"
        messages.append(
            "%s %s"
            % (verb, count_phrase(len(result["install_candidates"]), "application"))
        )
    if not messages:
        messages.append("installation is current")

    prefix = "Flatpak check mode" if check_mode else "Flatpak"
    return "%s: %s" % (prefix, "; ".join(messages))


class FlatpakManager:
    def __init__(
        self,
        module,
        executable,
        packages=None,
        remote="flathub",
        remote_url="https://dl.flathub.org/repo/flathub.flatpakrepo",
        method="system",
        upgrade_all=False,
    ):
        validate_remote(remote, remote_url)
        self.module = module
        self.executable = executable
        self.packages = normalize_packages(packages or [])
        self.remote = remote
        self.remote_url = remote_url
        self.method = method
        self.method_option = "--%s" % method
        self.upgrade_all = upgrade_all
        self.result = {
            "changed": False,
            "remote_added": False,
            "remote_enable_changed": False,
            "install_candidates": [],
            "packages_changed": False,
            "update_candidates": [],
            "updated_refs": [],
            "updates_changed": False,
        }

    def command(self, arguments):
        command = [self.executable] + list(arguments)
        rc, stdout, stderr = self.module.run_command(
            command,
            environ_update={"LANGUAGE": "C", "LC_ALL": "C"},
        )
        if rc != 0:
            detail = stderr.strip() or stdout.strip() or "no command output"
            raise FlatpakError(
                "Flatpak command failed with exit code %s: %s" % (rc, detail),
                command=command,
                rc=rc,
                stdout=stdout,
                stderr=stderr,
            )
        return stdout

    def remotes(self):
        arguments = [
            "remotes",
            self.method_option,
            "--show-disabled",
            "--columns=name,options",
        ]
        stdout = self.command(arguments)
        try:
            return parse_remotes(stdout)
        except FlatpakError as error:
            error.command = [self.executable] + arguments
            error.stdout = stdout
            raise

    def installed_refs(self):
        arguments = [
            "list",
            self.method_option,
            "--all",
            "--columns=application,ref,active",
        ]
        stdout = self.command(arguments)
        try:
            return parse_installed_refs(stdout)
        except FlatpakError as error:
            error.command = [self.executable] + arguments
            error.stdout = stdout
            raise

    def available_updates(self):
        arguments = [
            "remote-ls",
            self.method_option,
            "--updates",
            "--columns=ref",
        ]
        stdout = self.command(arguments)
        return parse_lines(stdout)

    def run(self):
        remotes = self.remotes()
        remote_state = remotes.get(self.remote)
        self.result["remote_added"] = remote_state is None
        self.result["remote_enable_changed"] = bool(
            remote_state is not None and not remote_state["enabled"]
        )

        installed_before = self.installed_refs()
        installed_applications = installed_application_ids(installed_before)
        self.result["install_candidates"] = [
            package for package in self.packages if package not in installed_applications
        ]

        if self.module.check_mode:
            if self.upgrade_all:
                self.result["update_candidates"] = self.available_updates()
            self.result["changed"] = bool(
                self.result["remote_added"]
                or self.result["remote_enable_changed"]
                or self.result["install_candidates"]
                or self.result["update_candidates"]
            )
            self.result["msg"] = summarize_result(
                self.result, self.remote, check_mode=True
            )
            return self.result

        if self.result["remote_added"]:
            self.command(
                [
                    "remote-add",
                    self.method_option,
                    "--if-not-exists",
                    self.remote,
                    self.remote_url,
                ]
            )
            self.result["changed"] = True
        elif self.result["remote_enable_changed"]:
            self.command(
                ["remote-modify", self.method_option, "--enable", self.remote]
            )
            self.result["changed"] = True

        installed_after_update = installed_before
        if self.upgrade_all:
            changed_before_update = self.result["changed"]
            self.command(["update", self.method_option, "--noninteractive"])
            # Treat a successfully returned update as potentially mutating until the
            # machine-oriented snapshot proves otherwise. This preserves partial state
            # if the follow-up query itself fails.
            self.result["changed"] = True
            installed_after_update = self.installed_refs()
            self.result["updated_refs"] = changed_refs(
                installed_before, installed_after_update
            )
            self.result["updates_changed"] = bool(self.result["updated_refs"])
            self.result["changed"] = bool(
                changed_before_update or self.result["updates_changed"]
            )

        installed_applications = installed_application_ids(installed_after_update)
        self.result["install_candidates"] = [
            package for package in self.packages if package not in installed_applications
        ]
        if self.result["install_candidates"]:
            self.command(
                [
                    "install",
                    self.method_option,
                    "--noninteractive",
                    self.remote,
                ]
                + self.result["install_candidates"]
            )
            self.result["packages_changed"] = True
            self.result["changed"] = True

        self.result["msg"] = summarize_result(self.result, self.remote)
        return self.result


def main():
    module = AnsibleModule(
        argument_spec={
            "packages": {"type": "list", "elements": "str", "default": []},
            "remote": {"type": "str", "default": "flathub"},
            "remote_url": {
                "type": "str",
                "default": "https://dl.flathub.org/repo/flathub.flatpakrepo",
            },
            "method": {
                "type": "str",
                "choices": ["system", "user"],
                "default": "system",
            },
            "upgrade_all": {"type": "bool", "default": False},
            "executable": {"type": "path", "default": "flatpak"},
        },
        supports_check_mode=True,
    )

    manager = None
    try:
        manager = FlatpakManager(
            module=module,
            executable=module.params["executable"],
            packages=module.params["packages"],
            remote=module.params["remote"],
            remote_url=module.params["remote_url"],
            method=module.params["method"],
            upgrade_all=module.params["upgrade_all"],
        )
        module.exit_json(**manager.run())
    except FlatpakError as error:
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
