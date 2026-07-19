# Contributing

Bug reports and pull requests are welcome.

For bugs, use the issue form and attach a redacted diagnostics export. Remove
private URLs and creator names if they are not necessary to reproduce the
problem. Never submit credentials, cookies, or API keys.

For routing changes, include the hostname, expected engine, selected mode, and
an example URL that can be shared legally. Keep site-specific behavior in
configuration when possible.

Before a pull request:

```sh
python -m py_compile backend/app.py backend/stash_integration.py
node --check backend/web/assets/app.js
docker build -t stash-dock:test .
```
