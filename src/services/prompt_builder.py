from __future__ import annotations

from src import config
from src.models import ScanResult
from src.services.chair_agent import CHAIR_SYSTEM_PROMPT, ChairAgent
from src.services.role_orchestrator import RoleAssignment, RoleOrchestrator, RoleRound


def _is_cancelled(cancel_event: object | None) -> bool:
    is_set = getattr(cancel_event, "is_set", None)
    return bool(is_set and is_set())


class PromptBuilder:
    def __init__(self, chair: ChairAgent):
        self.chair = chair

    def build_context_documents(
        self, scan: ScanResult, user_request: str, cancel_event: object | None = None
    ) -> tuple[str, str, str]:
        context = self._raw_context(scan)
        current_session = f"# Current Session\n\n## User Request\n\n{user_request}\n"
        context_pack = self.build_context_pack(scan, user_request, context, cancel_event)
        return context, current_session, context_pack

    def build_context_pack(
        self,
        scan: ScanResult,
        user_request: str,
        raw_context: str,
        cancel_event: object | None = None,
    ) -> str:
        prompt = f"""Create a concise shared context pack for AI coding agents.
Keep it under {config.MAX_CONTEXT_PACK_CHARS // 4} Japanese characters.

User request:
{user_request}

Project context:
{raw_context}

Required format:
共通前提:
今回の依頼:
対象プロジェクト:
関連ファイル:
採用済み判断:
禁止事項:
"""
        if _is_cancelled(cancel_event):
            return self._fallback_context_pack(scan, user_request)
        summary = self.chair.chat(
            CHAIR_SYSTEM_PROMPT,
            prompt,
            max_tokens=1200,
            timeout_seconds=config.LM_STUDIO_CONTEXT_TIMEOUT_SECONDS,
        )
        if summary:
            return summary[: config.MAX_CONTEXT_PACK_CHARS]
        return self._fallback_context_pack(scan, user_request)

    def build_agent_prompts(self, context_pack: str, user_request: str) -> dict[str, str]:
        plan = RoleOrchestrator().build_plan({"claude", "gemini", "codex"}, automation_level=1)
        if not plan:
            return {}
        return self.build_round_prompts(context_pack, user_request, plan[0], previous_round_summary="")

    def build_round_prompts(
        self,
        context_pack: str,
        user_request: str,
        role_round: RoleRound,
        previous_round_summary: str = "",
    ) -> dict[str, str]:
        prompts: dict[str, str] = {}
        for assignment in role_round.assignments:
            prompts[assignment.agent] = self.build_role_prompt(
                context_pack,
                user_request,
                assignment,
                role_round.focus,
                previous_round_summary,
            )
        return prompts

    def build_role_prompt(
        self,
        context_pack: str,
        user_request: str,
        assignment: RoleAssignment,
        round_focus: str,
        previous_round_summary: str = "",
    ) -> str:
        display_name = "gemini/antigravity" if assignment.agent == "gemini" else assignment.agent
        previous_context = previous_round_summary.strip() or "(This is the first round.)"
        return f"""You are {display_name} participating in an AI development orchestration workflow.

Read this shared context first:

{context_pack}

User request:
{user_request}

Round:
{assignment.round_number}

Round focus:
{round_focus}

Your assigned role for this round:
{assignment.role_label}

Role instruction:
{assignment.instruction}

Previous round summary:
{previous_context}

Rules:
- Do not propose paid API-key fallback or pay-as-you-go usage.
- Do not assume README.md is internal AI memory.
- Do not run shell commands, inspect files, use tools, or modify files.
- Use only the shared context included in this prompt.
- If previous round context is provided, improve or challenge it instead of repeating it.
- Respect your assigned role even if your model is usually stronger in another area.
- Keep the answer structured and concise.
- Include risks, missing assumptions, and concrete next steps.

Return this shape:
Role verdict:
Key findings:
Impact on the plan:
Implementation/test implications:
Questions or blockers:
"""

    def _raw_context(self, scan: ScanResult) -> str:
        tree = "\n".join(scan.tree[: config.MAX_TREE_ITEMS])
        file_sections = []
        for name, content in scan.important_files.items():
            file_sections.append(f"## {name}\n\n{content[: config.MAX_FILE_READ_CHARS]}")
        vendor = "\n".join(scan.vendor_paths) or "None"
        important_files = "\n\n".join(file_sections)
        return f"""# Project Root
{scan.project_root}

# Detected AI/vendor files
{vendor}

# File Tree
{tree}

# Important Files
{important_files}
"""

    def _fallback_context_pack(self, scan: ScanResult, user_request: str) -> str:
        important = ", ".join(scan.important_files) or "none detected"
        vendor = ", ".join(scan.vendor_paths) or "none detected"
        tree_preview = "\n".join(scan.tree[:80])
        return f"""共通前提:
- 追加課金なし。API key / pay-as-you-go / 自動クレジット購入は禁止。
- LM Studio が秘書兼議長AI。
- README.md はAI内部メモとして自動作成・編集しない。
- .ai-brainstorm/ をAI専用の作業領域にする。

今回の依頼:
{user_request}

対象プロジェクト:
{scan.project_root}

関連ファイル:
{important}

既存AI関連ファイル:
{vendor}

ファイルツリー概要:
{tree_preview}

禁止事項:
- 課金APIへの自動切り替え。
- 危険操作や外部送信の無確認実行。
"""
