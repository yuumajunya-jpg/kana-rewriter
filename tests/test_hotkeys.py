import ctypes
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch, call

from kana_rewriter.core import Config
from kana_rewriter.hotkeys import parse_hotkey


class HotkeyTests(unittest.TestCase):
    def test_defaults_and_aliases(self):
        binding = parse_hotkey(" alt + CONTROL + k ")
        self.assertEqual((binding.modifiers, binding.key, binding.label), (3, ord("K"), "Ctrl+Alt+K"))
        self.assertEqual(Config().hotkey_line, "Ctrl+Alt+K")
        self.assertEqual(Config().hotkey_selection, "Ctrl+Alt+J")
        self.assertEqual(Config().hotkey_quit, "")

    def test_function_and_named_keys(self):
        for text, modifiers, key in (("F8", 0, 0x77), ("Win+Shift+Space", 12, 0x20),
                                     ("Ctrl+9", 2, ord("9")), ("Alt+PageDown", 1, 0x22)):
            binding = parse_hotkey(text)
            self.assertEqual((binding.modifiers, binding.key), (modifiers, key))

    def test_invalid_bindings(self):
        for value in (None, 1, "", "Ctrl", "Ctrl+Control+K", "Ctrl+K+J", "K",
                      "Ctrl++K", "Ctrl+Unknown", "F0", "F25", "Ctrl+F12"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_hotkey(value)

    def test_duplicate_bindings_are_rejected(self):
        for options in ({"hotkey_selection": "Alt+Control+K"},
                        {"hotkey_quit": "ctrl+alt+j"}, {"hotkey_quit": None}):
            with self.assertRaises(ValueError):
                Config(**options)

    def test_settings_load_from_toml(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('hotkey_line = "Ctrl+Shift+K"\nhotkey_selection = "F8"\nhotkey_quit = "Ctrl+Alt+Q"\n',
                            encoding="utf-8")
            config = Config.load(str(path))
            self.assertEqual((config.hotkey_line, config.hotkey_selection, config.hotkey_quit),
                             ("Ctrl+Shift+K", "F8", "Ctrl+Alt+Q"))

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_registration_and_quit_dispatch_use_config(self):
        from kana_rewriter.__main__ import run_windows
        from kana_rewriter import windows as win

        def quit_message(pointer, *args):
            message = ctypes.cast(pointer, ctypes.POINTER(win.W.MSG)).contents
            message.message = 0x0312
            message.wParam = 3
            return 1

        with patch.object(win.user, "RegisterHotKey", return_value=1) as register, \
                patch.object(win.user, "UnregisterHotKey") as unregister, \
                patch.object(win.user, "PeekMessageW", side_effect=quit_message), \
                patch.object(win, "Desktop") as desktop, \
                patch("kana_rewriter.__main__.ThreadPoolExecutor") as pool, patch("builtins.print"):
            self.assertEqual(run_windows(Mock(), Config(hotkey_line="Ctrl+Shift+K",
                              hotkey_selection="F8", hotkey_quit="Ctrl+Alt+Q",
                              edit_backend="clipboard")), 0)
        self.assertEqual(register.call_args_list,
                         [call(None, 1, 0x4006, ord("K")), call(None, 2, 0x4000, 0x77),
                          call(None, 3, 0x4003, ord("Q"))])
        self.assertEqual(unregister.call_args_list, [call(None, 1), call(None, 2), call(None, 3)])
        desktop.return_value.close.assert_called_once()
        pool.return_value.shutdown.assert_called_once_with(wait=True, cancel_futures=True)

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_capture_waits_for_configured_trigger_and_win_release(self):
        from kana_rewriter import windows as win
        desktop = win.Desktop()
        states = {0x77: [0x8000, 0], 0x5B: [0x8000, 0]}

        def key_state(key):
            values = states.get(key, [])
            return values.pop(0) if values else 0

        with patch.object(win, "focus", return_value="target"), \
                patch.object(win.user, "GetAsyncKeyState", side_effect=key_state) as keys, \
                patch.object(win.time, "sleep") as sleep, \
                patch.object(desktop, "copy_selection", return_value="きょう"), \
                patch.object(desktop, "stamp", return_value=("target", 1, 2)):
            desktop.capture("selection", 1000, trigger_keys=(0x77,))
        self.assertIn(call(0x77), keys.call_args_list)
        self.assertEqual(states[0x77], [])
        self.assertEqual(states[0x5B], [])
        self.assertEqual(sleep.call_count, 2)
