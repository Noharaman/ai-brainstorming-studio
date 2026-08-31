"""One place that turns a normalized CLI status into Japanese guidance.

The app never repairs a CLI's login or configuration itself, so when something
fails the only useful output is an accurate description plus the command the
user would run in Terminal. Guidance must not claim a diagnosis the output
does not support: "auth_required" says a login *appears* to be needed, not
that the account is broken.
"""
from __future__ import annotations


_RECOVERY_GUIDANCE_JA: dict[str, str] = {
    "installed": "実行ファイルは見つかりました。",
    "installed_unverified": (
        "CLIはインストール済みですが、ログインとモデル応答はまだ確認していません。"
        "本実行前のプリフライト、または明示的なAI接続テストで確認します。"
    ),
    "checking": "AI接続を確認しています。",
    "service_unavailable": "ローカルサービスへ接続できません。起動状態と接続先を確認してください。",
    "health_check_error": "ヘルスチェック自体が失敗しました。詳細ログを確認して再チェックしてください。",
    "command_missing": "CLIが見つかりません。インストールしてから、アプリを再起動するか再確認してください。",
    "auth_required": "ログインが必要なようです。ターミナルでそのCLIにログインしてから、もう一度実行してください。アプリはログイン操作を代行しません。",
    "rate_limited": "利用上限またはレート制限に達したようです。リセットを待つか、他のAIの回答だけで続行してください。",
    "permission_error": "CLIがローカルの権限エラーになりました。選択したプロジェクトをターミナルで開き、そのCLIが読めるか確認してください。",
    "config_error": "CLIのオプションまたはローカル設定が不正なようです。CLIを直接実行して設定を確認してください。アプリは設定を変更していません。",
    "timeout": "時間内に終わりませんでした。再実行するか、他のAIの回答だけで続行してください。",
    "cancelled": "実行はキャンセルされました。",
    "unsupported_client": "このCLI/クライアントのバージョンは現在のアカウントでは使用できないようです。CLIを更新してください。",
    "api_key_blocked": "CLIがAPI keyを要求しています。既存の認証・provider設定を公式手順で確認してください。アプリから認証・キー・provider設定の変更は行いません。",
    "no_clean_exit": (
        "CLIは応答を返しましたが、その後も終了しませんでした。終了コードを確認できないため成功と判定していません。"
        "ターミナルで同じCLIを単体実行し、確認待ちや認証待ちで止まっていないか確認してください。"
    ),
    "empty_response": (
        "CLIは正常終了しましたが、何も出力しませんでした。ターミナルで同じCLIを単体実行し、応答が返るか確認してください。"
    ),
    "no_expected_reply": (
        "CLIは正常終了しましたが、依頼した応答が返りませんでした（起動時の通知だけを出して終了した可能性があります）。"
        "ターミナルで同じCLIを単体実行し、ログイン済みで実際に回答が返る状態か確認してください。"
    ),
    "slot_disabled": "このAI枠はビルド側で無効化されているため実行されませんでした。",
    "failed": "CLIがエラー終了しました。詳細ログを確認してください。",
    "unknown": "原因を特定できませんでした。詳細ログを確認してください。",
}

_STATUS_LABELS_JA: dict[str, str] = {
    "ok": "成功",
    "installed": "インストール済み",
    "installed_unverified": "インストール済み・接続未確認",
    "checking": "接続確認中",
    "service_unavailable": "サービス停止または接続不可",
    "health_check_error": "ヘルスチェック失敗",
    "auth_required": "ログインまたは認証が必要",
    "api_key_blocked": "APIキー要求/認証が必要",
    "cancelled": "キャンセル済み",
    "config_error": "CLIオプションまたはローカル設定エラー",
    "command_missing": "CLI未検出",
    "failed": "エラー終了",
    "no_clean_exit": "応答後に終了せず",
    "empty_response": "出力なしで終了",
    "no_expected_reply": "依頼した応答が返らず終了",
    "permission_error": "ローカル権限または信頼設定エラー",
    "rate_limited": "利用上限またはレート制限",
    "skipped": "スキップ",
    "slot_disabled": "スロット無効",
    "timeout": "タイムアウト",
    "unsupported_client": "旧クライアントまたは非対応利用枠",
    "unknown": "不明なエラー",
}

