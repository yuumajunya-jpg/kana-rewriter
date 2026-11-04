from pathlib import Path
import tempfile
import unittest

from kana_rewriter.core import Config


class SettingsMigrationTests(unittest.TestCase):
    def load(self, settings):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(settings, encoding="utf-8")
            return Config.load(path)

    def test_existing_python_tsf_settings_preserve_user_options(self):
        config = self.load('''editor_worker = "python"
ime_commit_backend = "tsf"
native_uia_wait = "poll"
native_worker_path = "old.exe"
tsf_monitor_path = "old.dll"
paste_wait_seconds = 0.1
auto_replace_on_ime_commit = true
hotkey_line = "半角全角"
model_format = "jinen_v2"
''')
        self.assertTrue(config.auto_replace_on_ime_commit)
        self.assertEqual(config.hotkey_line, "半角全角")
        self.assertEqual(config.model_format, "jinen_v2")

    def test_removed_methods_fail_with_actionable_errors(self):
        for settings in ('editor_worker = "native"', 'ime_commit_backend = "uia"'):
            with self.subTest(settings=settings), self.assertRaisesRegex(ValueError, "廃止"):
                self.load(settings)
        with self.assertRaisesRegex(ValueError, "edit_backend"):
            self.load('edit_backend = "clipboard"')

    def test_default_model_matches_distributed_configuration(self):
        example = Config.load(Path(__file__).resolve().parents[1] / "config.example.toml")
        self.assertEqual(example.model_format, Config().model_format)
        self.assertTrue(Config().model_path.endswith("zenz-v3.2-xsmall/ggml-model-Q5_K_M.gguf"))

    def test_existing_local_inference_settings_remain_loadable(self):
        config = self.load('''backend = "llama_cpp"
endpoint = "http://127.0.0.1:8080/v1/completions"
model = "local"
timeout_seconds = 30
api_key_env = "KANA_REWRITER_API_KEY"
model_format = "jinen_v2"
max_tokens = 128
''')
        self.assertEqual(config.model_format, "jinen_v2")
        self.assertEqual(config.max_tokens, 128)

    def test_http_configuration_is_rejected_instead_of_silently_switching(self):
        with self.assertRaisesRegex(ValueError, "backend.*廃止"):
            self.load('backend = "http"')
