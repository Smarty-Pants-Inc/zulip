# Production Deploy Runbook (Smarty Pants)

This document is the operational runbook for deploying changes to the production
Smarty Pants Zulip server on DigitalOcean.

The goal is repeatability and avoiding known production footguns.

## TL;DR

Run this on the droplet:

```bash
/opt/smarty-ops/zulip-deploy
```

This is a wrapper that executes:

```bash
/home/zulip/deployments/current/scripts/sp-prod-deploy.sh
```

## Host + Paths

- Droplet: `zulip-app-prod-1` (DigitalOcean)
- Zulip checkout: `/home/zulip/deployments/current`
- Static root (served by nginx): `/home/zulip/prod-static`

## Standard Deploy Procedure

1. SSH to the droplet:

```bash
ssh root@zulip-app-prod-1.tail62219a.ts.net
```

2. Run the deploy helper:

```bash
/opt/smarty-ops/zulip-deploy
```

What it does:

- `git pull --ff-only` in `/home/zulip/deployments/current`
- `./tools/webpack --quiet` (rebuilds production bundles + stats)
- `ZULIP_COLLECTING_STATIC=1 ./manage.py collectstatic --noinput --clear`
  - regenerates `/home/zulip/deployments/current/staticfiles.json`
  - copies assets to `/home/zulip/prod-static`
- runs a local smoke test (`GET / -> 302`, `GET /login/ -> 200`)
- restarts Zulip via `./scripts/restart-server`

## The Staticfiles Manifest Footgun (Important)

Symptom in the browser:

- `Internal server error ... The app will load automatically once it is working again.`

Symptom in `/var/log/zulip/errors.log`:

- `ValueError: Missing staticfiles manifest entry for 'webpack-bundles/app.<hash>.css'`

Root cause:

- In production, Zulip uses a staticfiles manifest at:
  - `/home/zulip/deployments/current/staticfiles.json`
- That manifest maps logical paths to on-disk static assets.
- If the manifest is empty or stale, Django will 500 rendering the app HTML.

The trap:

- Running `./manage.py collectstatic` without `ZULIP_COLLECTING_STATIC=1` can generate an
  empty `staticfiles.json`.
- Empty manifest means the UI cannot render because bundle filenames cannot be resolved.

Fix:

```bash
cd /home/zulip/deployments/current
sudo -u zulip env ZULIP_COLLECTING_STATIC=1 ./manage.py collectstatic --noinput --clear
sudo -u zulip ./scripts/restart-server
```

Or just rerun the deploy helper.

## Triage Checklist

If something looks broken after deploy:

- `supervisorctl status`
- `tail -n 200 /var/log/zulip/errors.log`
- `tail -n 200 /var/log/nginx/error.log`

Local HTTP checks (bypass DNS/TLS, but keep correct realm routing):

```bash
curl -sS -o /dev/null -w "%{http_code}\n" -H "Host: smarty-pants.smartypants.ai" http://127.0.0.1/login/
curl -sS -o /dev/null -w "%{http_code}\n" -H "Host: smarty-pants.smartypants.ai" http://127.0.0.1/
```

## Notes

- `./tools/webpack` does not accept `--release` in this tree; use `--quiet` for less output.
- The deploy helper is intentionally conservative and fails fast if it detects:
  - a missing/suspiciously-small `staticfiles.json`
  - missing `webpack-bundles/app.*` entries
  - failing local HTTP endpoints