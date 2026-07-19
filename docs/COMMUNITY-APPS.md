# Unraid Community Apps

## Install from a template

The template maps:

- Web UI: port `9091`
- Media: `/downloads`
- Persistent configuration: `/config`

The included template targets the public GHCR image published by this project.

## Publisher checklist

1. Create the public GitHub repository.
2. Push the project and enable GitHub Actions.
3. Confirm `ghcr.io/OWNER/stash-dock:latest` is public.
4. Host the XML and icon at stable public URLs.
5. Test a clean Unraid install, upgrade, and uninstall while preserving media.
6. Fork the Unraid Community Applications templates repository and submit the
   template according to its current contribution instructions.

## Upgrade behavior

All user state must remain under `/config`; media remains under `/downloads`.
Replacing the container must not remove either host directory.

## Support expectations

Bug reports should use the included form and attach the redacted diagnostics
export. Ask for the Stash Dock version, Unraid version, source host, selected
mode, and relevant job error. Never request API keys or site credentials.
