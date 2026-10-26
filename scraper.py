"""Scrape YouTube channels/videos: metadata, thumbnails, transcripts.

Uses yt-dlp only for channel listing; everything else goes through
YouTube's Innertube API directly (stdlib only), which avoids the
timedtext 429 rate-limiting that yt-dlp's subtitle path hits.

Usage:
    py scraper.py channel <url> [--limit N]
    py scraper.py videos <url> [<url> ...]

Data layout (relative to this file):
    data/videos.json            index of all known videos (lightweight)
    data/info/<id>.info.json  raw Innertube player response per video
    data/transcripts/<id>.json  parsed transcript [{"start", "text"}, ...]
    data/thumbs/<id>.jpg        thumbnail image
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "data"
INFO_DIR = DATA / "info"
TRANSCRIPT_DIR = DATA / "transcripts"
THUMB_DIR = DATA / "thumbs"
VIDEOS_JSON = DATA / "videos.json"

YT_DLP = ROOT / ".venv" / "Scripts" / "yt-dlp.exe"

ANDROID_KEY = "AIzaSyA8eiZmM1FaDVjRy-df2KTyQ_vz_yYM39w"
ANDROID_UA = "com.google.android.youtube/20.10.38 (Linux; U; Android 11) gzip"
REQUEST_DELAY = 0.8  # seconds between Innertube calls (be polite)


class Innertube:
    def __init__(self) -> None:
        self._last = 0.0

    def _post(self, endpoint: str, body: dict) -> dict:
        wait = REQUEST_DELAY - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        req = urllib.request.Request(
            f"https://www.youtube.com/youtubei/v1/{endpoint}?key={ANDROID_KEY}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "User-Agent": ANDROID_UA},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def player(self, vid: str) -> dict:
        return self._post(
            "player",
            {
                "context": {
                    "client": {
                        "clientName": "ANDROID",
                        "clientVersion": "20.10.38",
                        "androidSdkVersion": 30,
                        "hl": "en",
                        "gl": "US",
                    }
                },
                "videoId": vid,
            },
        )


def ytdlp(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(YT_DLP), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def load_index() -> dict:
    if VIDEOS_JSON.exists():
        return json.loads(VIDEOS_JSON.read_text(encoding="utf-8"))
    return {"videos": {}}


def save_index(index: dict) -> None:
    VIDEOS_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp = VIDEOS_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(VIDEOS_JSON)


def list_channel(url: str, limit: int) -> list[dict]:
    """Flat-list a channel's videos (ids + approximate dates) via yt-dlp."""
    proc = ytdlp(
        "--flat-playlist",
        "--extractor-args", "youtubetab:approximate_date",
        "--print-json",
        "--playlist-end", str(limit),
        url,
    )
    if proc.returncode != 0:
        print(proc.stderr[-2000:], file=sys.stderr)
        raise SystemExit(f"yt-dlp failed to list channel (exit {proc.returncode})")
    entries = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("id"):
            entries.append(
                {
                    "id": e["id"],
                    "title": e.get("title") or "",
                    "url": f"https://www.youtube.com/watch?v={e['id']}",
                    "channel": e.get("channel") or e.get("uploader") or "",
                    "published": e.get("upload_date") or "",
                }
            )
    # hard cap regardless of what yt-dlp returned (shelves/continuations can
    # make --playlist-end unreliable)
    return entries[:limit]


def extract_video_id(url: str) -> str | None:
    """Pull the 11-char video id out of any supported YouTube video URL."""
    import re

    url = (url or "").strip()
    m = re.search(r"(?:youtube\.com/watch\?[^#]*v=|youtu\.be/|youtube\.com/shorts/)([\w-]{6,20})", url)
    if m:
        return m.group(1)
    m = re.fullmatch(r"([\w-]{6,20})", url)
    return m.group(1) if m else None


def pick_english_track(tracks: list[dict]) -> dict | None:
    """Prefer a manual English track, fall back to English ASR, then first track."""
    if not tracks:
        return None
    return (
        next((t for t in tracks if t.get("languageCode") == "en" and t.get("kind") != "asr"), None)
        or next((t for t in tracks if t.get("languageCode") == "en"), None)
        or tracks[0]
    )


