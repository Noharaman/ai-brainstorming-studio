"""Checks on what the local chair model actually returned.

Two failure modes were measured across 32 runs against the real integrate
prompt:

1. **The heading wording drifts.** The same model wrote 意見が割れたこと,
   意見の相違 and 意見の対立 for the same requested section, and decorated it
   variously as `### 結論`, `**結論**` and `*   **結論**:`. Any check that
   asks `"結論" in answer` is testing the model's word choice, not whether it
   answered.
2. **The answer occasionally comes back in English.** One run in sixteen
   returned the whole document in English including the headings.

Both used to land in the same place: `_final_answer_uses_success()` returned
False and the app threw the chair's answer away, replacing it with a canned
summary and telling the user nothing about why.

Language and structure are checked separately on purpose — an English answer
with all the right sections is a language problem, not a malformed one.
"""

from __future__ import annotations

import re

# Hiragana, katakana and the長音 mark. Kanji is deliberately excluded: it also
# appears in Chinese and in Japanese technical nouns quoted inside English
# prose, so it does not separate the two languages. Kana does.
_KANA = re.compile(r"[ぁ-んァ-ヶー]")

# Leading markdown/list decoration and numbering on a heading line, e.g.
# "### ", "*   **", "- ", "#### 1. ". Stripped before matching so the heading
# text itself can be compared.
_LEADING = re.compile(r"^[#*_`~\-–—・>\s　]*(?:\d+[.)]\s*)?[#*_`~\s　]*")

# Emphasis left dangling after the heading word ("**結論**:" -> "**:").
_TRAILING_EMPHASIS = re.compile(r"^[*_`~]+")

# Spacing inside a single heading line. Removed so "結 論" still matches, but
# only ever within one line, so two lines each holding half a word can never
# be joined into a heading that was never written.
_INNER_SPACE = re.compile(r"[\s　]+")

# What may separate a heading word from whatever follows it on the same line.
# Beyond plain punctuation this covers two forms the chair actually produced:
# a compound heading ("意見の相違・対立点") and a parenthetical qualifier
# ("採用する方針（選択肢）"). Without them both were read as "section absent".
_HEADING_DELIMITERS = (
    ":", "：", ")", "）", "]", "】", "。", ".", "-", "—",
    "・", "、", "/", "／", "(", "（", "[", "【", "&", "＆",
)

#: Measured separation on real output: every Japanese answer scored >= 0.226,
#: the one English answer scored exactly 0.000. 0.05 sits in an empty gap an
#: order of magnitude wide, so it tolerates an answer that is mostly code or
#: identifiers without ever passing English prose.
MIN_JAPANESE_RATIO = 0.05

#: Accepted wordings per requested section. Language-agnostic: an English
#: heading still counts as "the section is present", because whether the answer
#: is in Japanese is `looks_japanese()`'s job, not this one's.
SECTION_VARIANTS: dict[str, tuple[str, ...]] = {
    # 統合提案 is what the chair writes when it leads with the proposal itself
    # instead of labelling a conclusion; observed repeatedly in real output.
    "結論": ("結論", "まとめ", "総括", "要約", "統合提案", "conclusion", "summary"),
    "採用する方針": ("採用する方針", "方針", "採用方針", "adoptedpolicy", "policy"),
    "次にやること": (
        "次にやること",
        "次のステップ",
        "次の作業",
        "次のアクション",
        "ネクストステップ",
        "今後の対応",
        "nextsteps",
        "nextactions",
    ),
    "意見が割れたこと": (
        "意見が割れたこと",
        "意見が割れた点",
        "意見の相違",
        "意見の対立",
        "対立点",
        "相違点",
        "disagreement",
        "disagreements",
    ),
    "あなたに確認したいこと": (
        "あなたに確認したいこと",
        "確認したいこと",
        "ユーザー確認が必要な点",
        "確認が必要な点",
        "要確認",
        "userconfirmation",
    ),
}


def japanese_ratio(text: str) -> float:
    """Share of the text made of kana. 0.0 for English prose."""
    if not text:
        return 0.0
    return len(_KANA.findall(text)) / len(text)


def looks_japanese(text: str, minimum: float = MIN_JAPANESE_RATIO) -> bool:
    return japanese_ratio(text) >= minimum


def _heading_text(line: str) -> str:
    """The heading candidate on `line`, with its decoration removed.

    Matching is anchored to the start of a line rather than searched anywhere
    in the answer. Searching the whole text meant ordinary prose satisfied the
    check — "次の作業には触れていない" counted as the 次にやること section being
    present, which is precisely backwards.
    """
    return _INNER_SPACE.sub("", _LEADING.sub("", line)).lower()


def _line_is_heading_for(line: str, variant: str) -> bool:
    heading = _heading_text(line)
    variant = _INNER_SPACE.sub("", variant).lower()
    if not variant or not heading.startswith(variant):
        return False
    rest = _TRAILING_EMPHASIS.sub("", heading[len(variant):])
    # Nothing after the word, or a delimiter introducing the body. A sentence
    # that merely opens with the word ("結論を先に書く") is not a heading.
    return not rest or rest.startswith(_HEADING_DELIMITERS)


def has_section(answer: str, section: str) -> bool:
    """Whether `answer` presents the requested section as a heading, under any
    accepted wording. Unknown section names fall back to a literal match."""
    variants = SECTION_VARIANTS.get(section, (section,))
    return any(
        _line_is_heading_for(line, variant)
        for line in answer.splitlines()
        for variant in variants
    )


def missing_sections(answer: str, sections: tuple[str, ...]) -> list[str]:
    return [section for section in sections if not has_section(answer, section)]
