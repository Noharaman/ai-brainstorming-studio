"""OS-level confinement for the processes this app launches.

The boundary lives behind an interface with one rule: **no backend, no run.**
There is no "sandbox failed, proceed anyway" path, because the only reason to
sandbox is that the command may be hostile, and that reason does not weaken
when the sandbox is missing.

Today the only backend that ships is `UnavailableSandbox`. That is a finding,
not an oversight — see `docs/safety-model.md` for the measurements. A caller
asks `active_sandbox()` for a backend, and gets one that refuses; callers must
treat refusal as "do not run the command", never as "run it unconfined".

A backend must also *prove* itself before being trusted. `self_check()` runs a
marker-based canary and a backend that cannot demonstrate the boundary is
treated as unavailable: a sandbox nobody verified is a claim, not a boundary.

Scope of that canary today: **basic file-write confinement only** — writes
inside the root, to a sibling, to HOME, to /tmp, and under a read-only policy.
It does NOT yet check the network, $TMPDIR, symlinks pointing outward,
inheritance by children and grandchildren, environment leakage, or
cancel/timeout cleanup. Those belong to the re-enable conditions in
`config.RUN_TESTS_AUTOMATICALLY` and must be added alongside the first real
backend; until then this file must not be read as covering them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

#: Environment variables a confined child may inherit. Everything else is
#: dropped: a test suite that echoes its environment is a normal way for a
#: credential to reach a log, and the child has no need for the parent's
#: tokens.
DEFAULT_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TMPDIR",
    "USER",
    "SHELL",
    "PWD",
)

_CANARY_TIMEOUT_SECONDS = 30


class SandboxUnavailable(Exception):
    """Raised when a command was asked to run confined and could not be."""


@dataclass(frozen=True)
class SandboxPolicy:
    """The boundary one command runs inside."""

    #: Directories the command may write to. Empty means read-only.
    writable_roots: tuple[Path, ...] = ()
    #: Off by default. A test suite that needs the network is a decision the
    #: user makes explicitly, not a default the app grants.
    allow_network: bool = False
    #: Names the child inherits; everything else is dropped.
    env_allowlist: tuple[str, ...] = DEFAULT_ENV_ALLOWLIST
    #: Replaces HOME (and TMPDIR) with a scratch directory for this run.
    #:
    #: An allowlist keeps the parent's *variables* out, but HOME still points
    #: at the real home directory, and a sandbox that only restricts writes
    #: leaves ~/.aws/credentials, ~/.ssh and every CLI's token file readable.
    #: A test the AI just edited can read one and print it, and the value ends
    #: up in the captured log. Pointing HOME somewhere empty removes the
    #: target rather than trusting the process not to look.
    private_home: Path | None = None

    @property
    def is_read_only(self) -> bool:
        return not self.writable_roots

    def child_env(
        self,
        parent_env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> dict[str, str]:
        source = os.environ if parent_env is None else parent_env
        env = {name: source[name] for name in self.env_allowlist if name in source}
        if self.private_home is not None:
            home = str(self.private_home)
            env["HOME"] = home
            env["TMPDIR"] = home
        if cwd is not None:
            # Derived from the real working directory rather than inherited,
            # so PWD cannot describe somewhere the child is not.
            env["PWD"] = str(cwd)
        return env


@dataclass
class CanaryResult:
    """What a backend actually demonstrated."""

    passed: bool = False
    checks: dict[str, bool] = field(default_factory=dict)
    detail: str = ""

    def failures(self) -> list[str]:
        return [name for name, ok in self.checks.items() if not ok]


class ProcessSandbox:
    """A way to run a command confined. Subclasses implement `wrap()`."""

    name = "abstract"

    def is_available(self) -> bool:
        raise NotImplementedError

    def wrap(self, command: list[str], policy: SandboxPolicy, cwd: Path) -> list[str]:
        """The argv that runs `command` inside `policy`."""
        raise NotImplementedError

    def unavailable_reason(self) -> str:
        return ""

    def self_check(self, root: Path | None = None) -> CanaryResult:
        """Prove the boundary with markers, before anything trusts it.

        Deliberately not a mock: it launches real commands and looks at what
        actually happened on disk. A backend that passes here has been shown
        to confine; one that merely reports success has not.
        """
        if not self.is_available():
            return CanaryResult(
                passed=False, detail=self.unavailable_reason() or "backend unavailable"
            )

        with tempfile.TemporaryDirectory(prefix="ai-brainstorm-canary-") as tmp:
            base = Path(tmp)
            inside = base / "root"
            outside = base / "sibling"
            inside.mkdir()
            outside.mkdir()
            policy = SandboxPolicy(writable_roots=(inside,))

            checks = {
                "writes_inside_the_root_succeed": self._marker(
                    inside / f"in-{uuid.uuid4().hex}", policy, inside, expect=True
                ),
                "writes_to_a_sibling_fail": self._marker(
                    outside / f"out-{uuid.uuid4().hex}", policy, inside, expect=False
                ),
                "writes_to_home_fail": self._marker(
                    Path.home() / f".ai-brainstorm-canary-{uuid.uuid4().hex}",
                    policy,
                    inside,
                    expect=False,
                ),
                "writes_to_tmp_fail": self._marker(
                    Path("/tmp") / f"ai-brainstorm-canary-{uuid.uuid4().hex}",
                    policy,
                    inside,
                    expect=False,
                ),
                "read_only_policy_blocks_writes": self._marker(
                    inside / f"ro-{uuid.uuid4().hex}",
                    SandboxPolicy(),
                    inside,
                    expect=False,
                ),
            }
            return CanaryResult(passed=all(checks.values()), checks=checks)

    def _marker(
        self, target: Path, policy: SandboxPolicy, cwd: Path, expect: bool
    ) -> bool:
        """Whether creating `target` had the expected outcome.

        Judged by the file existing, not by the exit status: a backend that
        reports failure while the write lands is the failure mode that
        matters.
        """
        try:
            argv = self.wrap(["/usr/bin/touch", str(target)], policy, cwd)
        except SandboxUnavailable:
            return False
        try:
            subprocess.run(
                argv,
                cwd=str(cwd),
                capture_output=True,
                timeout=_CANARY_TIMEOUT_SECONDS,
                stdin=subprocess.DEVNULL,
                env=policy.child_env(),
            )
        except (OSError, subprocess.SubprocessError):
            return expect is False
        created = target.exists()
        if created:
            try:
                target.unlink()
            except OSError:
                pass
        return created is expect


class UnavailableSandbox(ProcessSandbox):
    """The backend that ships. It refuses, and that is the point.

    Two candidates were measured on 2026-08-31 and neither can currently
    express "confine this command, and let it write only here":

    - `codex sandbox --permission-profile <NAME>` requires a profile resolved
      from the user's own Codex configuration. Deriving this app's security
      boundary from settings it does not control, and must not modify, is not
      a boundary.
    - `codex sandbox --sandbox-state-json` takes an undocumented internal
      shape (`PermissionProfileDe`, `sandboxCwd` as a file:// URI). An empty
      profile parses and does confine — writes inside the root, to a sibling,
      and to HOME were all denied — but every field name tried for granting
      write access was rejected, so the useful half could not be expressed.
      Reverse-engineering an internal schema of another tool and depending on
      it for confinement would break silently on their next release.
    - `sandbox-exec` / SBPL is deprecated and unsupported for third parties.

    So there is no backend, and callers must not run the commands that needed
    one. See docs/safety-model.md.
    """

    name = "unavailable"

    def is_available(self) -> bool:
        return False

    def unavailable_reason(self) -> str:
        return (
            "OSサンドボックスが未実装のため、隔離が必要なコマンドは実行しません。"
            "詳細は docs/safety-model.md を参照してください。"
        )

    def wrap(self, command: list[str], policy: SandboxPolicy, cwd: Path) -> list[str]:
        raise SandboxUnavailable(self.unavailable_reason())


def codex_sandbox_available() -> bool:
    """Whether the codex wrapper is even installed.

    Reported for diagnostics only. Presence is not capability: see
    `UnavailableSandbox` for why this is not wired up as a backend.
    """
    return bool(shutil.which("codex"))


_ACTIVE: ProcessSandbox = UnavailableSandbox()

#: Verified backends, by identity. The canary launches real processes, so
#: running it per command would be far too slow; running it once per backend
#: and remembering the answer keeps the check on the execution path without
#: paying for it repeatedly.
_VERIFIED: dict[int, bool] = {}


def active_sandbox() -> ProcessSandbox:
    """The configured backend, unverified. Diagnostics only.

    Callers that are about to *run* something must use
    `active_verified_sandbox()` instead: this one has not been shown to
    confine anything.
    """
    return _ACTIVE


def active_verified_sandbox() -> ProcessSandbox:
    """The backend to actually run commands through.

    Raises `SandboxUnavailable` unless the backend has passed its canary. This
    is the entry point precisely so that verification cannot be skipped by a
    caller that only remembers to call `wrap()`: a pass-through backend would
    otherwise sail past a canary that nothing on the execution path invokes.
    """
    sandbox = _ACTIVE
    if not sandbox.is_available():
        raise SandboxUnavailable(
            sandbox.unavailable_reason() or "no sandbox backend is available"
        )

    key = id(sandbox)
    verified = _VERIFIED.get(key)
    if verified is None:
        result = sandbox.self_check()
        verified = result.passed
        _VERIFIED[key] = verified
        if not verified:
            raise SandboxUnavailable(
                "サンドボックスの自己検証に失敗したため実行しません: "
                + (", ".join(result.failures()) or result.detail)
            )
    elif not verified:
        raise SandboxUnavailable(
            "サンドボックスの自己検証に失敗しているため実行しません。"
        )
    return sandbox


def reset_verification_cache() -> None:
    """Forget canary results. For tests that swap backends."""
    _VERIFIED.clear()
