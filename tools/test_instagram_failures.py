"""Offline regression checks: python tools/test_instagram_failures.py."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
candidate = os.environ.get("DUCKLOADER_TEST_MODULE")
if candidate:
    spec = importlib.util.spec_from_file_location("platforms_candidate", candidate)
    p = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(p)
else:
    import platforms as p
from yt_dlp.networking import Response


def api_failure(body, status=400):
    response = Response(io.BytesIO(json.dumps(body).encode()),
                        "https://www.instagram.com/api/v1/media/123/info/", {}, status)
    cause = p.YDLHTTPError(response)
    error = Exception("Video info extraction failed: HTTP Error 400")
    error.exc_info = (type(cause), cause, None)
    return error


class InstagramFailures(unittest.TestCase):
    def test_age_gate_preserved_from_wrapped_response(self):
        result = p._instagram_access_error(api_failure({
            "message": "geoblock_required", "gating_type": "unappealable",
            "title": "People under 25 can't see this content"}))
        self.assertEqual(p.classify_error("instagram", result), "ig_restricted")

    def test_missing_post_is_not_login(self):
        result = p._instagram_access_error(api_failure({"message": "Media not found or unavailable"}))
        self.assertEqual(p.classify_error("instagram", result), "ig_not_found")

    def test_throttle_wins_over_login_flag(self):
        result = p._instagram_access_error(api_failure({
            "message": "Please wait a few minutes before you try again.", "require_login": True}, 401))
        self.assertEqual(p.classify_error("instagram", result), "ig_rate_limited")
        self.assertFalse(p._is_retryable("https://instagram.com/reel/test/", result))

    def test_unknown_bad_request_is_not_guessed(self):
        self.assertIsNone(p._instagram_access_error(api_failure({"message": "Bad request"})))

    def test_empty_media_advice_is_not_session_evidence(self):
        error = Exception("Instagram sent an empty media response. Use --cookies for authentication; login may be required.")
        self.assertEqual(p.classify_error("instagram", error), "instagram_failed")

    def test_explicit_login_still_classified(self):
        self.assertEqual(p.classify_error("instagram", Exception("Instagram login required: has no session cookie")), "ig_login")

    def test_failed_public_route_does_not_report_auth(self):
        with patch.object(p, "_download_with_selector", side_effect=Exception("Instagram sent an empty media response")), \
             patch.object(p, "_report_auth_problem") as report:
            with self.assertRaises(Exception):
                p._download_instagram("https://www.instagram.com/reel/test/", "best")
            report.assert_not_called()

    def test_explicit_session_failure_reports_auth(self):
        with patch.object(p, "_download_with_selector", side_effect=Exception("browser has no session cookie")), \
             patch.object(p, "_instagram_auth_sources", return_value=[("browser", {})]), \
             patch.object(p, "_report_auth_problem") as report:
            with self.assertRaises(Exception):
                p._download_instagram("https://www.instagram.com/reel/test/", "best")
            report.assert_called_once()

    def test_instagram_codec_metadata_cannot_prove_silence(self):
        ydl = Mock()
        ydl.extract_info.return_value = {"id": "silent", "formats": [{"acodec": "none"}, {"acodec": "none"}]}
        ydl.process_ie_result.side_effect = RuntimeError("processing reached")
        with self.assertRaisesRegex(RuntimeError, "processing reached"):
            p._download_entries(ydl, "https://instagram.com/reel/test/", "/unused", True, None)
        self.assertEqual(p.classify_error("instagram", p.NoAudioError()), "v2a_no_audio")

    def test_instagram_audio_uses_progressive_video_sort(self):
        with patch.object(p, "_download_with_selector", return_value=({}, [], [])) as download:
            p._download_instagram("https://www.instagram.com/reel/test/", "audio")
        self.assertEqual(download.call_args.kwargs["format_sort"], p._video_format_sort())

    def test_unknown_codec_is_not_assumed_silent(self):
        ydl = Mock()
        ydl.extract_info.return_value = {"formats": [{"acodec": None}]}
        ydl.process_ie_result.side_effect = RuntimeError("processing reached")
        with self.assertRaisesRegex(RuntimeError, "processing reached"):
            p._download_entries(ydl, "https://instagram.com/reel/test/", "/unused", True, None)

    def test_audio_format_is_not_skipped(self):
        ydl = Mock()
        ydl.extract_info.return_value = {"formats": [{"acodec": "none"}, {"acodec": "aac"}]}
        ydl.process_ie_result.side_effect = RuntimeError("audio processing reached")
        with self.assertRaisesRegex(RuntimeError, "audio processing reached"):
            p._download_entries(ydl, "https://instagram.com/reel/test/", "/unused", True, None)

    def run_attempt(self, error, audio=False, probe=None):
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(p, "_new_job_dir", return_value="/fake-job"))
            cleanup = stack.enter_context(patch.object(p, "_remove_tree"))
            stack.enter_context(patch.object(p, "_open_ydl", return_value=contextlib.nullcontext(Mock())))
            stack.enter_context(patch.object(p, "_download_entries", side_effect=error))
            stack.enter_context(patch.object(p, "_find_downloaded_file", return_value="/fake-job/file.mp4"))
            stack.enter_context(patch.object(p, "has_audio_stream", return_value=probe))
            try:
                p._download_with_selector("https://instagram.com/reel/test/", "ba/b", audio,
                    plans=[{"label": "instagram:browser", "opts": {}, "auth": True}])
            except Exception as result:
                cleanup.assert_called_once_with("/fake-job")
                return result
        self.fail("Expected a failed attempt")

    def test_access_decision_survives_attempt_cleanup(self):
        result = self.run_attempt(api_failure({"message": "geoblock_required"}))
        self.assertEqual(p.classify_error("instagram", result), "ig_restricted")

    def test_ffprobe_confirmed_silence_translated(self):
        result = self.run_attempt(Exception("unable to obtain file audio codec with ffprobe"), True, False)
        self.assertIsInstance(result, p.NoAudioError)

    def test_unreadable_download_not_mislabeled_silent(self):
        error = Exception("unable to obtain file audio codec with ffprobe")
        self.assertIs(self.run_attempt(error, True, None), error)


if __name__ == "__main__":
    unittest.main()
