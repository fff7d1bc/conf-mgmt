#!/usr/bin/python

from __future__ import annotations

DOCUMENTATION = r"""
---
module: confmgmt_macos_defaults
short_description: Manage macOS preferences in bulk
description:
  - Manages a conservative set of macOS user preference operations through C(defaults).
  - Reads complete domains as XML property lists for type-preserving comparison.
  - Writes XML property-list values so nested dictionaries and arrays retain their native types.
  - Batches discovery by domain, preserves unmanaged dictionary entries when requested, and verifies
    every mutation.
  - Changed preferences require the managed user to log out and back in before taking effect.
options:
  preferences:
    description: Preferences to manage in declaration order.
    type: list
    elements: dict
    required: true
    suboptions:
      domain:
        description: Defaults domain containing the preference.
        type: str
        required: true
      key:
        description: Preference key within the domain.
        type: str
        required: true
      value:
        description:
          - Native property-list value required when O(state=present).
          - Booleans, integers, floats, strings, arrays, and dictionaries are supported.
        type: raw
      state:
        description: Whether the preference must exist with O(value) or be absent.
        type: str
        choices:
          - present
          - absent
        default: present
      host:
        description:
          - Scope of the preference.
          - C(anyHost) is the normal user preference domain; C(currentHost) passes
            C(-currentHost) to C(defaults).
        type: str
        choices:
          - anyHost
          - currentHost
        default: anyHost
      dict_mode:
        description:
          - C(replace) manages the complete value.
          - C(merge) requires a dictionary value and replaces only its declared top-level entries.
        type: str
        choices:
          - replace
          - merge
        default: replace
attributes:
  check_mode:
    support: full
  diff_mode:
    support: none
author:
  - conf-mgmt
"""

EXAMPLES = r"""
- name: Manage macOS preferences
  confmgmt_macos_defaults:
    preferences:
      - domain: NSGlobalDomain
        key: com.apple.swipescrolldirection
        value: false
      - domain: com.apple.dock
        key: wvous-tl-corner
        state: absent
      - domain: com.apple.coreservices.useractivityd
        host: currentHost
        key: ActivityAdvertisingAllowed
        value: false

- name: Merge one symbolic hotkey without replacing the others
  confmgmt_macos_defaults:
    preferences:
      - domain: com.apple.symbolichotkeys
        key: AppleSymbolicHotKeys
        dict_mode: merge
        value:
          '32':
            enabled: true
            value:
              type: standard
              parameters: [65535, 53, 524288]
"""

RETURN = r"""
managed_preferences:
  description: Stable labels for every declared preference.
  returned: always
  type: list
  elements: str
changed_preferences:
  description: Declared preferences that differed before mutation.
  returned: always
  type: list
  elements: str
applied_preferences:
  description: Preferences whose mutation command completed successfully.
  returned: always
  type: list
  elements: str
relogin_required:
  description: Whether applying the reported changes requires a new login session.
  returned: always
  type: bool
failed_command:
  description: Argument list for a failed C(defaults) command.
  returned: failure
  type: list
  elements: str
"""

import copy
import plistlib

from ansible.module_utils.basic import AnsibleModule


MISSING = object()


class MacOSDefaultsError(Exception):
    def __init__(self, message, command=None, rc=None, stdout="", stderr=""):
        super().__init__(message)
        self.message = message
        self.command = command
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr


def preference_label(preference):
    prefix = "currentHost:" if preference["host"] == "currentHost" else ""
    return "%s%s:%s" % (prefix, preference["domain"], preference["key"])


def values_equal(current, desired):
    if type(current) is not type(desired):
        return False
    if isinstance(current, dict):
        return current.keys() == desired.keys() and all(
            values_equal(current[key], desired[key]) for key in current
        )
    if isinstance(current, list):
        return len(current) == len(desired) and all(
            values_equal(current_item, desired_item)
            for current_item, desired_item in zip(current, desired)
        )
    return current == desired


def validate_plist_value(value, label):
    try:
        plistlib.dumps(value, fmt=plistlib.FMT_XML)
    except (OverflowError, TypeError, ValueError) as error:
        raise MacOSDefaultsError("Invalid property-list value for %s: %s" % (label, error)) from error


