"""Git state around an approved implementation.

The app never rewrites the user's history: no commit, no reset, no clean, no
stash. It records where the project was before the AI edited it, shows the
diff afterwards, and tells the user the exact command to undo it. Choosing to
run that command is theirs.

That restraint is the point. An automatic rollback would have to decide what
counts as "the AI's changes" in a tree the user may also have been editing,
and getting that wrong destroys work that was never ours to touch.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

#: Long enough for a large refactor, short enough that a hung git can't wedge
#: the run. These calls are local and normally finish in milliseconds.
_GIT_TIMEOUT_SECONDS = 20

#: A diff larger than this is summarised instead of shown in full: the GUI text
#: box and the chair's context window both cope badly with megabyte diffs.
MAX_DIFF_CHARS = 200_000

#: Untracked files above this size are recorded by size only. Hashing an
#: arbitrarily large binary at approval time would stall the gate; the
#: trade-off is reported rather than hidden (see `unhashed_paths`).
MAX_HASH_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class GitCheckpoint:
    """Where the project stood when the user was asked to approve."""

    is_repo: bool = False
    commit: str = ""
    branch: str = ""
    #: Paths already modified before the AI ran. Shown at the approval gate:
    #: the user needs to know their own uncommitted work is in the blast
    #: radius before they say yes.
    dirty_paths: tuple[str, ...] = ()
    #: Files git is not tracking, recorded by name at approval time.
    #:
    #: These are the ones git cannot get back. `git diff` never mentions an
    #: untracked file, and a deleted one is absent from the "currently
    #: untracked" listing too, so without this snapshot the app would report
    #: "no changes" after an agent deleted the user's scratch notes. Ignored
    #: files (.env and friends) are recorded separately for the same reason
    #: and are the more dangerous case.
    untracked_paths: tuple[str, ...] = ()
    ignored_paths: tuple[str, ...] = ()
    #: path -> content digest at approval time. Deletion is not the only way
    #: to lose an untracked file: overwriting one leaves it untracked and
    #: present, so it appears in neither the "new" nor the "deleted" set and
    #: the run reported "no changes" while the original content was gone.
    #: Comparing digests is the only way to see it.
    untracked_digests: dict[str, str] = field(default_factory=dict)
    #: Files too large to digest. Listed so "not modified" is never claimed
    #: for something that was never checked.
    unhashed_paths: tuple[str, ...] = ()
    error: str = ""

    @property
    def is_dirty(self) -> bool:
        return bool(self.dirty_paths)

    @property
    def can_diff(self) -> bool:
        return self.is_repo and bool(self.commit)

    def approval_warnings(self) -> list[str]:
        """What the user must be told before granting write access."""
        warnings: list[str] = []
        if not self.is_repo:
            warnings.append(
                "このフォルダはGit管理下にありません。AIの変更を元に戻す確実な手段がないため、"
                "実装を許可する前にバックアップを取ることを強く推奨します。"
            )
            return warnings
        if self.is_dirty:
            shown = ", ".join(self.dirty_paths[:10])
            more = "" if len(self.dirty_paths) <= 10 else f" ほか{len(self.dirty_paths) - 10}件"
            warnings.append(
                f"未コミットの変更が{len(self.dirty_paths)}件あります（{shown}{more}）。"
                "AIの変更と混ざると切り分けが難しくなるため、先にコミットまたは退避することを推奨します。"
            )
        unrecoverable = len(self.untracked_paths) + len(self.ignored_paths)
        if unrecoverable:
            detail = []
            if self.untracked_paths:
                detail.append(f"未追跡{len(self.untracked_paths)}件")
            if self.ignored_paths:
                detail.append(f"Git除外{len(self.ignored_paths)}件（.env等を含む可能性）")
            warnings.append(
                f"Gitが管理していないファイルが{unrecoverable}件あります（{' / '.join(detail)}）。"
                "これらはAIが削除・上書きしてもGitから復元できません。"
                "重要なものがある場合は、実装を許可する前に退避してください。"
            )
        return warnings

    def revert_hint(self) -> str:
        """The command the user can run to undo. Never run by the app."""
        if not self.can_diff:
            return "（Git管理下でないため、自動の取り消し手順は提示できません）"
        if self.is_dirty:
            return (
                f"git diff {self.commit} -- <path> で個別に確認し、"
                f"git checkout {self.commit} -- <path> で必要なファイルだけ戻せます。"
                "（承認時点で未コミットの変更があったため、一括の戻しは推奨しません）"
            )
        return (
            f"git reset --hard {self.commit} で承認直前の状態に戻せます。"
            "（このアプリは自動では実行しません）"
        )


@dataclass
class ImplementationDiff:
    """What actually changed while the grant was active."""

    changed_files: tuple[str, ...] = ()
    diff_text: str = ""
    truncated: bool = False
    error: str = ""
    stat: str = ""
    #: Files that existed at approval and are now gone, which git cannot
    #: restore. Reported separately because this is data loss, not a change.
    lost_paths: tuple[str, ...] = ()
    #: Untracked/ignored files whose content changed while the grant was
    #: active. Their previous content is gone and git never had a copy.
    overwritten_paths: tuple[str, ...] = ()
    #: Files that could not be digested, so nothing is claimed about them.
    unverified_paths: tuple[str, ...] = ()
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.changed_files and not self.diff_text

    @property
    def has_unrecoverable_loss(self) -> bool:
        """Deletion or overwrite of something git cannot restore."""
        return bool(self.lost_paths) or bool(self.overwritten_paths)


def _git(project_root: Path, *args: str) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return 127, "", "git executable not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"git {' '.join(args)} timed out"
    except OSError as exc:  # pragma: no cover - defensive
        return 1, "", str(exc)
    return completed.returncode, completed.stdout, completed.stderr


def capture(project_root: Path) -> GitCheckpoint:
    """Record the pre-implementation state. Never mutates the repository."""
    code, out, err = _git(project_root, "rev-parse", "--is-inside-work-tree")
    if code != 0 or out.strip() != "true":
        return GitCheckpoint(is_repo=False, error=err.strip())

    code, out, err = _git(project_root, "rev-parse", "HEAD")
    if code != 0:
        # A repository with no commits yet: real, but nothing to diff against.
        return GitCheckpoint(is_repo=True, error=err.strip() or "no commit on HEAD")
    commit = out.strip()

    _, branch_out, _ = _git(project_root, "rev-parse", "--abbrev-ref", "HEAD")
    _, status_out, _ = _git(project_root, "status", "--porcelain")
    dirty = tuple(
        line[3:].strip() for line in status_out.splitlines() if len(line) > 3
    )
    untracked = _list_files(project_root, "--others", "--exclude-standard")
    ignored = _list_files(project_root, "--others", "--ignored", "--exclude-standard")
    digests, unhashed = _digest_all(project_root, untracked + ignored)
    return GitCheckpoint(
        is_repo=True,
        commit=commit,
        branch=branch_out.strip(),
        dirty_paths=dirty,
        untracked_paths=untracked,
        ignored_paths=ignored,
        untracked_digests=digests,
        unhashed_paths=unhashed,
    )


def _digest_all(
    project_root: Path, paths: tuple[str, ...]
) -> tuple[dict[str, str], tuple[str, ...]]:
    digests: dict[str, str] = {}
    unhashed: list[str] = []
    for path in paths:
        digest = _digest(project_root / path)
        if digest is None:
            unhashed.append(path)
        else:
            digests[path] = digest
    return digests, tuple(unhashed)


def _digest(path: Path) -> str | None:
    """Content digest, or None if it could not be read or is too large."""
    try:
        if path.is_symlink():
            # Digest the target, not the file it points at: repointing a
            # symlink is a change the content hash would miss entirely.
            return "symlink:" + hashlib.sha256(
                str(path.readlink()).encode("utf-8", "replace")
            ).hexdigest()
        if not path.is_file():
            return None
        if path.stat().st_size > MAX_HASH_BYTES:
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _list_files(project_root: Path, *args: str) -> tuple[str, ...]:
    code, out, _ = _git(project_root, "ls-files", *args)
    if code != 0:
        return ()
    return tuple(line.strip() for line in out.splitlines() if line.strip())


def diff_since(project_root: Path, checkpoint: GitCheckpoint) -> ImplementationDiff:
    """Everything that changed since `checkpoint`, tracked or not."""
    if not checkpoint.can_diff:
        return ImplementationDiff(
            error="Git管理下でないため、差分を自動取得できません。"
        )

    code, name_out, err = _git(project_root, "diff", "--name-only", checkpoint.commit)
    if code != 0:
        return ImplementationDiff(error=err.strip() or "git diff failed")
    changed = [line.strip() for line in name_out.splitlines() if line.strip()]

    # Untracked files never appear in `git diff`, and a new module is the most
    # common shape of an AI implementation. Without this the diff would claim
    # nothing happened.
    untracked_now = _list_files(project_root, "--others", "--exclude-standard")
    ignored_now = _list_files(
        project_root, "--others", "--ignored", "--exclude-standard"
    )
    known_before = set(checkpoint.untracked_paths)
    new_files = [path for path in untracked_now if path not in known_before]

    # Deletions of files git never tracked are invisible to every command
    # above: absent from `git diff`, and absent from the current untracked
    # listing precisely because they are gone. Comparing against the snapshot
    # taken at approval is the only way to notice, and it is the case that
    # loses real user data — scratch notes, a local .env.
    lost = [
        path
        for path in checkpoint.untracked_paths
        if path not in set(untracked_now) and not (project_root / path).exists()
    ]
    lost_ignored = [
        path
        for path in checkpoint.ignored_paths
        if path not in set(ignored_now) and not (project_root / path).exists()
    ]

    _, stat_out, _ = _git(project_root, "diff", "--stat", checkpoint.commit)
    _, diff_out, _ = _git(project_root, "diff", checkpoint.commit)

    truncated = len(diff_out) > MAX_DIFF_CHARS
    if truncated:
        diff_out = diff_out[:MAX_DIFF_CHARS] + "\n... (差分が大きいため省略) ...\n"

    # Overwriting an untracked file leaves it untracked and present, so it is
    # in neither `new_files` nor `lost`. Only the digest shows it, and the
    # original content is unrecoverable.
    overwritten = []
    still_here = set(untracked_now) | set(ignored_now)
    for path, before in checkpoint.untracked_digests.items():
        if path not in still_here:
            continue
        after = _digest(project_root / path)
        if after is not None and after != before:
            overwritten.append(path)

    entries = list(changed)
    entries += [f"{path} (新規)" for path in new_files]
    entries += [f"{path} (削除・Git復元不可)" for path in lost]
    entries += [f"{path} (削除・Git除外ファイル・復元不可)" for path in lost_ignored]
    entries += [f"{path} (上書き・Git復元不可)" for path in overwritten]

    return ImplementationDiff(
        changed_files=tuple(entries),
        diff_text=diff_out,
        truncated=truncated,
        stat=stat_out.strip(),
        lost_paths=tuple(lost + lost_ignored),
        overwritten_paths=tuple(overwritten),
        unverified_paths=checkpoint.unhashed_paths,
    )
