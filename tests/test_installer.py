"""Offline installer checks; the fake CLI only reports its version."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]


def find_bash():
    if os.name != "nt":
        return shutil.which("bash")
    # Prefer Git Bash; the Windows WSL launcher needs Linux interpreter paths.
    candidates = [Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"]
    git = shutil.which("git")
    if git:
        candidates.append(Path(git).resolve().parents[1] / "bin/bash.exe")
    return next((str(path) for path in candidates if path.is_file()), None)


BASH = find_bash()


@unittest.skipUnless(BASH, "Bash is required for shell installer checks")
class InstallerChecks(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="generic-installer-check-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.target = self.root / "project with spaces"
        self.fake = self.root / "fake_opencode.py"
        self.fake.write_text(
            'import os, sys\nassert sys.argv[1:] == ["--version"]\n'
            'print(os.environ.get("INSTALLER_TEST_VERSION", "1.2.99"))\n',
            encoding="utf-8",
        )
        self.prefix = json.dumps([sys.executable, str(self.fake)])

    def invoke(self, *extra):
        env = dict(
            os.environ,
            INSTALLER_CHECK_SOURCE=(PROJECT / "install.sh").as_posix(),
            INSTALLER_CHECK_PYTHON=sys.executable,
            INSTALLER_CHECK_JSON=self.prefix,
            INSTALLER_CHECK_TARGET=self.target.as_posix(),
        )
        # Enter through Bash so Windows/MSYS command-line parsing cannot
        # consume JSON backslashes before the installer receives them.
        return subprocess.run(
            [BASH, "-c", 'exec "$BASH" "$INSTALLER_CHECK_SOURCE" --python "$INSTALLER_CHECK_PYTHON" '
             '--opencode-command-json "$INSTALLER_CHECK_JSON" --target "$INSTALLER_CHECK_TARGET" "$@"',
             "installer-check", *extra],
            env=env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
        )

    def test_install_and_repeat_preserve_existing_project_data(self):
        self.target.mkdir()
        queue = self.target / "experiment-queue.csv"
        queue.write_text("existing private queue\n", encoding="utf-8")
        ignore = self.target / ".gitignore"
        ignore.write_text("# existing rules\nprivate/\n", encoding="utf-8")
        completed = self.invoke()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(queue.read_text(), "existing private queue\n")
        self.assertIn("private/", ignore.read_text())
        self.assertIn(".research/", ignore.read_text())
        self.assertEqual((self.target / "scripts/experiment_runner.py").read_bytes(), (PROJECT / "scripts/experiment_runner.py").read_bytes())
        for module in ("task_runtime.py", "resource_monitor.py"):
            self.assertEqual((self.target / "scripts" / module).read_bytes(), (PROJECT / "scripts" / module).read_bytes())
        before = {path.relative_to(self.target): (path.read_bytes(), path.stat().st_mtime_ns) for path in self.target.rglob("*") if path.is_file()}
        completed = self.invoke()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        after = {path.relative_to(self.target): (path.read_bytes(), path.stat().st_mtime_ns) for path in self.target.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_blank_queue_is_created(self):
        completed = self.invoke()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual((self.target / "experiment-queue.csv").read_bytes(), (PROJECT / "templates/experiment-queue.template.csv").read_bytes())
        self.assertNotIn(b"APPROVED", (self.target / "experiment-queue.csv").read_bytes())

    def test_check_does_not_create_target(self):
        completed = self.invoke("--check")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(self.target.exists())

    def test_conflict_aborts_before_copying(self):
        conflict = self.target / ".opencode/agents/research-agent.md"
        conflict.parent.mkdir(parents=True)
        conflict.write_text("existing custom agent\n", encoding="utf-8")
        completed = self.invoke()
        self.assertEqual(completed.returncode, 2)
        self.assertIn("Conflicting destination", completed.stderr)
        self.assertEqual(conflict.read_text(), "existing custom agent\n")
        self.assertEqual([path for path in self.target.rglob("*") if path.is_file()], [conflict])

    def test_unsupported_opencode_prevents_install(self):
        for version in ("1.1.99", "2.0.0"):
            with self.subTest(version=version):
                env = dict(os.environ, INSTALLER_TEST_VERSION=version)
                with mock.patch.dict(os.environ, env):
                    completed = self.invoke()
                self.assertEqual(completed.returncode, 2)
                self.assertFalse(self.target.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX symlink behavior")
    def test_symlink_escape_prevents_install(self):
        self.target.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (self.target / "templates").symlink_to(outside, target_is_directory=True)
        completed = self.invoke()
        self.assertEqual(completed.returncode, 2)
        self.assertFalse((self.target / ".opencode").exists())
        self.assertFalse(list(outside.iterdir()))


if __name__ == "__main__":
    unittest.main()
