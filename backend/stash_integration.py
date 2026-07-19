from __future__ import annotations

import base64
import json
import mimetypes
import random
import re
import subprocess
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
              findTags(filter: {per_page: -1}) {
                tags { id name }
              }
              findScenes(filter: {per_page: -1}) {
                scenes {
                  id title urls files { path } paths { screenshot }
                  performers { id } galleries { id } tags { id }
                }
              }
              findGalleries(filter: {per_page: -1}) {
                galleries {
                  id title urls folder { path } performers { id }
                  scenes { id } tags { id }
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

    def create_tag(self, name: str) -> str:
        data = self.graphql(
            """mutation Create($input: TagCreateInput!) {
              tagCreate(input: $input) { id }
            }""",
            {"input": {"name": name}},
        )
        return data["tagCreate"]["id"]

    def update_scene_manifest(
        self, scene: dict, performer_id: str | None, tag_ids: list[str],
        title: str, source_url: str,
    ) -> bool:
        performers = {p["id"] for p in scene["performers"]}
        if performer_id:
            performers.add(performer_id)
        tags = {tag["id"] for tag in scene.get("tags") or []} | set(tag_ids)
        urls = set(scene.get("urls") or [])
        if source_url:
            urls.add(source_url)
        final_title = scene.get("title") or title
        if (
            performers == {p["id"] for p in scene["performers"]}
            and tags == {tag["id"] for tag in scene.get("tags") or []}
            and urls == set(scene.get("urls") or [])
            and final_title == (scene.get("title") or "")
        ):
            return False
        self.graphql(
            """mutation Update($input: SceneUpdateInput!) {
              sceneUpdate(input: $input) { id }
            }""",
            {"input": {
                "id": scene["id"], "title": final_title,
                "urls": sorted(urls), "performer_ids": sorted(performers),
                "tag_ids": sorted(tags),
            }},
        )
        return True

    def update_gallery_manifest(
        self, gallery: dict, performer_id: str | None, tag_ids: list[str],
        title: str, source_url: str,
    ) -> bool:
        performers = {p["id"] for p in gallery["performers"]}
        if performer_id:
            performers.add(performer_id)
        tags = {tag["id"] for tag in gallery.get("tags") or []} | set(tag_ids)
        urls = set(gallery.get("urls") or [])
        if source_url:
            urls.add(source_url)
        final_title = gallery.get("title") or title
        if (
            performers == {p["id"] for p in gallery["performers"]}
            and tags == {tag["id"] for tag in gallery.get("tags") or []}
            and urls == set(gallery.get("urls") or [])
            and final_title == (gallery.get("title") or "")
        ):
            return False
        self.graphql(
            """mutation Update($input: GalleryUpdateInput!) {
              galleryUpdate(input: $input) { id }
            }""",
            {"input": {
                "id": gallery["id"], "title": final_title,
                "urls": sorted(urls), "performer_ids": sorted(performers),
                "tag_ids": sorted(tags),
            }},
        )
        return True

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


def video_frame_avatar(
    scenes: list[dict], download_root: Path, cache_root: Path, creator: str
) -> str | None:
    safe_creator = re.sub(r"[^A-Za-z0-9._-]+", "_", creator).strip("._") or "creator"
    cache_root.mkdir(parents=True, exist_ok=True)
    cached = cache_root / f"{safe_creator}.jpg"
    if not cached.is_file():
        candidates: list[Path] = []
        for scene in scenes:
            for item in scene.get("files") or []:
                try:
                    relative = PurePosixPath(item["path"]).relative_to(STASH_LIBRARY)
                except (KeyError, ValueError):
                    continue
                local_path = download_root.joinpath(*relative.parts)
                if local_path.is_file():
                    candidates.append(local_path)
        random.SystemRandom().shuffle(candidates)
        for video in candidates:
            try:
                probe = subprocess.run(
                    [
                        "ffprobe", "-v", "error", "-show_entries",
                        "format=duration", "-of", "default=nw=1:nk=1", str(video),
                    ],
                    capture_output=True, text=True, timeout=30, check=True,
                )
                duration = float(probe.stdout.strip())
                timestamp = max(1.0, min(duration * 0.35, duration - 0.5))
                subprocess.run(
                    [
                        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-ss", f"{timestamp:.2f}", "-i", str(video), "-frames:v", "1",
                        "-vf", "scale=640:-2", "-q:v", "3", str(cached),
                    ],
                    capture_output=True, timeout=90, check=True,
                )
                if cached.is_file() and cached.stat().st_size > 1000:
                    break
                cached.unlink(missing_ok=True)
            except (OSError, ValueError, subprocess.SubprocessError):
                cached.unlink(missing_ok=True)
    if not cached.is_file():
        return None
    return f"data:image/jpeg;base64,{base64.b64encode(cached.read_bytes()).decode()}"


def load_manifests(manifest_root: Path | None) -> list[dict]:
    if not manifest_root or not manifest_root.is_dir():
        return []
    manifests = []
    for path in sorted(manifest_root.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and value.get("schema_version") == 1:
                manifests.append(value)
        except (OSError, json.JSONDecodeError):
            continue
    return manifests


def clean_tag_names(values: list[str]) -> list[str]:
    cleaned = []
    for value in values:
        name = re.sub(r"\s+", " ", str(value)).strip()[:80]
        if name and name.casefold() not in {item.casefold() for item in cleaned}:
            cleaned.append(name)
        if len(cleaned) >= 20:
            break
    return cleaned


def apply_manifests(
    client: StashClient, inventory: dict, manifests: list[dict]
) -> dict[str, int]:
    performers = inventory["findPerformers"]["performers"]
    scenes = inventory["findScenes"]["scenes"]
    galleries = inventory["findGalleries"]["galleries"]
    tags = inventory["findTags"]["tags"]
    by_performer = {item["name"].casefold(): item for item in performers}
    by_tag = {item["name"].casefold(): item["id"] for item in tags}
    by_scene_path = {
        PurePosixPath(file["path"]): scene
        for scene in scenes for file in scene.get("files") or []
    }
    stats = {
        "manifest_performers_created": 0, "manifest_tags_created": 0,
        "manifest_scenes_updated": 0, "manifest_galleries_updated": 0,
    }
    for manifest in manifests:
        creator = str(manifest.get("creator") or "").strip()
        performer_id = None
        if creator and creator.casefold() not in {
            "unknown", "unknown creator", "na"
        }:
            performer = by_performer.get(creator.casefold())
            if not performer:
                performer = {
                    "id": client.create_performer(creator), "name": creator,
                    "image_path": "default=true",
                }
                by_performer[creator.casefold()] = performer
                stats["manifest_performers_created"] += 1
            performer_id = performer["id"]
        tag_ids = []
        for name in clean_tag_names(manifest.get("tags") or []):
            tag_id = by_tag.get(name.casefold())
            if not tag_id:
                tag_id = client.create_tag(name)
                by_tag[name.casefold()] = tag_id
                stats["manifest_tags_created"] += 1
            tag_ids.append(tag_id)
        title = str(manifest.get("title") or "").strip()[:255]
        source_url = str((manifest.get("source") or {}).get("url") or "").strip()
        local_paths = [
            STASH_LIBRARY / PurePosixPath(item["path"])
            for item in manifest.get("media") or [] if item.get("path")
        ]
        matched_scenes = {
            scene["id"]: scene for path in local_paths
            if (scene := by_scene_path.get(path))
        }
        for scene in matched_scenes.values():
            stats["manifest_scenes_updated"] += int(
                client.update_scene_manifest(
                    scene, performer_id, tag_ids, title, source_url
                )
            )
        matched_galleries: dict[str, dict] = {}
        for gallery in galleries:
            folder = gallery.get("folder")
            if not folder:
                continue
            folder_path = PurePosixPath(folder["path"])
            if any(path == folder_path or folder_path in path.parents for path in local_paths):
                matched_galleries[gallery["id"]] = gallery
        for gallery in matched_galleries.values():
            stats["manifest_galleries_updated"] += int(
                client.update_gallery_manifest(
                    gallery, performer_id, tag_ids, title, source_url
                )
            )
    return stats


def organize(
    client: StashClient, download_root: Path, api_key: str, avatar_cache: Path,
    manifests: list[dict] | None = None,
) -> dict[str, int]:
    inventory = client.inventory()
    stats = {"performers_created": 0, "scenes_updated": 0,
             "galleries_updated": 0, "avatars_updated": 0}
    if manifests:
        stats.update(apply_manifests(client, inventory, manifests))
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

    for creator, album in sorted(set(album_scenes) | set(album_galleries)):
        performer = by_name.get(creator.casefold())
        if not performer:
            performer = {"id": client.create_performer(creator), "name": creator,
                         "image_path": "default=true"}
            by_name[creator.casefold()] = performer
            stats["performers_created"] += 1
        matching = album_scenes.get((creator, album), [])
        if "default=true" in (performer.get("image_path") or ""):
            image = local_avatar(download_root, creator)
            if not image:
                screenshots = [
                    value for value in
                    (screenshot_avatar(scene, api_key) for scene in matching)
                    if value
                ]
                image = (
                    random.SystemRandom().choice(screenshots) if screenshots
                    else video_frame_avatar(
                        matching, download_root, avatar_cache, creator
                    )
                )
            if image:
                client.set_performer_image(performer["id"], image)
                performer["image_path"] = "assigned"
                stats["avatars_updated"] += 1
        gallery = album_galleries.get((creator, album))
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
            image = (
                random.SystemRandom().choice(images) if images
                else video_frame_avatar(matching, download_root, avatar_cache, creator)
            )
            if image:
                client.set_performer_image(performer["id"], image)
                performer["image_path"] = "assigned"
                stats["avatars_updated"] += 1
        for scene in matching:
            stats["scenes_updated"] += int(
                client.update_scene(scene, performer["id"], None)
            )
    return stats


def synchronize(
    stash_url: str, api_key: str, download_root: Path, scan_wait: int = 25,
    avatar_cache: Path | None = None, manifest_root: Path | None = None,
) -> dict[str, int]:
    client = StashClient(stash_url, api_key)
    client.scan()
    time.sleep(scan_wait)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            return organize(
                client, download_root, api_key,
                avatar_cache or (download_root / ".stash-dock-avatars"),
                load_manifests(manifest_root),
            )
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"Stash synchronization failed: {last_error}")
