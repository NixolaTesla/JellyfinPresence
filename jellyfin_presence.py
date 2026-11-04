#!/usr/bin/env python3
"""
Usage:
    pip install -r requirements.txt
    cp config.example.json config.json   # then edit it
    python jellyfin_presence.py [-c config.json] [-v]
"""
import argparse
import json
import logging
import sys
import time
from pathlib import Path

import requests
from pypresence import ActivityType, Presence
from pypresence import exceptions as pp_exc

log = logging.getLogger("jellyfin-presence")

TICKS_PER_SECOND = 10_000_000
DISCORD_MIN_UPDATE_INTERVAL = 15  # Discord rate-limits presence updates
SEEK_TOLERANCE = 3.0              # seconds of drift before we treat it as a seek


# Config
def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"Config file not found: {path}\nCopy config.example.json to config.json and edit it.")
    with path.open(encoding="utf-8") as f:
        cfg = json.load(f)

    for key in ("jellyfin_url", "api_key", "discord_client_id"):
        if not cfg.get(key) or str(cfg[key]).startswith("YOUR_"):
            sys.exit(f"Missing/placeholder value for '{key}' in {path}")

    cfg["jellyfin_url"] = cfg["jellyfin_url"].rstrip("/")
    cfg["public_url"] = (cfg.get("public_url") or "").rstrip("/")
    cfg.setdefault("username", "")
    cfg.setdefault("poll_interval", 5)
    cfg.setdefault("show_quality", True)
    cfg.setdefault("show_buttons", True)
    cfg.setdefault("default_image", "jellyfin")
    return cfg



# Jellyfin

class Jellyfin:
    def __init__(self, base_url: str, api_key: str):
        self.base = base_url
        self.http = requests.Session()
        self.http.headers.update({
            "Authorization": f'MediaBrowser Token="{api_key}", Client="jellyfin-presence", '
                             f'Device="rpc", DeviceId="jellyfin-presence", Version="1.0"',
            "Accept": "application/json",
        })
        self._item_cache: dict[str, dict] = {}

    def sessions(self) -> list[dict]:
        r = self.http.get(f"{self.base}/Sessions", timeout=8)
        r.raise_for_status()
        return r.json()

    def full_item(self, item_id: str, user_id: str | None) -> dict:
        """Full item DTO (has MediaStreams + ProviderIds). Cached per item."""
        if item_id in self._item_cache:
            return self._item_cache[item_id]
        try:
            if user_id:
                url = f"{self.base}/Users/{user_id}/Items/{item_id}"
            else:
                url = f"{self.base}/Items/{item_id}"
            r = self.http.get(url, timeout=8)
            r.raise_for_status()
            item = r.json()
        except requests.RequestException as e:
            log.debug("Could not fetch full item %s: %s", item_id, e)
            item = {}
        if len(self._item_cache) > 200:
            self._item_cache.clear()
        self._item_cache[item_id] = item
        return item

    def find_audio_session(self, username: str) -> dict | None:
        """Return the most relevant session that is currently playing audio."""
        candidates = []
        for s in self.sessions():
            item = s.get("NowPlayingItem")
            if not item or item.get("MediaType") != "Audio":
                continue
            if username and (s.get("UserName") or "").lower() != username.lower():
                continue
            candidates.append(s)
        if not candidates:
            return None
        # Prefer actively playing over paused, then most recent activity
        candidates.sort(
            key=lambda s: (
                not s.get("PlayState", {}).get("IsPaused", False),
                s.get("LastActivityDate", ""),
            ),
            reverse=True,
        )
        return candidates[0]



# Metadata extraction

def build_quality(item: dict, transcoding: dict | None) -> str:
    """e.g. 'FLAC - 44.1 kHz / 16-bit' or 'MP3 - 320 kbps'."""
    streams = [s for s in item.get("MediaStreams", []) if s.get("Type") == "Audio"]
    if not streams:
        return ""
    a = streams[0]
    codec = (a.get("Codec") or item.get("Container") or "").upper()
    parts = [codec] if codec else []

    sr, bd, br = a.get("SampleRate"), a.get("BitDepth"), a.get("BitRate")
    lossless = codec in {"FLAC", "ALAC", "WAV", "PCM", "APE", "WAVPACK", "AIFF"} or (codec or "").startswith("PCM")
    detail = []
    if sr:
        detail.append(f"{sr / 1000:g} kHz")
    if bd:
        detail.append(f"{bd}-bit")
    if lossless and detail:
        parts.append(" / ".join(detail))
    elif br:
        parts.append(f"{round(br / 1000)} kbps")
    elif detail:
        parts.append(" / ".join(detail))

    text = " - ".join(parts)
    if transcoding and not transcoding.get("IsAudioDirect", True):
        tc = (transcoding.get("AudioCodec") or "").upper()
        text += f" (transcoded to {tc})" if tc else " (transcoded)"
    return text


