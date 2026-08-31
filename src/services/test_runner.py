"""Run the project's own tests after an approved implementation.

Two rules shape this module.

The command is *detected*, never invented. Running a guessed command in
someone's repository can do anything — a Makefile target called `test` may
deploy. If no known runner is recognised, the phase reports that and the user
decides; it does not fall back to "try something plausible".

The command is a fixed argv, never a shell string, so nothing from the AI's
output or the user's request is interpolated into it.

That is a narrower guarantee than it looks, and the difference matters. The
argv is fixed; what it *executes* is not. `npm test` runs whatever
`package.json` currently says, and the implementer that just edited this
repository could have changed it — likewise a conftest.py, a build.rs, or a
Makefile. Running the suite therefore executes code the AI may have written.
It is not made safe by the argv being a list, and the approval gate does not
make it safe either: approving an implementation is not approving "run
arbitrary code as me".

Because of that, the suite runs only inside a verified OS sandbox. `ProcessSandbox`
supplies the boundary, and when no backend is available the run is refused —
never downgraded to an unconfined one. Refusing is the safe outcome; running
unconfined is the thing the sandbox exists to prevent.

What this module does own is termination. Tests run in their own process
group, cancellation is honoured while they run, and a timeout or cancel kills
the whole group rather than orphaning grandchildren.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from src.services.process_sandbox import (
    SandboxPolicy,
    SandboxUnavailable,
    active_verified_sandbox,
)

#: Cap on captured output. Test suites can print megabytes; the tail is what
#: matters for a failure and the chair's context window is finite.
MAX_OUTPUT_CHARS = 20_000

DEFAULT_TEST_TIMEOUT_SECONDS = 600

#: How often the run is checked for cancellation while tests execute.
_POLL_SECONDS = 0.2

#: Grace between TERM and KILL for the test process group.
_TERMINATE_GRACE_SECONDS = 3


@dataclass(frozen=True)
class TestOutcome:
    ran: bool
    command: tuple[str, ...] = ()
    passed: bool | None = None
    output: str = ""
    reason: str = ""

    @property
    def command_text(self) -> str:
        return " ".join(self.command)


def detect_command(project_root: Path) -> tuple[str, ...] | None:
    """The project's test command, or None if we do not recognise one.

    Ordered by how unambiguous the signal is. Each candidate is returned only
    when a file that specifically implies it exists.
    """
    if (project_root / "tests").is_dir() and _looks_like_python(project_root):
        # This project's own documented command (see AGENTS.md / docs).
        return ("python3", "-m", "unittest", "discover", "-s", "tests")
    if (project_root / "pytest.ini").is_file():
        return ("python3", "-m", "pytest", "-q")
    if (project_root / "package.json").is_file() and _npm_has_test_script(project_root):
        return ("npm", "test", "--silent")
    if (project_root / "Cargo.toml").is_file():
        return ("cargo", "test")
    if (project_root / "go.mod").is_file():
        return ("go", "test", "./...")
    return None


def _looks_like_python(project_root: Path) -> bool:
    markers = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")
    if any((project_root / marker).is_file() for marker in markers):
        return True
    return any(project_root.glob("*.py"))


def _npm_has_test_script(project_root: Path) -> bool:
    """Only true for a real test script.

    `npm init` writes a default `test` script that exits 1 with "no test
    specified"; treating that as a test suite would report every run as a
    failure.
    """
    import json

    try:
        data = json.loads((project_root / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    script = (data.get("scripts") or {}).get("test", "")
    return bool(script) and "no test specified" not in script


class ProjectTestRunner:
    def run(
        self,
        project_root: Path,
        cancel_event: object | None = None,
        timeout_seconds: int = DEFAULT_TEST_TIMEOUT_SECONDS,
    ) -> TestOutcome:
        if _is_cancelled(cancel_event):
            return TestOutcome(ran=False, reason="キャンセルされたため、テストは実行していません。")

        command = detect_command(project_root)
        if command is None:
            return TestOutcome(
                ran=False,
                reason=(
                    "既知のテスト構成が見つからなかったため、テストは実行していません。"
                    "推測したコマンドを実行することはしません。"
                ),
            )

        # A scratch HOME for this run: the suite gets no path to the user's
        # credential files even if the backend only restricts writes.
        with tempfile.TemporaryDirectory(prefix="ai-brainstorm-testhome-") as home:
            return self._run_confined(project_root, command, Path(home),
                                      cancel_event, timeout_seconds)

    def _run_confined(
        self,
        project_root: Path,
        command: tuple[str, ...],
        private_home: Path,
        cancel_event: object | None,
        timeout_seconds: int,
    ) -> TestOutcome:
        policy = SandboxPolicy(
            writable_roots=(project_root.resolve(), private_home),
            private_home=private_home,
        )
        try:
            # Verified, not merely configured: a backend that never passed its
            # canary must not reach the execution path.
            sandbox = active_verified_sandbox()
            argv = sandbox.wrap(list(command), policy, project_root)
        except SandboxUnavailable as exc:
            return TestOutcome(ran=False, command=command, reason=str(exc))

        try:
            process = subprocess.Popen(
                argv,
                cwd=str(project_root),
                env=policy.child_env(cwd=project_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                stdin=subprocess.DEVNULL,
                # Its own process group, so a test runner that spawns workers
                # (pytest -n, npm scripts, cargo) can be terminated as a unit
                # instead of leaving grandchildren behind.
                start_new_session=True,
            )
        except FileNotFoundError:
            return TestOutcome(
                ran=False,
                command=command,
                reason=f"`{command[0]}` が見つからないため、テストを実行できませんでした。",
            )
        except OSError as exc:  # pragma: no cover - defensive
            return TestOutcome(ran=False, command=command, reason=str(exc))

        stdout, stderr, reason = self._wait(
            process, timeout_seconds, cancel_event, command
        )
        if reason:
            return TestOutcome(ran=False, command=command, reason=reason)

        output = (stdout or "") + (stderr or "")
        if len(output) > MAX_OUTPUT_CHARS:
            output = "... (先頭を省略) ...\n" + output[-MAX_OUTPUT_CHARS:]
        return TestOutcome(
            ran=True,
            command=command,
            passed=process.returncode == 0,
            output=output.strip(),
        )

    def _wait(
        self,
        process: subprocess.Popen,
        timeout_seconds: int,
        cancel_event: object | None,
        command: tuple[str, ...],
    ) -> tuple[str, str, str]:
        """Collect output, honouring cancellation while the suite runs.

        Returns `(stdout, stderr, reason)`; a non-empty `reason` means the run
        did not produce a verdict. `communicate()` is called repeatedly with a
        short timeout, which is supported and keeps partial output.
        """
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                stdout, stderr = process.communicate(timeout=_POLL_SECONDS)
                return stdout, stderr, ""
            except subprocess.TimeoutExpired:
                pass

            if _is_cancelled(cancel_event):
                self._terminate(process)
                return "", "", "キャンセルされたため、テストを中断しました。"
            if time.monotonic() >= deadline:
                self._terminate(process)
                return (
                    "",
                    "",
                    f"テストが{timeout_seconds}秒でタイムアウトしました。",
                )

    def _terminate(self, process: subprocess.Popen) -> None:
        """Stop the whole process group: TERM, then KILL if it lingers.

        Signalling only the direct child would leave a test runner's workers
        alive, still holding the project's files.
        """
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(process.pid), sig)
            except (ProcessLookupError, PermissionError, OSError):
                # Already gone, or never had its own group.
                try:
                    process.kill()
                except Exception:
                    pass
                return
            try:
                process.communicate(timeout=_TERMINATE_GRACE_SECONDS)
                return
            except subprocess.TimeoutExpired:
                continue
            except Exception:
                return


def _is_cancelled(cancel_event: object | None) -> bool:
    is_set = getattr(cancel_event, "is_set", None)
    return bool(is_set and is_set())
