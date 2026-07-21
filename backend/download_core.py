from __future__ import annotations

import json
import re
from datetime import date
from typing import Any


DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def valid_date(value: str) -> str:
    value = value.strip()
    if value and not DATE_PATTERN.fullmatch(value):
        raise ValueError("Dates must use YYYY-MM-DD format.")
    if value:
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("Enter a real calendar date in YYYY-MM-DD format.") from exc
    return value


def selection_args(engine: str, max_items: int | None,
                   date_after: str = "", date_before: str = "") -> list[str]:
    """Return conservative, engine-native collection selection arguments."""
    after = valid_date(date_after)
    before = valid_date(date_before)
    arguments: list[str] = []
    if engine == "gallery-dl":
        if max_items:
            arguments += ["--range", f"1-{max_items}"]
        if after:
            arguments += ["--date-after", after]
        if before:
            arguments += ["--date-before", before]
        return arguments
    if max_items:
        arguments += ["--playlist-end", str(max_items), "--max-downloads", str(max_items)]
    if after:
        arguments += ["--dateafter", after.replace("-", "")]
    if before:
        arguments += ["--datebefore", before.replace("-", "")]
    return arguments


def classify_failure(output: str) -> dict[str, str]:
    text = output.casefold()
    if "http error 429" in text or "too many requests" in text:
        return {"code": "RATE_LIMITED", "message": "The site is rate limiting requests. Wait before retrying."}
    if "no impersonate target" in text or "impersonation" in text and "required" in text:
        return {"code": "IMPERSONATION_REQUIRED", "message": "Browser-compatible networking is required for this site."}
    if "http error 403" in text or "forbidden" in text:
        return {"code": "ACCESS_BLOCKED", "message": "The site refused the request. A valid cookie profile may be required."}
    if "login required" in text or "sign in" in text or "authentication required" in text:
        return {"code": "LOGIN_REQUIRED", "message": "This content requires a valid signed-in cookie profile."}
    if "unsupported url" in text or "no suitable extractor" in text:
        return {"code": "UNSUPPORTED_URL", "message": "The selected engine does not support this URL."}
    if "timed out" in text or "timeout" in text:
        return {"code": "SITE_TIMEOUT", "message": "The site did not respond before the preflight timeout."}
    return {"code": "PREFLIGHT_FAILED", "message": "The link could not be inspected. Review the details or try the fallback engine."}


def parse_ytdlp_preflight(output: str, cap: int = 501) -> dict[str, Any]:
    data = json.loads(output.strip().splitlines()[-1])
    entries = data.get("entries") if isinstance(data, dict) else None
    count = len([item for item in entries or [] if item]) if isinstance(entries, list) else 1
    creator = ""
    for source in [data, *((entries or [])[:5] if isinstance(entries, list) else [])]:
        if not isinstance(source, dict):
            continue
        for key in ("uploader", "channel", "creator", "artist", "uploader_id", "channel_id"):
            if source.get(key):
                creator = str(source[key])
                break
        if creator:
            break
    return {
        "title": str(data.get("title") or data.get("playlist_title") or ""),
        "creator": creator,
        "item_count": count,
        "count_limited": bool(isinstance(entries, list) and len(entries) >= cap),
        "content_kind": "collection" if isinstance(entries, list) else "single",
    }


def parse_gallery_preflight(output: str, cap: int = 101) -> dict[str, Any]:
    records: list[Any] = []
    for line in output.splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    files = [item for item in records if isinstance(item, list) and item and item[0] in {2, 3}]
    metadata = next((item[-1] for item in records
                     if isinstance(item, list) and item and isinstance(item[-1], dict)), {})
    creator = ""
    for key in ("user", "username", "creator", "author", "account", "profile"):
        if metadata.get(key):
            creator = str(metadata[key])
            break
    return {
        "title": str(metadata.get("title") or metadata.get("album") or metadata.get("gallery_id") or ""),
        "creator": creator,
        "item_count": len(files) or None,
        "count_limited": len(files) >= cap,
        "content_kind": "collection" if len(files) != 1 else "single",
    }
