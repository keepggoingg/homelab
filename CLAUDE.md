# homelab

You are the admin of this homelab. The owner talks to you via Remote Control (claude.ai / Claude app) or SSH.
Machine-specific facts (hardware, IPs, accounts, devices) are in CLAUDE.private.md when it exists:

@CLAUDE.private.md

## Layout
- `/srv/homelab` is a **public** git repo. Every change is committed (`git add -A && git commit`) and pushed.
  Nothing personal or secret may land in it: no IPs, domains, names, tokens, device IDs. Those go in
  `config/homelab.env` (gitignored) or in the private overlay.
- Private overlay: `bin/private-git` (git dir `.git-private`, same work tree) tracks the personal paths the
  public `.gitignore` hides: CLAUDE.private.md, config/settings.json, apps/docs/site, apps/n8n/workflows,
  apps/upwork-agent, private/. After changing them: `bin/private-git sync "message"`.
  Add a new personal path to both `.gitignore` and `PATHS` in `bin/private-git`.
- `traefik/` reverse proxy on host port 80, docker provider, external network `proxy`.
- `cloudflared/config.yml` is rendered from `config.yml.tmpl`; /etc/cloudflared/config.yml links to it.
  The tunnel routes `$DOMAIN` and `*.$DOMAIN` to http://localhost:80 (Traefik). Cloudflare terminates TLS.
- `apps/<name>/compose.yml` one folder per project.

## Configuration
- `bin/homelab doctor` checks the whole setup (docker, tunnel, Access, firewall, screen) and prints fixes.
  Run it after changes and when something looks off. `bin/homelab setup --reconfigure` re-asks values.
- `config/homelab.env` (gitignored, mode 600): infra values and secrets. Every key is documented in
  `config/homelab.env.example`; a new key goes in the example too (`# secret` line above it if it is one).
- Each compose project's `.env` is a symlink to it (`bin/homelab setup` creates them), so compose files
  use `${DOMAIN}`, `${HL_HOST}` etc. Never hard-code those values.
- `config/public.env` is generated (non-secret keys only) for systemd `EnvironmentFile=`.
- `config/settings.json`: dashboard options (location, status check).
- Web UI: `http://$HL_HOST:8090/settings` (token = SETTINGS_TOKEN). Save writes the files, Apply runs
  `bin/homelab apply`: re-render templates, restart cloudflared if its config changed,
  `docker compose up -d` for traefik and every app.
- Screen remote on the internet: set SCREEN_REMOTE_HOST (e.g. remote.$DOMAIN) and Apply; traefik/dynamic/screen-remote.yml
  routes it to screenctl :8090 (ufw allows the proxy network to 8090). Only with a Cloudflare Access app on that host first.
- Never print secret values (`grep ^KEY= config/homelab.env` shows them; check with `[ -n "$KEY" ]` style tests).

## Deploy a new app
1. `apps/<name>/compose.yml`, join network `proxy`, labels:
   `traefik.enable=true` and ``traefik.http.routers.<name>.rule=Host(`<name>.${DOMAIN}`)``
   (add `traefik.http.services.<name>.loadbalancer.server.port=<port>` if not 80).
2. `bin/homelab apply` (links `.env`), check `curl -H "Host: <name>.$DOMAIN" localhost`.
3. Commit + push. DNS is a wildcard, so no DNS change needed.
- n8n: private on http://$HL_HOST:5678 always; on n8n.$DOMAIN only when CF_ACCESS_TEAM + CF_ACCESS_AUD are set
  (config/compose.env gets derived values from lib/hlconfig.py `derived()`; apps' .env link to compose.env).
- screenctl (:8090) is a boot-time user service, independent of the screen; it finds sway when it runs.
- Everything under *.$DOMAIN is PUBLIC on the internet. Admin UIs must not be exposed without auth: put a
  Cloudflare Access app with an owner-only policy in front. n8n uses apps/n8n/hooks/cf-access-sso.js to turn a
  verified Cf-Access-Jwt-Assertion (CF_ACCESS_TEAM / CF_ACCESS_AUD) into an n8n session.
- Mind RAM: check `free -h` / `docker stats --no-stream` before adding heavy services.

## Firewall
ufw: incoming only from the LAN on 22, 80, 443, 53. Docker-published ports bypass ufw: prefer not publishing ports, route via Traefik.

## Safety
- Ask the owner before deleting data volumes, changing router/Cloudflare account settings, or anything that costs money.
- Keep the owner informed of what you changed.

## Built-in display (screen/)
- sway kiosk on tty1 (getty autologin -> ~/.bash_profile -> sway). User units in screen/systemd (symlinked to ~/.config/systemd/user).
- screenctl.py :8090: modes dashboard/fireplace/youtube, /api/play (mpv+yt-dlp), /api/key/*, /api/screen/on|off,
  /settings. Chromium kiosk with TV user-agent, CDP on 127.0.0.1:9222.
- uxplay.service: AirPlay receiver named $HL_HOST. homekit.py: HomeKit TV accessory (HAP-python venv
  ~/.local/share/homekit-venv, port 51826, pairing state ~/.local/state/homekit).
- Screenshot of the display: XDG_RUNTIME_DIR=/run/user/$(id -u) WAYLAND_DISPLAY=wayland-1 grim /tmp/shot.png
- Chromium translate popup is disabled by policy: /etc/chromium-browser/policies/managed/homelab.json and
  /var/snap/chromium/current/policies/managed/homelab.json ({"TranslateEnabled": false}).
- Remote keys: in dashboard/fireplace the shell dock handles focus (left/right move, select activates, back -> dashboard);
  in YouTube, back on the home screen -> dashboard; "home" (HomeKit TV/info button) -> dashboard always.
- Dashboard (dashboard.html) polls /api/stats every 3 s, /api/claude every 2 s (tail of the newest
  ~/.claude/projects/*/*.jsonl) and /api/config every minute.
- Spotify: spotifyd (spotify/) is installed but disabled: Spotify refuses audio keys to librespot clients.

## n8n MCP server
n8n's built-in MCP server (Settings > MCP access) is reachable for Claude Code over Tailscale at
http://$HL_HOST/mcp-server/http (Traefik router n8n-mcp, private hosts only, bearer token = n8n MCP API key).
Not reachable via n8n.$DOMAIN without Cloudflare Access.

## Docs
A private HTML guide can live in apps/docs/site/guide/ (private overlay), served by apps/docs (nginx) at
http://$HL_HOST/guide/ (Tailscale/LAN hosts only). Update it when the setup changes.
