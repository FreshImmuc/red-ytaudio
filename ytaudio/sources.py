import asyncio
import base64
import json
import logging
import os
import re
import time
import urllib.parse
from dataclasses import asdict, dataclass, fields
from typing import List, Optional, Tuple

import aiohttp

log = logging.getLogger("red.ytaudio")

SPOTIFY_RE = re.compile(r"(?:open\.spotify\.com/(?:intl-[\w-]+/)?|spotify:)(track|album|playlist)[/:]([A-Za-z0-9]+)")
YT_ID_RE = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/live/)([\w-]{11})")
URL_RE = re.compile(r"^<?https?://", re.I)
UNAVAILABLE = {"[Private video]", "[Deleted video]"}
MAX_PLAYLIST = 1000


class ExtractError(Exception):
    pass


@dataclass
class Track:
    title: str
    url: str
    duration: Optional[float] = None
    author: str = ""
    thumbnail: Optional[str] = None
    requester: int = 0
    spotify_url: Optional[str] = None

    @property
    def lazy(self) -> bool:
        return self.url.startswith("ytsearch")

    @property
    def yt_id(self) -> Optional[str]:
        m = YT_ID_RE.search(self.url) if "youtu" in self.url else None
        return m.group(1) if m else None

    @property
    def link(self) -> Optional[str]:
        return self.spotify_url if self.lazy else self.url

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("requester")
        return d

    @classmethod
    def from_dict(cls, d: dict, requester: int = 0) -> "Track":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names}, requester=requester)


def _norm(text: Optional[str]) -> str:
    return re.sub(r"[\W_]+", "", (text or "").casefold())


def _entry_to_track(e: dict, requester: int) -> Optional[Track]:
    url = e.get("webpage_url") or e.get("url")
    if (e.get("ie_key") or e.get("extractor_key") or "").startswith("Youtube") and e.get("id") and len(e["id"]) == 11:
        url = f"https://www.youtube.com/watch?v={e['id']}"
    if not url or e.get("title") in UNAVAILABLE:
        return None
    thumb = e.get("thumbnail") or ((e.get("thumbnails") or [{}])[-1].get("url"))
    author = e.get("artist") or e.get("channel") or e.get("uploader") or ""
    return Track(e.get("title") or url, url, e.get("duration"), author, thumb, requester)


class YTDL:
    def __init__(self, deps):
        self.deps = deps

    async def run(self, *args: str, timeout: float = 90) -> dict:
        try:
            await asyncio.wait_for(self.deps.ready.wait(), 300)
        except asyncio.TimeoutError:
            raise ExtractError("yt-dlp is still being installed, try again in a minute.")
        if problem := self.deps.problems.get("yt-dlp") or self.deps.problems.get("python"):
            raise ExtractError(problem)
        cmd = [*self.deps.ytdlp_cmd, "-J", "--ignore-config", "--no-warnings"]
        if self.deps.js:
            cmd += ["--js-runtimes", self.deps.js]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={**os.environ, **self.deps.env} if self.deps.env else None,
            )
        except OSError as e:
            raise ExtractError(f"Couldn't start yt-dlp: {e}")
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise ExtractError("yt-dlp timed out.")
        if proc.returncode != 0:
            lines = [l for l in err.decode(errors="replace").splitlines() if "ERROR" in l] or ["unknown error"]
            log.warning("yt-dlp failed for %s: %s", args[-1], lines[-1])
            raise ExtractError(lines[-1].replace("ERROR: ", "")[:300])
        return json.loads(out)

    async def search(self, query: str, limit: int = 10, requester: int = 0) -> List[Track]:
        prefix = "ytsearch"
        if query.lower().startswith("sc "):
            prefix, query = "scsearch", query[3:]
        data = await self.run("--flat-playlist", f"{prefix}{limit}:{query}")
        return [t for e in data.get("entries") or [] if (t := _entry_to_track(e, requester))]

    async def load(self, query: str, requester: int) -> Tuple[List[Track], Optional[str]]:
        query = query.strip("<>")
        if not URL_RE.match(query):
            return (await self.search(query, 1, requester))[:1], None
        data = await self.run("--flat-playlist", "--no-playlist", "--playlist-end", str(MAX_PLAYLIST), query)
        if data.get("_type") == "playlist":
            tracks = [t for e in data.get("entries") or [] if (t := _entry_to_track(e, requester))]
            return tracks, data.get("title") or "Playlist"
        t = _entry_to_track(data, requester)
        return ([t] if t else []), None

    async def mix(self, video_id: str) -> List[Track]:
        data = await self.run("--flat-playlist", "--playlist-end", "25",
                              f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}")
        return [t for e in data.get("entries") or [] if (t := _entry_to_track(e, 0))]

    async def ytm_album(self, name: str, artist: str, requester: int) -> List[Track]:
        q = urllib.parse.quote_plus(f"{name} {artist}")
        data = await self.run("--flat-playlist", "--playlist-end", "2", f"https://music.youtube.com/search?q={q}#albums")
        for e in data.get("entries") or []:
            album = await self.run("--flat-playlist", "--playlist-end", str(MAX_PLAYLIST), e["url"])
            if _norm(re.sub(r"^\w+ - ", "", album.get("title") or "")) == _norm(name):
                return [t for x in album.get("entries") or [] if (t := _entry_to_track(x, requester))]
        return []

    async def _match(self, track: Track) -> dict:
        q = urllib.parse.quote_plus(f"{track.title} {track.author.split(', ')[0]}")
        try:
            data = await self.run("--flat-playlist", "--playlist-end", "5", f"https://music.youtube.com/search?q={q}#songs")
            for e in data.get("entries") or []:
                if _norm(e.get("title")) == _norm(track.title):
                    return e
        except ExtractError:
            pass
        data = await self.run("--flat-playlist", track.url)
        cands = [e for e in data.get("entries") or [] if e.get("title") not in UNAVAILABLE]
        if not cands:
            raise ExtractError(f"No YouTube match for {track.title}.")
        if track.duration:
            return next((e for e in cands if e.get("duration") and abs(e["duration"] - track.duration) <= 7), cands[0])
        return cands[0]

    async def stream(self, track: Track) -> Tuple[str, dict]:
        if track.lazy:
            match = _entry_to_track(await self._match(track), track.requester)
            track.url, track.thumbnail = match.url, track.thumbnail or match.thumbnail
        data = await self.run("-f", "bestaudio/best", "--no-playlist", track.url)
        track.duration = track.duration or data.get("duration")
        track.thumbnail = track.thumbnail or data.get("thumbnail")
        if data.get("is_live"):
            track.duration = None
        return data["url"], data.get("http_headers") or {}


