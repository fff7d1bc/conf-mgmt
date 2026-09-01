import unittest
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIRECTORY = REPOSITORY_ROOT / "roles" / "steamos" / "templates"


class SteamOSRoleTests(unittest.TestCase):
    def test_atomic_update_keep_paths_render_on_separate_lines(self):
        environment = Environment(
            loader=FileSystemLoader(TEMPLATE_DIRECTORY),
            undefined=StrictUndefined,
            trim_blocks=True,
        )
        template = environment.get_template("atomic-update.conf.j2")

        rendered = template.render(
            confmgmt={
                "steamos": {
                    "preserve": [
                        "/etc/ssh/sshd_config.d/99-auth.conf",
                        "/etc/systemd/logind.conf.d/xyz-preserve-user-processes.conf",
                    ]
                }
            }
        )

        self.assertEqual(
            rendered,
            "# Additional files managed by Ansible that must survive atomic updates.\n"
            "/etc/ssh/sshd_config.d/99-auth.conf\n"
            "/etc/systemd/logind.conf.d/xyz-preserve-user-processes.conf\n",
        )


if __name__ == "__main__":
    unittest.main()
