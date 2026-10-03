import json
from pathlib import Path
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import unittest
from unittest.mock import Mock, patch

from kana_rewriter.core import Config, Converter, Capture, apply_result


def response(text="今日は良い天気です", reason="stop"):
    return {"choices": [{"message": {"content": text}, "finish_reason": reason}]}


class CoreTests(unittest.TestCase):
    def test_mixed_selection_preserves_non_kana_and_passes_context(self):
        client = Converter(Config())
        with patch.object(client, "convert", side_effect=["歯医者", "廃車"]) as convert:
            self.assertEqual(client.convert_selection("歯:はいしゃ\n車:ハイシャ!", "前", "後"),
                             "歯:歯医者\n車:廃車!")
        self.assertEqual(convert.call_args_list[0].args,
                         ("はいしゃ", "前歯:", "\n車:ハイシャ!後"))
        self.assertEqual(convert.call_args_list[1].args,
                         ("ハイシャ", "前歯:歯医者\n車:", "!後"))

    def test_selection_without_kana_does_not_call_model(self):
        client = Converter(Config())
        with patch.object(client, "convert") as convert:
            self.assertEqual(client.convert_selection("今日。ABC 123\n"), "今日。ABC 123\n")
            convert.assert_not_called()

    def test_sentence_rebuild_keeps_existing_kanji_in_place(self):
        from kana_rewriter.text import split_at_caret
        source = "今日はいい天気だ。さんぽでもしようか"
        desktop = Mock()
        captured = Capture(source, ("window", 1, 2), split_at_caret(source, len(source)))
        desktop.stamp.return_value = captured.stamp
        desktop.copy_selection.return_value = source
        self.assertEqual(captured.source, "さんぽでもしようか")
        self.assertTrue(apply_result(desktop, captured, "散歩でもしようか"))
        desktop.replace.assert_called_once_with("今日はいい天気だ。散歩でもしようか", "window",
                                              caret=len("今日はいい天気だ。散歩でもしようか"))

    def test_caret_stays_after_punctuation_and_before_suffix(self):
        from kana_rewriter.text import split_at_caret
        source = "前。さんぽ?!後ろ"
        original_caret = len("前。さんぽ?!")
        desktop = Mock()
        captured = Capture(source, ("window", 1, 2), split_at_caret(source, original_caret), original_caret)
        desktop.stamp.return_value = captured.stamp
        desktop.copy_selection.return_value = source
        apply_result(desktop, captured, "散歩")
        desktop.replace.assert_called_once_with("前。散歩?!後ろ", "window", caret=len("前。散歩?!"))

    def test_direct_model_is_cached_and_uses_chat_api(self):
        with tempfile.NamedTemporaryFile(suffix=".gguf") as file:
            model = Mock()
            model.create_chat_completion.return_value = response()
            module = Mock()
            module.Llama.return_value = model
            with patch.dict("sys.modules", {"llama_cpp": module}):
                client = Converter(Config(model_path=file.name, model_format="chat"))
                for _ in range(2):
                    self.assertEqual(client.convert("きょうはいいてんきです"), "今日は良い天気です")
                module.Llama.assert_called_once()
                self.assertEqual(model.create_chat_completion.call_args.kwargs["messages"][1]["content"],
                                 "きょうはいいてんきです")

    def test_missing_model(self):
        with self.assertRaisesRegex(ValueError, "GGUF"):
            Converter(Config(model_path="missing-model.gguf")).load_model()

    def test_config_relative_to_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('model_path = "models/test.gguf"', encoding="utf-8")
            self.assertEqual(Config.load(str(path)).model_path,
                             str(Path(directory) / "models/test.gguf"))

    def test_rejects_remote_endpoint(self):
        for endpoint in ("https://example.com/v1/chat/completions", "http://localhost@example.com/x"):
            with self.assertRaises(ValueError):
                Config(endpoint=endpoint)

    def test_refuses_empty_or_excessive_source(self):
        for source in (" ", "あ" * 1001):
            with self.assertRaises(ValueError):
                Converter(Config()).convert(source)

    def test_bad_response_never_returned(self):
        client = Converter(Config())
        for payload in ({}, response("", "stop"), response("途中", "length"),
                        response("```変換```"), response("<think>考え中"), response("\x00"),
                        response(None), response("あ" * 2001)):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                client._validate(payload)

    def test_preserves_whitespace(self):
        self.assertEqual(Converter(Config())._validate(response(" 今日\n")), " 今日\n")

    def test_model_cannot_add_linebreaks_or_tabs(self):
        client = Converter(Config(model_format="chat"))
        client.llm = Mock()
        for text in ("今日\n", "今日\t", "今日\r"):
            client.llm.create_chat_completion.return_value = response(text)
            with self.assertRaises(ValueError):
                client.convert("きょう")

    def test_changes_cancel_replacement(self):
        desktop = Mock()
        captured = Capture("きょう", ("window", 1, 2))
        desktop.stamp.return_value = ("other", 1, 2)
        with self.assertRaises(RuntimeError):
            apply_result(desktop, captured, "今日")
        desktop.copy_selection.assert_not_called()
        desktop.replace.assert_not_called()

    def test_changed_selection_cancels(self):
        desktop = Mock()
        captured = Capture("きょう", ("window", 1, 2))
        desktop.stamp.return_value = captured.stamp
        desktop.copy_selection.return_value = "あした"
        with self.assertRaises(RuntimeError):
            apply_result(desktop, captured, "今日")
        desktop.replace.assert_not_called()

    def test_matching_selection_is_replaced(self):
        desktop = Mock()
        captured = Capture("きょう", ("window", 1, 2))
        desktop.stamp.return_value = captured.stamp
        desktop.copy_selection.return_value = captured.text
        self.assertTrue(apply_result(desktop, captured, "今日"))
        desktop.replace.assert_called_once_with("今日", "window")

    def test_unchanged_does_not_touch_editor(self):
        desktop = Mock()
        self.assertFalse(apply_result(desktop, Capture("今日", None), "今日"))
        self.assertEqual(desktop.mock_calls, [])

    def test_http_integration(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                body = json.dumps(response(), ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = Converter(Config(backend="http", model_format="chat", endpoint=f"http://127.0.0.1:{server.server_port}/v1/chat/completions"))
            self.assertEqual(client.convert("きょうはいいてんきです"), "今日は良い天気です")
            self.assertFalse(requests[0]["stream"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
