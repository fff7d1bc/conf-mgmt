import importlib.util
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CALLBACK_PATH = REPOSITORY_ROOT / "callback_plugins" / "confmgmt_changed_summary.py"
SPEC = importlib.util.spec_from_file_location("confmgmt_changed_summary", CALLBACK_PATH)
CHANGED_SUMMARY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHANGED_SUMMARY)


class FakeDisplay:
    def __init__(self):
        self.banners = []
        self.messages = []

    def banner(self, message):
        self.banners.append(message)

    def display(self, message):
        self.messages.append(message)


class FakeHost:
    def __init__(self, name):
        self.name = name

    def get_name(self):
        return self.name


class FakeResult:
    def __init__(self, host, task_name, changed):
        self.host = FakeHost(host)
        self.task_name = task_name
        self.changed = changed

    def is_changed(self):
        return self.changed


class ChangedSummaryTests(unittest.TestCase):
    def callback(self):
        callback = CHANGED_SUMMARY.CallbackModule()
        callback._display = FakeDisplay()
        return callback

    def test_unchanged_results_do_not_produce_a_summary(self):
        callback = self.callback()

        callback.v2_runner_on_ok(FakeResult("host-a", "role : unchanged", False))
        callback.v2_playbook_on_stats(None)

        self.assertEqual(callback._display.banners, [])
        self.assertEqual(callback._display.messages, [])

    def test_summary_groups_changed_tasks_and_marks_failures(self):
        callback = self.callback()

        callback.v2_runner_on_ok(FakeResult("host-z", "role : changed", True))
        callback.v2_runner_on_ok(FakeResult("host-a", "role : first", True))
        callback.v2_runner_on_ok(FakeResult("host-a", "role : first", True))
        callback.v2_runner_on_failed(
            FakeResult("host-a", "role : partial failure", True),
            ignore_errors=True,
        )
        callback.v2_runner_on_failed(
            FakeResult("host-a", "role : partial failure", True),
            ignore_errors=False,
        )
        callback.v2_runner_on_failed(
            FakeResult("host-a", "role : unchanged failure", False),
            ignore_errors=False,
        )
        callback.v2_playbook_on_stats(None)

        self.assertEqual(callback._display.banners, ["CHANGED TASKS"])
        self.assertEqual(
            callback._display.messages,
            [
                "host-a:",
                "  - role : first",
                "  - role : partial failure (failed)",
                "host-z:",
                "  - role : changed",
            ],
        )

    def test_ansible_loads_callback_and_reports_tasks_and_handlers(self):
        playbook = textwrap.dedent(
            """
            - name: callback integration
              hosts: localhost
              gather_facts: false
              connection: local
              handlers:
                - name: changed handler
                  ansible.builtin.debug:
                    msg: handler
                  changed_when: true
              tasks:
                - name: unchanged task
                  ansible.builtin.debug:
                    msg: unchanged
                - name: changed task
                  ansible.builtin.debug:
                    msg: changed
                  changed_when: true
                  notify: changed handler
                - name: changed loop
                  ansible.builtin.debug:
                    msg: "{{ item }}"
                  loop: [changed, unchanged]
                  changed_when: item == 'changed'
            """
        ).strip()

        with tempfile.TemporaryDirectory() as temporary_directory:
            playbook_path = Path(temporary_directory) / "callback.yml"
            playbook_path.write_text(playbook, encoding="utf-8")
            environment = os.environ.copy()
            environment.update(
                ANSIBLE_CONFIG=str(REPOSITORY_ROOT / "ansible.cfg"),
                NO_COLOR="1",
            )
            result = subprocess.run(
                [
                    str(Path(sys.executable).with_name("ansible-playbook")),
                    "--inventory",
                    "localhost,",
                    str(playbook_path),
                ],
                cwd=REPOSITORY_ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertIn("PLAY RECAP", result.stdout)
        self.assertIn("CHANGED TASKS", result.stdout)
        summary = result.stdout.split("CHANGED TASKS", 1)[1]
        self.assertIn("  - changed task", summary)
        self.assertIn("  - changed loop", summary)
        self.assertIn("  - changed handler", summary)
        self.assertNotIn("unchanged task", summary)
        self.assertEqual(summary.count("  - changed loop"), 1)


if __name__ == "__main__":
    unittest.main()
