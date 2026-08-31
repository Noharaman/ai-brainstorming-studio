from __future__ import annotations

import re


#: Leading list/enumeration decoration the chair puts on its own question lines
#: ("1. ", "- ", "* ", "1) ", "・"). Stripped so two identical questions written
#: with different bullets collapse into one entry.
_DECORATION = re.compile(r"^\s*(?:[-*・>]+|\d+[.)]|[（(]\d+[）)])\s*")

#: Markdown emphasis around a whole question ("**...**"). The chair bolds
#: questions inconsistently, which would otherwise defeat deduplication.
_EMPHASIS = re.compile(r"^\*\*(.*)\*\*$|^__(.*)__$")


class QuestionManager:
    """Pull the questions the chair actually asked out of its final answer.

    Deliberately narrow: a line counts only if it *ends* in a question mark.
    The previous rule accepted any line containing 確認 / 判断 / ?, which swept
    up ordinary prose ("既存の設定は確認済みです") and reported it to the user
    as an open question.
    """

    def extract_questions(self, final_answer: str, limit: int = 10) -> list[str]:
        questions: list[str] = []
        seen: set[str] = set()

        for raw_line in final_answer.splitlines():
            question = self._normalize(raw_line)
            if not question or not self._is_question(question):
                continue
            key = question.casefold()
            if key in seen:
                continue
            seen.add(key)
            questions.append(question)
            if len(questions) >= limit:
                break

        return questions

    def _normalize(self, line: str) -> str:
        text = _DECORATION.sub("", line).strip()
        match = _EMPHASIS.match(text)
        if match:
            text = (match.group(1) or match.group(2) or "").strip()
        return text

    def _is_question(self, text: str) -> bool:
        # A heading that merely announces a question section is not itself a
        # question, and neither is a lone punctuation mark.
        stripped = text.rstrip("?？ 　")
        if not stripped:
            return False
        return text.endswith("?") or text.endswith("？")
