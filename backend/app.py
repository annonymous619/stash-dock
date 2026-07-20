from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import queue
import re
import secrets
import shlex
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from stash_integration import synchronize
from advanced import (
    advanced_settings, cookie_file, duplicate_groups, emit_webhooks, index_paths,
    init_advanced_storage, load_plugins, match_rule, safe_library_root,
    storage_candidates,
)

APP_ROOT = Path(__file__).resolve().parent
WEB_ROOT = APP_ROOT / "web"
DOWNLOAD_ROOT = Path(os.getenv("DOWNLOAD_ROOT", "/downloads")).resolve()
CONFIG_ROOT = Path(os.getenv("CONFIG_ROOT", "/config")).resolve()
DB_PATH = CONFIG_ROOT / "jobs.sqlite3"
SETTINGS_PATH = CONFIG_ROOT / "settings.json"
MAX_LOG_LINES = 300
STASH_KEY_FILE = CONFIG_ROOT / "stash-api-key"
MANIFEST_ROOT = CONFIG_ROOT / "manifests"
MEDIA_EXTENSIONS = {
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v",
    ".jpg", ".jpeg", ".png", ".webp", ".gif",
    ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav",
}

GALLERY_HOST_HINTS = {
    "erome.com", "imgur.com", "flickr.com", "deviantart.com", "reddit.com",
    "instagram.com", "twitter.com", "x.com", "tumblr.com", "pixiv.net",
}
VIDEO_HOST_HINTS = {
    "youtube.com", "youtu.be", "vimeo.com", "tiktok.com", "twitch.tv",
    "pornhub.com", "xvideos.com", "xnxx.com", "redgifs.com",
}

app = FastAPI(title="Stash Dock", version="0.6.0")
app.mount("/assets", StaticFiles(directory=WEB_ROOT / "assets"), name="assets")
jobs_queue: queue.Queue[str] = queue.Queue()
stash_queue: queue.Queue[str] = queue.Queue()
cancel_events: dict[str, threading.Event] = {}


class DownloadRequest(BaseModel):
    url: str = Field(min_length=8, max_length=4096)
    mode: Literal["auto", "gallery", "video", "audio"] = "auto"
    authorized: bool
    recipe_id: str = Field(default="original", max_length=80)
    library_id: str = Field(default="stash", max_length=80)
    scheduled_at: int | None = Field(default=None, ge=0)


class AdvancedSettingsRequest(BaseModel):
    recipes: list[dict[str, Any]] = Field(max_length=50)
    libraries: list[dict[str, Any]] = Field(max_length=30)
    webhooks: list[dict[str, Any]] = Field(default_factory=list, max_length=30)
    storage_policy: dict[str, Any]
    plugins_enabled: bool = True
    rules: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    cookie_profiles: list[dict[str, Any]] = Field(default_factory=list, max_length=30)
    feature_toggles: dict[str, bool] = Field(default_factory=dict)


class ConfigImportRequest(BaseModel):
    bundle: dict[str, Any]


class SettingsRequest(BaseModel):
    stash_url: str = Field(min_length=8, max_length=512)
    api_key: str = Field(default="", max_length=4096)
    sync_enabled: bool = True
    scan_wait_seconds: int = Field(default=25, ge=5, le=300)
    unknown_creator_label: str = Field(default="Unknown Creator", min_length=1, max_length=80)
    folder_layout: Literal[
        "site_creator_title", "creator_site_title", "creator_title"
    ] = "site_creator_title"
    gallery_hosts: list[str] = Field(default_factory=list, max_length=100)
    video_hosts: list[str] = Field(default_factory=list, max_length=100)
    site_labels: dict[str, str] = Field(default_factory=dict)


DEFAULT_SETTINGS = {
    "stash_url": os.getenv("STASH_URL", "http://stash:9999"),
    "api_key": "",
    "sync_enabled": True,
    "scan_wait_seconds": 25,
    "unknown_creator_label": "Unknown Creator",
    "folder_layout": "site_creator_title",
    "gallery_hosts": sorted(GALLERY_HOST_HINTS),
    "video_hosts": sorted(VIDEO_HOST_HINTS),
    "site_labels": {"erome": "Erome", "pornhub": "Pornhub",
                    "xvideos": "XVideos", "xnxx": "XNXX"},
    "integration_api_key_hash": "",
    "integration_api_key_last_four": "",
    "integration_api_key_created_at": 0,
}