def extract_track(jf: Jellyfin, session: dict) -> dict:
    now = session["NowPlayingItem"]
    play = session.get("PlayState", {})
    item_id = now["Id"]
    full = jf.full_item(item_id, session.get("UserId")) or now

    artists = full.get("Artists") or now.get("Artists") or []
    if not artists and full.get("AlbumArtist"):
        artists = [full["AlbumArtist"]]

    runtime = (full.get("RunTimeTicks") or now.get("RunTimeTicks") or 0) / TICKS_PER_SECOND
    position = (play.get("PositionTicks") or 0) / TICKS_PER_SECOND

    return {
        "id": item_id,
        "title": full.get("Name") or now.get("Name") or "Unknown track",
        "artists": artists,
        "album": full.get("Album") or now.get("Album") or "",
        "album_artist": full.get("AlbumArtist") or "",
        "year": full.get("ProductionYear"),
        "duration": runtime,
        "position": position,
        "paused": bool(play.get("IsPaused")),
        "quality": build_quality(full, session.get("TranscodingInfo")),
        "provider_ids": full.get("ProviderIds") or {},
        "album_id": full.get("AlbumId") or now.get("AlbumId"),
        "album_tag": full.get("AlbumPrimaryImageTag") or now.get("AlbumPrimaryImageTag"),
        "image_tag": (full.get("ImageTags") or {}).get("Primary"),
        "client": session.get("Client", ""),
        "device": session.get("DeviceName", ""),
    }



# Cover art

class CoverResolver:
    """
    Discord can only load images from public URLs, so a LAN Jellyfin URL won't work.
    Order of preference:
      1. Your Jellyfin 'public_url' (if configured and reachable from the internet)
      2. Cover Art Archive via MusicBrainz IDs stored in Jellyfin metadata
      3. Default asset key uploaded to your Discord app
    """

    def __init__(self, cfg: dict):
        self.public_url = cfg["public_url"]
        self.default = cfg["default_image"]
        self.cache: dict[str, str] = {}

    def resolve(self, t: dict) -> str:
        key = t.get("album_id") or t["id"]
        if key in self.cache:
            return self.cache[key]

        url = self._from_jellyfin(t) or self._from_caa(t) or self.default
        self.cache[key] = url
        log.debug("Cover for %s -> %s", key, url)
        return url

    def _from_jellyfin(self, t: dict) -> str | None:
        if not self.public_url:
            return None
        if t["album_id"] and t["album_tag"]:
            return f'{self.public_url}/Items/{t["album_id"]}/Images/Primary?maxWidth=512&tag={t["album_tag"]}'
        if t["image_tag"]:
            return f'{self.public_url}/Items/{t["id"]}/Images/Primary?maxWidth=512&tag={t["image_tag"]}'
        return None

    def _from_caa(self, t: dict) -> str | None:
        ids = t["provider_ids"]
        attempts = []
        if ids.get("MusicBrainzAlbum"):
            attempts.append(f'https://coverartarchive.org/release/{ids["MusicBrainzAlbum"]}/front-500')
        if ids.get("MusicBrainzReleaseGroup"):
            attempts.append(f'https://coverartarchive.org/release-group/{ids["MusicBrainzReleaseGroup"]}/front-500')
        for url in attempts:
            try:
                # CAA redirects to archive.org; Discord needs the final direct URL
                r = requests.head(url, allow_redirects=True, timeout=6)
                if r.status_code == 200:
                    return r.url
            except requests.RequestException:
                continue
        return None



# Discord

def clamp(text: str, limit: int = 128) -> str | None:
    """Discord requires 2-128 characters for text fields."""
    text = (text or "").strip()
    if not text:
        return None
    if len(text) == 1:
        text += "\u200b"
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


class Discord:
    def __init__(self, client_id: str):
        self.client_id = client_id
        self.rpc: Presence | None = None
        self.last_update = 0.0

    def connect(self) -> bool:
        try:
            self.rpc = Presence(self.client_id)
            self.rpc.connect()
            log.info("Connected to Discord")
            return True
        except (pp_exc.DiscordNotFound, pp_exc.InvalidID, ConnectionError, OSError) as e:
            log.warning("Discord not available (%s). Is the desktop app running?", e)
            self.rpc = None
            return False

    def update(self, **kwargs) -> None:
        self.rpc.update(**kwargs)
        self.last_update = time.time()

    def clear(self) -> None:
        if self.rpc:
            try:
                self.rpc.clear()
            except Exception as e:  # noqa: BLE001
                log.debug("clear failed: %s", e)

    def close(self) -> None:
        if self.rpc:
            try:
                self.rpc.clear()
                self.rpc.close()
            except Exception:  # noqa: BLE001
                pass
            self.rpc = None

    def seconds_until_allowed(self) -> float:
        return max(0.0, DISCORD_MIN_UPDATE_INTERVAL - (time.time() - self.last_update))