def normalize_preferences(preferences):
    normalized = []
    seen = set()

    for raw_preference in preferences:
        domain = raw_preference.get("domain")
        key = raw_preference.get("key")
        state = raw_preference.get("state", "present")
        host = raw_preference.get("host", "anyHost")
        dict_mode = raw_preference.get("dict_mode", "replace")
        value = raw_preference.get("value", MISSING)

        if (
            not isinstance(domain, str)
            or not domain
            or domain != domain.strip()
            or "\0" in domain
        ):
            raise MacOSDefaultsError("Invalid defaults domain: %r" % domain)
        if not isinstance(key, str) or not key or key != key.strip() or "\0" in key:
            raise MacOSDefaultsError("Invalid defaults key: %r" % key)
        if state not in ("present", "absent"):
            raise MacOSDefaultsError("Invalid state for %s:%s: %r" % (domain, key, state))
        if host not in ("anyHost", "currentHost"):
            raise MacOSDefaultsError("Invalid host scope for %s:%s: %r" % (domain, key, host))
        if dict_mode not in ("replace", "merge"):
            raise MacOSDefaultsError("Invalid dictionary mode for %s:%s: %r" % (domain, key, dict_mode))

        preference = {
            "domain": domain,
            "key": key,
            "state": state,
            "host": host,
            "dict_mode": dict_mode,
        }
        label = preference_label(preference)
        if label in seen:
            raise MacOSDefaultsError("Duplicate macOS preference: %s" % label)

        if state == "present":
            if value is MISSING or value is None:
                raise MacOSDefaultsError("A non-null value is required for %s" % label)
            if dict_mode == "merge" and not isinstance(value, dict):
                raise MacOSDefaultsError("Dictionary merge requires a dictionary value for %s" % label)
            validate_plist_value(value, label)
            preference["value"] = copy.deepcopy(value)
        elif dict_mode != "replace":
            raise MacOSDefaultsError("Dictionary merge is not valid with absent state for %s" % label)

        normalized.append(preference)
        seen.add(label)

    return normalized


def host_args(host):
    return ["-currentHost"] if host == "currentHost" else []


def export_domain(module, defaults_path, host, domain):
    command = [defaults_path] + host_args(host) + ["export", domain, "-"]
    rc, stdout, stderr = module.run_command(command)
    diagnostic = "%s\n%s" % (stdout, stderr)

    if rc == 1 and "does not exist" in diagnostic.lower():
        return {}
    if rc != 0:
        raise MacOSDefaultsError(
            "Unable to export macOS defaults domain %s" % domain,
            command=command,
            rc=rc,
            stdout=stdout,
            stderr=stderr,
        )

    encoded = stdout if isinstance(stdout, bytes) else stdout.encode("utf-8")
    try:
        preferences = plistlib.loads(encoded)
    except (plistlib.InvalidFileException, ValueError, TypeError) as error:
        raise MacOSDefaultsError(
            "Unable to parse macOS defaults domain %s: %s" % (domain, error),
            command=command,
            rc=rc,
            stdout=stdout,
            stderr=stderr,
        ) from error
    if not isinstance(preferences, dict):
        raise MacOSDefaultsError("Exported macOS defaults domain %s is not a dictionary" % domain)

    return preferences


def plist_fragment(value):
    payload = plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=True).decode("utf-8")
    plist_start = payload.index("<plist version=\"1.0\">") + len("<plist version=\"1.0\">")
    plist_end = payload.rindex("</plist>")
    return payload[plist_start:plist_end].strip()


def mutation_command(defaults_path, change):
    preference = change["preference"]
    command = [defaults_path] + host_args(preference["host"])
    if preference["state"] == "absent":
        return command + ["delete", preference["domain"], preference["key"]]
    return command + [
        "write",
        preference["domain"],
        preference["key"],
        plist_fragment(change["desired"]),
    ]


