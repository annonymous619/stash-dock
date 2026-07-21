import json
import unittest

from download_core import (
    classify_failure, parse_ytdlp_preflight, selection_args, valid_date,
)


class DownloadCoreTests(unittest.TestCase):
    def test_ytdlp_collection_limits_and_dates(self):
        self.assertEqual(
            selection_args("yt-dlp", 10, "2026-01-02", "2026-02-03"),
            ["--playlist-end", "10", "--max-downloads", "10",
             "--dateafter", "20260102", "--datebefore", "20260203"],
        )

    def test_gallery_collection_limits_and_dates(self):
        self.assertEqual(
            selection_args("gallery-dl", 25, "2026-01-02", ""),
            ["--range", "1-25", "--date-after", "2026-01-02"],
        )

    def test_invalid_date_is_rejected(self):
        with self.assertRaises(ValueError):
            valid_date("01/02/2026")
        with self.assertRaises(ValueError):
            valid_date("2026-02-31")

    def test_rate_limit_error_is_actionable(self):
        self.assertEqual(classify_failure("HTTP Error 429: Too Many Requests")["code"], "RATE_LIMITED")

    def test_ytdlp_preflight_parses_collection(self):
        result = parse_ytdlp_preflight(json.dumps({
            "title": "Creator uploads", "uploader": "Rex",
            "entries": [{"id": "1"}, {"id": "2"}],
        }))
        self.assertEqual(result["creator"], "Rex")
        self.assertEqual(result["item_count"], 2)
        self.assertEqual(result["content_kind"], "collection")


if __name__ == "__main__":
    unittest.main()
