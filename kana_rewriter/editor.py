"""Shared text snapshots for clipboard-free editing (no Windows imports)."""
from dataclasses import dataclass

from .core import Capture
from .text import TextRegion, split_at_caret


def utf16_length(text):
    return len(text.encode("utf-16-le")) // 2


def simple_caret_text(text):
    """Conservative navigation set shared in meaning with the C++ worker."""
    return all(0x20 <= ord(c) <= 0x7e or c in "\n\r\t" or
               0x3041 <= ord(c) <= 0x3096 or 0x30a1 <= ord(c) <= 0x30fc or
               0x3400 <= ord(c) <= 0x9fff or c in "　。、！，．？" for c in text)


def python_offset(text, offset):
    payload = text.encode("utf-16-le")
    if not 0 <= offset <= len(payload) // 2:
        raise RuntimeError("入力欄の文字位置が本文の範囲外です")
    try:
        return len(payload[:offset * 2].decode("utf-16-le"))
    except UnicodeDecodeError as exc:
        raise RuntimeError("入力欄の文字位置がサロゲートペアの途中です") from exc


@dataclass(frozen=True)
class TextState:
    text: str
    start: int
    end: int


def make_capture(state, stamp, mode, config, token):
    if not 0 <= state.start <= state.end <= len(state.text):
        raise RuntimeError("入力欄の選択位置が不正です")
    if mode == "line":
        if state.start != state.end:
            raise RuntimeError("範囲が選択されています。選択範囲モードを使用してください")
        region = split_at_caret(state.text, state.end, config.conversion_delimiters,
                                config.stop_at_kanji, config.trailing_punctuation)
    else:
        if state.start == state.end:
            raise RuntimeError("変換する文字を選択してください")
        region = TextRegion(state.text[:state.start], state.text[state.start:state.end],
                            state.text[state.end:])
    if not region.target.strip() or len(region.target) > config.max_chars:
        raise ValueError("対象が空、または文字数上限を超えています")
    return Capture(state.text, stamp, region, state.end, token)


def replacement_plan(capture, result):
    region = capture.region
    start = len(region.left)
    end = start + len(region.target)
    expected = region.rebuild(result)
    caret = capture.caret + len(result) - len(region.target)
    return start, end, expected, caret
