from __future__ import annotations

import threading
import unittest

from src.services import chair_output
from src.services.refinement_loop import JAPANESE_REMINDER, RefinementLoop

# Verbatim excerpts from the 32-run measurement (2026-08-27). Using the real
# text matters: every one of these shapes was produced by the pinned chair
# model against the app's own prompt, and each one used to be handled wrong.

REAL_ENGLISH_ANSWER = """### Request Summary
Review the "run state machine" design and identify necessary fixes before \
implementing approval gates, specifically focusing on how to handle late status reports.

### Integrated Proposal

**Conclusion**
The core issue is the handling of late status reports in a parallel environment. \
While the current system logs these events, a decision is needed on whether this \
should be a visible warning or a silent discard.

**Adopted Policy**
*Pending User Decision:* Choose between **Visible Warning** (Claude) or \
**Silent Discard** (Gemini).
"""

REAL_HEADING_VARIANTS = {
    # All three were written for the same requested 意見が割れたこと section.
    "意見が割れたこと": "### 意見が割れたこと\n- 状態の粒度をどうするか。\n",
    "意見の相違": "*   **意見の相違:** なし（全エージェントが一致）。\n",
    "意見の対立": "*   **意見の対立**: 実装順序（ゲート先行 vs テスト先行）。\n",
}

REAL_FINAL_ANSWER_RENAMED = """### まとめ
状態機械の導入は妥当だが、承認ゲート前に握り潰しの分離が要る。

### 次のステップ
- 到達可能性のBFS検証を追加する。
- apply_run_state を表示用と権限用に分ける。
"""


class JapaneseDetectionTest(unittest.TestCase):
    def test_the_real_english_answer_scores_zero(self) -> None:
        self.assertEqual(chair_output.japanese_ratio(REAL_ENGLISH_ANSWER), 0.0)
        self.assertFalse(chair_output.looks_japanese(REAL_ENGLISH_ANSWER))

    def test_a_real_japanese_answer_is_far_above_the_threshold(self) -> None:
        ratio = chair_output.japanese_ratio(REAL_FINAL_ANSWER_RENAMED)
        self.assertTrue(chair_output.looks_japanese(REAL_FINAL_ANSWER_RENAMED))
        # Measured floor across 31 Japanese answers was 0.226.
        self.assertGreater(ratio, 0.15)

    def test_the_threshold_sits_in_the_measured_gap(self) -> None:
        """English scored 0.000 and the lowest Japanese answer 0.226. A
        threshold outside that gap would start misclassifying real output."""
        self.assertGreater(chair_output.MIN_JAPANESE_RATIO, 0.0)
        self.assertLess(chair_output.MIN_JAPANESE_RATIO, 0.226)

    def test_kanji_alone_does_not_count_as_japanese(self) -> None:
        """Kanji appears in English prose quoting Japanese identifiers, so it
        cannot separate the languages — only kana can."""
        self.assertFalse(chair_output.looks_japanese("Set the 結論 field. " * 20))

    def test_empty_text_is_not_japanese_and_does_not_divide_by_zero(self) -> None:
        self.assertEqual(chair_output.japanese_ratio(""), 0.0)
        self.assertFalse(chair_output.looks_japanese(""))


class SectionMatchingTest(unittest.TestCase):
    def test_every_observed_heading_variant_is_recognised(self) -> None:
        for label, text in REAL_HEADING_VARIANTS.items():
            with self.subTest(variant=label):
                self.assertTrue(chair_output.has_section(text, "意見が割れたこと"))

    def test_markdown_decoration_does_not_hide_a_heading(self) -> None:
        for decorated in (
            "### 結論\n本文",
            "**結論**\n本文",
            "*   **結論**: 本文",
            "## 結 論\n本文",
            "結論:\n本文",
        ):
            with self.subTest(form=decorated):
                self.assertTrue(chair_output.has_section(decorated, "結論"))

    def test_a_compound_heading_still_counts(self) -> None:
        """Real output: 意見の相違・対立点. The trailing ・対立点 made an
        earlier version read the section as absent."""
        line = "*   **意見の相違・対立点**:\n    *   Gemini は反対している。"
        self.assertTrue(chair_output.has_section(line, "意見が割れたこと"))

    def test_a_parenthetical_qualifier_still_counts(self) -> None:
        """Real output: 採用する方針（選択肢）."""
        line = "**採用する方針（選択肢）**\n1. Claude案\n2. Gemini案"
        self.assertTrue(chair_output.has_section(line, "採用する方針"))

    def test_a_sentence_opening_with_the_word_is_not_a_heading(self) -> None:
        self.assertFalse(chair_output.has_section("結論を先に書くべきである。", "結論"))
        self.assertFalse(
            chair_output.has_section("- 次の作業には触れていない。", "次にやること")
        )

    def test_synonyms_count_as_the_requested_section(self) -> None:
        self.assertTrue(chair_output.has_section("### まとめ\n本文", "結論"))
        self.assertTrue(chair_output.has_section("### 次のステップ\n本文", "次にやること"))

    def test_an_absent_section_is_still_reported_absent(self) -> None:
        answer = "### 結論\n本文だけで、次の作業には触れていない。"
        self.assertEqual(
            chair_output.missing_sections(answer, ("結論", "次にやること")),
            ["次にやること"],
        )

    def test_normalisation_does_not_join_separate_lines(self) -> None:
        """Stripping spaces must not fabricate a heading out of two lines that
        each held one half of the word."""
        self.assertFalse(chair_output.has_section("結\n論\n本文", "結論"))

    def test_an_unknown_section_name_falls_back_to_a_literal_match(self) -> None:
        self.assertTrue(chair_output.has_section("### 独自見出し\n本文", "独自見出し"))
        self.assertFalse(chair_output.has_section("### 別の見出し\n本文", "独自見出し"))


class FinalAnswerAcceptanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.loop = RefinementLoop.__new__(RefinementLoop)

    def test_a_renamed_heading_no_longer_throws_the_answer_away(self) -> None:
        """Regression: the exact-substring check rejected this real answer and
        replaced it with a canned summary, because the chair wrote まとめ and
        次のステップ instead of 結論 and 次にやること."""
        self.assertTrue(self.loop._final_answer_uses_success(REAL_FINAL_ANSWER_RENAMED))

    def test_the_canonical_headings_are_still_accepted(self) -> None:
        answer = "結論:\n" + "状態機械を導入した。" * 10 + "\n次にやること:\n- テストを足す。"
        self.assertTrue(self.loop._final_answer_uses_success(answer))

    def test_an_answer_missing_a_required_section_is_still_rejected(self) -> None:
        answer = "### 結論\n" + "状態機械を導入した。" * 10
        self.assertFalse(self.loop._final_answer_uses_success(answer))

    def test_a_stale_permission_seeking_answer_is_still_rejected(self) -> None:
        answer = "### 結論\n調査を開始します。\n### 次にやること\n- 待機"
        self.assertFalse(self.loop._final_answer_uses_success(answer))


class _RecordingChair:
    def __init__(self, *replies: str | None) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def chat(self, system_prompt, user_prompt, max_tokens=1200, **kwargs):
        self.prompts.append(user_prompt)
        return self.replies.pop(0) if self.replies else None


class ChairLanguageRetryTest(unittest.TestCase):
    def _loop(self, *replies: str | None) -> RefinementLoop:
        loop = RefinementLoop.__new__(RefinementLoop)
        loop.chair = _RecordingChair(*replies)
        return loop

    def test_a_japanese_answer_is_returned_without_a_second_call(self) -> None:
        loop = self._loop("### 結論\n状態機械を導入した。")
        answer = loop._chair_chat_in_japanese("prompt", 1200)
        self.assertIn("結論", answer)
        self.assertEqual(len(loop.chair.prompts), 1)

    def test_an_english_answer_triggers_exactly_one_retry(self) -> None:
        loop = self._loop(REAL_ENGLISH_ANSWER, "### 結論\n日本語で書き直した。")
        answer = loop._chair_chat_in_japanese("prompt", 1200)
        self.assertIn("日本語で書き直した", answer)
        self.assertEqual(len(loop.chair.prompts), 2)
        self.assertIn(JAPANESE_REMINDER.strip(), loop.chair.prompts[1])

    def test_english_twice_keeps_the_answer_rather_than_discarding_it(self) -> None:
        """A correct answer in the wrong language still beats the canned
        fallback, so the second English reply is returned, not dropped."""
        notes: list[str] = []
        loop = self._loop(REAL_ENGLISH_ANSWER, "### Conclusion\nStill English.")
        answer = loop._chair_chat_in_japanese("prompt", 1200, progress=notes.append)
        self.assertEqual(answer, "### Conclusion\nStill English.")
        self.assertEqual(len(loop.chair.prompts), 2)
        self.assertTrue(any("日本語" in note for note in notes))

    def test_the_retry_is_announced_to_the_user(self) -> None:
        notes: list[str] = []
        loop = self._loop(REAL_ENGLISH_ANSWER, "### 結論\n日本語。")
        loop._chair_chat_in_japanese("prompt", 1200, progress=notes.append)
        self.assertTrue(any("再依頼" in note for note in notes))

    def test_cancellation_between_the_two_calls_prevents_the_retry(self) -> None:
        cancel_event = threading.Event()
        cancel_event.set()
        loop = self._loop(REAL_ENGLISH_ANSWER, "### 結論\n届かないはず。")
        answer = loop._chair_chat_in_japanese("prompt", 1200, cancel_event)
        self.assertIsNone(answer)
        self.assertEqual(len(loop.chair.prompts), 1)

    def test_no_answer_at_all_does_not_trigger_a_retry(self) -> None:
        """An unavailable chair is not a language problem; retrying would just
        wait out a second timeout."""
        loop = self._loop(None)
        self.assertIsNone(loop._chair_chat_in_japanese("prompt", 1200))
        self.assertEqual(len(loop.chair.prompts), 1)


if __name__ == "__main__":
    unittest.main()