def load_settings() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    if SETTINGS_PATH.is_file():
        try:
            saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                settings.update(saved)
        except (OSError, json.JSONDecodeError):
            pass
    if not settings.get("api_key") and STASH_KEY_FILE.is_file():
        settings["api_key"] = STASH_KEY_FILE.read_text(encoding="utf-8").strip()
    return settings


def save_settings(settings: dict) -> None:
    temporary = SETTINGS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    temporary.replace(SETTINGS_PATH)


def public_settings() -> dict:
    settings = load_settings()
    settings["api_key"] = ""
    settings["api_key_configured"] = bool(load_settings().get("api_key"))
    settings["integration_api_configured"] = bool(
        settings.pop("integration_api_key_hash", "")
    )
    if isinstance(settings.get("advanced"), dict):
        for hook in settings["advanced"].get("webhooks", []):
            hook.pop("secret", None)
    return settings


def require_integration_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> None:
    supplied = x_api_key or ""
    if not supplied and authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    expected = load_settings().get("integration_api_key_hash", "")
    if not expected or not supplied:
        raise HTTPException(401, "A valid Stash Dock API key is required.")
    digest = hashlib.sha256(supplied.encode()).hexdigest()
    if not hmac.compare_digest(digest, expected):
        raise HTTPException(401, "A valid Stash Dock API key is required.")


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_storage() -> None:
    CONFIG_ROOT.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    gallery_config = CONFIG_ROOT / "gallery-dl.conf"
    if not gallery_config.exists():
        shutil.copy2(APP_ROOT / "gallery-dl.conf", gallery_config)
    with db() as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, url TEXT NOT NULL, host TEXT NOT NULL,
                requested_mode TEXT NOT NULL, engine TEXT NOT NULL,
                status TEXT NOT NULL, created_at INTEGER NOT NULL,
                started_at INTEGER, finished_at INTEGER, output_path TEXT,
                error TEXT, log TEXT NOT NULL DEFAULT ''
            )"""
        )
        init_advanced_storage(connection)


def public_http_url(raw: str) -> tuple[str, str]:
    parsed = urlparse(raw.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(400, "Enter a complete http:// or https:// URL.")
    if parsed.username or parsed.password:
        raise HTTPException(400, "URLs containing usernames or passwords are not accepted.")
    host = parsed.hostname.lower().rstrip(".")
    try:
        records = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise HTTPException(400, f"That host could not be resolved: {exc}") from exc
    for record in records:
        address = ipaddress.ip_address(record[4][0])
        if not address.is_global:
            raise HTTPException(400, "Local and private network addresses are blocked.")
    return parsed.geturl(), host


def site_name(host: str) -> str:
    host = host.removeprefix("www.").removeprefix("m.")
    label = host.split(".")[0]
    return re.sub(r"[^a-z0-9_-]+", "-", label)[:60] or "site"


def host_matches(host: str, hints: set[str]) -> bool:
    return any(host == hint or host.endswith("." + hint) for hint in hints)


def choose_engine(host: str, url: str, mode: str) -> str:
    if mode == "gallery":
        return "gallery-dl"
    if mode in {"video", "audio"}:
        return "yt-dlp"
    hints = set(load_settings().get("gallery_hosts") or GALLERY_HOST_HINTS)
    if host_matches(host, hints) or re.search(r"/(photos?|gallery|albums?)(/|$)", url, re.I):
        return "gallery-dl"
    return "yt-dlp"


def append_log(job_id: str, line: str) -> None:
    clean = line.rstrip()
    if not clean:
        return
    with db() as connection:
        row = connection.execute("SELECT log FROM jobs WHERE id=?", (job_id,)).fetchone()
        current = (row["log"] if row else "").splitlines()
        current.append(clean)
        connection.execute(
            "UPDATE jobs SET log=? WHERE id=?",
            ("\n".join(current[-MAX_LOG_LINES:]), job_id),
        )


def job_command(engine: str, url: str, host: str, mode: str,
                recipe_id: str = "original", library_id: str = "stash",
                cookie_profile: str = "") -> list[str]:
    settings = load_settings()
    advanced = advanced_settings(settings)
    recipe = next((item for item in advanced["recipes"] if item.get("id") == recipe_id), advanced["recipes"][0])
    root = safe_library_root(DOWNLOAD_ROOT, advanced, library_id)
    cookies = cookie_file(CONFIG_ROOT, advanced, cookie_profile)
    site = site_name(host)
    label = settings.get("site_labels", {}).get(site, site)
    if engine == "gallery-dl":
        command = [
            "gallery-dl", "--config", str(CONFIG_ROOT / "gallery-dl.conf"),
            "--directory", str(root),
        ]
        if cookies:
            command += ["--cookies", str(cookies)]
        return command + [url]
    creator = (
        "%(uploader,channel,creator,artist,uploader_id,channel_id|"
        + settings.get("unknown_creator_label", "Unknown Creator")
        + ")s"
    )
    title = "%(title).180B [%(id)s]"
    layout = settings.get("folder_layout", "site_creator_title")
    parts = {
        "site_creator_title": (label, creator, title),
        "creator_site_title": (creator, label, title),
        "creator_title": (creator, title),
    }[layout]
    template = str(root.joinpath(*parts) / f"{title}.%(ext)s")
    command = [
        "yt-dlp", "--newline", "--no-progress", "--continue",
        "--no-overwrites", "--download-archive", str(CONFIG_ROOT / "yt-dlp-archive.txt"),
        "--write-info-json", "--write-thumbnail", "--convert-thumbnails", "jpg",
        "--embed-metadata", "--embed-thumbnail", "-o", template,
    ]
    if cookies:
        command += ["--cookies", str(cookies)]
    if mode == "audio":
        command += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
    elif recipe.get("format") not in {"best", "original", None}:
        command += ["-f", str(recipe["format"])]
    command.append(url)
    return command


def run_engine(job_id: str, engine: str, url: str, host: str, mode: str,
               recipe_id: str, library_id: str, cookie_profile: str) -> int:
    command = job_command(
        engine, url, host, mode, recipe_id, library_id, cookie_profile
    )
    append_log(job_id, f"Using {engine}: {shlex.join(command[:-1])} [URL]")
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    assert process.stdout
    for line in process.stdout:
        append_log(job_id, line)
        if cancel_events[job_id].is_set():
            process.terminate()
            append_log(job_id, "Cancellation requested.")
            break
    return process.wait()


def snapshot_files() -> set[str]:
    return {str(path) for path in DOWNLOAD_ROOT.rglob("*") if path.is_file()}


def metadata_value(metadata: dict, *keys: str) -> str:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def infer_path_metadata(path: Path, settings: dict) -> tuple[str, str, str]:
    try:
        parts = path.relative_to(DOWNLOAD_ROOT).parts
    except ValueError:
        return "", "", ""
    if len(parts) < 2:
        return (parts[0] if parts else "", "", path.stem)
    layout = settings.get("folder_layout", "site_creator_title")
    if layout == "creator_site_title" and len(parts) >= 3:
        return parts[1], parts[0], parts[2]
    if layout == "creator_title":
        return "", parts[0], parts[1]
    return parts[0], parts[1], parts[2] if len(parts) >= 3 else path.stem


def write_manifest(job_id: str, row: sqlite3.Row, created_files: set[str]) -> Path:
    settings = load_settings()
    paths = sorted(Path(item) for item in created_files)
    sidecars = [path for path in paths if path.name.endswith(".info.json")]
    metadata: dict = {}
    for sidecar in sidecars:
        try:
            candidate = json.loads(sidecar.read_text(encoding="utf-8"))
            if isinstance(candidate, dict):
                metadata = candidate
                break
        except (OSError, json.JSONDecodeError):
            continue
    media = [path for path in paths if path.suffix.casefold() in MEDIA_EXTENSIONS]
    path_site, path_creator, path_title = infer_path_metadata(
        media[0] if media else DOWNLOAD_ROOT, settings
    )
    site = metadata_value(metadata, "extractor_key", "extractor") or path_site
    creator = metadata_value(
        metadata, "uploader", "channel", "creator", "artist",
        "uploader_id", "channel_id",
    ) or path_creator or settings.get("unknown_creator_label", "Unknown Creator")
    title = metadata_value(metadata, "title", "fulltitle", "album") or path_title
    source_url = metadata_value(
        metadata, "webpage_url", "original_url"
    ) or row["url"]
    raw_tags = [
        value for key in ("tags", "categories", "genres")
        for value in (metadata.get(key) or []) if isinstance(value, str)
    ]
    media_entries = []
    for path in media:
        try:
            relative = path.relative_to(DOWNLOAD_ROOT).as_posix()
        except ValueError:
            continue
        suffix = path.suffix.casefold()
        media_type = (
            "video" if suffix in {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}
            else "image" if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}
            else "audio"
        )
        media_entries.append({"path": relative, "type": media_type})
    manifest = {
        "schema_version": 1,
        "job_id": job_id,
        "created_at": int(time.time()),
        "source": {"url": source_url, "host": row["host"], "site": site},
        "creator": creator,
        "title": title,
        "tags": sorted(set(
            ["Stash Dock", f"Source: {site or site_name(row['host'])}"] + raw_tags
        ), key=str.casefold),
        "media": media_entries,
    }
    MANIFEST_ROOT.mkdir(parents=True, exist_ok=True)
    destination = MANIFEST_ROOT / f"{job_id}.json"
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    temporary.replace(destination)
    return destination


def process_job(job_id: str) -> None:
    with db() as connection:
        row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        connection.execute(
            "UPDATE jobs SET status='running', started_at=? WHERE id=?",
            (int(time.time()), job_id),
        )
    if not row:
        return
    before = snapshot_files()
    first_engine = row["engine"]
    engines = [first_engine]
    if row["requested_mode"] == "auto":
        engines.append("yt-dlp" if first_engine == "gallery-dl" else "gallery-dl")
    success = False
    error = ""
    used_engine = first_engine
    for index, engine in enumerate(engines):
        if cancel_events[job_id].is_set():
            break
        used_engine = engine
        code = run_engine(job_id, engine, row["url"], row["host"], row["requested_mode"],
                          row["recipe_id"] or "original", row["library_id"] or "stash",
                          row["cookie_profile"] or "")
        new_files = snapshot_files() - before
        if code == 0 and new_files:
            success = True
            break
        if code == 0 and not new_files:
            append_log(job_id, f"{engine} completed but produced no new files.")
        else:
            append_log(job_id, f"{engine} exited with code {code}.")
        if index == 0 and len(engines) > 1:
            append_log(job_id, f"Trying fallback engine: {engines[1]}.")
    status = "cancelled" if cancel_events[job_id].is_set() else ("completed" if success else "failed")
    if status == "failed":
        error = "Neither downloader produced a new file. The site may require cookies, block automation, or be unsupported."
    labels = load_settings().get("site_labels", {})
    output_site = labels.get(
        site_name(row["host"]),
        "Erome" if host_matches(row["host"], {"erome.com"}) else site_name(row["host"]),
    )
    output = str(DOWNLOAD_ROOT / output_site)
    with db() as connection:
        connection.execute(
            """UPDATE jobs SET status=?, engine=?, finished_at=?, output_path=?, error=?
               WHERE id=?""",
            (status, used_engine, int(time.time()), output, error, job_id),
        )
    if status == "completed":
        created = snapshot_files() - before
        manifest = write_manifest(job_id, row, created)
        append_log(job_id, f"Metadata manifest written: {manifest.name}")
        with db() as connection:
            stats = index_paths(connection, DOWNLOAD_ROOT, [Path(item) for item in created])
        append_log(job_id, f"Duplicate analysis: {json.dumps(stats, sort_keys=True)}")
    settings = load_settings()
    if (status == "completed" and settings.get("sync_enabled")
            and settings.get("api_key")
            and advanced_settings(settings)["feature_toggles"].get("stash_sync", True)):
        stash_queue.put(job_id)
    if status in {"completed", "failed"}:
        threading.Thread(
            target=emit_webhooks,
            args=(advanced_settings(settings)["webhooks"], f"download.{status}",
                  {"job_id": job_id, "url": row["url"], "engine": used_engine}),
            daemon=True,
        ).start()


def worker() -> None:
    while True:
        job_id = jobs_queue.get()
        try:
            process_job(job_id)
        except Exception as exc:
            append_log(job_id, f"Internal error: {exc}")
            with db() as connection:
                connection.execute(
                    "UPDATE jobs SET status='failed', finished_at=?, error=? WHERE id=?",
                    (int(time.time()), str(exc), job_id),
                )
        finally:
            jobs_queue.task_done()


def schedule_worker() -> None:
    while True:
        now = int(time.time())
        with db() as connection:
            due = connection.execute(
                """SELECT id FROM jobs WHERE status='scheduled'
                   AND scheduled_at<=? ORDER BY scheduled_at LIMIT 20""", (now,)
            ).fetchall()
            for row in due:
                connection.execute(
                    "UPDATE jobs SET status='queued' WHERE id=?", (row["id"],)
                )
                cancel_events[row["id"]] = threading.Event()
                jobs_queue.put(row["id"])
        time.sleep(15)


def stash_worker() -> None:
    while True:
        job_id = stash_queue.get()
        try:
            settings = load_settings()
            api_key = settings.get("api_key", "").strip()
            if not api_key:
                raise RuntimeError("Stash API key is empty.")
            append_log(job_id, "Starting direct Stash scan and metadata synchronization.")
            stats = synchronize(
                settings["stash_url"], api_key, DOWNLOAD_ROOT,
                int(settings.get("scan_wait_seconds", 25)),
                CONFIG_ROOT / "avatars",
                MANIFEST_ROOT,
            )
            append_log(job_id, f"Stash synchronization complete: {json.dumps(stats, sort_keys=True)}")
        except Exception as exc:
            append_log(job_id, f"Stash synchronization error: {exc}")
        finally:
            stash_queue.task_done()


@app.on_event("startup")
def startup() -> None:
    init_storage()
    threading.Thread(target=worker, daemon=True, name="download-worker").start()
    threading.Thread(target=schedule_worker, daemon=True, name="schedule-worker").start()
    threading.Thread(target=stash_worker, daemon=True, name="stash-worker").start()


@app.get("/")
def home() -> FileResponse:
    return FileResponse(WEB_ROOT / "index.html")


@app.get("/manifest.webmanifest")
def web_manifest() -> FileResponse:
    return FileResponse(WEB_ROOT / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/service-worker.js")
def service_worker() -> FileResponse:
    return FileResponse(WEB_ROOT / "service-worker.js", media_type="application/javascript")


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "queue": jobs_queue.qsize(),
        "stash_queue": stash_queue.qsize(),
        "stash_configured": bool(load_settings().get("api_key")),
    }


@app.get("/api/jobs")
def list_jobs() -> list[dict]:
    with db() as connection:
        rows = connection.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/jobs", status_code=202)
def create_job(request: DownloadRequest) -> dict:
    if not request.authorized:
        raise HTTPException(400, "Confirm that you are authorized to save this media.")
    url, host = public_http_url(request.url)
    advanced = advanced_settings(load_settings())
    toggles = advanced.get("feature_toggles", {})
    if not toggles.get("downloads", True):
        raise HTTPException(503, "Downloads are disabled by the administrator.")
    if request.mode == "audio" and not toggles.get("audio_mode", True):
        raise HTTPException(400, "Audio mode is disabled.")
    if request.scheduled_at and not toggles.get("schedules", True):
        raise HTTPException(400, "Scheduled downloads are disabled.")
    rule = match_rule(advanced, host, url, request.mode)
    mode = str(rule.get("force_mode", request.mode))
    recipe_id = str(rule.get("recipe_id", request.recipe_id))
    library_id = str(rule.get("library_id", request.library_id))
    cookie_profile = str(rule.get("cookie_profile", ""))
    safe_library_root(DOWNLOAD_ROOT, advanced, library_id)
    engine = choose_engine(host, url, mode)
    job_id = uuid.uuid4().hex[:12]
    cancel_events[job_id] = threading.Event()
    scheduled = bool(request.scheduled_at and request.scheduled_at > int(time.time()))
    status = "scheduled" if scheduled else "queued"
    with db() as connection:
        connection.execute(
            """INSERT INTO jobs
               (id,url,host,requested_mode,engine,status,created_at,recipe_id,
                library_id,cookie_profile,scheduled_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (job_id, url, host, mode, engine, status, int(time.time()),
             recipe_id, library_id, cookie_profile, request.scheduled_at),
        )
    if not scheduled:
        jobs_queue.put(job_id)
    return {
        "id": job_id, "engine": engine, "status": status,
        "rule_applied": bool(rule), "recipe_id": recipe_id,
        "library_id": library_id,
    }


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    event = cancel_events.get(job_id)
    if not event:
        raise HTTPException(404, "Job not found.")
    event.set()
    with db() as connection:
        connection.execute(
            """UPDATE jobs SET status='cancelled',finished_at=?
               WHERE id=? AND status='scheduled'""", (int(time.time()), job_id)
        )
    return {"id": job_id, "status": "cancelling"}


