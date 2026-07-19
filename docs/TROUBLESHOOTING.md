# Troubleshooting

## First checks

1. Open **Diagnostics** in Stash Dock.
2. Confirm the download and config paths are writable.
3. Select **Test Stash connection**.
4. Download the redacted report and attach it to a bug report.

The report excludes the Stash API key. Review it before posting in case a job
URL or folder name contains information you do not want to share.

## Stash connection fails

- Use a URL reachable from inside the Stash Dock container.
- Confirm Stash is running and its port is correct.
- Generate a new API key in Stash and save it in Stash Dock.
- If Docker containers share a custom network, prefer the container name.
- Do not use `localhost`; inside the container that means Stash Dock itself.

## Download succeeds but Stash shows nothing

- Map Stash Dock `/downloads` and a Stash library path to the same host folder.
- Confirm Stash can read the files and run **Manual Stash sync**.
- Check the file extension is enabled in Stash.
- Review the sync job log for scan or GraphQL errors.

## Creator is missing or incorrect

- Confirm the source URL represents a creator or post page.
- Check the resulting folder structure in the job log.
- Configure a clear unknown-creator label.
- Some extractors provide limited metadata. Include the redacted diagnostics
  report and a non-sensitive example URL when opening an issue.

## Performer has no avatar

Stash Dock prefers a downloaded image. For video-only creators it first uses a
Stash-generated scene screenshot, then falls back to extracting a frame roughly
one-third into a local video with FFmpeg. Generated frames are cached under
`/config/avatars`. Run **Manual Stash sync** after the scene scan completes.

## A site stopped working

The container updates `gallery-dl` and `yt-dlp` when a new image is built. Site
changes can temporarily break extractors. Record the detected engine and full
error from the job log, then try the manual gallery or video mode. Do not post
account cookies or credentials.

## Permission denied

On Unraid, ensure the host media and appdata directories are writable by the
container user. The Community Apps template defaults to Unraid's common
`PUID=99` and `PGID=100`.

## Duplicate or skipped files

Stash Dock keeps extractor archives in `/config` to prevent accidental repeated
downloads. Back up `/config` before deleting those archives. Removing them
allows previously recorded items to be attempted again.
