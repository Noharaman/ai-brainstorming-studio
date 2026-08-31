from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from src import config
from src.services import secret_redactor


def _redact_tree(value):
    """Redact every string in a nested dict/list structure."""
    if isinstance(value, str):
        return secret_redactor.redact(value)
    if isinstance(value, dict):
        return {key: _redact_tree(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_tree(item) for item in value]
    return value


class ChatRoomManager:
    def __init__(self, project_root: Path):
        self.project_root = project_root.resolve()
        self.brainstorm_dir = config.app_dir(self.project_root)
        self.path = self.brainstorm_dir / "chat_rooms.json"
        self.load_error = ""

    def list_rooms(self) -> list[dict]:
        data = self._read()
        return sorted(data["rooms"], key=lambda room: room.get("updated_at", ""), reverse=True)

    def active_room_id(self) -> str:
        return str(self._read().get("active_room_id") or "")

    def set_active_room(self, room_id: str) -> None:
        data = self._read()
        if self._find_room(data, room_id):
            data["active_room_id"] = room_id
            self._write(data)

    def create_room(self, title: str = "新しいチャット") -> str:
        data = self._read()
        now = self._now()
        room_id = "room_" + uuid.uuid4().hex[:12]
        data["rooms"].append(
            {
                "id": room_id,
                "title": title.strip() or "新しいチャット",
                "created_at": now,
                "updated_at": now,
                "messages": [],
            }
        )
        data["active_room_id"] = room_id
        self._write(data)
        return room_id

    def rename_room(self, room_id: str, title: str) -> None:
        data = self._read()
        room = self._find_room(data, room_id)
        if not room:
            return
        room["title"] = title.strip() or room.get("title") or "無題のチャット"
        room["updated_at"] = self._now()
        data["active_room_id"] = room_id
        self._write(data)

    def delete_room(self, room_id: str) -> None:
        data = self._read()
        data["rooms"] = [room for room in data["rooms"] if room.get("id") != room_id]
        if data.get("active_room_id") == room_id:
            data["active_room_id"] = data["rooms"][0]["id"] if data["rooms"] else ""
        self._write(data)

    def append_turn(
        self,
        room_id: str,
        user_content: str,
        assistant_content: str,
        session_id: str,
        agent_outputs: dict[str, str] | None = None,
        implementation: dict | None = None,
    ) -> str:
        data = self._read()
        room = self._find_room(data, room_id)
        if not room:
            room_id = self.create_room(self._title_from_user_text(user_content))
            data = self._read()
            room = self._find_room(data, room_id)
        if not room:
            raise ValueError("Failed to create chat room.")

        now = self._now()
        room["messages"].append(
            {
                "role": "user",
                "content": user_content,
                "session_id": session_id,
                "created_at": now,
            }
        )
        asst_msg = {
            "role": "assistant",
            "content": assistant_content,
            "session_id": session_id,
            "created_at": now,
        }
        if agent_outputs:
            asst_msg["agent_outputs"] = agent_outputs
        if implementation:
            asst_msg["implementation"] = implementation
        room["messages"].append(asst_msg)
        if room.get("title") in {"新しいチャット", "無題のチャット"}:
            room["title"] = self._title_from_user_text(user_content)
        room["updated_at"] = now
        data["active_room_id"] = room_id
        self._write(data)
        return room_id

    def render_room(self, room_id: str) -> str:
        room = self.get_room(room_id)
        if not room:
            return "チャット履歴はまだありません。"
        lines = [f"# {room.get('title') or '無題のチャット'}", ""]
        for message in room.get("messages", []):
            role = "あなた" if message.get("role") == "user" else "秘書兼議長AI"
            created_at = message.get("created_at", "")
            session_id = message.get("session_id", "")
            suffix = f" / Session: {session_id}" if session_id else ""
            lines.extend(
                [
                    f"## {role} ({created_at}{suffix})",
                    "",
                    str(message.get("content") or "").strip(),
                    "",
                ]
            )
        return "\n".join(lines).strip()

    def get_room(self, room_id: str) -> dict | None:
        return self._find_room(self._read(), room_id)

    def _read(self) -> dict:
        # Read-only: selecting/browsing a project must never touch disk, so a
        # corrupt file is only reported here, and quarantined lazily in _write()
        # the next time the user actually performs a write action.
        self.load_error = ""
        if not self.path.exists():
            return {"schema_version": 1, "active_room_id": "", "rooms": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = {}
            self.load_error = (
                "chat_rooms.json is unreadable; it will be backed up automatically "
                "the next time you write to this project's chat history."
            )
        if not isinstance(data, dict):
            data = {}
        rooms = data.get("rooms")
        if not isinstance(rooms, list):
            rooms = []
        return {
            "schema_version": 1,
            "active_room_id": str(data.get("active_room_id") or ""),
            "rooms": rooms,
        }

    def _quarantine_if_unreadable(self) -> None:
        """Back up an existing-but-unparseable file right before overwriting it.

        Only called from _write(), never from the read path, so opening or
        browsing a project stays read-only even when its chat_rooms.json is corrupt.
        """
        if not self.path.exists():
            return
        try:
            json.loads(self.path.read_text(encoding="utf-8"))
            return  # valid JSON already; nothing to protect
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup = self.path.with_name(f"chat_rooms.corrupt-{stamp}.json")
        try:
            self.path.replace(backup)
        except OSError:
            return

    def _write(self, data: dict) -> None:
        # Every string in the payload is redacted here, recursively, rather
        # than field by field at each caller. Room *titles* are how this was
        # getting out: a title is derived from the first line of the request,
        # so a request beginning "API_TOKEN=... を設定して" wrote the token
        # into chat_rooms.json even while the message bodies were clean.
        # Redacting the whole document means a field added later cannot
        # reintroduce the leak by being forgotten.
        data = _redact_tree(data)
        self.brainstorm_dir.mkdir(parents=True, exist_ok=True)
        self._quarantine_if_unreadable()
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        tmp_path = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, self.path)

    def _find_room(self, data: dict, room_id: str) -> dict | None:
        for room in data.get("rooms", []):
            if room.get("id") == room_id:
                return room
        return None

    def _title_from_user_text(self, text: str) -> str:
        title = " ".join(text.strip().split())
        if not title:
            return "無題のチャット"
        return title[:32]

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()
