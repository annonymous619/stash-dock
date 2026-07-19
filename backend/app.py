from __future__ import annotations

import ipaddress
import json
import os
import queue
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from stash_integration import synchronize

APP_ROOT = Path(__file__).resolve().parent
WEB_ROOT = APP_ROOT / "web"
DOWNLOAD_ROOT = Path(os.getenv("DOWNLOAD_ROOT", "/downloads")).resolve()
CONFIG_ROOT = Path(os.getenv("CONFIG_ROOT", "/config")).resolve()
DB_PATH = CONFIG_ROOT / "jobs.sqlite3"
SETTINGS_PATH = CONFIG_ROOT / "settings.json"
MAX_LOG_LINES = 300
STASH_KEY_FILE = CONFIG_ROOT / "stash-api-key"

GALLERY_HOST_HINTS = {
    "erome.com", "imgur.com", "flickr.com", "deviantart.com", "reddit.com",
    "instagram.com", "twitter.com", "x.com", "tumblr.com", "pixiv.net",
}
VIDEO_HOST_HINTS = {
    "youtube.com", "youtu.be", "vimeo.com", "tiktok.com", "twitch.tv",
    "pornhub.com", "xvideos.com", "xnxx.com", "redgifs.com",
}

app = FastAPI(title="Stash Dock", version="0.2.0")
app.mount("/assets", StaticFiles(directory=WEB_ROOT / "assets"), name="assets")
jobs_queue: queue.Queue[str] = queue.Queue()
stash_queue: queue.Queue[str] = queue.Queue()
cancel_events: dict[str, threading.Event] = {}


class DownloadRequest(BaseModel):
    url: str = Field(min_length=8, max_length=4096)
    mode: Literal["auto", "gallery", "video", "audio"] = "auto"
    authorized: bool


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
    return settings


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


def job_command(engine: str, url: str, host: str, mode: str) -> list[str]:
    settings = load_settings()
    site = site_name(host)
    label = settings.get("site_labels", {}).get(site, site)
    if engine == "gallery-dl":
        return [
            "gallery-dl", "--config", str(CONFIG_ROOT / "gallery-dl.conf"),
            url,
        ]
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
    template = str(DOWNLOAD_ROOT.joinpath(*parts) / f"{title}.%(ext)s")
    command = [
        "yt-dlp", "--newline", "--no-progress", "--continue",
        "--no-overwrites", "--download-archive", str(CONFIG_ROOT / "yt-dlp-archive.txt"),
        "--write-info-json", "--write-thumbnail", "--convert-thumbnails", "jpg",
        "--embed-metadata", "--embed-thumbnail", "-o", template,
    ]
    if mode == "audio":
        command += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
    command.append(url)
    return command


def run_engine(job_id: str, engine: str, url: str, host: str, mode: str) -> int:
    command = job_command(engine, url, host, mode)
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
        code = run_engine(job_id, engine, row["url"], row["host"], row["requested_mode"])
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
    settings = load_settings()
    if status == "completed" and settings.get("sync_enabled") and settings.get("api_key"):
        stash_queue.put(job_id)


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
    threading.Thread(target=stash_worker, daemon=True, name="stash-worker").start()


@app.get("/")
def home() -> FileResponse:
    return FileResponse(WEB_ROOT / "index.html")


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
    engine = choose_engine(host, url, request.mode)
    job_id = uuid.uuid4().hex[:12]
    cancel_events[job_id] = threading.Event()
    with db() as connection:
        connection.execute(
            """INSERT INTO jobs
               (id,url,host,requested_mode,engine,status,created_at)
               VALUES (?,?,?,?,?,'queued',?)""",
            (job_id, url, host, request.mode, engine, int(time.time())),
        )
    jobs_queue.put(job_id)
    return {"id": job_id, "engine": engine, "status": "queued"}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    event = cancel_events.get(job_id)
    if not event:
        raise HTTPException(404, "Job not found.")
    event.set()
    return {"id": job_id, "status": "cancelling"}


@app.post("/api/stash/sync", status_code=202)
def sync_stash() -> dict:
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
    save_settings(updated)
    return public_settings()


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
