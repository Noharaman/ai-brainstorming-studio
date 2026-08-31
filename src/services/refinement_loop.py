from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from pathlib import Path

from src import config
from src.models import BrainstormResult, CommandResult, ProgressCallback
from src.services import (
    agent_model_selector,
    autonomy_controller,
    chair_output,
    cli_status,
    git_checkpoint,
    implementation_plan as implementation_plan_module,
    write_grant,
)
from src.services.agent_model_selector import AgentSelection
from src.services.chair_agent import CHAIR_SYSTEM_PROMPT, ChairAgent
from src.services.cli_runner import CliRunner
from src.services.context_scanner import ContextScanner
from src.services.health_checker import HealthChecker, StatusCallback
from src.services.implementation_plan import (
    ApprovalCallback,
    ApprovalDecision,
    ApprovalRequest,
    ImplementationOutcome,
)
from src.services.lm_studio_manager import LMStudioManager
from src.services.prompt_builder import PromptBuilder
from src.services.question_manager import QuestionManager
from src.services.response_preprocessor import ResponsePreprocessor
from src.services.role_orchestrator import RoleAssignment, RoleOrchestrator, RoleRound
from src.services.run_state import RunState, StateCallback
from src.services import test_runner
from src.services.test_runner import ProjectTestRunner
from src.services.workspace_manager import WorkspaceManager


#: Sections the final user-facing answer must actually contain, checked
#: through chair_output's accepted-wording matching rather than literally.
FINAL_ANSWER_SECTIONS = ("結論", "次にやること")

#: How many times the implementer may try to fix its own failing tests before
#: the run reports the failure instead. Each attempt is another write pass over
#: the user's files, and a model that has failed twice is not converging.
MAX_TEST_REPAIR_ATTEMPTS = 2

#: Appended verbatim on the one retry we allow when the chair answers in the
#: wrong language. Kept blunt and in Japanese: a model that just ignored the
#: Japanese headings it was given needs the instruction restated, not softened.
JAPANESE_REMINDER = """

重要: 回答は必ず日本語で書いてください。見出しも日本語のまま使ってください。
英語で回答してはいけません。"""


