# Stash Dock

Stash Dock is a self-hosted download manager for media you are authorized to
save. One web interface routes gallery-style URLs to `gallery-dl`, video URLs
to `yt-dlp`, organizes the results, and then asks Stash to scan and associate
performers.

## Highlights

- One queue for gallery, video, and audio downloads
- Automatic URL routing with a manual mode override
- Configurable `site / creator / title` folder layouts
- Editable host routing and site labels
- Direct Stash GraphQL integration
- Write-only Stash API key field
- Connection test, manual sync, and redacted diagnostics export
- Persistent history and duplicate-download archives

## Quick start

```sh
docker compose up -d --build
```

Open `http://SERVER-IP:9091`, select **Settings**, enter the internal Stash URL
and a Stash API key, then select **Test connection**. The API key is stored only
in the persistent `/config` mapping and is never returned by the settings or
diagnostics APIs.

Default container mappings:

| Container path | Purpose |
| --- | --- |
| `/downloads` | The media folder that Stash scans |
| `/config` | Settings, queue database, archives, and logs |

## Stash setup

1. In Stash, add the host folder mapped to Stash Dock's `/downloads`.
2. Generate a Stash API key in **Settings → Security**.
3. In Stash Dock, open **Settings → Stash connection**.
4. Use the Docker-network URL when both apps share a network, or a LAN URL such
   as `http://SERVER-IP:6969`.
5. Paste the key, save, and test the connection.

Stash Dock calls Stash directly after a successful download. This is similar
to the API chain used by request managers and download clients: download,
organize, scan, then enrich.

## Organization

Choose one of these layouts in Settings:

- `Site / Creator / Title`
- `Creator / Site / Title`
- `Creator / Title`

Host lists control automatic routing. Site labels let users choose friendly
folder names without changing the downloader engine. If a creator cannot be
identified, the configured unknown-creator label is used.

## Support

Start with [Troubleshooting](docs/TROUBLESHOOTING.md). The Diagnostics page can
download a redacted JSON report suitable for a GitHub issue. Never post Stash
API keys, cookies, passwords, or authenticated URLs.

## Community Apps

Packaging and submission instructions are in
[docs/COMMUNITY-APPS.md](docs/COMMUNITY-APPS.md). The XML template is in
[`unraid/stash-dock.xml`](unraid/stash-dock.xml).

## Responsible use

Use Stash Dock only for content you own or have permission to download. Follow
applicable law and each source site's terms. The project does not bypass DRM,
paywalls, or access controls.

## License

MIT
