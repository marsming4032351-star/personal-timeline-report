import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from report_tools import coverage, dt, sql, window
from render_report import render, validate
from PIL import Image


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((ROOT / "examples/report-demo.json").read_text(encoding="utf-8"))

    def test_yesterday_uses_report_timezone_not_computer_date(self):
        result = window("yesterday", now="2026-11-16T18:00:00-08:00")
        self.assertEqual(result["start"], "2026-11-16T00:00:00+08:00")
        self.assertEqual(result["start_utc"], "2026-11-15T16:00:00+00:00")

    def test_previous_week_and_rolling_week_are_different(self):
        natural = window("last-week", now="2026-11-16T10:00:00+08:00")
        rolling = window("last-7-days", now="2026-11-16T10:00:00+08:00")
        self.assertEqual(natural["start"], "2026-11-09T00:00:00+08:00")
        self.assertEqual(natural["end_exclusive"], "2026-11-16T00:00:00+08:00")
        self.assertEqual(rolling["start"], "2026-11-09T10:00:00+08:00")

    def test_custom_includes_last_day_and_rejects_reversal(self):
        result = window("custom", start="2026-11-14", end="2026-11-16")
        self.assertEqual(result["end_exclusive"], "2026-11-17T00:00:00+08:00")
        with self.assertRaises(ValueError):
            window("custom", start="2026-11-16", end="2026-11-14")

    def test_dst_hours_are_elapsed_hours(self):
        result = window("hours", tz="America/Los_Angeles",
                        now="2026-11-01T02:30:00-08:00", hours=3)
        elapsed = (dt(result["end_utc"]) - dt(result["start_utc"])).total_seconds()
        self.assertEqual(elapsed, 3 * 3600)

    def test_sql_owner_scope_and_injection_rejection(self):
        a, b = "2026-11-13T16:00:00Z", "2026-11-14T16:00:00Z"
        with self.assertRaises(ValueError):
            sql("transcripts", a, b)
        with self.assertRaises(ValueError):
            sql("transcripts", a, b, owner="'; DROP TABLE transcripts;--")
        with self.assertRaises(ValueError):
            sql("transcripts", a, b, single_user=True, limit=1000)
        owner = "00000000-0000-0000-0000-000000000001"
        query = sql("transcripts", a, b, owner=owner, offset=200)
        self.assertIn(f"owner_id='{owner}'::uuid", query)
        self.assertIn("ORDER BY started_at,id LIMIT 200 OFFSET 200", query)
        with self.assertRaises(ValueError):
            sql("memories", a, b, owner=owner)
        with self.assertRaises(ValueError):
            sql("sync_runs", a, b, owner=owner)

    def test_coverage_clips_unions_and_keeps_untranscribed_gap(self):
        rows = [
            dict(id="r1", started_at="2026-11-13T23:50:00Z", ended_at="2026-11-14T00:10:00Z"),
            dict(id="r2", started_at="2026-11-14T00:05:00Z", ended_at="2026-11-14T00:15:00Z"),
            dict(id="r3", started_at="2026-11-14T00:15:00Z", ended_at="2026-11-14T00:30:00Z"),
        ]
        result = coverage(rows, [dict(id="t1", recording_id="r1")],
                          "2026-11-14T00:00:00Z", "2026-11-15T00:00:00Z")
        self.assertEqual(result["recording_union_seconds"], 1800)
        self.assertEqual(len(result["recordings_without_linked_transcripts"]), 2)
        self.assertEqual(result["recordings_without_linked_transcripts"][0]["recording_id"], "r2")

    def test_boundary_end_and_asset_link(self):
        rows = [dict(id="old", started_at="2026-11-13T23:45:00Z", ended_at="2026-11-14T00:00:00Z"),
                dict(id="new", external_id="audio-1", started_at="2026-11-14T00:00:00Z", duration_sec=60)]
        result = coverage(rows, [dict(id="t1", audio_asset_ids=["audio-1"])],
                          "2026-11-14T00:00:00Z", "2026-11-15T00:00:00Z")
        self.assertEqual(result["recording_union_seconds"], 60)
        self.assertEqual(result["recordings_without_linked_transcripts"], [])
        self.assertEqual(result["asset_linked_recordings"], ["new"])

    def test_unknown_speaker_and_media_cannot_confirm_personal_completion(self):
        self.data["events"][0]["category"] = "self_activity"
        with self.assertRaises(ValueError):
            validate(self.data)
        self.data["events"][0]["category"] = "conversation"
        self.data["events"][-1]["status"] = "completed"
        with self.assertRaises(ValueError):
            validate(self.data)

    def test_missing_evidence_and_unreviewed_privacy_fail(self):
        self.data["events"][0]["evidence_ids"] = ["NONEXISTENT"]
        with self.assertRaises(ValueError):
            validate(self.data)
        self.data["events"][0]["evidence_ids"] = ["E01"]
        self.data["privacy_reviewed"] = False
        with self.assertRaises(ValueError):
            validate(self.data)

    def test_live_report_project_must_match_authorized_target(self):
        validate(self.data, expected_project_id="synthetic-demo")
        with self.assertRaises(ValueError):
            validate(self.data, expected_project_id="another-project")
        self.data["project_id"] = ""
        with self.assertRaises(ValueError):
            validate(self.data)

    def test_empty_and_metadata_only_report_render(self):
        self.data["events"] = []
        self.data["highlights"] = self.data["ideas"] = self.data["todos"] = []
        self.data["retrieval"]["status"] = "metadata_only"
        self.data["summary"] = "录音尚无转写，活动内容未知。"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "empty.png"
            size = render(self.data, path)
            with Image.open(path) as image:
                self.assertEqual(image.size, size)
            self.assertIn("证据索引", path.with_suffix(".md").read_text(encoding="utf-8"))

    def test_long_report_grows_without_clipping_and_exports_pdf(self):
        self.data["ideas"] *= 25
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "long.png"
            size = render(self.data, path, pdf=True)
            self.assertGreater(size[1], 15000)
            self.assertTrue(path.with_suffix(".pdf").read_bytes().startswith(b"%PDF"))
            with Image.open(path) as image:
                self.assertEqual(image.size, size)
                self.assertEqual(image.getpixel((0, size[1]-1)), (255, 255, 255))


if __name__ == "__main__":
    unittest.main()
