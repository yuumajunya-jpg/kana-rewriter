import unittest

from scripts.summarize_timings import summarize


class TimingSummaryTests(unittest.TestCase):
    def test_nested_timings_are_not_counted_as_samples(self):
        result = summarize("""[診断] 時間: 適用/全体=20.0ms (完了)
[診断] 時間集計: 取得=10.0ms / AI=50.0ms / 適用=20.0ms / AI以外=30.0ms / 合計=80.0ms
[診断] 時間集計: 取得=20.0ms / AI=50.0ms / 適用=40.0ms / AI以外=60.0ms / 合計=110.0ms
中止: 入力先変更
""")
        self.assertEqual(result["completed_samples"], 2)
        self.assertEqual(result["aborted"], 1)
        self.assertEqual(result["timings_ms"]["AI以外"]["median"], 45)
        self.assertEqual(result["timings_ms"]["AI以外"]["p95_nearest_rank"], 60)

    def test_empty_log_has_no_fabricated_percentiles(self):
        self.assertEqual(summarize("起動"), {"completed_samples": 0, "aborted": 0, "timings_ms": {}})

    def test_native_readback_separates_counts_failures_and_milestones(self):
        result = summarize("""[診断] 時間: C++/uia_readback_text_query=20.0ms (完了)
[診断] 時間: C++/uia_readback_text_query=40.0ms (完了)
[診断] 時間: C++/uia_readback_first_text_match=50.0ms (完了)
[診断] 計数: C++/uia_readback_probes=5 (完了)
[診断] 計数: C++/uia_readback_probes=3 (完了)
[診断] 時間: C++/uia_readback_text_query=200.0ms (中止)
[診断] 計数: C++/uia_readback_confirmed=0 (中止)
[診断] 時間: C++/uia_readback_poll_wait=invalidms (完了)
[診断] 計数: C++/uia_readback_probes=1ms (完了)
""")
        completed = result["native_readback"]["完了"]
        self.assertEqual(completed["timings_ms"]["uia_readback_text_query"]["median"], 30)
        self.assertEqual(completed["timings_ms"]["uia_readback_first_text_match"]["samples"], 1)
        self.assertEqual(completed["counts"]["uia_readback_probes"]["median"], 4)
        self.assertEqual(completed["counts"]["uia_readback_probes"]["samples"], 2)
        self.assertNotIn("uia_readback_poll_wait", completed["timings_ms"])
        self.assertEqual(result["native_readback"]["中止"]["counts"]["uia_readback_confirmed"]["max"], 0)
        self.assertEqual(result["completed_samples"], 0)
