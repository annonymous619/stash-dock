from __future__ import annotations

import base64
import json
import mimetypes
import random
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

STASH_LIBRARY = PurePosixPath("/data")
EROME_LIBRARY = STASH_LIBRARY / "Erome"
VIDEO_SITES = {"pornhub", "xvideos", "xnxx"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


class StashClient:
    def __init__(self, url: str, api_key: str) -> None:
        self.url = url.rstrip("/") + "/graphql"
        self.headers = {"Content-Type": "application/json", "ApiKey": api_key}

    def graphql(self, query: str, variables: dict | None = None) -> dict:
        request = urllib.request.Request(
            self.url,
            data=json.dumps({"query": query, "variables": variables or {}}).encode(),
            headers=self.headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                result = json.load(response)
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Cannot reach Stash: {exc}") from exc
        if result.get("errors"):
            raise RuntimeError(json.dumps(result["errors"], ensure_ascii=False))
        return result["data"]

    def scan(self) -> None:
        self.graphql(
            """mutation Scan($input: ScanMetadataInput!) {
              metadataScan(input: $input)
            }""",
            {"input": {
                "paths": [str(STASH_LIBRARY)],
                "rescan": False,
                "scanGenerateCovers": True,
                "scanGenerateThumbnails": True,
            }},
        )

    def inventory(self) -> dict:
        return self.graphql(
            """query Inventory {
              findPerformers(filter: {per_page: -1}) {
                performers { id name image_path }
              }
              findScenes(filter: {per_page: -1}) {
                scenes {
                  id files { path } paths { screenshot }
                  performers { id } galleries { id }
                }
              }
              findGalleries(filter: {per_page: -1}) {
                galleries {
                  id folder { path } performers { id } scenes { id }
                }
              }
            }"""
        )

    def create_performer(self, name: str) -> str:
        data = self.graphql(
            """mutation Create($input: PerformerCreateInput!) {
              performerCreate(input: $input) { id }
            }""",
            {"input": {"name": name}},
        )
        return data["performerCreate"]["id"]

    def set_performer_image(self, performer_id: str, image: str) -> None:
        self.graphql(
            """mutation Update($input: PerformerUpdateInput!) {
              performerUpdate(input: $input) { id }
            }""",
            {"input": {"id": performer_id, "image": image}},
        )

    def update_scene(self, scene: dict, performer_id: str, gallery_id: str | None) -> bool:
        performer_ids = sorted({p["id"] for p in scene["performers"]} | {performer_id})
        gallery_ids = {g["id"] for g in scene["galleries"]}
        if gallery_id:
            gallery_ids.add(gallery_id)
        if (
            set(performer_ids) == {p["id"] for p in scene["performers"]}
            and gallery_ids == {g["id"] for g in scene["galleries"]}
        ):
            return False
        self.graphql(
            """mutation Update($input: SceneUpdateInput!) {
              sceneUpdate(input: $input) { id }
            }""",
            {"input": {
                "id": scene["id"],
                "performer_ids": performer_ids,
                "gallery_ids": sorted(gallery_ids),
            }},
        )
        return True

    def update_gallery(
        self, gallery: dict, performer_id: str, matching_scenes: list[dict]
    ) -> bool:
        performer_ids = sorted({p["id"] for p in gallery["performers"]} | {performer_id})
        scene_ids = sorted(
            {s["id"] for s in gallery["scenes"]} | {s["id"] for s in matching_scenes}
        )
        if (
            set(performer_ids) == {p["id"] for p in gallery["performers"]}
            and set(scene_ids) == {s["id"] for s in gallery["scenes"]}
        ):
            return False
        self.graphql(
            """mutation Update($input: GalleryUpdateInput!) {
              galleryUpdate(input: $input) { id }
            }""",
            {"input": {
                "id": gallery["id"],
                "performer_ids": performer_ids,
                "scene_ids": scene_ids,
            }},
        )
        return True


def parse_erome(path: str) -> tuple[str, str] | None:
    try:
        relative = PurePosixPath(path).relative_to(EROME_LIBRARY)
    except ValueError:
        return None
    return (relative.parts[0], relative.parts[1]) if len(relative.parts) >= 2 else None


def parse_video(path: str) -> tuple[str, str] | None:
    try:
        relative = PurePosixPath(path).relative_to(STASH_LIBRARY)
    except ValueError:
        return None
    if len(relative.parts) < 3 or relative.parts[0].casefold() not in VIDEO_SITES:
        return None
    if relative.parts[1].casefold() in {"na", "unknown", "unknown creator"}:
        return None
    return relative.parts[0], relative.parts[1]


def local_avatar(download_root: Path, creator: str) -> str | None:
    creator_root = download_root / "Erome" / creator
    candidates = [
        path for path in creator_root.rglob("*")
        if path.is_file() and path.suffix.casefold() in IMAGE_EXTENSIONS
    ] if creator_root.is_dir() else []
    if not candidates:
        return None
    selected = random.SystemRandom().choice(candidates)
    mime = mimetypes.guess_type(selected.name)[0] or "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(selected.read_bytes()).decode()}"


def screenshot_avatar(scene: dict, api_key: str) -> str | None:
    url = (scene.get("paths") or {}).get("screenshot")
    if not url:
        return None
    request = urllib.request.Request(url, headers={"ApiKey": api_key})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            mime = response.headers.get_content_type() or "image/jpeg"
            return f"data:{mime};base64,{base64.b64encode(response.read()).decode()}"
    except urllib.error.URLError:
        return None


def organize(client: StashClient, download_root: Path, api_key: str) -> dict[str, int]:
    inventory = client.inventory()
    performers = inventory["findPerformers"]["performers"]
    scenes = inventory["findScenes"]["scenes"]
    galleries = inventory["findGalleries"]["galleries"]
    by_name = {p["name"].casefold(): p for p in performers}
    album_scenes: dict[tuple[str, str], list[dict]] = {}
    album_galleries: dict[tuple[str, str], dict] = {}

    for scene in scenes:
        if scene["files"] and (parsed := parse_erome(scene["files"][0]["path"])):
            album_scenes.setdefault(parsed, []).append(scene)
    for gallery in galleries:
        folder = gallery.get("folder")
        if folder and (parsed := parse_erome(folder["path"])):
            album_galleries[parsed] = gallery

    stats = {"performers_created": 0, "scenes_updated": 0,
             "galleries_updated": 0, "avatars_updated": 0}
    for creator, album in sorted(set(album_scenes) | set(album_galleries)):
        performer = by_name.get(creator.casefold())
        if not performer:
            performer = {"id": client.create_performer(creator), "name": creator,
                         "image_path": "default=true"}
            by_name[creator.casefold()] = performer
            stats["performers_created"] += 1
        if "default=true" in (performer.get("image_path") or ""):
            if image := local_avatar(download_root, creator):
                client.set_performer_image(performer["id"], image)
                performer["image_path"] = "assigned"
                stats["avatars_updated"] += 1
        gallery = album_galleries.get((creator, album))
        matching = album_scenes.get((creator, album), [])
        for scene in matching:
            stats["scenes_updated"] += int(
                client.update_scene(scene, performer["id"], gallery["id"] if gallery else None)
            )
        if gallery:
            stats["galleries_updated"] += int(
                client.update_gallery(gallery, performer["id"], matching)
            )

    video_creators: dict[tuple[str, str], list[dict]] = {}
    for scene in scenes:
        if scene["files"] and (parsed := parse_video(scene["files"][0]["path"])):
            video_creators.setdefault(parsed, []).append(scene)
    for (_site, creator), matching in sorted(video_creators.items()):
        performer = by_name.get(creator.casefold())
        if not performer:
            performer = {"id": client.create_performer(creator), "name": creator,
                         "image_path": "default=true"}
            by_name[creator.casefold()] = performer
            stats["performers_created"] += 1
        if "default=true" in (performer.get("image_path") or ""):
            images = [image for image in
                      (screenshot_avatar(scene, api_key) for scene in matching) if image]
            if images:
                client.set_performer_image(
                    performer["id"], random.SystemRandom().choice(images)
                )
                performer["image_path"] = "assigned"
                stats["avatars_updated"] += 1
        for scene in matching:
            stats["scenes_updated"] += int(
                client.update_scene(scene, performer["id"], None)
            )
    return stats


def synchronize(
    stash_url: str, api_key: str, download_root: Path, scan_wait: int = 25
) -> dict[str, int]:
    client = StashClient(stash_url, api_key)
    client.scan()
    time.sleep(scan_wait)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            return organize(client, download_root, api_key)
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"Stash synchronization failed: {last_error}")
