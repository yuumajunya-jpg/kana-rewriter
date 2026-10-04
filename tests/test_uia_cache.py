import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class PropertyCacheTests(unittest.TestCase):
    def automation(self):
        from kana_rewriter.direct_uia import Automation
        automation = Automation.__new__(Automation)
        automation.client = Mock()
        automation.types = SimpleNamespace(TreeScope_Element=1, UIA_IsPasswordPropertyId=30019,
                                           UIA_IsEnabledPropertyId=30010, UIA_HasKeyboardFocusPropertyId=30008)
        automation.cache_requests = {}
        return automation

    def test_request_is_reused_but_values_are_refreshed_each_time(self):
        from kana_rewriter.direct_uia import property_values
        automation = self.automation()
        names = ("IsPassword", "IsEnabled", "HasKeyboardFocus")
        element = Mock()
        element.BuildUpdatedCache.side_effect = [
            SimpleNamespace(CachedIsPassword=False, CachedIsEnabled=True, CachedHasKeyboardFocus=True),
            SimpleNamespace(CachedIsPassword=True, CachedIsEnabled=False, CachedHasKeyboardFocus=False)]
        self.assertEqual(property_values(automation, element, names),
                         dict(IsPassword=False, IsEnabled=True, HasKeyboardFocus=True))
        self.assertEqual(property_values(automation, element, names),
                         dict(IsPassword=True, IsEnabled=False, HasKeyboardFocus=False))
        automation.client.CreateCacheRequest.assert_called_once()
        self.assertEqual(automation.client.CreateCacheRequest.return_value.AddProperty.call_count, 3)
        self.assertEqual(element.BuildUpdatedCache.call_count, 2)

    def test_unsupported_cache_falls_back_to_live_properties(self):
        from kana_rewriter.direct_uia import property_values
        element = SimpleNamespace(BuildUpdatedCache=Mock(side_effect=RuntimeError("unsupported")),
                                  CurrentIsEnabled=False)
        self.assertEqual(property_values(self.automation(), element, ("IsEnabled",)), {"IsEnabled": False})

    def test_failure_of_both_paths_is_not_treated_as_permission_to_edit(self):
        from kana_rewriter.direct_uia import property_values
        element = SimpleNamespace(BuildUpdatedCache=Mock(side_effect=RuntimeError("gone")))
        with self.assertRaises(AttributeError):
            property_values(self.automation(), element, ("IsEnabled",))

    def test_write_check_rejects_new_password_disabled_or_unfocused_state(self):
        from kana_rewriter.direct_uia import AutomationEditor
        for password, enabled, focus in ((True, True, True), (False, False, True), (False, True, False)):
            with self.subTest(password=password, enabled=enabled, focus=focus):
                editor = AutomationEditor.__new__(AutomationEditor)
                editor.automation = self.automation()
                editor.focused = Mock()
                editor.focused.BuildUpdatedCache.return_value = SimpleNamespace(
                    CachedIsPassword=password, CachedIsEnabled=enabled, CachedHasKeyboardFocus=focus)
                with self.assertRaises(RuntimeError):
                    editor.check_writable()
