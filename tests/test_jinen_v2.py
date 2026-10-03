import unittest
from unittest.mock import Mock

from kana_rewriter.core import Config, Converter
from kana_rewriter.text import jinen_prompt, jinen_v2_prompt


class JinenV2Tests(unittest.TestCase):
    def test_normalizes_context_and_reading_and_handles_empty_context(self):
        self.assertEqual(jinen_v2_prompt("か\u3099っこう", "ＡＢＣ①ｶﾞ"),
                         "\uee02ABC1ガ\uee00ガッコウ\uee01")
        self.assertEqual(jinen_v2_prompt("きょう"), "\uee00キョウ\uee01")
        self.assertEqual(jinen_v2_prompt("きょう", "左", 0), "\uee00キョウ\uee01")
        self.assertEqual(jinen_v2_prompt("あ", "前ＡＢＣ", 2),
                         "\uee02BC\uee00ア\uee01")
        self.assertEqual(jinen_prompt("あ", "Ａ"), "\uee02Ａ\uee00ア\uee01")

    def test_rejects_control_tokens_before_and_after_normalization(self):
        for context in ("\uee02", "</s>", "＜／ｓ＞"):
            with self.subTest(context=context), self.assertRaises(ValueError):
                jinen_v2_prompt("あ", context)

    def test_local_completion_uses_normalized_tokens_and_greedy_settings(self):
        client = Converter(Config(model_format="jinen_v2"))
        client.llm = Mock()
        client.llm.tokenize.return_value = [10, 11, 12]
        client.llm.create_completion.return_value = {
            "choices": [{"text": "歯医者", "finish_reason": "stop"}]}
        self.assertEqual(client.convert("はいしゃ", "Ａ歯が痛いので、", "右文脈"), "歯医者")
        client.llm.tokenize.assert_called_once_with(
            "\uee02A歯が痛いので、\uee00ハイシャ\uee01".encode("utf-8"),
            add_bos=False, special=True)
        request = client.llm.create_completion.call_args.kwargs
        self.assertEqual(request["prompt"], [10, 11, 12])
        self.assertEqual(request["temperature"], 0)
        self.assertEqual(request["top_k"], 1)
        self.assertEqual(request["repeat_penalty"], 1.0)
        client.llm.create_chat_completion.assert_not_called()

    def test_http_completion_and_selection_preserve_original_surrounding_text(self):
        client = Converter(Config(backend="http", model_format="jinen_v2"))
        client._http = Mock(return_value={
            "choices": [{"text": "今日", "finish_reason": "stop"}]})
        self.assertEqual(client.convert_selection("Ａ①きょう！", "ｶﾞ", "右文脈"),
                         "Ａ①今日！")
        request = client._http.call_args.args[0]
        self.assertEqual(request["prompt"], "\uee02ガA1\uee00キョウ\uee01")
        self.assertEqual(request["top_k"], 1)


if __name__ == "__main__":
    unittest.main()
