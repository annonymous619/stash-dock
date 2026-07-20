import sqlite3
import tempfile
import unittest
from pathlib import Path

from advanced import (
    DEFAULT_ADVANCED, duplicate_groups, index_paths, init_advanced_storage,
    match_rule, safe_library_root,
)


class AdvancedFeaturesTests(unittest.TestCase):
    def test_library_paths_cannot_escape_download_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = {**DEFAULT_ADVANCED, "libraries": [
                {"id": "bad", "name": "Bad", "path": "../outside", "default": True}
            ]}
            with self.assertRaises(ValueError):
                safe_library_root(root, settings, "bad")

    def test_exact_duplicate_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "Creator" / "first.mp4"
            second = root / "Creator" / "second.mp4"
            first.parent.mkdir()
            first.write_bytes(b"same media")
            second.write_bytes(b"same media")
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute("""CREATE TABLE jobs (
                id TEXT PRIMARY KEY, url TEXT, host TEXT, requested_mode TEXT,
                engine TEXT, status TEXT, created_at INTEGER)""")
            init_advanced_storage(connection)
            stats = index_paths(connection, root, [first, second])
            self.assertEqual(stats["indexed"], 2)
            groups = duplicate_groups(connection)
            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0]["kind"], "exact")

    def test_declarative_rules_merge_in_order(self):
        settings = {**DEFAULT_ADVANCED, "rules": [
            {"host": "youtube.com", "recipe_id": "balanced-1080", "enabled": True},
            {"host": "youtube.com", "url_contains": "/playlist",
             "library_id": "video", "tags": ["Playlist"], "enabled": True},
        ]}
        result = match_rule(
            settings, "www.youtube.com",
            "https://www.youtube.com/playlist?list=abc", "auto",
        )
        self.assertEqual(result["recipe_id"], "balanced-1080")
        self.assertEqual(result["library_id"], "video")
        self.assertEqual(result["tags"], ["Playlist"])


if __name__ == "__main__":
    unittest.main()