@app.post("/api/stash/sync", status_code=202)
def sync_stash() -> dict:
    if not advanced_settings(load_settings())["feature_toggles"].get("stash_sync", True):
        raise HTTPException(503, "Stash synchronization is disabled.")
    if not load_settings().get("api_key"):
        raise HTTPException(503, "Stash API key is not configured.")
    sync_id = "stash-sync-" + uuid.uuid4().hex[:8]
    with db() as connection:
        connection.execute(
            """INSERT INTO jobs
               (id,url,host,requested_mode,engine,status,created_at,finished_at,output_path)
               VALUES (?,?,?,?,?,'completed',?,?,?)""",
            (
                sync_id, "stash://manual-sync", "stash", "sync", "stash-api",
                int(time.time()), int(time.time()), str(DOWNLOAD_ROOT),
            ),
        )
    stash_queue.put(sync_id)
    return {"id": sync_id, "status": "queued"}


@app.get("/api/settings")
def get_settings() -> dict:
    return public_settings()


@app.get("/api/advanced")
def get_advanced() -> dict:
    result = advanced_settings(load_settings())
    for hook in result["webhooks"]:
        hook["secret_configured"] = bool(hook.pop("secret", ""))
    return result


@app.put("/api/advanced")
def update_advanced(request: AdvancedSettingsRequest) -> dict:
    current = load_settings()
    value = request.model_dump()
    ids: set[str] = set()
    for library in value["libraries"]:
        identifier = re.sub(r"[^a-z0-9_-]", "-", str(library.get("id", "")).casefold()).strip("-")
        if not identifier or identifier in ids:
            raise HTTPException(400, "Every library needs a unique ID.")
        ids.add(identifier)
        library["id"] = identifier
        safe_library_root(DOWNLOAD_ROOT, value, identifier)
    if not value["libraries"]:
        raise HTTPException(400, "Keep at least one library.")
    recipe_ids = {str(item.get("id", "")) for item in value["recipes"]}
    if not recipe_ids or "" in recipe_ids:
        raise HTTPException(400, "Every recipe needs an ID.")
    profile_ids: set[str] = set()
    for profile in value["cookie_profiles"]:
        identifier = re.sub(
            r"[^a-z0-9_-]", "-", str(profile.get("id", "")).casefold()
        ).strip("-")
        if not identifier or identifier in profile_ids:
            raise HTTPException(400, "Every cookie profile needs a unique ID.")
        profile_ids.add(identifier)
        profile["id"] = identifier
        profile["filename"] = Path(str(profile.get("filename", ""))).name
    for rule in value["rules"]:
        if rule.get("recipe_id") and rule["recipe_id"] not in recipe_ids:
            raise HTTPException(400, f"Rule references unknown recipe: {rule['recipe_id']}")
        if rule.get("library_id") and rule["library_id"] not in ids:
            raise HTTPException(400, f"Rule references unknown library: {rule['library_id']}")
        if rule.get("cookie_profile") and rule["cookie_profile"] not in profile_ids:
            raise HTTPException(400, f"Rule references unknown cookie profile: {rule['cookie_profile']}")
    old_hooks = {item.get("id"): item for item in advanced_settings(current)["webhooks"]}
    for hook in value["webhooks"]:
        if not hook.get("secret"):
            hook["secret"] = old_hooks.get(hook.get("id"), {}).get("secret", "")
    current["advanced"] = value
    save_settings(current)
    return get_advanced()


