"""Simple caret-based target extraction and zenz input formatting."""
from dataclasses import dataclass
import unicodedata


MARKERS = tuple(chr(0xEE00 + i) for i in range(8))
TRAILING_PUNCTUATION = "。、？！?!,.，．"


def is_hiragana(character: str) -> bool:
    return "\u3041" <= character <= "\u3096" or character in "\u3099\u309a\u309d\u309eー"


def is_kanji(character: str) -> bool:
    name = unicodedata.name(character, "")
    return name.startswith(("CJK UNIFIED IDEOGRAPH-", "CJK COMPATIBILITY IDEOGRAPH-")) or character in "々〆〇"


def reading_spans(text: str):
    """Split a mixed selection into contiguous kana spans without morphology."""
    start = None
    for index, character in enumerate(text):
        kana = is_hiragana(character) or "\u30a1" <= character <= "\u30fa" or character in "ヽヾ"
        if kana and start is None:
            start = index
        elif not kana and start is not None:
            yield start, index
            start = None
    if start is not None:
        yield start, len(text)


@dataclass(frozen=True)
class TextRegion:
    left: str
    target: str
    right: str

    def rebuild(self, converted: str) -> str:
        return self.left + converted + self.right


def split_at_caret(text: str, caret: int, conversion_delimiters: str = "。",
                   stop_at_kanji: bool = True,
                   trailing_punctuation: str = TRAILING_PUNCTUATION) -> TextRegion:
    """Scan left to configured boundaries, retaining trailing punctuation."""
    if not 0 <= caret <= len(text):
        raise ValueError("カーソル位置が文字列の範囲外です")
    # Keep punctuation verbatim in the right context, including runs such as ?!.
    end = caret
    while end > 0 and text[end - 1] in trailing_punctuation:
        end -= 1
    start = end
    while start > 0:
        character = text[start - 1]
        if (character in conversion_delimiters or character in "\r\n"
                or (stop_at_kanji and is_kanji(character))):
            break
        start -= 1
    if not any(reading_spans(text[start:end])):
        raise ValueError("区切り内に変換対象のかながありません")
    return TextRegion(text[:start], text[start:end], text[end:])


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


def jinen_v2_prompt(source: str, left: str = "", context_chars: int = 64) -> str:
    """Use v2's left-only protocol and normalize only the model input."""
    prompt = unicodedata.normalize("NFKC", zenz_prompt(source, left, context_chars=context_chars))
    # Normalization can turn full-width context into an EOS token.
    if "</s>" in prompt:
        raise ValueError("テキストにモデルの制御マーカーが含まれています")
    return prompt
