# Jellyfin Presence

Show the music you're streaming from a [Jellyfin](https://jellyfin.org) server as a
**Listening to** activity on your Discord profile, via Rich Presence.

- Title, artist(s), album and year
- Audio quality (e.g. `FLAC - 44.1 kHz / 16-bit`, `MP3 - 320 kbps`) and a transcoding flag
- Live progress bar, with pause detection
- Album art, via your public Jellyfin URL or the [Cover Art Archive](https://coverartarchive.org)
- Optional "View on MusicBrainz" button
- Audio only: video playback is ignored

## Requirements

- Python 3.10+
- The **Discord desktop app** running on the same machine (the web client doesn't support Rich Presence)
- A Jellyfin server you can reach from that machine

## Setup

### 1. Get your credentials

**Jellyfin API key:** Dashboard -> API Keys -> add a new key.

**Discord application ID:**
1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and create a new application.
2. Name it what you want shown on your profile (e.g. `Jellyfin`).
3. Copy the **Application ID** from the General Information page.
4. Under **Rich Presence -> Art Assets**, upload an icon with the key `jellyfin`. It's used as the fallback cover and small icon.

### 2. Install

```bash
git clone https://github.com/YOUR_USERNAME/jellyfin-presence.git
cd jellyfin-presence

python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

<details>
<summary>NixOS</summary>

Use the system Python through nix-shell instead of pip:

```bash
nix-shell -p "python3.withPackages (ps: [ ps.pypresence ps.requests ])"
```
</details>

### 3. Configure

```bash
cp config.example.json config.json
```

(You will need to make a config.json with the format of config.example.json, but with your actual API key, application ID, e.t.c.)

Edit `config.json`:

| Key | Required | Description |
|---|---|---|
| `jellyfin_url` | yes | Your server address including port, e.g. `http://192.168.1.50:8096` |
| `api_key` | yes | Jellyfin API key |
| `discord_client_id` | yes | Discord Application ID |
| `username` | recommended | Only show this Jellyfin user's playback. Empty = any user |
| `public_url` | no | Internet-reachable Jellyfin URL, so Discord can load covers directly from your server |
| `poll_interval` | no | Seconds between checks (default `5`) |
| `show_quality` | no | Show codec/bitrate info on the cover tooltip (default `true`) |
| `show_buttons` | no | Show the MusicBrainz button when IDs are available (default `true`) |
| `default_image` | no | Discord art asset key used as fallback (default `jellyfin`) |

`config.json` is git-ignored because it contains your API key. Never commit it.

### 4. Run

```bash
python jellyfin_presence.py        # add -v for debug logging
```

Start Discord, play a track in Jellyfin, and it should appear within a few seconds.
Press `Ctrl+C` to stop; your presence is cleared on exit.

## Run on startup (Linux / systemd)

To have it start automatically at login and run in the background, set it up as a systemd
**user** service. It must be a user service (not a system one), because Discord's Rich Presence
socket lives in your user session.

1. Create `~/.config/systemd/user/jellyfin-presence.service`:

   ```ini
   [Unit]
   Description=Jellyfin Discord Rich Presence

   [Service]
   WorkingDirectory=%h/jellyfin-presence
   ExecStart=%h/jellyfin-presence/.venv/bin/python %h/jellyfin-presence/jellyfin_presence.py
   Restart=on-failure
   RestartSec=10

   [Install]
   WantedBy=default.target
   ```

   `%h` expands to your home directory. This assumes you cloned the project to
   `~/jellyfin-presence` and created the `.venv` as shown in [Install](#2-install). Adjust both
   paths if yours differ.

2. Enable and start it:

   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now jellyfin-presence.service
   ```

You don't need to start Discord first: the script retries the connection every 15 seconds until
Discord is running.

Useful commands:

```bash
systemctl --user status jellyfin-presence           # is it running?
journalctl --user -u jellyfin-presence -f           # live logs
systemctl --user restart jellyfin-presence          # after editing config.json
systemctl --user stop jellyfin-presence             # stop for now
systemctl --user disable jellyfin-presence          # don't start at login
```

<details>
<summary>NixOS (declarative)</summary>

Instead of a venv, you can define the service in your NixOS configuration (replace `YOU` with your
username and adjust the paths):

```nix
systemd.user.services.jellyfin-presence = {
  description = "Jellyfin Discord Rich Presence";
  wantedBy = [ "default.target" ];
  serviceConfig = {
    WorkingDirectory = "/home/YOU/jellyfin-presence";
    ExecStart = "${pkgs.python3.withPackages (ps: [ ps.pypresence ps.requests ])}/bin/python /home/YOU/jellyfin-presence/jellyfin_presence.py";
    Restart = "on-failure";
    RestartSec = 10;
  };
};
```

Then run `sudo nixos-rebuild switch`. This needs pypresence 4.3.0 or newer in your nixpkgs.
</details>

> Flatpak/Snap installs of Discord may hide the IPC socket from the service. See
> [Troubleshooting](#troubleshooting).

## Cover art

Discord can only load images from public URLs, so a LAN-only Jellyfin address can't be used
directly. Covers are resolved in this order:

1. `public_url`, if set
2. Cover Art Archive, using the MusicBrainz IDs Jellyfin stores for the album
3. The `default_image` asset

For option 2, enable the MusicBrainz metadata provider in your Jellyfin music library and refresh
metadata, or tag your files with [MusicBrainz Picard](https://picard.musicbrainz.org).

## Troubleshooting

| Problem | Fix |
|---|---|
| `Discord not available` | Make sure the desktop app is running. Flatpak/Snap installs can block the IPC socket |
| `Jellyfin unreachable` | Check `jellyfin_url`, port and network access |
| Nothing shows while playing | Check `username` matches your Jellyfin login, or set it to `""` |
| Terminal says it updated but the profile is empty | Enable activity sharing in Discord -> Settings -> Activity Privacy |
| Generic icon instead of album art | Tracks lack MusicBrainz IDs and no `public_url` is set (see Cover art) |

## License

MIT
