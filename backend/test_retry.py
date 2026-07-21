import queue
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as app_module
from fastapi import HTTPException


class RetryJobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.temporary.name)
        self.config = root / "config"
        self.downloads = root / "downloads"
        self.patches = [
            patch.object(app_module, "CONFIG_ROOT", self.config),
            patch.object(app_module, "DOWNLOAD_ROOT", self.downloads),
            patch.object(app_module, "DB_PATH", self.config / "jobs.sqlite3"),
            patch.object(app_module, "SETTINGS_PATH", self.config / "settings.json"),
            patch.object(app_module, "STASH_KEY_FILE", self.config / "stash-api-key"),
            patch.object(app_module, "MANIFEST_ROOT", self.config / "manifests"),
        ]
        for active_patch in self.patches:
            active_patch.start()
        while True:
            try:
                app_module.jobs_queue.get_nowait()
            except queue.Empty:
                break
        app_module.init_storage()

    def tearDown(self):
        for active_patch in reversed(self.patches):
            active_patch.stop()
        self.temporary.cleanup()

    def insert_job(self, status="failed"):
        with app_module.db() as connection:
            connection.execute(
                """INSERT INTO jobs
                   (id,url,host,requested_mode,engine,status,created_at,recipe_id,
                    library_id,cookie_profile,max_items,date_after,date_before,error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "original123", "https://example.com/creator", "example.com",
                    "auto", "yt-dlp", status, 100, "balanced-1080", "stash",
                    "signed-in", 25, "2026-01-01", "2026-12-31", "ACCESS_BLOCKED",
                ),
            )

    def test_retry_clones_failed_job_settings_and_tracks_lineage(self):
        self.insert_job()
        settings = {**app_module.DEFAULT_SETTINGS, "gallery_hosts": []}
        with patch.object(app_module, "public_http_url", return_value=(
            "https://example.com/creator", "example.com"
        )), patch.object(app_module, "load_settings", return_value=settings):
            result = app_module.retry_job(
                "original123", app_module.RetryRequest(authorized=True)
            )
        with app_module.db() as connection:
            retry = connection.execute(
                "SELECT * FROM jobs WHERE id=?", (result["id"],)
            ).fetchone()
        self.assertEqual(retry["status"], "queued")
        self.assertEqual(retry["retried_from"], "original123")
        self.assertEqual(retry["recipe_id"], "balanced-1080")
        self.assertEqual(retry["library_id"], "stash")
        self.assertEqual(retry["cookie_profile"], "signed-in")
        self.assertEqual(retry["max_items"], 25)
        self.assertIn("original settings preserved", retry["log"])
        self.assertEqual(app_module.jobs_queue.get_nowait(), result["id"])

    def test_retry_requires_explicit_authorization(self):
        self.insert_job()
        with self.assertRaises(HTTPException) as raised:
            app_module.retry_job(
                "original123", app_module.RetryRequest(authorized=False)
            )
        self.assertEqual(raised.exception.status_code, 400)

    def test_second_retry_for_same_url_is_blocked(self):
        self.insert_job()
        settings = {**app_module.DEFAULT_SETTINGS, "gallery_hosts": []}
        with patch.object(app_module, "public_http_url", return_value=(
            "https://example.com/creator", "example.com"
        )), patch.object(app_module, "load_settings", return_value=settings):
            app_module.retry_job(
                "original123", app_module.RetryRequest(authorized=True)
            )
            with self.assertRaises(HTTPException) as raised:
                app_module.retry_job(
                    "original123", app_module.RetryRequest(authorized=True)
                )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("already queued", raised.exception.detail)

    def test_startup_recovery_keeps_one_active_job_per_url(self):
        self.insert_job(status="running")
        with app_module.db() as connection:
            connection.execute(
                """INSERT INTO jobs
                   (id,url,host,requested_mode,engine,status,created_at,retried_from)
                   VALUES (?,?,?,?,?,'queued',?,?)""",
                (
                    "duplicate456", "https://example.com/creator", "example.com",
                    "auto", "yt-dlp", 101, "original123",
                ),
            )
        result = app_module.recover_active_jobs()
        with app_module.db() as connection:
            original = connection.execute(
                "SELECT status FROM jobs WHERE id='original123'"
            ).fetchone()
            duplicate = connection.execute(
                "SELECT status,error FROM jobs WHERE id='duplicate456'"
            ).fetchone()
        self.assertEqual(result, {"resumed": 1, "cancelled_duplicates": 1})
        self.assertEqual(original["status"], "queued")
        self.assertEqual(duplicate["status"], "cancelled")
        self.assertIn("Duplicate active job", duplicate["error"])
        self.assertEqual(app_module.jobs_queue.get_nowait(), "original123")

    def test_completed_job_cannot_be_retried(self):
        self.insert_job(status="completed")
        with self.assertRaises(HTTPException) as raised:
            app_module.retry_job(
                "original123", app_module.RetryRequest(authorized=True)
            )
        self.assertEqual(raised.exception.status_code, 409)

    def test_completed_url_requires_explicit_history_confirmation(self):
        self.insert_job(status="completed")
        settings = {**app_module.DEFAULT_SETTINGS, "gallery_hosts": []}
        request = app_module.DownloadRequest(
            url="https://example.com/creator", authorized=True
        )
        with patch.object(app_module, "public_http_url", return_value=(
            "https://example.com/creator", "example.com"
        )), patch.object(app_module, "load_settings", return_value=settings):
            with self.assertRaises(HTTPException) as raised:
                app_module.create_job(request)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("already completed", raised.exception.detail)

    def test_confirmed_history_check_tracks_original_job(self):
        self.insert_job(status="completed")
        settings = {**app_module.DEFAULT_SETTINGS, "gallery_hosts": []}
        request = app_module.DownloadRequest(
            url="https://example.com/creator", authorized=True, allow_repeat=True
        )
        with patch.object(app_module, "public_http_url", return_value=(
            "https://example.com/creator", "example.com"
        )), patch.object(app_module, "load_settings", return_value=settings):
            result = app_module.create_job(request)
        with app_module.db() as connection:
            repeated = connection.execute(
                "SELECT duplicate_of FROM jobs WHERE id=?", (result["id"],)
            ).fetchone()
        self.assertEqual(result["duplicate_of"], "original123")
        self.assertEqual(repeated["duplicate_of"], "original123")

    def test_preflight_reports_completed_url_history(self):
        self.insert_job(status="completed")
        settings = {**app_module.DEFAULT_SETTINGS, "gallery_hosts": []}
        inspected = {
            "ready": True, "engine": "yt-dlp", "content_kind": "collection",
            "title": "Creator uploads", "creator": "Creator", "item_count": 4,
            "count_limited": False, "free_bytes": 1000, "destination": "/downloads",
        }
        with patch.object(app_module, "public_http_url", return_value=(
            "https://example.com/creator", "example.com"
        )), patch.object(app_module, "load_settings", return_value=settings), patch.object(
            app_module, "inspect_link", return_value=inspected
        ):
            result = app_module.preflight(app_module.PreflightRequest(
                url="https://example.com/creator"
            ))
        self.assertEqual(result["previous_download"]["id"], "original123")
        self.assertEqual(result["repeat_kind"], "collection")

    def test_archive_skip_completes_without_creating_duplicate_media(self):
        self.insert_job(status="completed")
        settings = {**app_module.DEFAULT_SETTINGS, "gallery_hosts": [], "api_key": ""}
        request = app_module.DownloadRequest(
            url="https://example.com/creator", authorized=True, allow_repeat=True
        )
        with patch.object(app_module, "public_http_url", return_value=(
            "https://example.com/creator", "example.com"
        )), patch.object(app_module, "load_settings", return_value=settings):
            result = app_module.create_job(request)
            with patch.object(app_module, "run_engine", return_value=0), patch.object(
                app_module, "snapshot_files", return_value=set()
            ):
                app_module.process_job(result["id"])
        with app_module.db() as connection:
            repeated = connection.execute(
                "SELECT status,log FROM jobs WHERE id=?", (result["id"],)
            ).fetchone()
        self.assertEqual(repeated["status"], "completed")
        self.assertIn("archive prevented duplicates", repeated["log"])

    def test_ytdlp_command_emits_paced_live_progress(self):
        settings = {**app_module.DEFAULT_SETTINGS, "gallery_hosts": []}
        with patch.object(app_module, "load_settings", return_value=settings):
            command = app_module.job_command(
                "yt-dlp", "https://example.com/video", "example.com", "video"
            )
        self.assertIn("--progress", command)
        self.assertIn("--newline", command)
        self.assertEqual(command[command.index("--progress-delta") + 1], "1")
        self.assertNotIn("--no-progress", command)

    def test_gallery_command_reports_each_prepared_file_without_verbose_secrets(self):
        settings = {**app_module.DEFAULT_SETTINGS}
        with patch.object(app_module, "load_settings", return_value=settings):
            command = app_module.job_command(
                "gallery-dl", "https://example.com/gallery", "example.com", "gallery"
            )
        self.assertIn("--Print", command)
        self.assertIn("[gallery] preparing {filename}.{extension}", command)
        self.assertIn("--no-colors", command)
        self.assertNotIn("--verbose", command)


if __name__ == "__main__":
    unittest.main()
