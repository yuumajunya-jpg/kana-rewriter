import unittest
from unittest.mock import Mock

from kana_rewriter.core import Config, Converter, Capture, apply_result
from kana_rewriter.text import TextRegion, split_at_caret, zenz_prompt, jinen_prompt, katakana_reading


class ZenzTests(unittest.TestCase):
    def test_split_target_left_and_right(self):
        line = "歯が痛いので、はいしゃに行く"
        caret = len("歯が痛いので、はいしゃ")
        region = split_at_caret(line, caret)
        self.assertEqual(region, TextRegion("歯が痛", "いので、はいしゃ", "に行く"))
        self.assertEqual(region.rebuild("いので、歯医者"), "歯が痛いので、歯医者に行く")

    def test_scan_stops_at_boundaries(self):
        for boundary in ("。", "漢", "𠮷", "々", "\n"):
            line = "まえ" + boundary + "きょう"
            self.assertEqual(split_at_caret(line, len(line)).target, "きょう")

    def test_default_boundary_is_only_kanji_and_period(self):
        for middle in ("、", "?", "!", " ", "A", "カ"):
            line = "前。まえ" + middle + "きょう"
            self.assertEqual(split_at_caret(line, len(line)).target, "まえ" + middle + "きょう")

    def test_configured_boundaries_and_trailing_characters_are_independent(self):
        line = "前。まえ、きょう?"
        self.assertEqual(split_at_caret(line, len(line)), TextRegion("前。", "まえ、きょう", "?"))
        self.assertEqual(split_at_caret(line, len(line), conversion_delimiters="。、"),
                         TextRegion("前。まえ、", "きょう", "?"))
        self.assertEqual(split_at_caret(line, len(line), trailing_punctuation="。"),
                         TextRegion("前。", "まえ、きょう?", ""))
        self.assertEqual(split_at_caret("前きょう", 4, stop_at_kanji=False),
                         TextRegion("", "前きょう", ""))

    def test_simple_rule_includes_particles(self):
        self.assertEqual(split_at_caret("私はきょう", 5), TextRegion("私", "はきょう", ""))

    def test_no_hiragana_at_caret(self):
        for line, caret in (("今日", 2), ("今日。", 3), ("？！", 2), ("。", 1), ("", 0), ("きょう", 0)):
            with self.assertRaises(ValueError):
                split_at_caret(line, caret)

    def test_trailing_punctuation_is_retained(self):
        for punctuation in ("。", "、", "？", "?", "！", "!", ",", ".", "，", "．", "?!", "。。"):
            with self.subTest(punctuation=punctuation):
                text = "前。きょう" + punctuation + "後"
                region = split_at_caret(text, len(text) - 1)
                self.assertEqual(region, TextRegion("前。", "きょう", punctuation + "後"))
                self.assertEqual(region.rebuild("今日"), "前。今日" + punctuation + "後")

    def test_sentence_final_period_is_preserved_and_scan_stops_at_boundary(self):
        for left in ("まえ。", "漢"):
            with self.subTest(left=left):
                line = left + "さんぽでもしようか。後ろ"
                caret = len(left + "さんぽでもしようか。")
                region = split_at_caret(line, caret)
                self.assertEqual(region, TextRegion(left, "さんぽでもしようか", "。後ろ"))
                self.assertEqual(region.rebuild("散歩でもしようか"), left + "散歩でもしようか。後ろ")

    def test_sentence_final_period_at_end_of_line(self):
        line = "今日はいい天気だ。さんぽでもしようか。"
        region = split_at_caret(line, len(line))
        self.assertEqual(region, TextRegion("今日はいい天気だ。", "さんぽでもしようか", "。"))
        self.assertEqual(region.rebuild("散歩でもしようか"), "今日はいい天気だ。散歩でもしようか。")

    def test_caret_bounds(self):
        for caret in (-1, 10):
            with self.assertRaises(ValueError):
                split_at_caret("あ", caret)

    def test_exact_v32_protocol(self):
        self.assertEqual(zenz_prompt("はいしゃ", "歯が痛いので、", "に行く"),
                         "\uee02歯が痛いので、\uee07に行く\uee00ハイシャ\uee01")
        self.assertEqual(zenz_prompt("きょう"), "\uee00キョウ\uee01")
        self.assertEqual(zenz_prompt("あ", right="後"), "\uee07後\uee00ア\uee01")

    def test_context_is_trimmed_nearest_to_target(self):
        self.assertEqual(zenz_prompt("あ", "012345", "abcdef", 3),
                         "\uee02345\uee07abc\uee00ア\uee01")
        self.assertEqual(zenz_prompt("あ", "左", "右", 0), "\uee00ア\uee01")

    def test_normalize_reading_without_normalizing_context(self):
        self.assertEqual(katakana_reading("か\u3099っこうヴァーゝゞ"), "ガッコウヴァーヽヾ")
        self.assertIn("Ａ", zenz_prompt("あ", "Ａ"))

    def test_reject_nonreading(self):
        for reading in ("今日", "きょう。", "きょう です", "ｷｮｳ", ""):
            with self.assertRaises(ValueError):
                katakana_reading(reading)

    def test_reject_protocol_tokens_in_context(self):
        for marker in ("\uee00", "\uee01", "\uee02", "\uee07", "</s>"):
            with self.assertRaises(ValueError):
                zenz_prompt("あ", left=marker)

    def test_raw_completion_and_no_chat_template(self):
        client = Converter(Config(model_format="zenz_v3_2"))
        client.llm = Mock()
        client.llm.tokenize.return_value = [100, 101, 102]
        client.llm.create_completion.return_value = {
            "choices": [{"text": "歯医者", "finish_reason": "stop"}]}
        self.assertEqual(client.convert("はいしゃ", "歯が痛いので、", "に行く"), "歯医者")
        client.llm.tokenize.assert_called_once_with(
            "\uee02歯が痛いので、\uee07に行く\uee00ハイシャ\uee01".encode("utf-8"),
            add_bos=False, special=True)
        args = client.llm.create_completion.call_args.kwargs
        self.assertEqual(args["prompt"], [100, 101, 102])
        self.assertEqual(args["temperature"], 0)
        self.assertEqual(args["repeat_penalty"], 1.0)
        self.assertEqual(args["stop"], ["</s>"])
        client.llm.create_chat_completion.assert_not_called()

    def test_jinen_left_only_protocol(self):
        self.assertEqual(jinen_prompt("はいしゃ", "歯が痛いので、"),
                         "\uee02歯が痛いので、\uee00ハイシャ\uee01")
        self.assertEqual(jinen_prompt("きょう"), "\uee02\uee00キョウ\uee01")
        client = Converter(Config())
        client.llm = Mock()
        client.llm.create_completion.return_value = {
            "choices": [{"text": "歯医者", "finish_reason": "stop"}]}
        self.assertEqual(client.convert("はいしゃ", "歯が痛いので、", "右文脈"), "歯医者")
        actual = client.llm.tokenize.call_args.args[0].decode("utf-8")
        self.assertEqual(actual, "\uee02歯が痛いので、\uee00ハイシャ\uee01")
        self.assertNotIn("\uee07", actual)

    def test_completion_rejects_truncation_and_markers(self):
        client = Converter(Config())
        client.llm = Mock()
        for text, reason in (("歯医者", "length"), ("\uee02歯医者", "stop"),
                             ("歯医者\n", "stop"), ("歯 医者", "stop")):
            client.llm.create_completion.return_value = {
                "choices": [{"text": text, "finish_reason": reason}]}
            with self.assertRaises(ValueError):
                client.convert("はいしゃ")

    def test_replacement_preserves_original_context_not_trimmed_prompt(self):
        desktop = Mock()
        region = TextRegion("長い文脈。歯が痛いので、", "はいしゃ", "に行く。後ろの文")
        original = region.rebuild(region.target)
        capture = Capture(original, ("window", 1, 2), region)
        desktop.stamp.return_value = capture.stamp
        desktop.copy_selection.return_value = original
        self.assertEqual(capture.source, "はいしゃ")
        self.assertTrue(apply_result(desktop, capture, "歯医者"))
        desktop.replace.assert_called_once_with(region.rebuild("歯医者"), "window",
                                              caret=len(region.left + "歯医者"))

    def test_region_no_change_does_not_reinsert_line(self):
        desktop = Mock()
        region = TextRegion("前", "です", "後")
        capture = Capture(region.rebuild(region.target), None, region)
        self.assertFalse(apply_result(desktop, capture, "です"))
        self.assertEqual(desktop.mock_calls, [])