class RefinementLoop:
    def __init__(self, prefer_rtk: bool = True):
        self.chair = ChairAgent()
        self.prompt_builder = PromptBuilder(self.chair)
        self.cli_runner = CliRunner(prefer_rtk=prefer_rtk)
        self.preprocessor = ResponsePreprocessor()
        self.question_manager = QuestionManager()
        self.lm_manager = LMStudioManager()
        self.role_orchestrator = RoleOrchestrator()

    async def run(
        self,
        project_root: Path,
        user_request: str,
        automation_level: int = 2,
        progress: ProgressCallback | None = None,
        cancel_event: object | None = None,
        run_context: RunContext | None = None,
        health_status: StatusCallback | None = None,
        state_callback: StateCallback | None = None,
        approval_callback: ApprovalCallback | None = None,
    ) -> BrainstormResult:
        def emit(message: str) -> None:
            if progress:
                progress(message)

        def enter(state: RunState) -> None:
            if state_callback:
                state_callback(state)

        if self._is_cancelled(cancel_event):
            emit("Cancellation requested before starting; nothing was sent to LM Studio or any AI CLI.")
            return self._cancelled_result_minimal()

        enter(RunState.PREPARING)

        if not self.chair.available():
            if self._is_cancelled(cancel_event):
                emit("Cancellation requested; skipping LM Studio auto-start.")
                return self._cancelled_result_minimal()
            emit("LM Studio is offline. Attempting auto-start via `lms server start`...")
            started, msg = self.lm_manager.ensure_server_running(timeout_seconds=8, cancel_event=cancel_event)
            if started:
                self.chair.invalidate_cache()
            emit(f"LM Studio status: {msg}")

            if self._is_cancelled(cancel_event):
                emit("Cancellation requested; skipping the rest of the run.")
                return self._cancelled_result_minimal()

        session_id = self._new_session_id()
        workspace = WorkspaceManager(project_root)
        emit("Scanning project...")
        scan = ContextScanner(project_root).scan()
        emit(
            f"Scan complete: {len(scan.tree)} tree entries, "
            f"{len(scan.important_files)} important files, "
            f"{len(scan.vendor_paths)} existing AI config paths."
        )
        emit("Initializing .ai-brainstorm...")
        workspace.initialize()

        warnings: list[str] = []
        emit("Running preflight before sending the main request to AI CLIs...")
        preflight_results, preflight_warnings = await HealthChecker().preflight_all(
            cwd=project_root,
            automation_level=automation_level,
            prefer_rtk=False,
            progress=progress,
            cancel_event=cancel_event,
            **({"status_callback": health_status} if health_status else {}),
        )
        warnings.extend(preflight_warnings)
        for agent, result in preflight_results.items():
            workspace.write_session_artifact(
                session_id,
                f"{agent}_preflight.md",
                f"# {agent} preflight\n\nstatus: {result.status}\nreturncode: {result.returncode}\ncommand: {self._display_command(result.command)}\n\n## stdout\n\n{result.stdout}\n\n## stderr\n\n{result.stderr}\n",
            )
        available_agents = {agent for agent, result in preflight_results.items() if result.ok}
        skipped_agents = sorted(set(preflight_results) - available_agents)
        if skipped_agents:
            emit("Preflight skipped main request for: " + ", ".join(skipped_agents))
        if self._is_cancelled(cancel_event):
            return self._cancelled_result(session_id, workspace, preflight_results, warnings)
        if not available_agents:
            emit("No CLI passed preflight; main request will not be sent.")
            # Still a completed run, not a failed one: the user gets a real
            # final answer explaining which CLI was unavailable and why.
            enter(RunState.COMPLETED)
            final_answer = self._failed_consultation_summary(preflight_results)
            workspace.write_session_artifact(session_id, "final_answer.md", final_answer)
            workspace.append_history(session_id, final_answer)
            return BrainstormResult(
                session_id=session_id,
                context_pack="",
                prompts={},
                command_results=preflight_results,
                integrated_summary="",
                final_answer=final_answer,
                questions=self.question_manager.extract_questions(final_answer),
                warnings=warnings,
            )

        if self._is_cancelled(cancel_event):
            emit("Cancellation requested; skipping context pack generation.")
            return self._cancelled_result(session_id, workspace, preflight_results, warnings)

        enter(RunState.PLANNING)
        emit("Building local context pack with LM Studio if available...")
        context, current_session, context_pack = self.prompt_builder.build_context_documents(
            scan, user_request, cancel_event
        )
        emit(f"Context pack ready: {len(context_pack)} characters.")

        if self._is_cancelled(cancel_event):
            # build_context_documents() may have returned a normal-looking
            # fallback pack if cancellation landed during its own chair call —
            # the caller can't tell that apart from a real success, so check
            # again here before writing anything or doing further work.
            emit("Cancellation requested; skipping the rest of context preparation.")
            return self._cancelled_result(session_id, workspace, preflight_results, warnings)

        emit("Writing .ai-brainstorm context files...")
        workspace.write_context_files(context, current_session, context_pack)
        workspace.write_vendor_context(scan.vendor_paths, scan.important_files)
        emit("Context files written: context.md, current_session.md, context_pack.md, vendor_context/detected.md.")

        role_rounds = self.role_orchestrator.build_plan(available_agents, automation_level)
        workspace.write_session_artifact(
            session_id,
            "role_rotation_plan.md",
            self.role_orchestrator.render_plan(role_rounds),
        )
        emit(
            f"Role rotation plan ready: {len(role_rounds)} round(s): "
            f"{self._role_round_overview(role_rounds)}"
        )

        chair_auto_agents = agent_model_selector.resolve_chair_auto_agents(run_context, available_agents)
        if chair_auto_agents:
            emit(
                "Asking the chair to pick a model/effort for: "
                + ", ".join(sorted(chair_auto_agents))
                + "..."
            )
        elif run_context is not None:
            emit("Using user-selected model preferences.")
        else:
            emit("Asking the chair to pick a model/effort per agent from the safe catalog...")
        agent_selection = self.determine_agent_selection(
            user_request, available_agents, run_context=run_context, cancel_event=cancel_event
        )

        if self._is_cancelled(cancel_event):
            # agent_model_selector.select() may have been cancelled mid-flight
            # inside its own chair.chat() call; don't persist a selection or
            # start role rounds off the back of a run that should have
            # stopped.
            emit("Cancellation requested; skipping agent selection persistence and role rounds.")
            return self._cancelled_result(session_id, workspace, preflight_results, warnings)

        workspace.write_session_artifact(session_id, "agent_selection.md", agent_selection.render())
        emit(f"AI実行プラン:\n{agent_selection.render()}")

        prompts, results, round_summaries, run_warnings = await self._run_role_rounds(
            project_root,
            user_request,
            context_pack,
            session_id,
            workspace,
            role_rounds,
            available_agents,
            automation_level,
            progress,
            cancel_event,
            agent_selection,
        )
        warnings.extend(run_warnings)
        for agent, result in preflight_results.items():
            if agent not in available_agents and agent not in results:
                results[agent] = result
        emit(f"Role-rotated CLI outputs written under .ai-brainstorm/sessions/{session_id}/.")

        outcome = await self._implementation_phase(
            project_root=project_root,
            user_request=user_request,
            session_id=session_id,
            workspace=workspace,
            results=results,
            round_summaries=round_summaries,
            available_agents=available_agents,
            role_rounds=role_rounds,
            automation_level=automation_level,
            agent_selection=agent_selection,
            run_context=run_context,
            approval_callback=approval_callback,
            cancel_event=cancel_event,
            progress=progress,
            enter=enter,
            emit=emit,
        )
        if outcome.notes:
            warnings.extend(outcome.notes)

        if self._is_cancelled(cancel_event):
            return self._cancelled_result(session_id, workspace, results, warnings)

        enter(RunState.INTEGRATING)
        integrated_summary, final_answer, refinement_summary = self._integrate_and_finalize(
            user_request, context_pack, round_summaries, results, cancel_event, progress
        )
        if refinement_summary:
            workspace.write_session_artifact(session_id, "role_round_summaries.md", refinement_summary)
        # The implementation report is prepended rather than handed to the
        # chair: what changed on disk is a fact, and must not be reworded,
        # summarised away, or dropped because a model chose a different shape.
        final_answer = self._prepend_implementation_report(final_answer, outcome)
        questions = self.question_manager.extract_questions(final_answer)
        emit(f"Final answer ready: {len(final_answer)} characters; extracted {len(questions)} question(s).")

        workspace.write_session_artifact(session_id, "integrated_summary.md", integrated_summary)
        if refinement_summary:
            workspace.write_session_artifact(session_id, "refinement_summary.md", refinement_summary)
        workspace.write_session_artifact(session_id, "final_answer.md", final_answer)
        workspace.append_history(session_id, final_answer)
        emit("Session artifacts and history written.")

        return BrainstormResult(
            session_id=session_id,
            context_pack=context_pack,
            prompts=prompts,
            command_results=results,
            integrated_summary=integrated_summary,
            refinement_summary=refinement_summary,
            final_answer=final_answer,
            questions=questions,
            warnings=warnings,
            implementation=outcome,
        )

    async def _implementation_phase(
        self,
        project_root: Path,
        user_request: str,
        session_id: str,
        workspace: WorkspaceManager,
        results: dict,
        round_summaries: list,
        available_agents: set[str],
        role_rounds: list[RoleRound],
        automation_level: int,
        agent_selection: AgentSelection,
        run_context: RunContext | None,
        approval_callback: ApprovalCallback | None,
        cancel_event: object | None,
        progress: ProgressCallback | None,
        enter,
        emit,
    ) -> ImplementationOutcome:
        """Plan -> human approval -> write -> test.

        Every early return leaves `attempted=False`, so the caller cannot
        mistake "we never got permission" for "we implemented nothing".
        """
        caps = autonomy_controller.capabilities_for(automation_level)
        outcome = ImplementationOutcome()

        if not caps.can_implement:
            return outcome
        if self._is_cancelled(cancel_event):
            return outcome

        if approval_callback is None:
            # A run with no way to ask stops here rather than implementing
            # unattended. Reported as a note so the user sees why.
            outcome.notes.append(
                "自動化レベルは実装可能ですが、承認を求める経路がないため実装は行いませんでした。"
            )
            return outcome

        implementer = self._choose_implementer(role_rounds, available_agents, results)
        if implementer is None:
            outcome.notes.append(
                "実装を担当できるAIが今回の実行にいないため、実装フェーズは省略しました。"
            )
            return outcome
        outcome.implementer = implementer

        emit("実装プランを作成しています（議長AI）...")
        plan_text = self._build_implementation_plan_text(
            user_request, round_summaries, results, cancel_event, progress
        )
        if self._is_cancelled(cancel_event):
            return outcome
        if not plan_text:
            outcome.notes.append(
                "議長AIが実装プランを生成できなかったため、実装フェーズは省略しました。"
            )
            return outcome

        plan = implementation_plan_module.parse(plan_text)
        workspace.write_session_artifact(session_id, "implementation_plan.md", plan_text)

        checkpoint = git_checkpoint.capture(project_root)
        for warning in checkpoint.approval_warnings():
            emit(f"注意: {warning}")

        enter(RunState.WAITING_APPROVAL)
        emit(f"承認待ち: {implementer} に実装させてよいか確認しています。")
        request = ApprovalRequest(
            run_id=run_context.run_id if run_context else "",
            plan=plan,
            implementer=implementer,
            checkpoint=checkpoint,
            project_root=str(project_root),
        )
        try:
            decision = approval_callback(request)
        except Exception as exc:  # a broken GUI must not grant write access
            outcome.notes.append(f"承認処理でエラーが発生したため、実装は行いませんでした: {exc}")
            return outcome

        if decision is None or decision.cancelled:
            emit("承認待ちの間にキャンセルされました。実装は行っていません。")
            return outcome
        if not decision.approved:
            emit("承認されなかったため、実装は行っていません。ファイルは一切変更していません。")
            outcome.notes.append("実装は承認されませんでした。ファイルは変更していません。")
            if decision.feedback:
                outcome.notes.append(f"却下理由: {decision.feedback}")
            return outcome
        if self._is_cancelled(cancel_event):
            return outcome

        outcome.approved = True
        outcome.attempted = True
        outcome.revert_hint = checkpoint.revert_hint()

        grant = write_grant.granted_after_approval(
            run_id=run_context.run_id if run_context else session_id,
            agent=implementer,
            project_root=project_root,
            approved=decision.approved,
            baseline_commit=checkpoint.commit,
        )
        emit(f"承認されました。{grant.describe()}")

        enter(RunState.IMPLEMENTING)
        prompt = self._implementation_prompt(user_request, plan, decision.feedback)
        workspace.write_session_artifact(
            session_id, f"implementation_{implementer}_prompt.md", prompt
        )
        impl_results, impl_warnings = await self.cli_runner.run_all(
            {implementer: prompt},
            cwd=project_root,
            automation_level=automation_level,
            progress=progress,
            cancel_event=cancel_event,
            agent_selection=agent_selection,
            grant=grant,
            run_id=grant.run_id,
        )
        outcome.notes.extend(impl_warnings)
        impl_result = impl_results.get(implementer)
        if impl_result is not None:
            results[f"{implementer}_implementer"] = impl_result
            workspace.write_session_artifact(
                session_id,
                f"implementation_{implementer}.md",
                f"# {implementer} implementation\n\n"
                f"status: {impl_result.status}\n"
                f"returncode: {impl_result.returncode}\n"
                f"command: {self._display_command(impl_result.command)}\n\n"
                f"## stdout\n\n{impl_result.stdout}\n\n"
                f"## stderr\n\n{impl_result.stderr}\n",
            )
            if not impl_result.ok:
                outcome.notes.append(
                    f"{implementer} の実装実行は status={impl_result.status} で終了しました。"
                )

        self._record_diff(project_root, checkpoint, outcome, workspace, session_id, emit)

        if (
            caps.runs_tests
            and not config.RUN_TESTS_AUTOMATICALLY
            and not self._is_cancelled(cancel_event)
        ):
            # Detection only. Running the suite would execute code the AI just
            # wrote, with this app's full user rights and no sandbox — and the
            # user approved an implementation, not "run arbitrary code as me".
            # The automatic run comes back once the OS sandbox lands; until
            # then the command is shown and the user decides.
            # See docs/safety-model.md and config.RUN_TESTS_AUTOMATICALLY.
            self._report_manual_test_command(project_root, outcome, emit)

        if caps.runs_tests and config.RUN_TESTS_AUTOMATICALLY and not self._is_cancelled(
            cancel_event
        ):
            await self._test_and_repair(
                project_root=project_root,
                grant=grant,
                implementer=implementer,
                plan=plan,
                checkpoint=checkpoint,
                outcome=outcome,
                workspace=workspace,
                session_id=session_id,
                results=results,
                automation_level=automation_level,
                agent_selection=agent_selection,
                cancel_event=cancel_event,
                progress=progress,
                enter=enter,
                emit=emit,
            )

        enter(RunState.REVIEWING)
        return outcome

    def _report_manual_test_command(
        self,
        project_root: Path,
        outcome: ImplementationOutcome,
        emit,
    ) -> None:
        """Tell the user how to run the tests, without running them.

        `test_passed` stays None: nothing was verified, and reporting a
        verdict for a suite that never ran would be worse than reporting
        none.
        """
        command = test_runner.detect_command(project_root)
        if command is None:
            emit(
                "テストは自動実行しません。既知のテスト構成も見つからなかったため、"
                "検証は手動で行ってください。"
            )
            outcome.notes.append(
                "テストは実行していません（自動実行は無効。既知のテスト構成も未検出）。"
            )
            return

        command_text = " ".join(command)
        outcome.test_command = command_text
        outcome.test_passed = None
        emit(
            "テストは自動実行しません（サンドボックス未実装のため）。"
            f"次のコマンドを手動で実行してください: {command_text}"
        )
        outcome.notes.append(
            "テストは実行していません。AIが書いたコードをこのアプリの権限で実行しないためです。"
            f"手動で実行してください: {command_text}"
        )

    def _record_diff(
        self,
        project_root: Path,
        checkpoint,
        outcome: ImplementationOutcome,
        workspace,
        session_id: str,
        emit,
    ) -> None:
        """Refresh the outcome's view of what is on disk right now."""
        diff = git_checkpoint.diff_since(project_root, checkpoint)
        outcome.changed_files = diff.changed_files
        outcome.diff_text = diff.diff_text
        outcome.diff_stat = diff.stat
        if diff.error and diff.error not in outcome.notes:
            outcome.notes.append(diff.error)
        if diff.has_unrecoverable_loss:
            # Data loss, not a change: git cannot bring these back, so it goes
            # in front of the user rather than only into the file list.
            outcome.lost_paths = tuple(diff.lost_paths) + tuple(diff.overwritten_paths)
            if diff.lost_paths:
                emit("警告: Git管理外のファイルが削除されました: " + ", ".join(diff.lost_paths))
                outcome.notes.append(
                    "AIがGit管理外のファイルを削除しました（"
                    + ", ".join(diff.lost_paths)
                    + "）。Gitからは復元できません。"
                )
            if diff.overwritten_paths:
                emit(
                    "警告: Git管理外のファイルが上書きされました: "
                    + ", ".join(diff.overwritten_paths)
                )
                outcome.notes.append(
                    "AIがGit管理外のファイルを上書きしました（"
                    + ", ".join(diff.overwritten_paths)
                    + "）。変更前の内容はGitからは復元できません。"
                )
        if diff.unverified_paths:
            outcome.notes.append(
                "サイズ等の理由で変更を確認できなかったGit管理外ファイルがあります（"
                + ", ".join(diff.unverified_paths)
                + "）。"
            )
        if diff.changed_files:
            workspace.write_session_artifact(
                session_id, "implementation_diff.patch", diff.diff_text or "(空の差分)"
            )
            emit(f"変更されたファイル: {len(diff.changed_files)}件")
        else:
            emit("ファイルの変更は検出されませんでした。")

    async def _test_and_repair(
        self,
        project_root: Path,
        grant,
        implementer: str,
        plan,
        checkpoint,
        outcome: ImplementationOutcome,
        workspace,
        session_id: str,
        results: dict,
        automation_level: int,
        agent_selection: AgentSelection,
        cancel_event: object | None,
        progress: ProgressCallback | None,
        enter,
        emit,
    ) -> None:
        """Run the tests; on failure let the implementer try to fix its own work.

        Bounded at MAX_TEST_REPAIR_ATTEMPTS. A model that has failed twice is
        not converging, and each attempt is another write pass over the user's
        files — so the run stops and reports rather than grinding on. The
        original failure is kept when repair does not help, because the first
        failure is usually the informative one.
        """
        for attempt in range(MAX_TEST_REPAIR_ATTEMPTS + 1):
            if self._is_cancelled(cancel_event):
                return

            enter(RunState.TESTING)
            emit(
                "プロジェクトのテストを実行しています..."
                if attempt == 0
                else f"修正後のテストを再実行しています（{attempt}回目の修正）..."
            )
            test_outcome = await asyncio.to_thread(
                ProjectTestRunner().run, project_root, cancel_event
            )
            outcome.test_command = test_outcome.command_text
            outcome.test_passed = test_outcome.passed
            outcome.test_output = test_outcome.output

            if not test_outcome.ran:
                emit(f"テストは実行していません: {test_outcome.reason}")
                if test_outcome.reason not in outcome.notes:
                    outcome.notes.append(test_outcome.reason)
                return

            workspace.write_session_artifact(
                session_id,
                f"implementation_tests{'' if attempt == 0 else f'_retry{attempt}'}.md",
                f"# tests (attempt {attempt + 1})\n\n"
                f"command: {test_outcome.command_text}\n"
                f"passed: {test_outcome.passed}\n\n```\n{test_outcome.output}\n```\n",
            )
            emit(
                f"テスト結果: {'成功' if test_outcome.passed else '失敗'} "
                f"({test_outcome.command_text})"
            )

            if test_outcome.passed:
                if attempt:
                    outcome.repair_attempts = attempt
                    outcome.notes.append(
                        f"テストは{attempt}回の自動修正後に成功しました。"
                    )
                return

            if attempt >= MAX_TEST_REPAIR_ATTEMPTS:
                outcome.repair_attempts = attempt
                outcome.notes.append(
                    f"テストが失敗したままです（自動修正を{attempt}回試行して解決しませんでした）。"
                    "内容を確認してください。"
                )
                return

            enter(RunState.IMPLEMENTING)
            emit(f"テストが失敗したため、{implementer} に修正を依頼します...")
            repair_result = await self._run_repair_attempt(
                project_root=project_root,
                grant=grant,
                implementer=implementer,
                plan=plan,
                test_outcome=test_outcome,
                attempt=attempt + 1,
                workspace=workspace,
                session_id=session_id,
                automation_level=automation_level,
                agent_selection=agent_selection,
                cancel_event=cancel_event,
                progress=progress,
            )
            outcome.repair_attempts = attempt + 1
            if repair_result is not None:
                results[f"{implementer}_repair{attempt + 1}"] = repair_result
                if not repair_result.ok:
                    outcome.notes.append(
                        f"{implementer} の修正実行は status={repair_result.status} で終了しました。"
                    )
                    return
            self._record_diff(
                project_root, checkpoint, outcome, workspace, session_id, emit
            )

    async def _run_repair_attempt(
        self,
        project_root: Path,
        grant,
        implementer: str,
        plan,
        test_outcome,
        attempt: int,
        workspace,
        session_id: str,
        automation_level: int,
        agent_selection: AgentSelection,
        cancel_event: object | None,
        progress: ProgressCallback | None,
    ):
        prompt = f"""あなたが行った実装で、プロジェクトのテストが失敗しました。原因を調べて修正してください。

実行したテストコマンド:
{test_outcome.command_text}

テスト出力:
{test_outcome.output}

承認済みの実装プラン（この範囲を超えないこと）:
{plan.render()}

守ること:
- テストを通すことが目的だが、テストを削除・スキップ・無効化して通してはいけない。
- 承認されたプランの範囲内だけを変更する。範囲外のファイルは触らない。
- `git commit`、`git push`、`git reset`、`git clean` は実行しない。
- 認証情報、`.env`、秘密鍵には触れない。
- 修正内容を簡潔に日本語で報告する。
"""
        workspace.write_session_artifact(
            session_id, f"repair{attempt}_{implementer}_prompt.md", prompt
        )
        repair_results, _warnings = await self.cli_runner.run_all(
            {implementer: prompt},
            cwd=project_root,
            automation_level=automation_level,
            progress=progress,
            cancel_event=cancel_event,
            agent_selection=agent_selection,
            grant=grant,
            run_id=grant.run_id,
        )
        result = repair_results.get(implementer)
        if result is not None:
            workspace.write_session_artifact(
                session_id,
                f"repair{attempt}_{implementer}.md",
                f"# {implementer} repair attempt {attempt}\n\n"
                f"status: {result.status}\n"
                f"returncode: {result.returncode}\n\n"
                f"## stdout\n\n{result.stdout}\n\n"
                f"## stderr\n\n{result.stderr}\n",
            )
        return result

    def _choose_implementer(
        self,
        role_rounds: list[RoleRound],
        available_agents: set[str],
        results: dict,
    ) -> str | None:
        """The single agent that gets write access.

        The Author of the final round: by then it has seen the critic's and
        verifier's objections, so it is the role holding the converged
        proposal. Only an agent that actually answered is eligible — handing a
        grant to a CLI that failed every round would write nothing and hide
        the real failure behind an empty diff.
        """
        def answered(agent: str) -> bool:
            result = results.get(agent)
            return bool(result is not None and result.ok)

        for role_round in reversed(role_rounds):
            for assignment in role_round.assignments:
                if assignment.role == "author" and answered(assignment.agent):
                    return assignment.agent
        for agent in sorted(available_agents):
            if answered(agent):
                return agent
        return None

    def _build_implementation_plan_text(
        self,
        user_request: str,
        round_summaries: list,
        results: dict,
        cancel_event: object | None,
        progress: ProgressCallback | None,
    ) -> str:
        prompt = f"""以下の相談結果をもとに、実装プランを作成してください。

ユーザーの依頼:
{user_request}

AIの検討結果:
{self._combined_role_context([str(s) for s in round_summaries], results)}

必ず次の見出しをこの順で使い、各項目を具体的に書いてください。
推測でファイル名を作らず、検討結果に出てきたものだけを挙げてください。

## 変更概要
## 対象ファイル
## 実装手順
## テスト
## リスク・注意点
"""
        return self._chair_chat_in_japanese(
            prompt, 1500, cancel_event, progress, label="実装プラン"
        ) or ""

    def _implementation_prompt(self, user_request: str, plan, feedback: str) -> str:
        """What the implementer is told. Scope is stated as a constraint."""
        correction = f"\n\nユーザーからの補足指示:\n{feedback}\n" if feedback else ""
        return f"""あなたはこのプロジェクトの実装担当です。以下の承認済みプランを実装してください。

ユーザーの依頼:
{user_request}

承認済みの実装プラン:
{plan.render()}
{correction}
守ること:
- 承認されたプランの範囲内だけを変更する。範囲外のファイルは触らない。
- 既存のコードスタイル、命名、コメントの粒度に合わせる。
- 既存のテストを壊さない。必要ならテストを追加する。
- `git commit`、`git push`、`git reset`、`git clean` は実行しない。変更は作業ツリーに残す。
- 大量のファイル削除・移動を行わない。
- 認証情報、`.env`、秘密鍵には触れない。
- 実装後、変更した内容を簡潔に日本語で報告する。
"""

    def _prepend_implementation_report(
        self, final_answer: str, outcome: ImplementationOutcome
    ) -> str:
        if not outcome.attempted:
            return final_answer

        lines = ["## 実装結果", ""]
        if outcome.lost_paths:
            lines += [
                "> ⚠️ **Gitで復元できないファイルが削除されました**: "
                + ", ".join(outcome.lost_paths),
                "",
            ]
        lines.append(f"- 実装担当: {outcome.implementer}")
        if outcome.changed_files:
            lines.append(f"- 変更ファイル: {len(outcome.changed_files)}件")
            lines.extend(f"  - {path}" for path in outcome.changed_files[:30])
            if len(outcome.changed_files) > 30:
                lines.append(f"  - ... ほか{len(outcome.changed_files) - 30}件")
        else:
            lines.append("- 変更ファイル: なし（AIはファイルを変更しませんでした）")

        if outcome.test_command:
            repaired = (
                f"（自動修正{outcome.repair_attempts}回）" if outcome.repair_attempts else ""
            )
            if outcome.test_passed is True:
                lines.append(f"- テスト: 成功{repaired} (`{outcome.test_command}`)")
            elif outcome.test_passed is False:
                lines.append(f"- テスト: **失敗**{repaired} (`{outcome.test_command}`)")
            else:
                lines.append(f"- テスト: 未完了{repaired} (`{outcome.test_command}`)")

        if outcome.revert_hint:
            lines += ["", f"取り消す場合: {outcome.revert_hint}"]

        return "\n".join(lines) + "\n\n---\n\n" + final_answer

    def determine_agent_selection(
        self,
        user_request: str,
        available_agents: set[str],
        run_context: RunContext | None = None,
        cancel_event: object | None = None,
    ) -> AgentSelection:
        """Resolve each available agent independently into: no override (not
        present in run_context.selected_models), an explicit user pick (a real
        model id), or a chair-auto pick (agent_model_selector.CHAIR_AUTO_SELECT
        — the "議長AIにお任せ" GUI choice).

        run_context=None means no per-agent GUI selection exists at all (e.g. a
        caller outside the GUI flow) — every available agent goes to the
        chair, preserving select()'s original whole-set behavior.
        """
        if run_context is None:
            return agent_model_selector.select(
                user_request, available_agents, self.chair, cancel_event=cancel_event
            )

        selected_models = run_context.selected_models
        selected_efforts = run_context.selected_efforts
        default_effort = run_context.effort

        chair_auto_agents = agent_model_selector.resolve_chair_auto_agents(run_context, available_agents)
        explicit_choices = {
            agent: (model_id, selected_efforts.get(agent) or default_effort)
            for agent, model_id in selected_models.items()
            if model_id and model_id != agent_model_selector.CHAIR_AUTO_SELECT
        }

        auto_selection = AgentSelection.empty()
        if chair_auto_agents:
            # One batched chair call for every "お任せ" agent, not one call
            # each — chair calls in this app measure 30-90+ seconds, so
            # per-agent calls would multiply run latency for no benefit.
            auto_selection = agent_model_selector.select(
                user_request, chair_auto_agents, self.chair, cancel_event=cancel_event
            )

        return AgentSelection(
            choices={**explicit_choices, **auto_selection.choices},
            chair_auto_agents=frozenset(chair_auto_agents),
        )

    def run_sync(
        self,
        project_root: Path,
        user_request: str,
        automation_level: int = 2,
        progress: ProgressCallback | None = None,
        cancel_event: object | None = None,
        run_context: RunContext | None = None,
        health_status: StatusCallback | None = None,
        state_callback: StateCallback | None = None,
        approval_callback: ApprovalCallback | None = None,
    ) -> BrainstormResult:
        return asyncio.run(
            self.run(
                project_root,
                user_request,
                automation_level,
                progress,
                cancel_event,
                run_context,
                health_status,
                state_callback,
                approval_callback,
            )
        )

    def _new_session_id(self) -> str:
        # Timestamp prefix keeps sessions human-sortable; the suffix guarantees
        # uniqueness even when two tabs run against the same project within
        # the same second (otherwise their session artifact directories collide).
        return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    def _integrate_and_finalize(
        self,
        user_request: str,
        context_pack: str,
        round_summaries: list[str],
        results: dict[str, CommandResult],
        cancel_event: object | None,
        progress: ProgressCallback | None,
    ) -> tuple[str, str, str]:
        """Runs the two remaining chair calls (_integrate then _finalize),
        re-checking cancellation before each. Neither _integrate()'s nor
        _finalize()'s HTTP call can be aborted mid-flight (ChairAgent.chat()
        has no cancel hook), but these checks stop a *new* chair call from
        starting once cancellation is detected.

        Returns (integrated_summary, final_answer, refinement_summary).
        """
        def emit(message: str) -> None:
            if progress:
                progress(message)

        if self._is_cancelled(cancel_event):
            emit("Cancellation requested; skipping chair integration.")
            return "", self._cancelled_summary(results), ""

        emit("Preprocessing role-rotated answers for local chair...")
        compact_answers = self._combined_role_context(round_summaries, results)
        emit(f"Preprocessed answer bundle: {len(compact_answers)} characters.")

        if self._is_cancelled(cancel_event):
            emit("Cancellation requested; skipping chair integration.")
            return "", self._cancelled_summary(results), ""

        emit("Asking LM Studio to compare role-rotated findings (local AI inference in progress, may take 30-60s)...")
        integrated_summary = self._integrate(
            user_request, context_pack, compact_answers, cancel_event, progress
        )
        emit(f"Integrated summary ready: {len(integrated_summary)} characters.")

        refinement_summary = "\n\n".join(round_summaries)

        if self._is_cancelled(cancel_event):
            emit("Cancellation requested during integration; skipping final answer generation.")
            return integrated_summary, self._cancelled_summary(results), refinement_summary

        emit("Creating final answer with LM Studio (local AI finalizing output)...")
        final_answer = self._finalize(
            user_request,
            integrated_summary,
            refinement_summary,
            results,
            compact_answers,
            cancel_event,
            progress,
        )
        return integrated_summary, final_answer, refinement_summary

    def _integrate(
        self,
        user_request: str,
        context_pack: str,
        compact_answers: str,
        cancel_event: object | None = None,
        progress: ProgressCallback | None = None,
    ) -> str:
        prompt = f"""Compare these role-rotated AI responses and create an integrated proposal.

User request:
{user_request}

Shared context:
{context_pack}

AI responses:
{compact_answers}

Return:
- 結論
- 採用する方針
- ラウンド間で合意したこと
- 意見が割れたこと
- 重要な指摘
- 不足している観点
- ユーザー確認が必要な点
"""
        if self._is_cancelled(cancel_event):
            return ""
        answer = self._chair_chat_in_japanese(
            prompt, 1800, cancel_event, progress, label="統合"
        )
        if answer:
            return answer
        return "LM Studio が未起動または応答不可のため、ローカル議長AIによる自動比較・統合は行われませんでした。"

    async def _run_role_rounds(
        self,
        project_root: Path,
        user_request: str,
        context_pack: str,
        session_id: str,
        workspace: WorkspaceManager,
        role_rounds: list[RoleRound],
        available_agents: set[str],
        automation_level: int,
        progress: ProgressCallback | None,
        cancel_event: object | None,
        agent_selection: agent_model_selector.AgentSelection | None = None,
    ) -> tuple[dict[str, str], dict[str, CommandResult], list[str], list[str]]:
        def emit(message: str) -> None:
            if progress:
                progress(message)

        all_prompts: dict[str, str] = {}
        all_results: dict[str, CommandResult] = {}
        all_warnings: list[str] = []
        round_summaries: list[str] = []
        previous_round_summary = ""

        for role_round in role_rounds:
            if self._is_cancelled(cancel_event):
                emit("Cancellation requested before next role-rotation round.")
                break

            emit(f"Round {role_round.number}: {self._assignment_overview(role_round)}")
            round_prompts = self.prompt_builder.build_round_prompts(
                context_pack,
                user_request,
                role_round,
                previous_round_summary,
            )
            round_prompts = {
                agent: prompt for agent, prompt in round_prompts.items() if agent in available_agents
            }
            for agent, prompt in round_prompts.items():
                assignment = role_round.assignment_for(agent)
                if not assignment:
                    continue
                prompt_key = self._round_result_key(assignment)
                all_prompts[prompt_key] = prompt
                workspace.write_prompt(prompt_key, prompt, session_id)

            if not round_prompts:
                emit(f"Round {role_round.number}: no available agents to run.")
                continue

            emit(f"Round {role_round.number}: running role-rotated AI CLI prompts safely...")
            round_results, round_warnings = await self.cli_runner.run_all(
                round_prompts,
                cwd=project_root,
                automation_level=automation_level,
                progress=progress,
                cancel_event=cancel_event,
                agent_selection=agent_selection,
            )
            all_warnings.extend(round_warnings)

            labeled_round_results: dict[str, CommandResult] = {}
            for agent, result in round_results.items():
                assignment = role_round.assignment_for(agent)
                if not assignment:
                    continue
                result_key = self._round_result_key(assignment)
                labeled_round_results[result_key] = result
                all_results[result_key] = result
                workspace.write_session_artifact(
                    session_id,
                    f"{result_key}_output.md",
                    self._format_round_result(result_key, assignment, result),
                )

            is_last_round = role_round.number == role_rounds[-1].number
            if not is_last_round and self._round_failed_unrecoverably(labeled_round_results):
                emit(
                    f"Round {role_round.number}: every agent hit an unrecoverable status "
                    "(rate limit / missing CLI / unsupported client / etc.); stopping early "
                    "instead of repeating the same failure."
                )
                break

            if self._is_cancelled(cancel_event):
                emit(f"Round {role_round.number}: cancellation requested; skipping chair summarization.")
                break

            round_summary = self._summarize_round(
                user_request,
                context_pack,
                role_round,
                labeled_round_results,
                previous_round_summary,
                cancel_event,
            )
            if round_summary:
                previous_round_summary = round_summary
                round_summaries.append(f"## Round {role_round.number}\n\n{round_summary}")
                workspace.write_session_artifact(
                    session_id,
                    f"round{role_round.number}_summary.md",
                    round_summary,
                )
                if not is_last_round and self._round_converged(round_summary):
                    emit(f"Round {role_round.number}: chair reports convergence; skipping remaining rounds.")
                    break

        return all_prompts, all_results, round_summaries, all_warnings

    def _round_failed_unrecoverably(self, round_results: dict[str, CommandResult]) -> bool:
        """True only when every agent hit a status that a same-setup retry can't fix."""
        if not round_results:
            return False
        unrecoverable = {
            "rate_limited",
            "command_missing",
            "unsupported_client",
            "api_key_blocked",
            "config_error",
        }
        return all(result.status in unrecoverable for result in round_results.values())

    def _round_converged(self, round_summary: str) -> bool:
        """Reads the chair's own self-reported convergence line (see _summarize_round's
        prompt). Fails open (False) when the chair didn't answer in the expected shape,
        since we can't infer semantic convergence from the compressed-answer fallback text."""
        for line in round_summary.splitlines():
            stripped = line.strip(" -")
            if stripped.startswith("収束"):
                return "はい" in stripped and "いいえ" not in stripped
        return False

    def _summarize_round(
        self,
        user_request: str,
        context_pack: str,
        role_round: RoleRound,
        round_results: dict[str, CommandResult],
        previous_round_summary: str,
        cancel_event: object | None = None,
    ) -> str:
        if not round_results:
            return ""
        compact_answers = self.preprocessor.summarize_results(round_results)
        prompt = f"""Summarize this role-rotation round for the next round and final chair decision.

User request:
{user_request}

Shared context:
{context_pack}

Round:
{role_round.number}

Round focus:
{role_round.focus}

Assignments:
{self._assignment_overview(role_round)}

Previous round summary:
{previous_round_summary or '(none)'}

Round answers:
{compact_answers}

Return:
- 合意点
- 意見が割れた点
- 採用候補
- 修正すべき計画
- 次ラウンドまたは人間確認で見るべき点
- 収束(次ラウンド省略可否): 大きな矛盾が解消され追加ラウンドが不要なら「はい」、まだ必要なら「いいえ」
"""
        if self._is_cancelled(cancel_event):
            return ""
        answer = self.chair.chat(CHAIR_SYSTEM_PROMPT, prompt, max_tokens=1200)
        if answer:
            return answer
        return (
            "LM Studio が未起動または応答不可のため、このラウンドはCLI回答の圧縮版を暫定要約として使います。\n\n"
            + compact_answers[:6000]
        )

    def _combined_role_context(self, round_summaries: list[str], results: dict[str, CommandResult]) -> str:
        sections: list[str] = []
        if round_summaries:
            sections.append("# Role Rotation Round Summaries\n\n" + "\n\n".join(round_summaries))
            compact_results = self.preprocessor.summarize_results(
                results, max_chars_per_agent=800, max_total_chars=4000
            )
        else:
            compact_results = self.preprocessor.summarize_results(
                results, max_chars_per_agent=1500, max_total_chars=6000
            )
        if compact_results:
            sections.append("# Role Rotation Raw Outputs\n\n" + compact_results)
        return "\n\n".join(sections)

    def _role_round_overview(self, role_rounds: list[RoleRound]) -> str:
        if not role_rounds:
            return "none"
        return "; ".join(
            f"R{role_round.number}({self._assignment_overview(role_round)})"
            for role_round in role_rounds
        )

    def _assignment_overview(self, role_round: RoleRound) -> str:
        return ", ".join(
            f"{self._agent_label(assignment.agent)}={assignment.role_label}"
            for assignment in role_round.assignments
        )

    def _round_result_key(self, assignment: RoleAssignment) -> str:
        return f"round{assignment.round_number}_{assignment.agent}_{assignment.role}"

    def _format_round_result(
        self,
        result_key: str,
        assignment: RoleAssignment,
        result: CommandResult,
    ) -> str:
        return (
            f"# {self._agent_label(result_key)}\n\n"
            f"round: {assignment.round_number}\n"
            f"agent: {assignment.agent}\n"
            f"role: {assignment.role_label}\n"
            f"status: {result.status}\n"
            f"returncode: {result.returncode}\n"
            f"command: {self._display_command(result.command)}\n\n"
            f"## stdout\n\n{result.stdout}\n\n"
            f"## stderr\n\n{result.stderr}\n"
        )

    def _finalize(
        self,
        user_request: str,
        integrated_summary: str,
        refinement_summary: str,
        results: dict,
        compact_answers: str,
        cancel_event: object | None = None,
        progress: ProgressCallback | None = None,
    ) -> str:
        if not any(result.ok for result in results.values()):
            return self._failed_consultation_summary(results)
        if self._chair_was_unavailable(integrated_summary):
            return self._chair_unavailable_summary(results)

        prompt = f"""Create the final user-facing answer. Keep it concise.

User request:
{user_request}

CLI status:
{self._join_lines(self._status_lines(results))}

Integrated summary:
{integrated_summary}

Successful AI answers:
{compact_answers}

Refinement:
{refinement_summary or '(none)'}

Rules:
- If at least one AI answered successfully, summarize what was already learned.
- Do not say you will start the investigation if the successful AI answer already contains findings.
- Do not ask for permission to perform the same read-only investigation that already happened.
- Mention failed AI agents only briefly.
- Do not ask the user clarifying questions. If information is missing, state what is missing under
  リスク・注意点 as a blocker instead of asking the user to decide.

Use this exact shape:
結論:
採用する方針:
実行済み:
次にやること:
リスク・注意点:
"""
        if self._is_cancelled(cancel_event):
            return self._cancelled_summary(results)
        answer = self._chair_chat_in_japanese(
            prompt, 1200, cancel_event, progress, label="最終回答"
        )
        if answer and self._final_answer_uses_success(answer):
            notice = self._partial_failure_notice(results)
            return f"{notice}\n\n{answer}" if notice else answer
        return self._successful_consultation_summary(results)

    def _chair_chat_in_japanese(
        self,
        prompt: str,
        max_tokens: int,
        cancel_event: object | None = None,
        progress: ProgressCallback | None = None,
        label: str = "chair",
    ) -> str | None:
        """Ask the chair, and retry once if it answers in the wrong language.

        Measured at roughly one run in sixteen: the model returns the whole
        document in English, headings included. That used to fail the section
        check and get thrown away with no explanation, so the user saw a canned
        summary and never learned the chair had actually answered.
        """
        answer = self.chair.chat(CHAIR_SYSTEM_PROMPT, prompt, max_tokens=max_tokens)
        if not answer or chair_output.looks_japanese(answer):
            return answer

        if self._is_cancelled(cancel_event):
            # The first answer is unusable and we must not start a new call.
            return None
        if progress:
            progress(f"{label}: 議長AIが日本語以外で回答したため、一度だけ再依頼します。")
        retry = self.chair.chat(
            CHAIR_SYSTEM_PROMPT, prompt + JAPANESE_REMINDER, max_tokens=max_tokens
        )
        if retry and chair_output.looks_japanese(retry):
            return retry
        if progress:
            progress(
                f"{label}: 再依頼後も日本語になりませんでした。"
                "議長AIの出力をそのまま使います（表示が英語になる場合があります）。"
            )
        # Prefer whatever came back over nothing: a correct answer in the wrong
        # language is still more useful to the user than the canned fallback.
        return retry or answer

    def _chair_was_unavailable(self, integrated_summary: str) -> bool:
        return "LM Studio が未起動または応答不可" in integrated_summary

    def _failed_consultation_summary(self, results: dict[str, CommandResult]) -> str:
        status_lines = self._status_lines(results)
        next_steps = []
        for agent, result in results.items():
            step = self._next_step_for(agent, result.status)
            if step:
                next_steps.append(f"- {step}")

        return f"""結論:
今回は Claude / Antigravity / Codex から有効な回答を取得できませんでした。アプリから認証や設定の自動変更は行いません。

採用する方針:
まず各CLIを単体でログイン・権限確認し、既存のログインおよび設定を尊重して呼び出します。Antigravity枠は旧 `gemini` ではなく Antigravity CLI の `agy` を利用します。

実行済み:
{self._join_lines(status_lines)}

次にやること:
{self._join_lines(next_steps) or '- CLIのログイン状態と権限を確認してから再実行してください。'}

リスク・注意点:
すべてのCLIが失敗しているため、今回はAI同士の比較・ブラッシュアップは実施できていません。詳細ログは `.ai-brainstorm/sessions/` に保存済みです。
"""

    def _chair_unavailable_summary(self, results: dict[str, CommandResult]) -> str:
        successful = [self._agent_label(agent) for agent, result in results.items() if result.ok]
        failed_notice = self._partial_failure_notice(results)
        return f"""{failed_notice + chr(10) + chr(10) if failed_notice else ''}結論:
{', '.join(successful)} から回答は取得できましたが、LM Studio が未起動または応答不可のため、秘書兼議長AIによる自動統合は行っていません。

採用する方針:
現時点では未確定です。LM Studio を起動して再実行するか、下のログ / AI別出力で個別回答を確認してください。

実行済み:
{self._join_lines(self._status_lines(results))}

次にやること:
- LM Studio の Local Server を起動してください。
- 起動後に同じ依頼で再実行すると、ローカル議長AIが比較・統合します。

リスク・注意点:
未統合のため、複数AIの意見の優先順位や矛盾解消はまだ行われていません。
"""

    def _successful_consultation_summary(self, results: dict[str, CommandResult]) -> str:
        successful = {agent: result for agent, result in results.items() if result.ok}
        failed_notice = self._partial_failure_notice(results)
        excerpts = []
        for agent, result in successful.items():
            excerpts.append(f"## {self._agent_label(agent)} の回答\n{self._agent_excerpt(result)}")

        return f"""{failed_notice + chr(10) + chr(10) if failed_notice else ''}結論:
回答できたAIの内容をもとに、今回の依頼に対する暫定整理を作成しました。

採用する方針:
成功したAIの回答を優先して要点を確認し、失敗したAIの観点は不足分として扱います。

実行済み:
{self._join_lines(self._status_lines(results))}

回答できたAIの要点:
{self._join_lines(excerpts)}

次にやること:
- 上記の整理で方向性が合っているか確認してください。
- 必要ならClaude / Antigravity / Codex のログイン状態を直して再実行し、追加レビューを取ります。

リスク・注意点:
一部AIが失敗している場合、設計リスクや抜け漏れ確認の観点が不足している可能性があります。
"""

    def _final_answer_uses_success(self, answer: str) -> bool:
        stale_phrases = (
            "調査を開始しても",
            "調査を開始します",
            "許可が出たら",
            "確認してもよろしい",
            "開始してもよろしい",
            "まずはファイルを読み取り",
            "まずはフォルダ内",
        )
        if any(phrase in answer for phrase in stale_phrases):
            return False
        # Judge by shape, not by length: the app's own spec asks the chair for a
        # short answer, so a long-answer threshold would reject good output.
        #
        # Matching is by accepted wording, not literal substring. The chair
        # renames its own headings run to run (まとめ for 結論, 次のステップ for
        # 次にやること) and decorates them differently every time; an exact
        # match was testing word choice, and a rename silently threw away a
        # perfectly good answer.
        if chair_output.missing_sections(answer, FINAL_ANSWER_SECTIONS):
            return False
        return len(answer.strip()) >= 80

    def _agent_excerpt(self, result: CommandResult, max_chars: int = 1800) -> str:
        text = result.stdout.strip() if result.stdout.strip() else result.stderr.strip()
        if "tokens used" in text:
            text = text.split("tokens used", 1)[0].strip()
        if len(text) > max_chars:
            return text[:max_chars].rstrip() + "\n... truncated ..."
        return text or "(no output)"

    def _cancelled_summary(self, results: dict[str, CommandResult]) -> str:
        return f"""結論:
実行をキャンセルしました。APIキー課金や従量課金ルートには切り替えていません。

採用する方針:
必要であれば依頼内容を短くして再実行してください。

実行済み:
{self._join_lines(self._status_lines(results))}

次にやること:
再実行する場合は、依頼内容と対象プロジェクトを確認してから「3社に同時ブレスト依頼」を押してください。

リスク・注意点:
キャンセル時点までのログは `.ai-brainstorm/sessions/` に保存されています。
"""

    def _cancelled_result(
        self,
        session_id: str,
        workspace: WorkspaceManager,
        results: dict[str, CommandResult],
        warnings: list[str],
    ) -> BrainstormResult:
        final_answer = self._cancelled_summary(results)
        workspace.write_session_artifact(session_id, "final_answer.md", final_answer)
        workspace.append_history(session_id, final_answer)
        return BrainstormResult(
            session_id=session_id,
            context_pack="",
            prompts={},
            command_results=results,
            integrated_summary="",
            final_answer=final_answer,
            warnings=warnings,
        )

    def _cancelled_result_minimal(self) -> BrainstormResult:
        """Same shape as _cancelled_result(), for the two points before
        session_id/workspace exist yet, so there's nothing to persist."""
        return BrainstormResult(
            session_id=self._new_session_id(),
            context_pack="",
            prompts={},
            command_results={},
            integrated_summary="",
            final_answer=self._cancelled_summary({}),
        )

    def _partial_failure_notice(self, results: dict[str, CommandResult]) -> str:
        failed = {agent: result for agent, result in results.items() if not result.ok}
        if not failed:
            return ""
        return "注意:\n一部AIから回答を得られませんでした。\n" + self._join_lines(self._status_lines(failed))

    def _status_lines(self, results: dict[str, CommandResult]) -> list[str]:
        return [
            f"- {self._agent_label(agent)}: {self._status_label(result.status)}"
            for agent, result in results.items()
        ]

    def _join_lines(self, lines: list[str]) -> str:
        return "\n".join(lines)

    def _agent_label(self, agent: str) -> str:
        if agent.startswith("round"):
            parts = agent.split("_")
            if len(parts) == 3 and parts[0].startswith("round"):
                round_number = parts[0].replace("round", "", 1)
                base_agent = parts[1]
                role = parts[2]
                return f"Round {round_number} {self._agent_label(base_agent)} ({role.capitalize()})"
        return {
            "claude": "Claude",
            "gemini": "Antigravity",
            "codex": "Codex",
        }.get(agent, agent)

    def _base_agent(self, agent: str) -> str:
        if agent.startswith("round"):
            parts = agent.split("_")
            if len(parts) == 3:
                return parts[1]
        return agent

    def _display_command(self, command: list[str]) -> str:
        if not command:
            return "(none)"
        shortened = []
        for index, part in enumerate(command):
            if index == len(command) - 1 and len(part) > 160:
                shortened.append(part[:157] + "...")
            else:
                shortened.append(part)
        return " ".join(shortened)

    def _status_label(self, status: str) -> str:
        return cli_status.label_for(status)

    def _next_step_for(self, agent: str, status: str) -> str:
        return cli_status.next_step_for(
            self._agent_label(agent), self._base_agent(agent), status
        )

    def _is_cancelled(self, cancel_event: object | None) -> bool:
        is_set = getattr(cancel_event, "is_set", None)
        return bool(is_set and is_set())