def parse_transcript_payload(raw: str) -> list[dict] | None:
    """Parse a timedtext payload (srv3 XML or json3) into deduped segments
    [{"start": seconds, "text": str}]. Pure function — no network."""
    import xml.etree.ElementTree as ET

    if not raw or not raw.strip():
        return None
    if raw.lstrip().startswith("{"):
        data = json.loads(raw)
        segments = [
            {"start": round(ev["tStartMs"] / 1000, 1),
             "text": "".join(s.get("utf8", "") for s in ev.get("segs", [])).strip()}
            for ev in data.get("events", [])
            if ev.get("tStartMs") is not None
        ]
    else:
        root = ET.fromstring(raw)
        body = root.find("body")
        if body is None:
            return None
        segments = []
        for p in body.iter("p"):
            t_attr = p.get("t") or p.get("start")
            text = " ".join("".join(p.itertext()).split())
            if t_attr is not None and text:
                segments.append({"start": round(int(float(t_attr)) / 1000, 1), "text": text})
    cleaned = []
    for seg in segments:
        if cleaned and cleaned[-1]["text"] == seg["text"]:
            continue
        cleaned.append(seg)
    return cleaned or None


def fetch_transcript(api: Innertube, player: dict) -> list[dict] | None:
    """Fetch the english transcript. Works even when timedtext rate-limits
    other clients, because the track URL comes from the Innertube player API."""
    tracks = (
        player.get("captions", {})
        .get("playerCaptionsTracklistRenderer", {})
        .get("captionTracks", [])
    )
    en = pick_english_track(tracks)
    if en is None:
        return None
    url = en["baseUrl"] + "&fmt=srv3"
    req = urllib.request.Request(url, headers={"User-Agent": ANDROID_UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode("utf-8", "replace")
    return parse_transcript_payload(raw)


def fetch_missing_transcripts(api: Innertube, index: dict) -> None:
    """Backfill transcripts for indexed videos that lack them, reusing the
    saved player response and refetching it only if the track URL failed."""
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    for vid in index["videos"]:
        out = TRANSCRIPT_DIR / f"{vid}.json"
        if out.exists():
            continue
        info_file = INFO_DIR / f"{vid}.info.json"
        player = None
        if info_file.exists():
            try:
                player = json.loads(info_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                player = None
        transcript = None
        if player and player.get("captions"):
            try:
                transcript = fetch_transcript(api, player)
            except Exception as e:
                print(f"  [warn] cached captions failed for {vid}: {e}", file=sys.stderr)
                transcript = None
        if transcript is None:
            try:
                player = api.player(vid)
                (INFO_DIR / f"{vid}.info.json").write_text(
                    json.dumps(player, ensure_ascii=False), encoding="utf-8"
                )
                transcript = fetch_transcript(api, player)
            except Exception as e:
                print(f"  [warn] transcript failed for {vid}: {e}", file=sys.stderr)
                continue
        if transcript:
            out.write_text(json.dumps(transcript, ensure_ascii=False), encoding="utf-8")
        print(f"  transcript {vid}: {'yes' if transcript else 'NO'}")


def enrich(api: Innertube, ids: list[str]) -> None:
    """Fetch full metadata + english transcript for each video id."""
    INFO_DIR.mkdir(parents=True, exist_ok=True)
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    for vid in ids:
        try:
            player = api.player(vid)
        except Exception as e:
            print(f"  [warn] player API failed for {vid}: {e}", file=sys.stderr)
            continue
        (INFO_DIR / f"{vid}.info.json").write_text(
            json.dumps(player, ensure_ascii=False), encoding="utf-8"
        )
        try:
            transcript = fetch_transcript(api, player)
        except Exception as e:
            print(f"  [warn] transcript failed for {vid}: {e}", file=sys.stderr)
            transcript = None
        if transcript:
            (TRANSCRIPT_DIR / f"{vid}.json").write_text(
                json.dumps(transcript, ensure_ascii=False), encoding="utf-8"
            )
        print(f"  enriched {vid} (transcript: {'yes' if transcript else 'NO'})")


def fetch_thumbnail(vid: str) -> bool:
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    dest = THUMB_DIR / f"{vid}.jpg"
    if dest.exists() and dest.stat().st_size > 2000:
        return True
    for quality in ("maxresdefault", "sddefault", "hqdefault"):
        try:
            with urllib.request.urlopen(
                f"https://i.ytimg.com/vi/{vid}/{quality}.jpg", timeout=15
            ) as r:
                blob = r.read()
            if len(blob) > 2000:  # skip YouTube's grey placeholder
                dest.write_bytes(blob)
                return True
        except Exception:
            continue
    return False


def ingest(entries: list[dict], index: dict) -> list[str]:
    """Merge new entries into the index; return ids that still need enrichment."""
    need = []
    for e in entries:
        vid = e["id"]
        existing = index["videos"].get(vid, {})
        index["videos"][vid] = {
            "id": vid,
            "title": e.get("title") or existing.get("title") or "",
            "channel": e.get("channel") or existing.get("channel") or "",
            "url": f"https://www.youtube.com/watch?v={vid}",
            "published": e.get("published") or existing.get("published") or "",
            "learned": existing.get("learned", False),
            "added": existing.get("added", time.strftime("%Y-%m-%d")),
        }
        if not (INFO_DIR / f"{vid}.info.json").exists():
            need.append(vid)
    save_index(index)
    return need


def backfill_index(index: dict) -> None:
    """Pull metadata from .info.json files into the index."""
    changed = False
    for vid, meta in index["videos"].items():
        info_file = INFO_DIR / f"{vid}.info.json"
        if not info_file.exists():
            continue
        try:
            info = json.loads(info_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        details = info.get("videoDetails", {})
        micro = info.get("microformat", {}).get("playerMicroformatRenderer", {})
        published = micro.get("publishDate") or meta.get("published", "")
        if isinstance(published, str) and "-" in published:
            published = published.replace("-", "")
        updates = {
            "duration": int(details.get("lengthSeconds") or 0),
            "published": published,
            "description": (details.get("shortDescription") or "")[:500],
            "channel": details.get("author") or meta.get("channel", ""),
        }
        for k, v in updates.items():
            if meta.get(k) != v:
                meta[k] = v
                changed = True
    if changed:
        save_index(index)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    ch = sub.add_parser("channel", help="scrape the latest N videos of a channel")
    ch.add_argument("url")
    ch.add_argument("--limit", type=int, default=30)

    vd = sub.add_parser("videos", help="scrape individual video urls")
    vd.add_argument("urls", nargs="+")

    sub.add_parser("transcripts", help="backfill missing transcripts for indexed videos")

    args = ap.parse_args()
    index = load_index()

    if args.cmd == "transcripts":
        print("Backfilling missing transcripts...")
        fetch_missing_transcripts(Innertube(), index)
        total = len(index["videos"])
        with_transcript = sum(
            1 for v in index["videos"] if (TRANSCRIPT_DIR / f"{v}.json").exists()
        )
        print(f"Done. {with_transcript}/{total} videos with transcripts.")
        return

    if args.cmd == "channel":
        print(f"Listing channel: {args.url} (limit {args.limit})")
        entries = list_channel(args.url, args.limit)
    else:
        entries = [
            {"id": vid, "title": "", "url": u, "channel": "", "published": ""}
            for u in args.urls
            if (vid := extract_video_id(u))
        ]

    print(f"Found {len(entries)} videos")
    need = ingest(entries, index)
    if need:
        print(f"Fetching metadata + transcripts for {len(need)} new videos...")
        enrich(Innertube(), need)
    else:
        print("All videos already enriched.")

    for e in entries:
        if not fetch_thumbnail(e["id"]):
            print(f"  [warn] no thumbnail for {e['id']}")

    backfill_index(index)
    total = len(index["videos"])
    with_transcript = sum(
        1 for v in index["videos"] if (TRANSCRIPT_DIR / f"{v}.json").exists()
    )
    print(f"Done. {total} videos in index, {with_transcript} with transcripts.")


if __name__ == "__main__":
    main()
