# Metadata handoff

Stash Dock writes one normalized JSON manifest after each successful job. This
gives gallery-dl and yt-dlp downloads the same predictable structure before
Stash processes them.

Each manifest contains:

- Schema and job identifiers
- Download time
- Original source URL, hostname, and site label
- Creator and post title
- Source categories and tags
- Exact media paths and media types

The synchronization process asks Stash to scan first, then matches scanned
scene file paths and gallery folders against the manifest. It adds the
performer, title, source URL, and tags while preserving metadata already
present in Stash.

Folder parsing remains enabled as a fallback for old downloads and sources that
do not expose enough metadata.

## Privacy

Manifests remain in `/config/manifests` and can contain source URLs or creator
names. Treat `/config` as private appdata. Diagnostics exports report only the
number of manifests and never include their contents.
