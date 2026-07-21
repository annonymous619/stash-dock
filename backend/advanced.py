from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlparse

MEDIA_EXTENSIONS = {
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v",
    ".jpg", ".jpeg", ".png", ".webp", ".gif",
    ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav",
}

DEFAULT_ADVANCED = {
    "recipes": [
        {"id": "original", "name": "Original quality", "mode": "auto", "format": "best"},
        {"id": "balanced-1080", "name": "Balanced 1080p", "mode": "video", "format": "bv*[height<=1080]+ba/b[height<=1080]"},
        {"id": "audio-best", "name": "Audio · best MP3", "mode": "audio", "format": "bestaudio/best"},
        {"id": "gallery-original", "name": "Gallery · originals", "mode": "gallery", "format": "original"},
    ],
    "libraries": [
        {"id": "stash", "name": "Stash", "path": "", "default": True},
    ],
    "webhooks": [],
    "storage_policy": {
        "enabled": False, "review_only": True, "minimum_age_days": 90,
        "maximum_play_count": 0, "minimum_size_mb": 100,
    },
    "plugins_enabled": True,
    "rules": [],
    "cookie_profiles": [],
    "feature_toggles": {
        "downloads": True, "audio_mode": True, "schedules": True,
        "duplicate_review": True, "storage_review": True,
        "plugins": True, "webhooks": True, "stash_sync": True,
    },
}


def advanced_settings(settings: dict) -> dict:
    result = json.loads(json.dumps(DEFAULT_ADVANCED))
    saved = settings.get("advanced")
    if isinstance(saved, dict):
        result.update(saved)
        if isinstance(saved.get("storage_policy"), dict):
            result["storage_policy"] = {
                **DEFAULT_ADVANCED["storage_policy"], **saved["storage_policy"]
            }
    return result


def safe_library_root(download_root: Path, advanced: dict, library_id: str) -> Path:
    library = next(
        (item for item in advanced["libraries"] if item.get("id") == library_id),
        next((item for item in advanced["libraries"] if item.get("default")), None),
    )
    if not library:
        raise ValueError("No download library is configured.")
    relative = str(library.get("path", "")).strip().replace("\\", "/").strip("/")
    candidate = (download_root / relative).resolve()
    if candidate != download_root and download_root not in candidate.parents:
        raise ValueError("Library paths must stay inside /downloads.")
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def match_rule(advanced: dict, host: str, url: str, mode: str) -> dict:
    """Merge enabled declarative rules in order; later matches win."""
    result: dict = {}
    for rule in advanced.get("rules", []):
        if not rule.get("enabled", True):
            continue
        host_pattern = str(rule.get("host", "")).casefold().strip()
        url_contains = str(rule.get("url_contains", "")).casefold().strip()
        rule_mode = str(rule.get("mode", "")).casefold().strip()
        if host_pattern and not (
            host == host_pattern or host.endswith("." + host_pattern)
        ):
            continue
        if url_contains and url_contains not in url.casefold():
            continue
        if rule_mode and rule_mode != mode:
            continue
        for key in ("recipe_id", "library_id", "cookie_profile", "force_mode"):
            if rule.get(key):
                result[key] = rule[key]
        if isinstance(rule.get("tags"), list):
            result["tags"] = list(dict.fromkeys(result.get("tags", []) + rule["tags"]))
    return result


def cookie_file(config_root: Path, advanced: dict, profile_id: str) -> Path | None:
    profile = next(
        (item for item in advanced.get("cookie_profiles", [])
         if item.get("id") == profile_id and item.get("enabled", True)),
        None,
    )
    if not profile:
        return None
    filename = Path(str(profile.get("filename", ""))).name
    if not filename:
        return None
    candidate = (config_root / "cookies" / filename).resolve()
    cookie_root = (config_root / "cookies").resolve()
    if cookie_root not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


