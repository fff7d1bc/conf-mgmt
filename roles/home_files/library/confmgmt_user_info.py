#!/usr/bin/python

from __future__ import annotations

DOCUMENTATION = r"""
---
module: confmgmt_user_info
short_description: Resolve POSIX user account details
description:
  - Resolves user accounts through the managed host's Python account database.
  - Uses Directory Services on macOS and NSS on Linux through C(pwd.getpwnam).
  - Returns account details without changing the managed host.
options:
  names:
    description: Usernames to resolve.
    type: list
    elements: str
    required: true
attributes:
  check_mode:
    support: full
  diff_mode:
    support: none
author:
  - conf-mgmt
"""

EXAMPLES = r"""
- name: Resolve users receiving home files
  confmgmt_user_info:
    names:
      - root
      - piotr
  register: home_files_users
"""

RETURN = r"""
users:
  description: Account details keyed by each requested username.
  returned: success
  type: dict
  contains:
    name:
      description: Canonical username returned by the account database.
      type: str
    uid:
      description: Numeric user identifier.
      type: int
    gid:
      description: Numeric primary group identifier.
      type: int
    home:
      description: Absolute home directory path.
      type: str
missing_users:
  description: Usernames not present in the target's account database.
  returned: failure
  type: list
  elements: str
invalid_names:
  description: Empty usernames or usernames containing a null byte.
  returned: failure
  type: list
  elements: str
invalid_home_users:
  description: Usernames whose account records do not contain absolute home paths.
  returned: failure
  type: list
  elements: str
"""

import os
import pwd

from ansible.module_utils.basic import AnsibleModule


class UserInfoError(Exception):
    def __init__(self, invalid_names=None, missing_users=None, invalid_home_users=None):
        self.invalid_names = invalid_names or []
        self.missing_users = missing_users or []
        self.invalid_home_users = invalid_home_users or []

        problems = []
        if self.invalid_names:
            problems.append("invalid usernames: %s" % ", ".join(self.invalid_names))
        if self.missing_users:
            problems.append("users not found: %s" % ", ".join(self.missing_users))
        if self.invalid_home_users:
            problems.append(
                "users with non-absolute home directories: %s"
                % ", ".join(self.invalid_home_users)
            )
        super().__init__("; ".join(problems))


def resolve_users(names, lookup=pwd.getpwnam):
    users = {}
    invalid_names = []
    missing_users = []
    invalid_home_users = []

    for requested_name in dict.fromkeys(names):
        if not requested_name or "\0" in requested_name:
            invalid_names.append(repr(requested_name))
            continue

        try:
            account = lookup(requested_name)
        except (KeyError, ValueError):
            missing_users.append(requested_name)
            continue

        if not isinstance(account.pw_dir, str) or not os.path.isabs(account.pw_dir):
            invalid_home_users.append(requested_name)
            continue

        users[requested_name] = {
            "name": account.pw_name,
            "uid": account.pw_uid,
            "gid": account.pw_gid,
            "home": account.pw_dir,
        }

    if invalid_names or missing_users or invalid_home_users:
        raise UserInfoError(
            invalid_names=invalid_names,
            missing_users=missing_users,
            invalid_home_users=invalid_home_users,
        )

    return users


def main():
    module = AnsibleModule(
        argument_spec={
            "names": {"type": "list", "elements": "str", "required": True},
        },
        supports_check_mode=True,
    )

    try:
        users = resolve_users(module.params["names"])
    except UserInfoError as error:
        module.fail_json(
            changed=False,
            msg=str(error),
            invalid_names=error.invalid_names,
            missing_users=error.missing_users,
            invalid_home_users=error.invalid_home_users,
        )

    module.exit_json(changed=False, users=users)


if __name__ == "__main__":
    main()
