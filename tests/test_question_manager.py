import unittest

from src.services.question_manager import QuestionManager


class QuestionExtractionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = QuestionManager()

    def test_only_lines_ending_in_a_question_mark_are_returned(self) -> None:
        answer = (
            "結論:\n"
            "既存の設定は確認済みです。\n"
            "採用する方針:\n"
            "この判断はユーザーに委ねます。\n"
            "通知の対象イベントはどれにしますか？\n"
        )
        self.assertEqual(
            self.manager.extract_questions(answer),
            ["通知の対象イベントはどれにしますか？"],
        )

    def test_prose_containing_the_old_keywords_is_no_longer_swept_up(self) -> None:
        """The previous rule matched any line containing 確認 / 判断 / ?."""
        answer = "実行済み:\n- ログイン状態を確認しました\n- 影響範囲を判断済みです\n"
        self.assertEqual(self.manager.extract_questions(answer), [])

    def test_bullets_and_numbering_are_stripped(self) -> None:
        answer = "1. 署名は必須ですか？\n- タブ遷移も実装しますか？\n"
        self.assertEqual(
            self.manager.extract_questions(answer),
            ["署名は必須ですか？", "タブ遷移も実装しますか？"],
        )

    def test_duplicates_differing_only_by_decoration_collapse(self) -> None:
        answer = "- 署名は必須ですか？\n1. 署名は必須ですか？\n**署名は必須ですか？**\n"
        self.assertEqual(self.manager.extract_questions(answer), ["署名は必須ですか？"])

    def test_ascii_question_marks_are_supported(self) -> None:
        answer = "Should we sign the bundle?\n"
        self.assertEqual(
            self.manager.extract_questions(answer), ["Should we sign the bundle?"]
        )

    def test_a_bare_question_mark_is_not_a_question(self) -> None:
        self.assertEqual(self.manager.extract_questions("?\n？\n"), [])

    def test_the_limit_is_respected(self) -> None:
        answer = "\n".join(f"質問{i}ですか？" for i in range(20))
        self.assertEqual(len(self.manager.extract_questions(answer)), 10)

    def test_an_answer_with_no_questions_returns_empty(self) -> None:
        self.assertEqual(self.manager.extract_questions("結論:\n問題ありません。\n"), [])


if __name__ == "__main__":
    unittest.main()