def init_advanced_storage(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
    for name, definition in (
        ("recipe_id", "TEXT"), ("library_id", "TEXT"),
        ("cookie_profile", "TEXT"), ("scheduled_at", "INTEGER"),
        ("max_items", "INTEGER"), ("date_after", "TEXT"),
        ("date_before", "TEXT"), ("retried_from", "TEXT"),
        ("duplicate_of", "TEXT"),
    ):
        if name not in columns:
            connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS media_index (
           path TEXT PRIMARY KEY, size INTEGER NOT NULL, modified INTEGER NOT NULL,
           sha256 TEXT NOT NULL, normalized_name TEXT NOT NULL, media_type TEXT NOT NULL,
           indexed_at INTEGER NOT NULL)"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS storage_reviews (
           id TEXT PRIMARY KEY, path TEXT NOT NULL, reason TEXT NOT NULL,
           size INTEGER NOT NULL, created_at INTEGER NOT NULL,
           status TEXT NOT NULL DEFAULT 'pending')"""
    )


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalized_name(path: Path) -> str:
    value = re.sub(r"\[[^\]]+\]|\([^\)]+\)", "", path.stem.casefold())
    return re.sub(r"[^a-z0-9]+", "", value)


def index_paths(connection: sqlite3.Connection, root: Path, paths: list[Path]) -> dict:
    indexed = exact = probable = 0
    for path in paths:
        if not path.is_file() or path.suffix.casefold() not in MEDIA_EXTENSIONS:
            continue
        stat = path.stat()
        relative = path.relative_to(root).as_posix()
        existing = connection.execute(
            "SELECT size, modified, sha256 FROM media_index WHERE path=?", (relative,)
        ).fetchone()
        digest = existing["sha256"] if existing and existing["size"] == stat.st_size and existing["modified"] == int(stat.st_mtime) else file_hash(path)
        name = normalized_name(path)
        exact_matches = connection.execute(
            "SELECT path FROM media_index WHERE sha256=? AND path<>?", (digest, relative)
        ).fetchall()
        probable_matches = connection.execute(
            """SELECT path FROM media_index WHERE normalized_name=? AND size BETWEEN ? AND ?
               AND path<>? AND sha256<>?""",
            (name, int(stat.st_size * .98), int(stat.st_size * 1.02), relative, digest),
        ).fetchall() if name else []
        connection.execute(
            """INSERT INTO media_index(path,size,modified,sha256,normalized_name,media_type,indexed_at)
               VALUES(?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
               size=excluded.size,modified=excluded.modified,sha256=excluded.sha256,
               normalized_name=excluded.normalized_name,media_type=excluded.media_type,
               indexed_at=excluded.indexed_at""",
            (relative, stat.st_size, int(stat.st_mtime), digest, name,
             "video" if path.suffix.casefold() in {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"} else
             "image" if path.suffix.casefold() in {".jpg", ".jpeg", ".png", ".webp", ".gif"} else "audio",
             int(time.time())),
        )
        indexed += 1
        exact += len(exact_matches)
        probable += len(probable_matches)
    return {"indexed": indexed, "exact_matches": exact, "probable_matches": probable}


def duplicate_groups(connection: sqlite3.Connection) -> list[dict]:
    groups = []
    for row in connection.execute(
        """SELECT sha256, COUNT(*) count, SUM(size) total_size
           FROM media_index GROUP BY sha256 HAVING COUNT(*) > 1 ORDER BY total_size DESC"""
    ):
        files = [dict(item) for item in connection.execute(
            "SELECT path,size,media_type FROM media_index WHERE sha256=? ORDER BY path",
            (row["sha256"],),
        )]
        groups.append({
            "kind": "exact", "fingerprint": row["sha256"][:12],
            "reclaimable_bytes": row["total_size"] - files[0]["size"], "files": files,
        })
    for row in connection.execute(
        """SELECT normalized_name, COUNT(*) count FROM media_index
           WHERE normalized_name<>'' GROUP BY normalized_name HAVING COUNT(*) > 1 LIMIT 100"""
    ):
        files = [dict(item) for item in connection.execute(
            "SELECT path,size,media_type,sha256 FROM media_index WHERE normalized_name=? ORDER BY path",
            (row["normalized_name"],),
        )]
        if len({item["sha256"] for item in files}) > 1 and max(item["size"] for item in files) <= min(item["size"] for item in files) * 1.02:
            groups.append({"kind": "probable", "fingerprint": row["normalized_name"][:24],
                           "reclaimable_bytes": 0, "files": files})
    return groups


def storage_candidates(connection: sqlite3.Connection, policy: dict) -> list[dict]:
    cutoff = int(time.time()) - int(policy["minimum_age_days"]) * 86400
    minimum = int(policy["minimum_size_mb"]) * 1024 * 1024
    return [dict(row) | {"reason": f"Older than {policy['minimum_age_days']} days; Stash play-count check required before deletion"}
            for row in connection.execute(
                "SELECT path,size,modified,media_type FROM media_index WHERE modified<? AND size>=? ORDER BY size DESC LIMIT 250",
                (cutoff, minimum),
            )]


def load_plugins(config_root: Path) -> list[dict]:
    plugins = []
    plugin_root = config_root / "plugins"
    plugin_root.mkdir(parents=True, exist_ok=True)
    for path in plugin_root.glob("*.json"):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(item, dict) or not item.get("name"):
                raise ValueError("name is required")
            allowed = {"name", "version", "description", "host_patterns", "default_recipe", "default_library", "tags"}
            plugins.append({key: value for key, value in item.items() if key in allowed} | {"file": path.name, "valid": True})
        except Exception as exc:
            plugins.append({"file": path.name, "valid": False, "error": str(exc)})
    return plugins


def emit_webhooks(webhooks: list[dict], event: str, data: dict) -> None:
    payload = json.dumps({"id": uuid.uuid4().hex, "event": event, "created_at": int(time.time()), "data": data},
                         separators=(",", ":")).encode()
    for hook in webhooks:
        if not hook.get("enabled", True) or event not in hook.get("events", []):
            continue
        url = str(hook.get("url", ""))
        if urlparse(url).scheme not in {"http", "https"}:
            continue
        signature = hmac.new(str(hook.get("secret", "")).encode(), payload, hashlib.sha256).hexdigest()
        request = urllib.request.Request(url, data=payload, method="POST", headers={
            "Content-Type": "application/json", "X-Stash-Dock-Event": event,
            "X-Stash-Dock-Signature": f"sha256={signature}",
        })
        try:
            urllib.request.urlopen(request, timeout=8).close()
        except Exception:
            pass
