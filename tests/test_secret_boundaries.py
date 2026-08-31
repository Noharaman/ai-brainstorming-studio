"""Redaction must happen at the write boundary, not at each call site.

Test output, diffs and CLI stdout all come from the user's repository and all
get persisted. Relying on a dozen callers to remember `redact()` means one
forgotten call site leaks a credential into a long-lived file.
"""

import json
import tempfile
import unittest
from pathlib import Path

from src.services import secret_redactor
from src.services.chat_room_manager import ChatRoomManager
from src.services.workspace_manager import WorkspaceManager

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r"
PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc123def456\n-----END RSA PRIVATE KEY-----"


class PatternTest(unittest.TestCase):
    def test_credentials_test_output_actually_contains(self) -> None:
        for secret, text in (
            ("hunter2", "DATABASE_URL=postgres://user:hunter2@db:5432/app"),
            ("abc123def456", "API_TOKEN=abc123def456"),
            ("dBjftJeZ4CVPmB92K27uhbUJU1p1r", JWT),
            ("abcdefghijkl", "xoxb-123456789012-abcdefghijkl"),
            ("MIIabc123def456", PEM),
        ):
            with self.subTest(text=text[:30]):
                self.assertNotIn(secret, secret_redactor.redact(text))

    def test_the_variable_name_survives_so_logs_stay_readable(self) -> None:
        self.assertIn("API_TOKEN", secret_redactor.redact("API_TOKEN=abc123def456"))

    def test_ordinary_output_is_untouched(self) -> None:
        for text in (
            "5 tests passed in 3s",
            "PATH=/usr/bin:/bin",
            "FAILED tests/test_x.py::test_y",
            "version=1.2.3",
        ):
            with self.subTest(text=text):
                self.assertEqual(secret_redactor.redact(text), text)


class WorkspaceBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.workspace = WorkspaceManager(self.root)
        self.workspace.initialize()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_session_artifacts_are_redacted_without_the_caller_asking(self) -> None:
        self.workspace.write_session_artifact(
            "s1", "implementation_tests.md", "output\nAPI_TOKEN=abc123def456\n"
        )
        written = (
            self.root / ".ai-brainstorm" / "sessions" / "s1" / "implementation_tests.md"
        ).read_text(encoding="utf-8")
        self.assertNotIn("abc123def456", written)

    def test_prompts_are_redacted(self) -> None:
        """Repair prompts embed raw test output and are sent to an AI CLI."""
        self.workspace.write_prompt("codex", f"fix this:\n{JWT}\n", "s1")
        written = (
            self.root / ".ai-brainstorm" / "prompts" / "s1" / "codex.md"
        ).read_text(encoding="utf-8")
        self.assertNotIn("dBjftJeZ4CVPmB92K27uhbUJU1p1r", written)

    def test_history_is_redacted(self) -> None:
        self.workspace.append_history("s1", "結論:\nAPI_TOKEN=abc123def456\n")
        written = (self.root / ".ai-brainstorm" / "history.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("abc123def456", written)


class ChatRoomBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.manager = ChatRoomManager(self.root)
        self.room = self.manager.create_room("t")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _saved(self) -> str:
        return self.manager.path.read_text(encoding="utf-8")

    def test_the_assistant_answer_is_redacted(self) -> None:
        self.manager.append_turn(
            self.room, "req", f"結論:\n{JWT}\n", "s1"
        )
        self.assertNotIn("dBjftJeZ4CVPmB92K27uhbUJU1p1r", self._saved())

    def test_agent_outputs_are_redacted(self) -> None:
        self.manager.append_turn(
            self.room,
            "req",
            "answer",
            "s1",
            agent_outputs={"codex": "API_TOKEN=abc123def456"},
        )
        self.assertNotIn("abc123def456", self._saved())

    def test_the_user_message_is_redacted(self) -> None:
        self.manager.append_turn(
            self.room, "my key is API_TOKEN=abc123def456", "answer", "s1"
        )
        self.assertNotIn("abc123def456", self._saved())

    def test_a_room_title_derived_from_the_request_is_redacted(self) -> None:
        """Titles come from the first line of the request, so a request that
        starts with a credential wrote it into the file even while the message
        bodies were clean."""
        self.manager.create_room("API_TOKEN=abc123def456 を設定して")
        self.assertNotIn("abc123def456", self._saved())

    def test_renaming_a_room_is_redacted(self) -> None:
        self.manager.rename_room(self.room, "PASSWORD=zzz999yyy888")
        self.assertNotIn("zzz999yyy888", self._saved())

    def test_an_auto_created_room_title_is_redacted(self) -> None:
        """append_turn() with an unknown room id names the room after the
        request text."""
        self.manager.append_turn(
            "no-such-room", "SECRET_KEY=qqq111www222 を消して", "ans", "s1"
        )
        self.assertNotIn("qqq111www222", self._saved())

    def test_a_nested_implementation_payload_is_redacted(self) -> None:
        self.manager.append_turn(
            self.room,
            "req",
            "ans",
            "s1",
            implementation={"attempted": True, "diff_text": "API_TOKEN=nested123456"},
        )
        self.assertNotIn("nested123456", self._saved())

    def test_the_file_is_still_valid_json(self) -> None:
        self.manager.append_turn(self.room, "req", "answer", "s1")
        json.loads(self._saved())


if __name__ == "__main__":
    unittest.main()