@app.post("/api/library/import")
def import_library() -> dict:
    paths = [path for path in DOWNLOAD_ROOT.rglob("*") if path.is_file()]
    with db() as connection:
        stats = index_paths(connection, DOWNLOAD_ROOT, paths)
    return {**stats, "files_seen": len(paths)}


@app.get("/api/duplicates")
def duplicates() -> dict:
    if not advanced_settings(load_settings())["feature_toggles"].get("duplicate_review", True):
        raise HTTPException(503, "Duplicate review is disabled.")
    with db() as connection:
        groups = duplicate_groups(connection)
    return {"groups": groups, "count": len(groups)}


@app.get("/api/storage/candidates")
def review_storage() -> dict:
    if not advanced_settings(load_settings())["feature_toggles"].get("storage_review", True):
        raise HTTPException(503, "Storage review is disabled.")
    policy = advanced_settings(load_settings())["storage_policy"]
    with db() as connection:
        candidates = storage_candidates(connection, policy)
    return {"policy": policy, "candidates": candidates,
            "note": "Review only. No files are deleted by this endpoint."}


@app.get("/api/plugins")
def plugins() -> dict:
    if not advanced_settings(load_settings())["feature_toggles"].get("plugins", True):
        raise HTTPException(503, "Plugins are disabled.")
    return {"plugins": load_plugins(CONFIG_ROOT),
            "format": "Declarative JSON only; plugins cannot execute code."}


