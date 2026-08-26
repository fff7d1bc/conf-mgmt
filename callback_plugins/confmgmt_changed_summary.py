from __future__ import annotations

from ansible.plugins.callback import CallbackBase


DOCUMENTATION = r"""
name: confmgmt_changed_summary
type: aggregate
short_description: Summarize changed tasks at the end of a playbook run
description:
  - Records successful and failed task results that Ansible reports as changed.
  - Prints a controller-side summary grouped by inventory host after handlers and the normal recap.
requirements:
  - Enable in Ansible configuration.
"""


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "aggregate"
    CALLBACK_NAME = "confmgmt_changed_summary"
    CALLBACK_NEEDS_ENABLED = True

    def __init__(self):
        super().__init__()
        self._changed_tasks = {}

    def _record_changed(self, result, outcome=""):
        if not result.is_changed():
            return

        host_tasks = self._changed_tasks.setdefault(result.host.get_name(), {})
        task_name = result.task_name
        previous_outcome = host_tasks.get(task_name)
        outcome_priority = {None: -1, "": 0, "failed, ignored": 1, "failed": 2}

        if outcome_priority[outcome] > outcome_priority[previous_outcome]:
            host_tasks[task_name] = outcome

    def v2_runner_on_ok(self, result):
        self._record_changed(result)

    def v2_runner_on_failed(self, result, ignore_errors=False):
        outcome = "failed, ignored" if ignore_errors else "failed"
        self._record_changed(result, outcome=outcome)

    def v2_playbook_on_stats(self, stats):
        if not self._changed_tasks:
            return

        self._display.banner("CHANGED TASKS")
        for host_name in sorted(self._changed_tasks):
            self._display.display("%s:" % host_name)
            for task_name, outcome in self._changed_tasks[host_name].items():
                suffix = " (%s)" % outcome if outcome else ""
                self._display.display("  - %s%s" % (task_name, suffix))