class Spotify:
    def __init__(self, bot):
        self.bot = bot
        self._token: Optional[str] = None
        self._expires = 0.0

    async def load(self, session: aiohttp.ClientSession, kind: str, sid: str, requester: int):
        try:
            return await self._api(session, kind, sid, requester)
        except Exception as e:
            log.debug("Spotify API unavailable (%s), using embed page", e)
        return await self._embed(session, kind, sid, requester)

    @staticmethod
    def _track(name: str, artists: str, ms: Optional[int], tid: Optional[str], requester: int) -> Track:
        artists = artists.replace(" ", " ")
        return Track(name, f"ytsearch5:{artists} - {name}", ms / 1000 if ms else None, artists,
                     None, requester, f"https://open.spotify.com/track/{tid}" if tid else None)

    async def _get_token(self, session) -> str:
        if self._token and time.time() < self._expires:
            return self._token
        keys = await self.bot.get_shared_api_tokens("spotify")
        if not keys.get("client_id") or not keys.get("client_secret"):
            raise ExtractError("no Spotify API keys set")
        auth = base64.b64encode(f"{keys['client_id']}:{keys['client_secret']}".encode()).decode()
        async with session.post("https://accounts.spotify.com/api/token", data={"grant_type": "client_credentials"},
                                headers={"Authorization": f"Basic {auth}"}) as r:
            r.raise_for_status()
            d = await r.json()
        self._token, self._expires = d["access_token"], time.time() + d["expires_in"] - 60
        return self._token

    async def _api(self, session, kind, sid, requester):
        headers = {"Authorization": f"Bearer {await self._get_token(session)}"}

        async def get(url):
            async with session.get(url, headers=headers) as r:
                r.raise_for_status()
                return await r.json()

        base = "https://api.spotify.com/v1"
        if kind == "track":
            t = await get(f"{base}/tracks/{sid}")
            return [self._track(t["name"], ", ".join(a["name"] for a in t["artists"]), t["duration_ms"], t["id"], requester)], None
        meta = await get(f"{base}/{kind}s/{sid}?fields=name")
        url, tracks = f"{base}/{kind}s/{sid}/tracks?limit=50", []
        while url and len(tracks) < MAX_PLAYLIST:
            page = await get(url)
            for item in page["items"]:
                t = item.get("track") or item.get("item") or item if kind == "playlist" else item
                if t and t.get("name"):
                    tracks.append(self._track(t["name"], ", ".join(a["name"] for a in t["artists"]),
                                              t.get("duration_ms"), t.get("id"), requester))
            url = page.get("next")
        return tracks, meta.get("name")

    async def _embed(self, session, kind, sid, requester):
        async with session.get(f"https://open.spotify.com/embed/{kind}/{sid}", headers={"User-Agent": "Mozilla/5.0"}) as r:
            if r.status != 200:
                raise ExtractError(f"Spotify returned HTTP {r.status}.")
            html = await r.text()
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>', html)
        if not m:
            raise ExtractError("Couldn't read that Spotify page.")
        e = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
        if kind == "track":
            artists = ", ".join(a["name"] for a in e.get("artists", []))
            return [self._track(e["name"], artists, e.get("duration"), sid, requester)], None
        tracks = [self._track(t["title"], t.get("subtitle", ""), t.get("duration"), t["uri"].split(":")[-1], requester)
                  for t in e.get("trackList", []) if t.get("isPlayable", True)]
        return tracks, e.get("name")
