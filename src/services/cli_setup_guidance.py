"""User-initiated setup guidance for CLI recovery.

This module stores commands only for display/copy.  It never launches an
installer, login flow, shell, or browser by itself.  Authentication and
installation change external state, so the GUI must wait for an explicit user
action and keep credentials outside the app.
"""
from __future__ import annotations

from dataclasses import dataclass


CODEX_CLI_DOCS_URL = "https://learn.chatgpt.com/docs/codex/cli"
CODEX_AUTH_DOCS_URL = "https://learn.chatgpt.com/docs/auth"
CODEX_CONFIG_DOCS_URL = "https://learn.chatgpt.com/docs/config-file/config-basic"
CODEX_INSTALL_COMMAND = "curl -fsSL https://chatgpt.com/codex/install.sh | sh"
CODEX_LOGIN_COMMAND = "codex login"
CLAUDE_CLI_DOCS_URL = "https://code.claude.com/docs/en/installation"
CLAUDE_AUTH_DOCS_URL = "https://code.claude.com/docs/en/authentication"
CLAUDE_INSTALL_COMMAND = "curl -fsSL https://claude.ai/install.sh | bash"
CLAUDE_LOGIN_COMMAND = "claude auth login"
ANTIGRAVITY_CLI_DOCS_URL = "https://antigravity.google/docs/cli/install"
ANTIGRAVITY_AUTH_DOCS_URL = ANTIGRAVITY_CLI_DOCS_URL
ANTIGRAVITY_INSTALL_COMMAND = "curl -fsSL https://antigravity.google/cli/install.sh | bash"
ANTIGRAVITY_LOGIN_COMMAND = "agy"


@dataclass(frozen=True)
class SetupGuidance:
    title: str
    command: str
    command_button_label: str
    official_url: str
    url_button_label: str
    note: str


def for_status(tool_name: str, status: str) -> SetupGuidance | None:
    """Return reviewed setup guidance for a tool/status pair.

    Every command here was reviewed against the vendor's official macOS/Linux
    documentation. The app displays or copies these strings; it never executes
    them and never chooses an account/provider on the user's behalf.
    """
    if tool_name == "claude":
        if status == "command_missing":
            return SetupGuidance(
                title="Claude Code のインストール",
                command=CLAUDE_INSTALL_COMMAND,
                command_button_label="Claude導入コマンドをコピー",
                official_url=CLAUDE_CLI_DOCS_URL,
                url_button_label="Claude公式手順を開く",
                note=(
                    "Anthropic公式のmacOS/Linux用ネイティブインストーラーです。"
                    "アプリは自動実行しません。"
                ),
            )
        if status == "auth_required":
            return SetupGuidance(
                title="Claude Code のログイン",
                command=CLAUDE_LOGIN_COMMAND,
                command_button_label="claude auth login をコピー",
                official_url=CLAUDE_AUTH_DOCS_URL,
                url_button_label="Claude公式認証手順を開く",
                note=(
                    "CLIに表示される選択肢から、ご自身の契約に合う認証方式を選んでください。"
                    "アプリは認証方式を固定せず、認証情報を扱いません。"
                ),
            )
        if status == "api_key_blocked":
            return SetupGuidance(
                title="Claude Code の設定確認",
                command="",
                command_button_label="",
                official_url=CLAUDE_AUTH_DOCS_URL,
                url_button_label="Claude公式認証手順を開く",
                note=(
                    "CLIがAPI keyを要求しています。既存の認証・provider設定を公式手順で"
                    "確認してください。アプリは設定や認証情報を変更しません。"
                ),
            )
        return None

    if tool_name == "Antigravity(agy)":
        if status in {"command_missing", "unsupported_client"}:
            return SetupGuidance(
                title="Antigravity CLI のインストール",
                command=ANTIGRAVITY_INSTALL_COMMAND,
                command_button_label="Antigravity導入コマンドをコピー",
                official_url=ANTIGRAVITY_CLI_DOCS_URL,
                url_button_label="Antigravity公式手順を開く",
                note=(
                    "Google公式のmacOS/Linux用インストーラーです。通常は"
                    "~/.local/bin/agyへ配置されます。アプリは自動実行しません。"
                ),
            )
        if status == "auth_required":
            return SetupGuidance(
                title="Antigravity CLI のログイン",
                command=ANTIGRAVITY_LOGIN_COMMAND,
                command_button_label="agy 起動コマンドをコピー",
                official_url=ANTIGRAVITY_AUTH_DOCS_URL,
                url_button_label="Antigravity公式認証手順を開く",
                note=(
                    "agyを起動し、CLIに表示される選択肢からご自身の契約に合う認証方式を"
                    "選んでください。API key/provider設定がある場合もアプリは変更しません。"
                ),
            )
        if status == "api_key_blocked":
            return SetupGuidance(
                title="Antigravity CLI の設定確認",
                command="",
                command_button_label="",
                official_url=ANTIGRAVITY_AUTH_DOCS_URL,
                url_button_label="Antigravity公式認証手順を開く",
                note=(
                    "CLIがAPI keyを要求しています。既存のmodel provider・認証設定を公式手順で"
                    "確認してください。アプリは設定や認証情報を変更しません。"
                ),
            )
        return None

    if tool_name != "codex":
        return None
    if status == "command_missing":
        return SetupGuidance(
            title="Codex CLI のインストール",
            command=CODEX_INSTALL_COMMAND,
            command_button_label="Codex導入コマンドをコピー",
            official_url=CODEX_CLI_DOCS_URL,
            url_button_label="Codex公式手順を開く",
            note=(
                "OpenAI公式のmacOS/Linux用コマンドです。アプリは自動実行しません。"
                "コピー後に内容を確認し、ターミナルで実行してください。"
            ),
        )
    if status == "auth_required":
        return SetupGuidance(
            title="Codex CLI のログイン",
            command=CODEX_LOGIN_COMMAND,
            command_button_label="codex login をコピー",
            official_url=CODEX_AUTH_DOCS_URL,
            url_button_label="Codex公式認証手順を開く",
            note=(
                "ブラウザでChatGPTログインを完了してください。アプリは認証方式を選ばず、"
                "認証情報を読み取り・保存しません。"
            ),
        )
    if status == "api_key_blocked":
        return SetupGuidance(
            title="Codex CLI の設定確認",
            command="",
            command_button_label="",
            official_url=CODEX_CONFIG_DOCS_URL,
            url_button_label="Codex公式設定手順を開く",
            note=(
                "CLIがAPI keyを要求しています。Codexは設定ファイルでmodel providerを選択できるため、"
                "既存設定を公式手順で確認してください。アプリは設定や認証情報を変更しません。"
            ),
        )
    return None
