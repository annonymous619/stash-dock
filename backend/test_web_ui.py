import unittest
from pathlib import Path


WEB = Path(__file__).parent / "web"


class WebUiTests(unittest.TestCase):
    def test_dashboard_has_immediate_theme_control_without_scheduling(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="theme-quick-select"', html)
        self.assertIn("Start a download", html)
        self.assertNotIn('id="schedule-at"', html)
        self.assertNotIn('id="feature-schedules"', html)

    def test_asset_and_service_worker_versions_match(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        worker = (WEB / "service-worker.js").read_text(encoding="utf-8")
        self.assertIn("app.css?v=0.8.3", html)
        self.assertIn("app.js?v=0.8.3", html)
        self.assertIn('CACHE = "stash-dock-0.8.3"', worker)

    def test_failed_jobs_offer_retry(self):
        script = (WEB / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn('class="quiet retry"', script)
        self.assertIn('/retry`', script)
        self.assertIn("Already ${esc(active.status)}", script)

    def test_preflight_exposes_completed_url_confirmation(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        script = (WEB / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="duplicate-warning"', html)
        self.assertIn('id="allow-repeat"', html)
        self.assertIn("previous_download", script)
        self.assertIn("allow_repeat", script)


if __name__ == "__main__":
    unittest.main()