# Statuses worth surfacing in the run log as they happen. "ok" and "skipped"
# are not here: they are already visible from the result itself.
NOTABLE_STATUSES = frozenset(_RECOVERY_GUIDANCE_JA) | {
    "error",
    "safety_blocked",
    "security_blocked",
    "auth_setup_required",
}


def label_for(status: str) -> str:
    """Returns a short Japanese label for a status."""
    return _STATUS_LABELS_JA.get(status, status)


def guidance_for(status: str) -> str:
    """Japanese recovery guidance for a normalized status."""
    return _RECOVERY_GUIDANCE_JA.get(status, _RECOVERY_GUIDANCE_JA["unknown"])


def next_step_for(agent_label: str, base_agent: str, status: str) -> str:
    """Returns the recommended next step/action for the user in Japanese."""
    if status == "auth_required":
        if base_agent == "gemini":
            return "Antigravity CLI を単体で `agy` として起動し、ログインを完了してください。"
        if base_agent == "claude":
            return "Claude CLI を単体で起動し、表示されるログイン手順を完了してください。"
        if base_agent == "codex":
            return "Codex CLI を単体で起動し、ログインまたは作業フォルダの信頼確認を完了してください。"
        return f"{agent_label} CLI を単体で起動し、ログインを完了してください。"
    if status in {"installed", "installed_unverified", "checking"}:
        return ""
    if status == "unsupported_client":
        return "旧Gemini CLIは現在の利用枠で非対応のため、Antigravity CLI (`agy`) を使ってください。"
    if status == "api_key_blocked":
        return (
            f"{agent_label} CLI がAPIキー設定または認証を要求しました。"
            "アプリから認証・キー設定の変更は行いません。CLI単体でログインまたは設定を確認してください。"
        )
    if status == "cancelled":
        return ""
    if status == "config_error":
        return f"{agent_label} CLI の起動オプションまたはローカル設定に問題があります。CLIを直接実行して設定を確認してください。"
    if status == "permission_error":
        return f"{agent_label} CLI をターミナル単体で実行し、作業フォルダ権限または信頼設定を確認してください。"
    if status == "timeout":
        return f"{agent_label} CLI がタイムアウトしました。単体起動で認証待ちや確認待ちになっていないか確認してください。"
    if status == "no_clean_exit":
        return (
            f"{agent_label} CLI は応答後も終了しませんでした。単体起動で入力待ちや確認待ちにならないか確認してください。"
        )
    if status == "empty_response":
        return f"{agent_label} CLI が何も出力せずに終了しました。単体起動で同じ依頼に応答するか確認してください。"
    if status == "no_expected_reply":
        return (
            f"{agent_label} CLI が依頼した応答を返さずに終了しました。"
            "単体起動でログイン状態を確認し、実際に回答が返るか確認してください。"
        )
    if status == "command_missing":
        if base_agent == "gemini":
            return "Antigravity CLI (`agy`) をインストールし、PATHから実行できる状態にしてください。"
        return f"{agent_label} CLI をインストールし、PATHから実行できる状態にしてください。"
    if status == "rate_limited":
        return f"{agent_label} は利用上限に達している可能性があります。リセットを待つか、そのAIをスキップしてください。"
    return ""


def all_failed_guidance(statuses: dict[str, str]) -> str:
    """What to tell the user when no AI produced an answer.

    A generic empty result sends people looking for a bug in the app; naming
    each agent's own status points at the one thing they can actually fix.
    """
    lines = ["どのAIからも回答を得られませんでした。AIごとの状況は次のとおりです。", ""]
    for agent in sorted(statuses):
        lines.append(f"- {agent}: {guidance_for(statuses[agent])}")
    return "\n".join(lines)