@app.get("/api/config/export")
def export_config() -> JSONResponse:
    settings = load_settings()
    bundle = {
        "schema_version": 1,
        "exported_at": int(time.time()),
        "app": "Stash Dock",
        "app_version": app.version,
        "core": {
            key: settings.get(key, DEFAULT_SETTINGS[key])
            for key in (
                "sync_enabled", "scan_wait_seconds", "unknown_creator_label",
                "folder_layout", "gallery_hosts", "video_hosts", "site_labels",
            )
        },
        "advanced": advanced_settings(settings),
    }
    for hook in bundle["advanced"]["webhooks"]:
        hook.pop("secret", None)
        hook["enabled"] = False
    bundle["advanced"]["cookie_notice"] = (
        "Cookie files and account credentials are never included in exports."
    )
    return JSONResponse(
        bundle,
        headers={"Content-Disposition": "attachment; filename=stash-dock-community-config.json"},
    )


@app.post("/api/config/import")
def import_config(request: ConfigImportRequest) -> dict:
    bundle = request.bundle
    if bundle.get("schema_version") != 1 or not isinstance(bundle.get("advanced"), dict):
        raise HTTPException(400, "This is not a supported Stash Dock configuration bundle.")
    current = load_settings()
    core = bundle.get("core", {})
    allowed_core = {
        "sync_enabled", "scan_wait_seconds", "unknown_creator_label",
        "folder_layout", "gallery_hosts", "video_hosts", "site_labels",
    }
    for key in allowed_core:
        if key in core:
            current[key] = core[key]
    imported = bundle["advanced"]
    imported.pop("cookie_notice", None)
    for hook in imported.get("webhooks", []):
        hook.pop("secret", None)
        hook["enabled"] = False
    validated = AdvancedSettingsRequest.model_validate(imported)
    update_advanced(validated)
    current = load_settings()
    for key in allowed_core:
        if key in core:
            current[key] = core[key]
    save_settings(current)
    return {
        "imported": True,
        "secrets_imported": False,
        "note": "Stash keys, webhook secrets, passwords, and cookie contents were not imported.",
    }


