import asyncio
import logging
import random
import shlex
import time
from collections import deque
from typing import TYPE_CHECKING, Deque, List, Optional, Tuple

import discord

from .sources import ExtractError, Track

if TYPE_CHECKING:
    from .ytaudio import YTAudio

log = logging.getLogger("red.ytaudio")
STREAM_TTL = 3 * 3600


def fmt_time(seconds: Optional[float]) -> str:
    if seconds is None:
        return "LIVE"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    return f"{h}:{rem // 60:02d}:{rem % 60:02d}" if h else f"{rem // 60}:{rem % 60:02d}"


class GuildPlayer:
    def __init__(self, cog: "YTAudio", guild: discord.Guild):
        self.cog = cog
        self.guild = guild
        self.queue: List[Track] = []
        self.history: Deque[Track] = deque(maxlen=50)
        self.current: Optional[Track] = None
        self.text_channel: Optional[discord.abc.Messageable] = None
        self.volume = 100
        self.votes: set = set()
        self.empty_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._gen = 0
        self._started = 0.0
        self._offset = 0.0
        self._paused_at: Optional[float] = None
        self._stream: Optional[tuple] = None
        self._cache: Optional[tuple] = None
        self._inflight: Optional[tuple] = None
        self._prefetch_task: Optional[asyncio.Task] = None
        self._autoplay_next: Optional[Track] = None
        self._shuffle_pick: Optional[Track] = None

    @property
    def vc(self) -> Optional[discord.VoiceClient]:
        return self.guild.voice_client

    @property
    def position(self) -> float:
        if not self.current:
            return 0.0
        return self._offset + ((self._paused_at or time.monotonic()) - self._started)

    @property
    def paused(self) -> bool:
        return self._paused_at is not None

    def pause(self):
        if self.vc and self.vc.is_playing():
            self.vc.pause()
            self._paused_at = time.monotonic()

    def resume(self):
        if self.vc and self.vc.is_paused():
            self.vc.resume()
            self._started += time.monotonic() - (self._paused_at or time.monotonic())
            self._paused_at = None

    async def send(self, content=None, **kwargs):
        if self.text_channel:
            try:
                await self.text_channel.send(content, **kwargs)
            except discord.HTTPException:
                pass

    async def start_if_idle(self):
        async with self._lock:
            if self.current is None:
                await self._advance()

    async def skip(self):
        if self.vc and (self.vc.is_playing() or self.vc.is_paused()):
            self.vc.stop()
        else:
            async with self._lock:
                self.current = None
                await self._advance()

    async def previous(self) -> bool:
        async with self._lock:
            if not self.history:
                return False
            if self.current:
                self.queue.insert(0, self.current)
            self.queue.insert(0, self.history.pop())
            self._halt()
            await self._advance()
            return True

    async def seek(self, seconds: float):
        async with self._lock:
            track, stream = self.current, self._stream
            self._halt()
            if not await self._start(track, max(0.0, seconds), stream):
                await self._advance()

    async def set_volume(self, volume: int):
        self.volume = volume
        vc = self.vc
        if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = volume / 100
        elif self.current and self.current.duration and vc and (vc.is_playing() or vc.is_paused()):
            await self.seek(self.position)

    async def stop(self):
        async with self._lock:
            self.queue.clear()
            self._halt()
            self._cache = self._inflight = self._autoplay_next = self._shuffle_pick = None

    async def destroy(self):
        await self.stop()
        if self.empty_task:
            self.empty_task.cancel()
        if self.vc:
            await self.vc.disconnect(force=True)

    def _halt(self):
        self._gen += 1
        self.current = None
        self._paused_at = None
        if self.vc:
            self.vc.stop()

    def _after(self, gen: int, error: Optional[Exception]):
        if error:
            log.warning("Playback error in %s: %s", self.guild.id, error)
        asyncio.run_coroutine_threadsafe(self._on_end(gen), self.cog.bot.loop)

    async def _on_end(self, gen: int):
        async with self._lock:
            if gen != self._gen:
                return
            if self.current:
                self.history.append(self.current)
                if await self.cog.config.guild(self.guild).repeat():
                    self.queue.append(self.current)
            self.current = None
            await self._advance()

    async def _advance(self):
        settings = await self.cog.config.guild(self.guild).all()
        failures = 0
        while self.vc and self.vc.is_connected():
            if self.queue:
                pick = settings["shuffle"] and self.queue[0] is not self._shuffle_pick
                track = self.queue.pop(random.randrange(len(self.queue)) if pick else 0)
            elif settings["autoplay"] and (track := self._autoplay_next or await self._autoplay_track()):
                self._autoplay_next = None
            else:
                await self.send("Queue ended.")
                if settings["dc_at_end"]:
                    await self.vc.disconnect(force=True)
                return
            if await self._start(track, 0.0, quiet=failures > 0):
                return
            failures += 1
            if failures >= 3:
                self.queue.clear()
                self._autoplay_next = self._shuffle_pick = None
                await self.send("Stopped: 3 tracks in a row couldn't be played, so I cleared the queue.")
                return

    async def _autoplay_track(self) -> Optional[Track]:
        recent = list(self.history) + ([self.current] if self.current else [])
        seed = next((t.yt_id for t in reversed(recent) if t.yt_id), None)
        if not seed:
            return None
        try:
            mix = await self.cog.ytdl.mix(seed)
        except ExtractError:
            return None
        played = {t.yt_id for t in recent}
        options = [t for t in mix if t.yt_id not in played][:10]
        if not options:
            return None
        track = random.choice(options)
        track.requester = self.cog.bot.user.id
        return track

    def schedule_prefetch(self):
        if self.current and not (self._prefetch_task and not self._prefetch_task.done()):
            self._prefetch_task = asyncio.create_task(self._prefetch())

    async def _peek_next(self) -> Optional[Track]:
        settings = await self.cog.config.guild(self.guild).all()
        if self.queue:
            if settings["shuffle"] and self.queue[0] is not self._shuffle_pick:
                self.queue.insert(0, self.queue.pop(random.randrange(len(self.queue))))
                self._shuffle_pick = self.queue[0]
            return self.queue[0]
        if settings["autoplay"]:
            if not self._autoplay_next:
                self._autoplay_next = await self._autoplay_track()
            return self._autoplay_next
        return None

    async def _prefetch(self):
        while self.current:
            track = await self._peek_next()
            if track is None or (self._cache and self._cache[0] is track):
                return
            future = asyncio.ensure_future(self.cog.ytdl.stream(track))
            self._inflight = (track, future)
            try:
                url, headers = await future
            except ExtractError:
                return
            finally:
                self._inflight = None
            self._cache = (track, url, headers, time.monotonic())

    async def _resolve(self, track: Track) -> Tuple[str, dict]:
        cache, self._cache = self._cache, None
        if cache and cache[0] is track and time.monotonic() - cache[3] < STREAM_TTL:
            return cache[1], cache[2]
        if self._inflight and self._inflight[0] is track:
            return await asyncio.shield(self._inflight[1])
        return await self.cog.ytdl.stream(track)

    async def _start(self, track: Track, offset: float, stream: Optional[tuple] = None, quiet: bool = False) -> bool:
        maxlength = await self.cog.config.guild(self.guild).maxlength()
        try:
            if stream and time.monotonic() - stream[2] < STREAM_TTL:
                url, headers = stream[0], stream[1]
            else:
                url, headers = await self._resolve(track)
        except ExtractError as e:
            if not quiet:
                await self.send(f"Couldn't play **{discord.utils.escape_markdown(track.title)}**: {e}")
            return False
        if maxlength and track.duration and track.duration > maxlength:
            if not quiet:
                await self.send(f"Skipped **{discord.utils.escape_markdown(track.title)}**: longer than {fmt_time(maxlength)}.")
            return False
        vc = self.vc
        if not vc or not vc.is_connected():
            return False
        before = "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
        if headers:
            before += " -headers " + shlex.quote("".join(f"{k}: {v}\r\n" for k, v in headers.items()))
        if offset:
            before += f" -ss {offset:.1f}"
        ffmpeg = self.cog.deps.ffmpeg or "ffmpeg"
        try:
            if self.cog.deps.opus_available():
                source = discord.PCMVolumeTransformer(
                    discord.FFmpegPCMAudio(url, executable=ffmpeg, before_options=before, options="-vn"),
                    volume=self.volume / 100,
                )
            else:
                source = discord.FFmpegOpusAudio(
                    url, executable=ffmpeg, before_options=before, options=f"-vn -filter:a volume={self.volume / 100:.2f}"
                )
        except (discord.ClientException, OSError) as e:
            log.warning("FFmpeg failed to start: %s", e)
            if not quiet:
                await self.send(f"Couldn't start FFmpeg: {e}")
            asyncio.create_task(self.cog.deps.ensure())
            return False
        self._gen += 1
        gen = self._gen
        self.current, self.votes = track, set()
        self._started, self._offset, self._paused_at = time.monotonic(), offset, None
        self._stream = (url, headers, stream[2] if stream else time.monotonic())
        if self._shuffle_pick is track:
            self._shuffle_pick = None
        vc.play(source, after=lambda e: self._after(gen, e))
        self.schedule_prefetch()
        if not offset and await self.cog.config.guild(self.guild).notify():
            await self.send(embed=await self.cog.now_embed(self, title="Now Playing"))
        return True
