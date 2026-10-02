import asyncio
import logging
import random
import shlex
import time
from collections import deque
from typing import TYPE_CHECKING, Deque, List, Optional

import discord

from .sources import ExtractError, Track

if TYPE_CHECKING:
    from .ytaudio import YTAudio

log = logging.getLogger("red.ytaudio")


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
            track = self.current
            self._halt()
            if not await self._start(track, max(0.0, seconds)):
                await self._advance()

    async def stop(self):
        async with self._lock:
            self.queue.clear()
            self._halt()

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
        while self.vc and self.vc.is_connected():
            if self.queue:
                track = self.queue.pop(random.randrange(len(self.queue)) if settings["shuffle"] else 0)
            elif settings["autoplay"] and (track := await self._autoplay_track()):
                pass
            else:
                await self.send("Queue ended.")
                if settings["dc_at_end"]:
                    await self.vc.disconnect(force=True)
                return
            if await self._start(track, 0.0):
                return

    async def _autoplay_track(self) -> Optional[Track]:
        seed = next((t.yt_id for t in reversed(self.history) if t.yt_id), None)
        if not seed:
            return None
        try:
            mix = await self.cog.ytdl.mix(seed)
        except ExtractError:
            return None
        played = {t.yt_id for t in self.history}
        options = [t for t in mix if t.yt_id not in played][:10]
        if not options:
            return None
        track = random.choice(options)
        track.requester = self.cog.bot.user.id
        return track

    async def _start(self, track: Track, offset: float) -> bool:
        maxlength = await self.cog.config.guild(self.guild).maxlength()
        try:
            url, headers = await self.cog.ytdl.stream(track)
        except ExtractError as e:
            await self.send(f"Couldn't play **{discord.utils.escape_markdown(track.title)}**: {e}")
            return False
        if maxlength and track.duration and track.duration > maxlength:
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
        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(url, before_options=before, options="-vn"), volume=self.volume / 100
        )
        self._gen += 1
        gen = self._gen
        self.current, self.votes = track, set()
        self._started, self._offset, self._paused_at = time.monotonic(), offset, None
        vc.play(source, after=lambda e: self._after(gen, e))
        if not offset and await self.cog.config.guild(self.guild).notify():
            await self.send(embed=await self.cog.now_embed(self, title="Now Playing"))
        return True