@app.put("/api/settings")
def update_settings(request: SettingsRequest) -> dict:
    current = load_settings()
    updated = request.model_dump()
    updated["stash_url"] = updated["stash_url"].rstrip("/")
    if not updated["api_key"]:
        updated["api_key"] = current.get("api_key", "")
    updated["gallery_hosts"] = sorted({
        host.lower().strip().removeprefix("www.")
        for host in updated["gallery_hosts"] if host.strip()
    })
    updated["video_hosts"] = sorted({
        host.lower().strip().removeprefix("www.")
        for host in updated["video_hosts"] if host.strip()
    })
    updated["site_labels"] = {
        key.lower().strip(): value.strip()
        for key, value in updated["site_labels"].items()
        if key.strip() and value.strip()
    }
    for key in (
        "integration_api_key_hash", "integration_api_key_last_four",
        "integration_api_key_created_at", "advanced",
    ):
        updated[key] = current.get(key, DEFAULT_SETTINGS.get(key))
    save_settings(updated)
    return public_settings()


@app.post("/api/settings/integration-key")
def generate_integration_key() -> dict:
    plain_key = "sd_" + secrets.token_urlsafe(32)
    settings = load_settings()
    settings["integration_api_key_hash"] = hashlib.sha256(
        plain_key.encode()
    ).hexdigest()
    settings["integration_api_key_last_four"] = plain_key[-4:]
    settings["integration_api_key_created_at"] = int(time.time())
    save_settings(settings)
    return {
        "api_key": plain_key,
        "last_four": plain_key[-4:],
        "created_at": settings["integration_api_key_created_at"],
        "warning": "Copy this key now. It will not be shown again.",
    }


