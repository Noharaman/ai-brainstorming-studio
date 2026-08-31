import json
import tempfile
import unittest
from pathlib import Path

from src.services.process_sandbox import CanaryResult, ProcessSandbox
from src.services.test_runner import ProjectTestRunner, detect_command


class DetectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_an_unrecognised_project_gets_no_command(self) -> None:
        """The rule that matters: never guess a command to run in someone's repo."""
        (self.root / "README.md").write_text("hi", encoding="utf-8")
        self.assertIsNone(detect_command(self.root))

    def test_a_python_project_with_tests_is_recognised(self) -> None:
        (self.root / "tests").mkdir()
        (self.root / "pyproject.toml").write_text("", encoding="utf-8")
        self.assertEqual(
            detect_command(self.root),
            ("python3", "-m", "unittest", "discover", "-s", "tests"),
        )

    def test_a_tests_directory_alone_is_not_enough(self) -> None:
        """A `tests/` folder in a non-Python project must not imply unittest."""
        (self.root / "tests").mkdir()
        self.assertIsNone(detect_command(self.root))

    def test_the_npm_placeholder_script_is_not_a_test_suite(self) -> None:
        (self.root / "package.json").write_text(
            json.dumps(
                {"scripts": {"test": 'echo "Error: no test specified" && exit 1'}}
            ),
            encoding="utf-8",
        )
        self.assertIsNone(detect_command(self.root))

    def test_a_real_npm_test_script_is_recognised(self) -> None:
        (self.root / "package.json").write_text(
            json.dumps({"scripts": {"test": "jest"}}), encoding="utf-8"
        )
        self.assertEqual(detect_command(self.root), ("npm", "test", "--silent"))

    def test_a_corrupt_package_json_is_not_a_test_suite(self) -> None:
        (self.root / "package.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(detect_command(self.root))


class RunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_an_unrecognised_project_reports_instead_of_running(self) -> None:
        outcome = ProjectTestRunner().run(self.root)
        self.assertFalse(outcome.ran)
        self.assertIsNone(outcome.passed)
        self.assertIn("推測", outcome.reason)

    def test_cancellation_skips_the_run(self) -> None:
        class Cancelled:
            def is_set(self):
                return True

        outcome = ProjectTestRunner().run(self.root, cancel_event=Cancelled())
        self.assertFalse(outcome.ran)
        self.assertEqual(outcome.command, ())



class _PassThroughSandbox(ProcessSandbox):
    """Runs the command as-is, and declares its canary passed.

    Used only to exercise the termination logic, which cannot be observed
    while the real backend refuses every command. It confines nothing and is
    not a stand-in for one that does — note that it has to *lie* in
    self_check() to get past active_verified_sandbox(), which is the point of
    that gate.
    """

    name = "test-passthrough"

    def is_available(self):
        return True

    def wrap(self, command, policy, cwd):
        return list(command)

    def self_check(self, root=None):
        return CanaryResult(passed=True, checks={"stubbed": True})


class TerminationTest(unittest.TestCase):
    """A running suite must be stoppable, including anything it spawned.

    Before this, tests ran under `subprocess.run()`: cancellation was only
    checked before launch, so a cancel during a 10-minute suite did nothing,
    and a timeout killed the direct child while its workers kept running.
    """

    def setUp(self) -> None:
        from src.services import process_sandbox

        original = process_sandbox._ACTIVE
        process_sandbox._ACTIVE = _PassThroughSandbox()
        process_sandbox.reset_verification_cache()
        self.addCleanup(process_sandbox.reset_verification_cache)
        self.addCleanup(setattr, process_sandbox, "_ACTIVE", original)

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "tests").mkdir()
        (self.root / "pyproject.toml").write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_test(self, body: str) -> None:
        (self.root / "tests" / "test_slow.py").write_text(body, encoding="utf-8")

    def test_cancellation_stops_a_running_suite(self) -> None:
        import threading
        import time

        self._write_test("import time\ntime.sleep(60)\n")
        cancel = threading.Event()
        threading.Timer(0.6, cancel.set).start()

        started = time.monotonic()
        outcome = ProjectTestRunner().run(self.root, cancel_event=cancel, timeout_seconds=60)
        elapsed = time.monotonic() - started

        self.assertFalse(outcome.ran)
        self.assertIn("キャンセル", outcome.reason)
        self.assertLess(elapsed, 15, "cancel must not wait for the suite to finish")

    def test_a_timeout_stops_the_suite(self) -> None:
        self._write_test("import time\ntime.sleep(60)\n")
        outcome = ProjectTestRunner().run(self.root, timeout_seconds=1)
        self.assertFalse(outcome.ran)
        self.assertIn("タイムアウト", outcome.reason)

    def test_spawned_grandchildren_do_not_survive(self) -> None:
        """A runner's workers must not keep holding the project's files."""
        import subprocess
        import threading
        import time
        import uuid

        marker = f"ai-brainstorm-test-{uuid.uuid4().hex}"
        self._write_test(
            "import subprocess, time\n"
            f"subprocess.Popen(['sh', '-c', 'exec -a {marker} sleep 90'])\n"
            "time.sleep(90)\n"
        )
        cancel = threading.Event()
        threading.Timer(1.0, cancel.set).start()
        ProjectTestRunner().run(self.root, cancel_event=cancel, timeout_seconds=60)

        time.sleep(0.5)
        found = subprocess.run(
            ["pgrep", "-f", marker], capture_output=True, text=True
        ).stdout.strip()
        if found:  # pragma: no cover - cleanup so a failure leaks nothing
            subprocess.run(["pkill", "-f", marker], capture_output=True)
        self.assertEqual(found, "", "the process group must be terminated as a unit")


if __name__ == "__main__":
    unittest.main()