def build_activity(t: dict, cfg: dict, cover: str) -> dict:
    artists = ", ".join(t["artists"]) if t["artists"] else ""
    state = f"by {artists}" if artists else None
    if t["paused"]:
        state = f"Paused - {artists}" if artists else "Paused"

    large_text = t["album"]
    if t["year"] and t["album"]:
        large_text = f'{t["album"]} ({t["year"]})'
    if cfg["show_quality"] and t["quality"]:
        large_text = f'{large_text} | {t["quality"]}' if large_text else t["quality"]

    activity = {
        "activity_type": ActivityType.LISTENING,
        "details": clamp(t["title"]),
        "state": clamp(state),
        "large_image": cover,
        "large_text": clamp(large_text),
        "small_image": cfg["default_image"],
        "small_text": clamp(f'Jellyfin - {t["client"]}' if t["client"] else "Jellyfin"),
    }

    # Progress bar only while actually playing
    if not t["paused"] and t["duration"] > 0:
        start = time.time() - t["position"]
        activity["start"] = int(start)
        activity["end"] = int(start + t["duration"])

    if cfg["show_buttons"]:
        ids = t["provider_ids"]
        buttons = []
        if ids.get("MusicBrainzTrack") or ids.get("MusicBrainzRecording"):
            mb = ids.get("MusicBrainzRecording") or ids.get("MusicBrainzTrack")
            buttons.append({"label": "View on MusicBrainz", "url": f"https://musicbrainz.org/recording/{mb}"})
        elif ids.get("MusicBrainzAlbum"):
            buttons.append({"label": "View on MusicBrainz",
                            "url": f'https://musicbrainz.org/release/{ids["MusicBrainzAlbum"]}'})
        if buttons:
            activity["buttons"] = buttons

    return {k: v for k, v in activity.items() if v is not None}


def needs_update(prev: dict | None, cur: dict) -> bool:
    if prev is None:
        return True
    if prev["id"] != cur["id"] or prev["paused"] != cur["paused"]:
        return True
    if not cur["paused"]:
        # Expected position given elapsed wall-clock time vs. what Jellyfin reports
        expected = prev["position"] + (cur["_seen_at"] - prev["_seen_at"])
        if abs(cur["position"] - expected) > SEEK_TOLERANCE:
            return True
    return False



# Main loop

def main() -> None:
    ap = argparse.ArgumentParser(description="Jellyfin audio -> Discord Rich Presence")
    ap.add_argument("-c", "--config", default=str(Path(__file__).with_name("config.json")))
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_config(Path(args.config))
    jf = Jellyfin(cfg["jellyfin_url"], cfg["api_key"])
    covers = CoverResolver(cfg)
    dc = Discord(cfg["discord_client_id"])

    last_sent: dict | None = None   # last track state pushed to Discord
    pending = False                 # an update is waiting on the rate limit
    showing = False

    log.info("Watching %s%s", cfg["jellyfin_url"],
             f' for user "{cfg["username"]}"' if cfg["username"] else " (all users)")

    try:
        while True:
            # ---- Ensure Discord connection
            if dc.rpc is None and not dc.connect():
                time.sleep(15)
                continue

            # ---- Ask Jellyfin what's playing
            try:
                session = jf.find_audio_session(cfg["username"])
            except requests.RequestException as e:
                log.warning("Jellyfin unreachable: %s", e)
                time.sleep(cfg["poll_interval"] * 2)
                continue

            try:
                if session is None:
                    if showing:
                        log.info("Playback stopped - clearing presence")
                        dc.clear()
                        showing = False
                    last_sent, pending = None, False
                else:
                    track = extract_track(jf, session)
                    track["_seen_at"] = time.time()

                    if needs_update(last_sent, track) or pending:
                        if dc.seconds_until_allowed() > 0:
                            pending = True  # try again next tick
                        else:
                            cover = covers.resolve(track)
                            dc.update(**build_activity(track, cfg, cover))
                            log.info("%s: %s - %s%s",
                                     "Paused" if track["paused"] else "Now playing",
                                     ", ".join(track["artists"]) or "Unknown artist",
                                     track["title"],
                                     f' [{track["quality"]}]' if track["quality"] else "")
                            last_sent, pending, showing = track, False, True
            except (pp_exc.InvalidPipe, pp_exc.ConnectionTimeout, BrokenPipeError, ConnectionError, OSError) as e:
                log.warning("Lost Discord connection (%s) - reconnecting", e)
                dc.rpc = None
                last_sent = None
            except pp_exc.PyPresenceException as e:
                log.error("Discord rejected update: %s", e)

            time.sleep(cfg["poll_interval"])
    except KeyboardInterrupt:
        log.info("Shutting down")
    finally:
        dc.close()


if __name__ == "__main__":
    main()
