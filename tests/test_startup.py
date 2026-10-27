"""Exercise real stdio startup with an unavailable optional legacy launcher."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class StartupTests(unittest.TestCase):
    def check_start(self, broken_launcher):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            flag = home / '.local/share/claude-helper-launcher/codex-legacy-enabled'
            flag.parent.mkdir(parents=True)
            flag.touch()
            if broken_launcher:
                entry = home / 'Developer/claude-helper-launcher/legacy_entry.py'
                entry.parent.mkdir(parents=True)
                entry.write_text('raise RuntimeError("synthetic launcher failure")\n')
            env = dict(os.environ, HOME=directory)
            result = subprocess.run([sys.executable, str(ROOT / 'server.py')],
                                    input='', text=True, capture_output=True,
                                    env=env, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('Traceback', result.stderr)

    def test_missing_launcher_keeps_normal_startup(self):
        self.check_start(False)

    def test_broken_launcher_keeps_normal_startup(self):
        self.check_start(True)


if __name__ == '__main__':
    unittest.main()