class MacOSDefaultsManager:
    def __init__(self, module, defaults_path, preferences):
        self.module = module
        self.defaults_path = defaults_path
        self.preferences = normalize_preferences(preferences)
        self.changed = False
        self.changed_preferences = []
        self.applied_preferences = []
        self.domain_states = {}
        self.changes = []

    def read_domains(self):
        for preference in self.preferences:
            domain_id = (preference["host"], preference["domain"])
            if domain_id not in self.domain_states:
                self.domain_states[domain_id] = export_domain(
                    self.module,
                    self.defaults_path,
                    preference["host"],
                    preference["domain"],
                )

    def plan(self):
        self.read_domains()
        for preference in self.preferences:
            domain_id = (preference["host"], preference["domain"])
            current = self.domain_states[domain_id].get(preference["key"], MISSING)
            desired = MISSING

            if preference["state"] == "absent":
                needs_change = current is not MISSING
            elif preference["dict_mode"] == "merge":
                if current is MISSING:
                    desired = copy.deepcopy(preference["value"])
                elif not isinstance(current, dict):
                    raise MacOSDefaultsError(
                        "Cannot merge dictionary into non-dictionary preference %s"
                        % preference_label(preference)
                    )
                else:
                    desired = copy.deepcopy(current)
                    desired.update(copy.deepcopy(preference["value"]))
                needs_change = current is MISSING or not values_equal(current, desired)
            else:
                desired = preference["value"]
                needs_change = current is MISSING or not values_equal(current, desired)

            if needs_change:
                label = preference_label(preference)
                self.changed_preferences.append(label)
                self.changes.append({"preference": preference, "desired": desired})

    def result(self, message):
        return {
            "changed": bool(self.changed_preferences),
            "msg": message,
            "managed_preferences": [
                preference_label(preference) for preference in self.preferences
            ],
            "changed_preferences": self.changed_preferences,
            "applied_preferences": self.applied_preferences,
            "relogin_required": bool(self.changed_preferences),
        }

    def verify(self):
        verified_domains = {}
        failed_preferences = []

        for change in self.changes:
            preference = change["preference"]
            domain_id = (preference["host"], preference["domain"])
            if domain_id not in verified_domains:
                verified_domains[domain_id] = export_domain(
                    self.module,
                    self.defaults_path,
                    preference["host"],
                    preference["domain"],
                )
            current = verified_domains[domain_id].get(preference["key"], MISSING)
            if preference["state"] == "absent":
                retained = current is MISSING
            else:
                retained = current is not MISSING and values_equal(current, change["desired"])
            if not retained:
                failed_preferences.append(preference_label(preference))

        if failed_preferences:
            raise MacOSDefaultsError(
                "macOS did not retain preferences: %s" % ", ".join(failed_preferences)
            )

    def run(self):
        self.plan()
        if not self.changes:
            return self.result("Managed macOS preferences already match")
        if self.module.check_mode:
            return self.result("macOS preferences would be updated")

        for change in self.changes:
            command = mutation_command(self.defaults_path, change)
            rc, stdout, stderr = self.module.run_command(command)
            if rc != 0:
                raise MacOSDefaultsError(
                    "Unable to manage macOS preference %s"
                    % preference_label(change["preference"]),
                    command=command,
                    rc=rc,
                    stdout=stdout,
                    stderr=stderr,
                )
            self.changed = True
            self.applied_preferences.append(preference_label(change["preference"]))

        self.verify()
        return self.result("Updated %d macOS preferences" % len(self.changes))


def main():
    module = AnsibleModule(
        argument_spec={
            "preferences": {
                "type": "list",
                "elements": "dict",
                "required": True,
                "options": {
                    "domain": {"type": "str", "required": True},
                    "key": {"type": "str", "required": True},
                    "value": {"type": "raw"},
                    "state": {
                        "type": "str",
                        "choices": ["present", "absent"],
                        "default": "present",
                    },
                    "host": {
                        "type": "str",
                        "choices": ["anyHost", "currentHost"],
                        "default": "anyHost",
                    },
                    "dict_mode": {
                        "type": "str",
                        "choices": ["replace", "merge"],
                        "default": "replace",
                    },
                },
            },
        },
        supports_check_mode=True,
    )
    defaults_path = module.get_bin_path("defaults", required=True, opt_dirs=["/usr/bin"])

    try:
        manager = MacOSDefaultsManager(module, defaults_path, module.params["preferences"])
        module.exit_json(**manager.run())
    except MacOSDefaultsError as error:
        result = {
            "changed": manager.changed if "manager" in locals() else False,
            "msg": error.message,
            "managed_preferences": (
                [preference_label(preference) for preference in manager.preferences]
                if "manager" in locals()
                else []
            ),
            "changed_preferences": (
                manager.changed_preferences if "manager" in locals() else []
            ),
            "applied_preferences": (
                manager.applied_preferences if "manager" in locals() else []
            ),
            "relogin_required": manager.changed if "manager" in locals() else False,
        }
        if error.command is not None:
            result.update(
                failed_command=error.command,
                rc=error.rc,
                stdout=error.stdout,
                stderr=error.stderr,
            )
        module.fail_json(**result)


if __name__ == "__main__":
    main()
