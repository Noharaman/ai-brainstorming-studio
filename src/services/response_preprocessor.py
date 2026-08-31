from __future__ import annotations

import re

from src.models import CommandResult


class ResponsePreprocessor:
    def summarize_results(
        self,
        results: dict[str, CommandResult],
        max_chars_per_agent: int = 1500,
        max_total_chars: int = 6000,
    ) -> str:
        sections: list[str] = []
        for agent, result in results.items():
            text = result.stdout.strip() if result.stdout.strip() else result.stderr.strip()
            text = self._strip_large_code_blocks(text)
            if len(text) > max_chars_per_agent:
                text = text[:max_chars_per_agent] + "\n... [truncated for context size] ..."
            sections.append(
                f"## {agent}\nstatus: {result.status}\nok: {result.ok}\n\n{text or '(no output)'}"
            )
        output = "\n\n".join(sections)
        if len(output) > max_total_chars:
            output = output[:max_total_chars] + "\n\n... [remaining outputs truncated for context size] ..."
        return output

    def _strip_large_code_blocks(self, text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            block = match.group(0)
            if len(block) > 1200:
                return "```text\n[large code/log block omitted for chair context]\n```"
            return block

        return re.sub(r"```.*?```", replace, text, flags=re.DOTALL)

