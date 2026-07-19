# Integration API

Stash Dock can generate its own API key for another container, application, or
automation. This key is separate from the Stash API key:

- The **Stash API key** lets Stash Dock call Stash.
- The **Stash Dock API key** lets another app call Stash Dock.

Generate a key under **Settings → Connect other apps**. It is displayed once
and stored as a one-way SHA-256 hash. Send it using either:

```http
X-API-Key: sd_your_key
```

or:

```http
Authorization: Bearer sd_your_key
```

## Check status

```http
GET /api/integrations/status
```

## Submit a download

```http
POST /api/integrations/download
Content-Type: application/json

{
  "url": "https://example.com/authorized-media",
  "mode": "auto",
  "authorized": true
}
```

Modes are `auto`, `gallery`, `video`, and `audio`. The response contains a job
ID.

## Check a job

```http
GET /api/integrations/jobs/JOB_ID
```

## Request a Stash sync

```http
POST /api/integrations/stash/sync
```

Not every media player accepts a generic downloader API. Apps that support
custom HTTP webhooks can call these endpoints directly; others may need a small
adapter. Never place the key in a public URL or post it in a support report.
