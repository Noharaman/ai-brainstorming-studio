"""The sandbox boundary, and the rule that there is no way around it.

The single property worth protecting here: a missing or unproven backend must
stop the command, never let it through unconfined. Everything else in this
file exists to make that hard to undo by accident.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from src.services import test_runner
from src.services.process_sandbox import (
    DEFAULT_ENV_ALLOWLIST,
    CanaryResult,
    ProcessSandbox,
    SandboxPolicy,
    SandboxUnavailable,
    UnavailableSandbox,
    active_sandbox,
)


class PolicyTest(unittest.TestCase):
    def test_no_writable_roots_means_read_only(self) -> None:
        self.assertTrue(SandboxPolicy().is_read_only)
        self.assertFalse(SandboxPolicy(writable_roots=(Path("/x"),)).is_read_only)

    def test_network_is_off_by_default(self) -> None:
        """A suite needing the network is an explicit decision, not a default."""
        self.assertFalse(SandboxPolicy().allow_network)

    def test_the_child_environment_is_an_allowlist(self) -> None:
        parent = {
            "PATH": "/usr/bin",
            "HOME": "/Users/x",
            "ANTHROPIC_API_KEY": "sk-ant-secret",
            "AWS_SECRET_ACCESS_KEY": "shh",
            "DATABASE_URL": "postgres://u:p@h/db",
        }
        child = SandboxPolicy().child_env(parent)
        self.assertEqual(child, {"PATH": "/usr/bin", "HOME": "/Users/x"})

    def test_no_credential_shaped_name_is_on_the_allowlist(self) -> None:
        for name in DEFAULT_ENV_ALLOWLIST:
            for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"):
                self.assertNotIn(marker, name.upper(), name)


class UnavailableBackendTest(unittest.TestCase):
    """The backend that ships refuses, and refusal must be unambiguous."""

    def setUp(self) -> None:
        self.sandbox = UnavailableSandbox()

    def test_it_reports_itself_unavailable(self) -> None:
        self.assertFalse(self.sandbox.is_available())

    def test_wrapping_raises_rather_than_returning_the_bare_command(self) -> None:
        """Returning `command` unchanged would run it unconfined, which is
        exactly the outcome this class exists to prevent."""
        with self.assertRaises(SandboxUnavailable):
            self.sandbox.wrap(["/bin/echo", "hi"], SandboxPolicy(), Path("."))

    def test_the_self_check_does_not_pass(self) -> None:
        self.assertFalse(self.sandbox.self_check().passed)

    def test_the_shipped_backend_is_the_unavailable_one(self) -> None:
        self.assertIsInstance(active_sandbox(), UnavailableSandbox)


class _PretendSandbox(ProcessSandbox):
    """A backend that claims to confine but does not. The canary must catch it."""

    name = "pretend"

    def is_available(self) -> bool:
        return True

    def wrap(self, command, policy, cwd):
        return command


class _RefusingSandbox(ProcessSandbox):
    """Available, but every wrap fails."""

    name = "refusing"

    def is_available(self) -> bool:
        return True

    def wrap(self, command, policy, cwd):
        raise SandboxUnavailable("nope")


class CanaryTest(unittest.TestCase):
    """A backend has to demonstrate the boundary, not assert it."""

    def test_a_backend_that_does_not_confine_fails_the_canary(self) -> None:
        result = _PretendSandbox().self_check()
        self.assertFalse(result.passed)
        # It lets writes through everywhere, so the "must fail" checks fail.
        self.assertIn("writes_to_a_sibling_fail", result.failures())

    def test_a_backend_that_blocks_everything_also_fails(self) -> None:
        """Blocking the legitimate write is not confinement either."""
        result = _RefusingSandbox().self_check()
        self.assertFalse(result.passed)
        self.assertIn("writes_inside_the_root_succeed", result.failures())

    def test_the_canary_checks_the_boundaries_that_matter(self) -> None:
        checks = set(_PretendSandbox().self_check().checks)
        for required in (
            "writes_inside_the_root_succeed",
            "writes_to_a_sibling_fail",
            "writes_to_home_fail",
            "writes_to_tmp_fail",
            "read_only_policy_blocks_writes",
        ):
            self.assertIn(required, checks)

    def test_the_canary_leaves_nothing_behind(self) -> None:
        home_before = set(Path.home().glob(".ai-brainstorm-canary-*"))
        _PretendSandbox().self_check()
        leaked = set(Path.home().glob(".ai-brainstorm-canary-*")) - home_before
        for path in leaked:  # pragma: no cover - cleanup on failure
            path.unlink()
        self.assertEqual(leaked, set(), "canary markers must be cleaned up")

    def test_an_empty_result_is_not_a_pass(self) -> None:
        self.assertFalse(CanaryResult().passed)


class TestRunnerRefusesWithoutASandboxTest(unittest.TestCase):
    """Detection still works; execution does not happen unconfined."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "tests").mkdir()
        (self.root / "pyproject.toml").write_text("", encoding="utf-8")
        marker = self.root / "SUITE_RAN"
        (self.root / "tests" / "test_x.py").write_text(
            f"open({str(marker)!r}, 'w').close()\n", encoding="utf-8"
        )
        self.marker = marker

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_the_suite_is_not_executed(self) -> None:
        outcome = test_runner.ProjectTestRunner().run(self.root)
        self.assertFalse(outcome.ran)
        self.assertIsNone(outcome.passed)
        self.assertFalse(
            self.marker.exists(), "the suite must not run without a sandbox"
        )

    def test_the_reason_names_the_sandbox(self) -> None:
        outcome = test_runner.ProjectTestRunner().run(self.root)
        self.assertIn("サンドボックス", outcome.reason)

    def test_the_command_is_still_detected(self) -> None:
        """The user is told what to run themselves."""
        self.assertEqual(
            test_runner.detect_command(self.root),
            ("python3", "-m", "unittest", "discover", "-s", "tests"),
        )


class SandboxSourceGuardTest(unittest.TestCase):
    """No caller may quietly fall back to an unconfined run."""

    def test_the_test_runner_does_not_swallow_unavailability(self) -> None:
        source = Path(test_runner.__file__).read_text(encoding="utf-8")
        # It must return an outcome, not carry on to Popen with the raw argv.
        self.assertIn("except SandboxUnavailable", source)
        self.assertNotIn("except SandboxUnavailable:\n            pass", source)

    def test_popen_receives_the_wrapped_argv(self) -> None:
        source = Path(test_runner.__file__).read_text(encoding="utf-8")
        self.assertIn("subprocess.Popen(\n                argv,", source)
        self.assertNotIn("subprocess.Popen(\n                list(command),", source)


if __name__ == "__main__":
    unittest.main()
