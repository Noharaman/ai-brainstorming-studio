"""The only thing that can unlock file-editing for an AI CLI.

Read-only is the absence of a grant, not a flag set to False. Every command
builder takes `grant: WriteGrant | None = None` and defaults to the read-only
argv, so a caller that forgets about grants — or a code path written before
this module existed — cannot accidentally hand an AI write access.

A grant is deliberately awkward to construct: it must name the run, the single
agent it applies to, and the directory it is scoped to, and it can only be
produced by `granted_after_approval()`, which requires an approval decision
that a human actually made. There is no module-level default instance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class WriteGrantError(Exception):
    """Raised when something tries to build a grant that was never approved."""


#: Passed by `granted_after_approval()` to prove it built the grant. A grant
#: constructed directly — bypassing the approval check — will not have it.
#: This does not make direct construction impossible in Python; it makes it
#: impossible to do *accidentally*, and makes the attempt visible at the point
#: of use rather than silently granting write access.
_APPROVAL_TOKEN = object()


@dataclass(frozen=True)
class WriteGrant:
    """Permission for exactly one agent, in one run, to edit one directory.

    Build these with `granted_after_approval()`. Constructing one directly
    leaves `_token` unset, and `is_authentic` then reports False; the command
    builders refuse such a grant and fall back to read-only.
    """

    run_id: str
    agent: str
    project_root: Path
    approved_at: str
    #: The git commit the project was on when the user approved. Recorded so
    #: the diff shown afterwards is measured against what they actually saw,
    #: and so a grant issued against a different checkout is detectable.
    baseline_commit: str = ""
    _token: object = None

    @property
    def is_authentic(self) -> bool:
        """Whether this grant came from the approval path."""
        return self._token is _APPROVAL_TOKEN

    def applies_to(self, agent: str, run_id: str) -> bool:
        """Whether this grant authorizes `agent` in `run_id`.

        Both must match. A grant for the implementer must not leak to the
        critic in the same run, and a grant from a previous run must not
        survive into the next one.
        """
        return (
            self.is_authentic and self.agent == agent and self.run_id == run_id
        )

    def describe(self) -> str:
        return (
            f"{self.agent} に {self.project_root} 配下の編集を許可 "
            f"(run={self.run_id}, 承認={self.approved_at}, "
            f"基準コミット={self.baseline_commit or '(なし)'})"
        )


def granted_after_approval(
    run_id: str,
    agent: str,
    project_root: Path,
    approved: bool,
    baseline_commit: str = "",
) -> WriteGrant:
    """Build the grant for an approved plan.

    `approved` is passed in rather than assumed so the caller cannot express
    "grant write access" without also stating that approval happened; passing
    False is a programming error and raises rather than silently returning
    None, because a caller that reaches here with False has already decided to
    write.
    """
    from src import config

    # Gate 2 of three. Even an approved plan produces no grant while writes
    # are withdrawn: approval is the user's part, but the app still has to be
    # able to confine what it authorises, and right now it cannot.
    if not config.IMPLEMENTATION_WRITES_ENABLED:
        raise WriteGrantError(
            "ファイル書き込みは無効化されています "
            "(config.IMPLEMENTATION_WRITES_ENABLED)。"
            "OSサンドボックスが未実装のため、AIによる編集は行いません。"
        )
    if not approved:
        raise WriteGrantError(
            "a write grant was requested for a plan the user did not approve"
        )
    if not run_id or not agent:
        raise WriteGrantError("a write grant must name both a run and an agent")
    return WriteGrant(
        run_id=run_id,
        agent=agent,
        project_root=project_root,
        approved_at=datetime.now(timezone.utc).isoformat(),
        baseline_commit=baseline_commit,
        _token=_APPROVAL_TOKEN,
    )


def grant_for(
    grant: WriteGrant | None, agent: str, run_id: str
) -> WriteGrant | None:
    """The grant that applies to `agent`, or None.

    Command builders call this instead of testing `grant is not None`, so the
    agent/run check cannot be skipped at an individual call site.
    """
    if grant is None:
        return None
    return grant if grant.applies_to(agent, run_id) else None
