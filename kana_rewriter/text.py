"""Simple caret-based target extraction and zenz input formatting."""
from dataclasses import dataclass
import unicodedata


MARKERS = tuple(chr(0xEE00 + i) for i in range(8))


def is_hiragana(character: str) -> bool:
    return "\u3041" <= character <= "\u3096" or character in "\u3099\u309a\u309d\u309eー"


@dataclass(frozen=True)
class TextRegion:
    left: str
    target: str
    right: str

    def rebuild(self, converted: str) -> str:
        return self.left + converted + self.right


def split_at_caret(text: str, caret: int) -> TextRegion:
    """Take the contiguous hiragana run immediately before the caret.

    Kanji, punctuation (including 。), whitespace, Latin letters and katakana
    stop the scan. No morphology or particle detection is attempted.
    """
    if not 0 <= caret <= len(text):
        raise ValueError("カーソル位置が文字列の範囲外です")
    start = caret
    while start > 0 and is_hiragana(text[start - 1]):
        start -= 1
    if start == caret:
        raise ValueError("カーソル直前に変換対象のひらがながありません")
    return TextRegion(text[:start], text[start:caret], text[caret:])


def katakana_reading(text: str) -> str:
    # NFC joins decomposed dakuten without normalizing the surrounding context.
    text = unicodedata.normalize("NFC", text)
    result = []
    for character in text:
        if "\u3041" <= character <= "\u3096" or character in "ゝゞ":
            character = chr(ord(character) + 0x60)
        if not ("\u30a1" <= character <= "\u30fa" or character in "ーヽヾ"):
            raise ValueError("変換専用モデルの対象はひらがな・全角カタカナの読みだけにしてください")
        result.append(character)
    if not result:
        raise ValueError("変換対象が空です")
    return "".join(result)


def zenz_prompt(source: str, left: str = "", right: str = "", context_chars: int = 64) -> str:
    # Reject protocol markers even if they occur outside the retained context.
    for part in (source, left, right):
        if any(marker in part for marker in MARKERS) or "</s>" in part:
            raise ValueError("テキストにモデルの制御マーカーが含まれています")
    left = left[-context_chars:] if context_chars else ""
    right = right[:context_chars] if context_chars else ""
    prompt = ("\uee02" + left) if left else ""
    if right:
        prompt += "\uee07" + right
    return prompt + "\uee00" + katakana_reading(source) + "\uee01"


def jinen_prompt(source: str, left: str = "", context_chars: int = 64) -> str:
    # jinen-v1 documents LEFT context only. Never send the zenz-v3.2 right marker.
    prompt = zenz_prompt(source, left, context_chars=context_chars)
    return prompt if prompt.startswith("\uee02") else "\uee02" + prompt
