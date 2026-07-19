import unittest

from stash_integration import apply_manifests, clean_tag_names


class FakeClient:
    def __init__(self):
        self.performers = []
        self.tags = []
        self.scenes = []
        self.galleries = []

    def create_performer(self, name):
        self.performers.append(name)
        return "performer-1"

    def create_tag(self, name):
        self.tags.append(name)
        return f"tag-{len(self.tags)}"

    def update_scene_manifest(self, *args):
        self.scenes.append(args)
        return True

    def update_gallery_manifest(self, *args):
        self.galleries.append(args)
        return True


class MetadataHandoffTests(unittest.TestCase):
    def test_cleans_and_deduplicates_tags(self):
        self.assertEqual(
            clean_tag_names([" Stash   Dock ", "stash dock", "Source: Test"]),
            ["Stash Dock", "Source: Test"],
        )

    def test_manifest_matches_scene_and_gallery(self):
        client = FakeClient()
        inventory = {
            "findPerformers": {"performers": []},
            "findTags": {"tags": []},
            "findScenes": {"scenes": [{
                "id": "scene-1", "title": "", "urls": [], "tags": [],
                "performers": [], "galleries": [],
                "files": [{"path": "/data/Test/Creator/Post/video.mp4"}],
                "paths": {"screenshot": ""},
            }]},
            "findGalleries": {"galleries": [{
                "id": "gallery-1", "title": "", "urls": [], "tags": [],
                "performers": [], "scenes": [],
                "folder": {"path": "/data/Test/Creator/Post"},
            }]},
        }
        manifest = {
            "schema_version": 1,
            "creator": "Creator",
            "title": "Post",
            "source": {"url": "https://example.com/post"},
            "tags": ["Stash Dock", "Source: Test"],
            "media": [{
                "path": "Test/Creator/Post/video.mp4", "type": "video"
            }],
        }
        stats = apply_manifests(client, inventory, [manifest])
        self.assertEqual(client.performers, ["Creator"])
        self.assertEqual(client.tags, ["Stash Dock", "Source: Test"])
        self.assertEqual(len(client.scenes), 1)
        self.assertEqual(len(client.galleries), 1)
        self.assertEqual(stats["manifest_scenes_updated"], 1)
        self.assertEqual(stats["manifest_galleries_updated"], 1)


if __name__ == "__main__":
    unittest.main()