@app.delete("/api/settings/integration-key")
def revoke_integration_key() -> dict:
    settings = load_settings()
    settings["integration_api_key_hash"] = ""
    settings["integration_api_key_last_four"] = ""
    settings["integration_api_key_created_at"] = 0
    save_settings(settings)
    return {"revoked": True}


@app.get("/api/integrations/status")
def integration_status(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> dict:
    require_integration_key(x_api_key, authorization)
    return {**health(), "version": app.version}


@app.post("/api/integrations/download", status_code=202)
def integration_download(
    request: DownloadRequest,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> dict:
    require_integration_key(x_api_key, authorization)
    return create_job(request)


@app.get("/api/integrations/jobs/{job_id}")
def integration_job(
    job_id: str,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> dict:
    require_integration_key(x_api_key, authorization)
    with db() as connection:
        row = connection.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Job not found.")
    return dict(row)


@app.post("/api/integrations/stash/sync", status_code=202)
def integration_sync(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> dict:
    require_integration_key(x_api_key, authorization)
    return sync_stash()


@app.post("/api/settings/test")
def test_stash() -> dict:
    settings = load_settings()
    if not settings.get("api_key"):
        raise HTTPException(400, "Paste a Stash API key and save settings first.")
    try:
        from stash_integration import StashClient
        inventory = StashClient(
            settings["stash_url"], settings["api_key"]
        ).inventory()
    except Exception as exc:
        raise HTTPException(502, f"Stash connection failed: {exc}") from exc
    return {
        "connected": True,
        "performers": len(inventory["findPerformers"]["performers"]),
        "scenes": len(inventory["findScenes"]["scenes"]),
        "galleries": len(inventory["findGalleries"]["galleries"]),
    }


@app.get("/api/diagnostics")
def diagnostics() -> dict:
    def version(command: list[str]) -> str:
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=10, check=False
            )
            return (result.stdout or result.stderr).splitlines()[0][:200]
        except Exception as exc:
            return f"unavailable: {exc}"

    settings = load_settings()
    with db() as connection:
        counts = {
            row["status"]: row["count"]
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
            ).fetchall()
        }
    return {
        "app_version": app.version,
        "gallery_dl": version(["gallery-dl", "--version"]),
        "yt_dlp": version(["yt-dlp", "--version"]),
        "ffmpeg": version(["ffmpeg", "-version"]),
        "downloads_writable": os.access(DOWNLOAD_ROOT, os.W_OK),
        "config_writable": os.access(CONFIG_ROOT, os.W_OK),
        "stash_url": settings["stash_url"],
        "stash_configured": bool(settings.get("api_key")),
        "integration_api_configured": bool(
            settings.get("integration_api_key_hash")
        ),
        "metadata_manifests": len(list(MANIFEST_ROOT.glob("*.json")))
        if MANIFEST_ROOT.is_dir() else 0,
        "sync_enabled": settings.get("sync_enabled", False),
        "queue": jobs_queue.qsize(),
        "stash_queue": stash_queue.qsize(),
        "job_counts": counts,
    }


@app.get("/api/diagnostics/export")
def export_diagnostics() -> JSONResponse:
    payload = diagnostics()
    payload["recent_jobs"] = [
        {key: value for key, value in row.items() if key != "log"}
        for row in list_jobs()[:10]
    ]
    return JSONResponse(
        payload,
        headers={"Content-Disposition": "attachment; filename=stash-dock-diagnostics.json"},
    )
