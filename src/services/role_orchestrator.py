from __future__ import annotations

from dataclasses import dataclass


AGENT_ORDER = ("claude", "gemini", "codex")
ROLE_ORDER = ("author", "critic", "verifier")

ROLE_LABELS = {
    "author": "Author",
    "critic": "Critic",
    "verifier": "Verifier",
}

ROLE_INSTRUCTIONS = {
    "author": (
        "Create the best actionable proposal for the current request. "
        "Cover the intended behavior, implementation shape, test strategy, "
        "risks, and concrete next steps."
    ),
    "critic": (
        "Find omissions, contradictions, unsafe assumptions, UX risks, "
        "edge cases, and maintainability risks. Separate blocking concerns "
        "from minor improvements."
    ),
    "verifier": (
        "Check whether the proposal can actually be implemented and tested. "
        "Identify affected files, likely failure modes, validation commands, "
        "and acceptance criteria."
    ),
}

ROUND_FOCUS = {
    1: "Initial proposal and first-pass risk discovery.",
    2: "Cross-review the previous round and challenge weak assumptions.",
    3: "Converge on implementation readiness, tests, and remaining human decisions.",
}


@dataclass(frozen=True)
class RoleAssignment:
    agent: str
    role: str
    round_number: int

    @property
    def role_label(self) -> str:
        return ROLE_LABELS[self.role]

    @property
    def instruction(self) -> str:
        return ROLE_INSTRUCTIONS[self.role]


@dataclass(frozen=True)
class RoleRound:
    number: int
    focus: str
    assignments: tuple[RoleAssignment, ...]

    def assignment_for(self, agent: str) -> RoleAssignment | None:
        for assignment in self.assignments:
            if assignment.agent == agent:
                return assignment
        return None


class RoleOrchestrator:
    def build_plan(self, available_agents: set[str], automation_level: int) -> list[RoleRound]:
        agents = [agent for agent in AGENT_ORDER if agent in available_agents]
        if not agents:
            return []
        round_count = self.round_count_for_level(automation_level)
        return [self._build_round(round_index, agents) for round_index in range(round_count)]

    def round_count_for_level(self, automation_level: int) -> int:
        """Rounds of Author/Critic/Verifier rotation for a level.

        The mapping lives in `autonomy_controller` now, so the count a level
        promises and the count it runs cannot drift apart.
        """
        from src.services import autonomy_controller

        return autonomy_controller.capabilities_for(automation_level).rounds

    def render_plan(self, rounds: list[RoleRound]) -> str:
        lines = ["# Role Rotation Plan", ""]
        for role_round in rounds:
            lines.extend(
                [
                    f"## Round {role_round.number}",
                    role_round.focus,
                    "",
                ]
            )
            for assignment in role_round.assignments:
                lines.append(f"- {assignment.agent}: {assignment.role_label}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _build_round(self, round_index: int, agents: list[str]) -> RoleRound:
        round_number = round_index + 1
        assignments = []
        for agent_index, agent in enumerate(agents):
            role_index = (agent_index - round_index) % len(ROLE_ORDER)
            assignments.append(
                RoleAssignment(
                    agent=agent,
                    role=ROLE_ORDER[role_index],
                    round_number=round_number,
                )
            )
        return RoleRound(
            number=round_number,
            focus=ROUND_FOCUS.get(round_number, ROUND_FOCUS[3]),
            assignments=tuple(assignments),
        )
