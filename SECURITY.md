# Security policy

Report security issues privately through GitHub's security advisory feature
after the repository is published. Do not open a public issue containing
credentials, cookies, private URLs, or API keys.

Stash Dock is intended to run on a trusted local network. For remote access,
place it behind an authenticated reverse proxy or identity-aware tunnel. Do not
publish port 9091 directly to the internet.

The Stash API key is stored in `/config/settings.json`, is masked in the UI, and
is excluded from diagnostics responses. Protect and back up `/config` as a
secret-bearing directory.
