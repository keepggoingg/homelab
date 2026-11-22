<div align="center">

# ▍homelab

**an old laptop, lid closed, running the house**

<sub>sway kiosk · traefik · cloudflare tunnel · n8n · airplay · homekit · administered by claude code</sub>

<br>

![ubuntu](https://img.shields.io/badge/ubuntu-26.04-0e0d0b?style=flat-square&logo=ubuntu&logoColor=e8a75b&labelColor=0e0d0b&color=1b1916) ![sway](https://img.shields.io/badge/wm-sway-0e0d0b?style=flat-square&logo=wayland&logoColor=e8a75b&labelColor=0e0d0b&color=1b1916) ![docker](https://img.shields.io/badge/docker-compose-0e0d0b?style=flat-square&logo=docker&logoColor=e8a75b&labelColor=0e0d0b&color=1b1916) ![traefik](https://img.shields.io/badge/traefik-v3-0e0d0b?style=flat-square&logo=traefikproxy&logoColor=e8a75b&labelColor=0e0d0b&color=1b1916) ![cloudflare](https://img.shields.io/badge/cloudflare-tunnel-0e0d0b?style=flat-square&logo=cloudflare&logoColor=e8a75b&labelColor=0e0d0b&color=1b1916) ![n8n](https://img.shields.io/badge/n8n-automations-0e0d0b?style=flat-square&logo=n8n&logoColor=e8a75b&labelColor=0e0d0b&color=1b1916) ![claude](https://img.shields.io/badge/admin-claude_code-0e0d0b?style=flat-square&logo=claude&logoColor=e8a75b&labelColor=0e0d0b&color=1b1916)

<br>

<img src="docs/dashboard.png" alt="Kiosk dashboard: clock, weather, forecast, server stats, devices and the live Claude session" width="100%">

<sub>the built-in 15″ screen · 1920×1080 · demo data</sub>

</div>

<br>

## ░ details

|                 |                                                                                   |
| --------------- | --------------------------------------------------------------------------------- |
| **machine**     | 2020 15″ laptop · i5-10210U · 8 GB · NVMe · Wi-Fi only · lid closed, screen on    |
| **os**          | Ubuntu Server 26.04 LTS                                                           |
| **wm**          | sway on tty1, no bar, no cursor, one workspace per mode                           |
| **kiosk**       | Chromium `--kiosk`, driven over the DevTools protocol by `screenctl.py`           |
| **dashboard**   | plain HTML, no framework, no build step · weather by [Open-Meteo]                 |
| **modes**       | dashboard · fireplace · YouTube TV (cast target) · AirPlay ([UxPlay]) · mpv       |
| **remote**      | phone page · Apple Home TV remote ([HAP-python]) · air mouse · keyboard           |
| **proxy**       | Traefik v3, Docker labels, one `compose.yml` per app                              |
| **ingress**     | Cloudflare Tunnel (no open ports) · Cloudflare Access in front of admin UIs       |
| **automations** | n8n + Postgres, SSO through Cloudflare Access, MCP server for Claude Code         |
| **admin**       | Claude Code over Remote Control, every change a git commit                        |
| **font**        | whatever the system ships; the dashboard draws its own icons                      |

<br>

## ░ screens

<table>
<tr>
<td width="50%" valign="top">
<img src="docs/remote.png" alt="Phone remote: dashboard, fireplace, YouTube, stop video, screen on/off, play a link">
<p align="center"><sub><b>remote</b> · <code>:8090</code> · switch modes, play any link, screen off</sub></p>
</td>
<td width="50%" valign="top">
<img src="docs/settings.png" alt="Settings page: city search, coordinates, time zone, infrastructure keys">
<p align="center"><sub><b>settings</b> · <code>:8090/settings</code> · dashboard + every infra value</sub></p>
</td>
</tr>
</table>

The screen is one Chromium per mode, each parked on its own sway workspace, so switching is instant and
nothing reloads. Cast a YouTube video from a phone and the screen jumps to it, then back to the dashboard
when it ends. AirPlay and other links play as an overlay on top of whatever mode is on.

<br>

## ░ how it fits together

```
                     internet
                        │
              ┌─────────▼─────────┐
              │ cloudflare        │  TLS · Access (SSO) · *.example.com
              └─────────┬─────────┘
                        │ tunnel (outbound only)
┌───────────────────────▼────────────────────────────────────────────────┐
│ laptop                                                                 │
│                                                                        │
│   cloudflared ──► traefik :80 ──► n8n · postgres · docs · <your app>   │
│                                                                        │
│   sway ─┬─ ws1  chromium   dashboard + fireplace  ◄─┐                  │
│         ├─ ws2  chromium   youtube tv (cast)      ◄─┤                  │
│         ├─ ws3  mpv · uxplay overlays             ◄─┤ CDP · swaymsg    │
│         └─ ws4  chrome     spotify web player     ◄─┤                  │
│                                                     │                  │
│   screenctl.py :8090 ───────────────────────────────┘                  │
│        ▲                                                               │
│        └── phone remote · homekit.py · settings page                   │
│                                                                        │
│   config/homelab.env ──► compose .env · cloudflared · systemd units    │
└────────────────────────────────────────────────────────────────────────┘
          ▲
          └── tailscale / LAN only: :8090, ssh, n8n MCP
```

<br>

## ░ tree

```
bin/homelab            setup · render · apply · check
bin/private-git        optional private overlay repo (same work tree, own .git dir)
config/                homelab.env.example (every key, documented)
                       homelab.env · settings.json · public.env stay local
lib/hlconfig.py        reads and writes the config files
traefik/               reverse proxy
cloudflared/           config.yml.tmpl, rendered with your tunnel id and domain
apps/<name>/           compose.yml per app (n8n, docs, hello)
screen/                screenctl.py · dashboard.html · control.html · settings.html
                       homekit.py · sway.config · systemd user units
system/                claude-rc.service (Claude Code Remote Control in tmux)
```

<br>

## ░ install

Ubuntu Server with Docker (compose v2), Tailscale and `cloudflared`.

```sh
git clone https://github.com/keepggoingg/homelab /srv/homelab && cd /srv/homelab
docker network create proxy

bin/homelab setup      # config/homelab.env from the example, links every app's .env, lists empty keys
$EDITOR config/homelab.env      # or later in the browser: http://<host>:8090/settings
bin/homelab apply      # renders cloudflared/config.yml, starts traefik and the apps

sudo ln -sf /srv/homelab/cloudflared/config.yml /etc/cloudflared/config.yml
```

<details>
<summary><b>the screen</b> (optional)</summary>

<br>

```sh
ln -s /srv/homelab/screen/systemd/* ~/.config/systemd/user/
cat screen/bash_profile.snippet >> ~/.bash_profile          # start sway on tty1
mkdir -p ~/.config/sway && ln -s /srv/homelab/screen/sway.config ~/.config/sway/config
sudo install -D -m644 screen/autologin.conf \
  /etc/systemd/system/getty@tty1.service.d/autologin.conf   # replace YOUR_USER first
```

Needs `sway`, `chromium`, `mpv`, `yt-dlp`, `uxplay`, `python3-websocket`, and a HAP-python venv in
`~/.local/share/homekit-venv` for the HomeKit remote. Put a looping `fireplace.mp4` in `/srv/media/`.

</details>

<br>

## ░ settings

`http://<HL_HOST>:8090/settings`, unlocked with `SETTINGS_TOKEN` from `config/homelab.env`.

- **dashboard**: city search fills in name, coordinates and time zone; pick which URL the Home tile
  checks. Saved to `config/settings.json`, on screen within a minute.
- **infrastructure**: every key from `homelab.env.example` with its help text. Secrets are write-only:
  the page shows whether they are set, never their value.
- **apply**: runs `bin/homelab apply`, which re-renders templates, restarts cloudflared if its config
  changed, and runs `docker compose up -d` everywhere (only changed containers are recreated).

Port 8090 should only be reachable from the LAN or Tailscale (ufw), never through the tunnel unless
Cloudflare Access is in front of it.

<br>

## ░ rules

Everything that identifies one install (domain, IPs, hostnames, IDs, tokens) lives in
`config/homelab.env`. Compose files use `${VAR}`; systemd units read `config/public.env`, which has the
non-secret keys only. Personal files go in the private overlay (`bin/private-git sync`), never here.
See [`CLAUDE.md`](CLAUDE.md): it's the admin's handbook, and the admin is Claude.

<br>

<div align="center"><sub>lid closed · fan quiet · fire on</sub></div>

[Open-Meteo]: https://open-meteo.com
[UxPlay]: https://github.com/FDH2/UxPlay
[HAP-python]: https://github.com/ikalchev/HAP-python
