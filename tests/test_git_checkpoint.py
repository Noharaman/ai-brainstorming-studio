import subprocess
import tempfile
import unittest
from pathlib import Path

from src.services import git_checkpoint


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(root),
        check=True,
        capture_output=True,
        text=True,
    )


class NonRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_plain_folder_is_reported_as_unmanaged(self) -> None:
        checkpoint = git_checkpoint.capture(self.root)
        self.assertFalse(checkpoint.is_repo)
        self.assertFalse(checkpoint.can_diff)

    def test_the_user_is_warned_before_approving_an_unmanaged_folder(self) -> None:
        warnings = git_checkpoint.capture(self.root).approval_warnings()
        self.assertTrue(any("Git管理下にありません" in w for w in warnings))

    def test_no_revert_is_promised_when_none_exists(self) -> None:
        hint = git_checkpoint.capture(self.root).revert_hint()
        self.assertNotIn("git reset", hint)


class RepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _git(self.root, "init", "-q")
        _git(self.root, "config", "user.email", "t@example.com")
        _git(self.root, "config", "user.name", "T")
        (self.root / "a.txt").write_text("one\n", encoding="utf-8")
        _git(self.root, "add", "a.txt")
        _git(self.root, "commit", "-qm", "init")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_clean_repository_records_its_commit(self) -> None:
        checkpoint = git_checkpoint.capture(self.root)
        self.assertTrue(checkpoint.is_repo)
        self.assertTrue(checkpoint.can_diff)
        self.assertFalse(checkpoint.is_dirty)
        self.assertEqual(checkpoint.approval_warnings(), [])

    def test_uncommitted_work_is_surfaced_before_approval(self) -> None:
        (self.root / "a.txt").write_text("two\n", encoding="utf-8")
        checkpoint = git_checkpoint.capture(self.root)
        self.assertTrue(checkpoint.is_dirty)
        self.assertTrue(any("未コミット" in w for w in checkpoint.approval_warnings()))

    def test_a_dirty_checkout_is_not_offered_a_blanket_reset(self) -> None:
        """Suggesting `reset --hard` would destroy the user's own edits."""
        (self.root / "a.txt").write_text("two\n", encoding="utf-8")
        hint = git_checkpoint.capture(self.root).revert_hint()
        self.assertNotIn("reset --hard", hint)

    def test_a_clean_checkout_gets_the_exact_revert_command(self) -> None:
        checkpoint = git_checkpoint.capture(self.root)
        self.assertIn(f"git reset --hard {checkpoint.commit}", checkpoint.revert_hint())

    def test_modifications_appear_in_the_diff(self) -> None:
        checkpoint = git_checkpoint.capture(self.root)
        (self.root / "a.txt").write_text("changed\n", encoding="utf-8")
        diff = git_checkpoint.diff_since(self.root, checkpoint)
        self.assertIn("a.txt", diff.changed_files)
        self.assertIn("changed", diff.diff_text)

    def test_new_files_are_reported_even_though_git_diff_ignores_them(self) -> None:
        checkpoint = git_checkpoint.capture(self.root)
        (self.root / "new_module.py").write_text("x = 1\n", encoding="utf-8")
        diff = git_checkpoint.diff_since(self.root, checkpoint)
        self.assertTrue(
            any("new_module.py" in path for path in diff.changed_files),
            f"untracked file missing from {diff.changed_files}",
        )

    def test_an_untouched_repository_reports_an_empty_diff(self) -> None:
        checkpoint = git_checkpoint.capture(self.root)
        self.assertTrue(git_checkpoint.diff_since(self.root, checkpoint).is_empty)

    def test_capture_does_not_modify_the_repository(self) -> None:
        before = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(self.root),
            capture_output=True,
            text=True,
        ).stdout
        git_checkpoint.capture(self.root)
        git_checkpoint.diff_since(self.root, git_checkpoint.capture(self.root))
        after = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(self.root),
            capture_output=True,
            text=True,
        ).stdout
        self.assertEqual(before, after)



