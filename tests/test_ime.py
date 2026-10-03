import ctypes as C
from contextlib import contextmanager, ExitStack
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class ImeTests(unittest.TestCase):
    @contextmanager
    def control(self, opened=True, effective=True):
        from kana_rewriter import ime
        state = SimpleNamespace(opened=opened, setters=[])

        def message(hwnd, code, command, value, flags, timeout, output):
            self.assertEqual((hwnd, code), (99, 0x0283))
            result = C.cast(output, C.POINTER(C.c_size_t))
            if command == 5:
                result.contents.value = int(state.opened)
            elif command == 6:
                state.setters.append(bool(value))
                if effective:
                    state.opened = bool(value)
                result.contents.value = 0
            else:
                self.fail("unexpected IME command")
            return 1

        with ExitStack() as stack:
            stack.enter_context(patch.object(ime.imm, "ImmGetDefaultIMEWnd", return_value=99))
            stack.enter_context(patch.object(ime.user, "SendMessageTimeoutW", side_effect=message))
            stack.enter_context(patch.object(ime, "window_identity", return_value=(1, 2, 3)))
            yield ime, state

    def test_on_is_closed_then_restored_and_close_is_idempotent(self):
        with self.control() as (ime, state):
            session = ime.ImeSession((1, 2, 3))
            session.disable()
            self.assertFalse(state.opened)
            session.close()
            session.close()
            self.assertTrue(state.opened)
            self.assertEqual(state.setters, [False, True])

    def test_original_off_is_not_enabled_after_edit(self):
        with self.control(opened=False) as (ime, state):
            session = ime.ImeSession((1, 2, 3))
            session.disable()
            session.close()
            self.assertFalse(state.opened)
            self.assertEqual(state.setters, [])

    def test_ineffective_switch_is_rejected(self):
        with self.control(effective=False) as (ime, state):
            session = ime.ImeSession((1, 2, 3))
            with patch.object(ime.time, "monotonic", side_effect=[0, 1]):
                with self.assertRaisesRegex(RuntimeError, "切り替えを確認できません"):
                    session.disable()
            session.close()
            self.assertTrue(state.opened)
            self.assertEqual(state.setters, [False])

    def test_focus_change_does_not_modify_new_input_target(self):
        with self.control() as (ime, state):
            session = ime.ImeSession((1, 2, 3))
            session.disable()
            with patch.object(ime, "window_identity", return_value=(4, 5, 6)), \
                    self.assertLogs("kana_rewriter.ime", level="WARNING"):
                session.close()
            self.assertEqual(state.setters, [False])

    def test_missing_ime_window_is_rejected(self):
        from kana_rewriter import ime
        with patch.object(ime.imm, "ImmGetDefaultIMEWnd", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "IME状態を確認できません"):
                ime.ImeSession((1, 2, 3))

    def test_message_timeout_is_not_interpreted_as_ime_off(self):
        from kana_rewriter import ime
        with patch.object(ime.imm, "ImmGetDefaultIMEWnd", return_value=99), \
                patch.object(ime.user, "SendMessageTimeoutW", return_value=0):
            with self.assertRaisesRegex(RuntimeError, "応答を確認できません"):
                ime.ImeSession((1, 2, 3))