class UntrackedFileLossTest(unittest.TestCase):
    """Files git never tracked are the ones git cannot bring back.

    `git diff` never mentions them, and a deleted one is absent from the
    current untracked listing too — so before this the app reported "no
    changes" after an agent deleted the user's notes or their local .env.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _git(self.root, "init", "-q")
        _git(self.root, "config", "user.email", "t@example.com")
        _git(self.root, "config", "user.name", "T")
        (self.root / "a.txt").write_text("one\n", encoding="utf-8")
        (self.root / ".gitignore").write_text(".env\n", encoding="utf-8")
        _git(self.root, "add", "a.txt", ".gitignore")
        _git(self.root, "commit", "-qm", "init")
        (self.root / "user-notes.txt").write_text("my notes\n", encoding="utf-8")
        (self.root / ".env").write_text("TOKEN=1\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_untracked_and_ignored_files_are_recorded_at_approval(self) -> None:
        checkpoint = git_checkpoint.capture(self.root)
        self.assertIn("user-notes.txt", checkpoint.untracked_paths)
        self.assertIn(".env", checkpoint.ignored_paths)

    def test_the_user_is_warned_that_these_cannot_be_restored(self) -> None:
        warnings = git_checkpoint.capture(self.root).approval_warnings()
        self.assertTrue(
            any("復元できません" in w for w in warnings),
            f"no unrecoverable-file warning in {warnings}",
        )

    def test_deleting_an_untracked_file_is_detected(self) -> None:
        checkpoint = git_checkpoint.capture(self.root)
        (self.root / "user-notes.txt").unlink()

        diff = git_checkpoint.diff_since(self.root, checkpoint)

        self.assertIn("user-notes.txt", diff.lost_paths)
        self.assertTrue(diff.has_unrecoverable_loss)
        self.assertFalse(diff.is_empty, "this must never report 'no changes'")

    def test_deleting_an_ignored_file_is_detected(self) -> None:
        checkpoint = git_checkpoint.capture(self.root)
        (self.root / ".env").unlink()

        diff = git_checkpoint.diff_since(self.root, checkpoint)

        self.assertIn(".env", diff.lost_paths)

    def test_a_new_untracked_file_is_not_reported_as_lost(self) -> None:
        checkpoint = git_checkpoint.capture(self.root)
        (self.root / "new_module.py").write_text("x = 1\n", encoding="utf-8")

        diff = git_checkpoint.diff_since(self.root, checkpoint)

        self.assertEqual(diff.lost_paths, ())
        self.assertTrue(any("new_module.py" in p for p in diff.changed_files))

    def test_overwriting_an_untracked_file_is_detected(self) -> None:
        """Deletion is not the only way to lose one of these.

        An overwritten untracked file is still untracked and still present, so
        it is in neither the "new" nor the "deleted" set. Only the content
        digest shows it, and the original content is gone for good.
        """
        (self.root / "user-notes.txt").write_text("ORIGINAL\n", encoding="utf-8")
        checkpoint = git_checkpoint.capture(self.root)
        (self.root / "user-notes.txt").write_text("REPLACED\n", encoding="utf-8")

        diff = git_checkpoint.diff_since(self.root, checkpoint)

        self.assertIn("user-notes.txt", diff.overwritten_paths)
        self.assertTrue(diff.has_unrecoverable_loss)
        self.assertFalse(diff.is_empty, "this must never report 'no changes'")

    def test_overwriting_an_ignored_file_is_detected(self) -> None:
        checkpoint = git_checkpoint.capture(self.root)
        (self.root / ".env").write_text("TOKEN=2\n", encoding="utf-8")
        diff = git_checkpoint.diff_since(self.root, checkpoint)
        self.assertIn(".env", diff.overwritten_paths)

    def test_rewriting_identical_content_is_not_an_overwrite(self) -> None:
        """Digest comparison, not mtime: a no-op write must stay quiet."""
        original = (self.root / "user-notes.txt").read_text(encoding="utf-8")
        checkpoint = git_checkpoint.capture(self.root)
        (self.root / "user-notes.txt").write_text(original, encoding="utf-8")
        diff = git_checkpoint.diff_since(self.root, checkpoint)
        self.assertEqual(diff.overwritten_paths, ())

    def test_repointing_a_symlink_is_detected(self) -> None:
        (self.root / "target-a.txt").write_text("a\n", encoding="utf-8")
        (self.root / "target-b.txt").write_text("b\n", encoding="utf-8")
        link = self.root / "link.txt"
        link.symlink_to("target-a.txt")
        checkpoint = git_checkpoint.capture(self.root)
        link.unlink()
        link.symlink_to("target-b.txt")

        diff = git_checkpoint.diff_since(self.root, checkpoint)

        self.assertIn("link.txt", diff.overwritten_paths)

    def test_an_untouched_tree_reports_no_loss(self) -> None:
        checkpoint = git_checkpoint.capture(self.root)
        diff = git_checkpoint.diff_since(self.root, checkpoint)
        self.assertEqual(diff.lost_paths, ())
        self.assertEqual(diff.overwritten_paths, ())
        self.assertFalse(diff.has_unrecoverable_loss)


if __name__ == "__main__":
    unittest.main()
