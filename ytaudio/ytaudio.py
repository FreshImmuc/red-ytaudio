import asyncio
import logging
import random
from collections import Counter
from typing import Dict, Optional, Union

import aiohttp
import discord
from redbot.core import Config, commands
from redbot.core.data_manager import cog_data_path
from redbot.core.utils.chat_formatting import box, pagify
from redbot.core.utils.menus import menu

from .deps import Deps
from .player import GuildPlayer, fmt_time
from .sources import SPOTIFY_RE, YTDL, ExtractError, Spotify, Track

log = logging.getLogger("red.ytaudio")


class Feedback(Exception):
    pass


def esc(text: str) -> str:
    return discord.utils.escape_markdown(text or "")


def track_line(t: Track) -> str:
    title = f"[{esc(t.title)}]({t.link})" if t.link else esc(t.title)
    author = f" - {esc(t.author)}" if t.author else ""
    return f"{title}{author} `{fmt_time(t.duration)}`"


class YTAudio(commands.Cog):
    """Music player using yt-dlp. Plays YouTube, SoundCloud and anything else yt-dlp supports, plus Spotify links (matched on YouTube)."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x79746175646F, force_registration=True)
        self.config.register_global(ytdlp_path=None, js_runtime=None, auto_update=True, last_update=0.0)
        self.config.register_guild(
            volume=100, max_volume=150, repeat=False, shuffle=False, autoplay=False, notify=True,
            dj_enabled=False, dj_role=None, vote_percent=0, empty_dc=300, dc_at_end=False, maxlength=0,
            playlists={},
        )
        self.players: Dict[int, GuildPlayer] = {}
        self.deps = Deps(self, cog_data_path(self))
        self.ytdl = YTDL(self.deps)
        self.spotify = Spotify(bot)
        self.session = aiohttp.ClientSession()
        self._deps_task: Optional[asyncio.Task] = None

    async def cog_load(self):
        self._deps_task = asyncio.create_task(self.deps.loop())

    async def cog_unload(self):
        if self._deps_task:
            self._deps_task.cancel()
        for player in list(self.players.values()):
            try:
                await player.destroy()
            except Exception:
                log.exception("Failed to disconnect in %s", player.guild.id)
        await self.session.close()

    async def red_delete_data_for_user(self, **kwargs):
        return

    async def cog_check(self, ctx: commands.Context) -> bool:
        return ctx.guild is not None

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        if isinstance(error, commands.CommandInvokeError) and isinstance(error.original, (Feedback, ExtractError)):
            await ctx.send(str(error.original))
        else:
            await self.bot.on_command_error(ctx, error, unhandled_by_cog=True)

    async def require(self, ctx: commands.Context):
        deps = self.deps
        if deps.ready.is_set() and not deps.blocking() and not deps.lock.locked():
            return
        notice = await ctx.send("Setting up music support, one moment. I'll continue automatically.")
        async with ctx.typing():
            await deps.recheck()
        try:
            await notice.delete()
        except discord.HTTPException:
            pass
        if blocking := deps.blocking():
            raise Feedback("Music isn't available right now:\n" + "\n".join(f"- {p}" for p in blocking.values())
                           + "\nThe bot owner has been notified.")

    def get_player(self, guild: discord.Guild) -> GuildPlayer:
        if guild.id not in self.players:
            self.players[guild.id] = GuildPlayer(self, guild)
        return self.players[guild.id]

    async def is_dj(self, ctx: commands.Context) -> bool:
        member = ctx.author
        if await self.bot.is_owner(member) or await self.bot.is_mod(member) or member.guild_permissions.manage_guild:
            return True
        settings = await self.config.guild(ctx.guild).all()
        if not settings["dj_enabled"]:
            return True
        return any(r.id == settings["dj_role"] for r in member.roles)

    async def connect(self, ctx: commands.Context) -> GuildPlayer:
        voice = ctx.author.voice
        if not voice or not voice.channel:
            raise Feedback("Join a voice channel first.")
        await self.require(ctx)
        vc = ctx.guild.voice_client
        player = self.get_player(ctx.guild)
        if vc is None:
            perms = voice.channel.permissions_for(ctx.me)
            if not (perms.connect and perms.speak):
                raise Feedback("I need permission to connect and speak in that channel.")
            await voice.channel.connect(self_deaf=True)
        elif vc.channel != voice.channel:
            if player.current and not await self.is_dj(ctx):
                raise Feedback("I'm already playing in another channel.")
            await vc.move_to(voice.channel)
        if not player.current:
            player.volume = await self.config.guild(ctx.guild).volume()
        player.text_channel = ctx.channel
        return player

    async def control(self, ctx: commands.Context, dj: bool = True) -> GuildPlayer:
        vc = ctx.guild.voice_client
        if not vc or ctx.guild.id not in self.players:
            raise Feedback("I'm not connected to a voice channel.")
        if dj and not await self.is_dj(ctx):
            raise Feedback("You need the DJ role to do that.")
        if not (await self.bot.is_owner(ctx.author) or await self.bot.is_mod(ctx.author)):
            if not ctx.author.voice or ctx.author.voice.channel != vc.channel:
                raise Feedback("You must be in my voice channel to do that.")
        player = self.players[ctx.guild.id]
        player.text_channel = ctx.channel
        return player

    async def resolve(self, query: str, requester: int):
        if m := SPOTIFY_RE.search(query):
            tracks, title = await self.spotify.load(self.session, m.group(1), m.group(2), requester)
            if m.group(1) == "album" and tracks and title:
                try:
                    tracks = await self.ytdl.ytm_album(title, tracks[0].author.split(", ")[0], requester) or tracks
                except ExtractError:
                    pass
                try:
                    await self.ytdl.stream(tracks[0])
                except ExtractError as e:
                    raise Feedback(f"Can't play **{esc(title)}**: {e}")
            return tracks, title
        return await self.ytdl.load(query, requester)

    async def enqueue(self, ctx: commands.Context, player: GuildPlayer, tracks, title, bump=False):
        if not tracks:
            raise Feedback("Nothing found.")
        maxlength = await self.config.guild(ctx.guild).maxlength()
        if maxlength:
            tracks = [t for t in tracks if not t.duration or t.duration <= maxlength]
            if not tracks:
                raise Feedback(f"That's longer than the {fmt_time(maxlength)} limit.")
        was_busy = player.current is not None
        if bump:
            player.queue[0:0] = tracks
            player._shuffle_pick = tracks[0]
        else:
            player.queue.extend(tracks)
        player.schedule_prefetch()
        if title:
            await ctx.send(embed=discord.Embed(
                colour=await ctx.embed_colour(), title="Playlist Enqueued",
                description=f"**{esc(title)}**: {len(tracks)} tracks added."))
        elif was_busy:
            pos = 1 if bump else len(player.queue)
            await ctx.send(embed=discord.Embed(
                colour=await ctx.embed_colour(), title="Track Enqueued",
                description=f"{track_line(tracks[0])}\nPosition in queue: {pos}"))
        await player.start_if_idle()

    async def now_embed(self, player: GuildPlayer, title="Now Playing") -> discord.Embed:
        t = player.current
        colour = await self.bot.get_embed_colour(player.text_channel) if player.text_channel else discord.Colour.red()
        embed = discord.Embed(colour=colour, title=title)
        if not t:
            embed.description = "Nothing playing."
            return embed
        embed.description = track_line(t)
        if t.duration:
            done = int(player.position / t.duration * 20)
            embed.description += f"\n`{fmt_time(player.position)}` {'▬' * done}🔘{'▬' * (20 - done)} `{fmt_time(t.duration)}`"
        if t.thumbnail:
            embed.set_thumbnail(url=t.thumbnail)
        requester = player.guild.get_member(t.requester)
        s = await self.config.guild(player.guild).all()
        flags = [n for n, on in (("Repeat", s["repeat"]), ("Shuffle", s["shuffle"]), ("Autoplay", s["autoplay"]),
                                 ("Paused", player.paused)) if on]
        embed.set_footer(text=(f"Requested by {requester.display_name} | " if requester else "")
                         + f"{len(player.queue)} in queue | Volume {player.volume}%"
                         + (f" | {', '.join(flags)}" if flags else ""))
        return embed

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before, after):
        player = self.players.get(member.guild.id)
        if not player:
            return
        if member.id == self.bot.user.id and after.channel is None:
            await player.stop()
            if player.empty_task:
                player.empty_task.cancel()
            self.players.pop(member.guild.id, None)
            return
        vc = player.vc
        if not vc or not vc.channel:
            return
        listeners = [m for m in vc.channel.members if not m.bot]
        if listeners and player.empty_task:
            player.empty_task.cancel()
            player.empty_task = None
        elif not listeners and not player.empty_task:
            delay = await self.config.guild(member.guild).empty_dc()
            if delay:
                player.empty_task = asyncio.create_task(self._empty_disconnect(player, delay))

    async def _empty_disconnect(self, player: GuildPlayer, delay: int):
        await asyncio.sleep(delay)
        vc = player.vc
        if vc and vc.channel and not [m for m in vc.channel.members if not m.bot]:
            player.empty_task = None
            await player.destroy()
            self.players.pop(player.guild.id, None)

    @commands.command()
    async def play(self, ctx: commands.Context, *, query: str):
        """Play a URL (YouTube, Spotify, SoundCloud, ...) or search YouTube."""
        if not await self.is_dj(ctx):
            raise Feedback("You need the DJ role to queue tracks.")
        player = await self.connect(ctx)
        async with ctx.typing():
            tracks, title = await self.resolve(query, ctx.author.id)
            await self.enqueue(ctx, player, tracks, title)

    @commands.command()
    async def bumpplay(self, ctx: commands.Context, *, query: str):
        """Add a track to the front of the queue."""
        if not await self.is_dj(ctx):
            raise Feedback("You need the DJ role to queue tracks.")
        player = await self.connect(ctx)
        async with ctx.typing():
            tracks, title = await self.resolve(query, ctx.author.id)
            await self.enqueue(ctx, player, tracks, title, bump=True)

    @commands.command()
    async def search(self, ctx: commands.Context, *, query: str):
        """Search YouTube and pick a result. Start the query with `sc ` to search SoundCloud."""
        if not await self.is_dj(ctx):
            raise Feedback("You need the DJ role to queue tracks.")
        await self.require(ctx)
        async with ctx.typing():
            results = await self.ytdl.search(query, 10, ctx.author.id)
        if not results:
            raise Feedback("Nothing found.")
        lines = "\n".join(f"`{i}.` {track_line(t)}" for i, t in enumerate(results, 1))
        await ctx.send(embed=discord.Embed(colour=await ctx.embed_colour(), title="Pick a number (30s)", description=lines))
        try:
            msg = await self.bot.wait_for("message", timeout=30, check=lambda m: (
                m.author == ctx.author and m.channel == ctx.channel and m.content.isdigit()
                and 1 <= int(m.content) <= len(results)))
        except asyncio.TimeoutError:
            return
        player = await self.connect(ctx)
        await self.enqueue(ctx, player, [results[int(msg.content) - 1]], None)

    @commands.command()
    async def autoplay(self, ctx: commands.Context):
        """Toggle autoplay: when the queue ends, keep playing related YouTube tracks."""
        if not await self.is_dj(ctx):
            raise Feedback("You need the DJ role to do that.")
        on = not await self.config.guild(ctx.guild).autoplay()
        await self.config.guild(ctx.guild).autoplay.set(on)
        await ctx.send(f"Autoplay {'enabled' if on else 'disabled'}.")
        player = self.players.get(ctx.guild.id)
        if on and player and ctx.guild.voice_client:
            await player.start_if_idle()
            player.schedule_prefetch()

    @commands.command(aliases=["np"])
    async def now(self, ctx: commands.Context):
        """Show the current track."""
        player = self.players.get(ctx.guild.id)
        if not player or not player.current:
            raise Feedback("Nothing playing.")
        await ctx.send(embed=await self.now_embed(player))

    @commands.command()
    async def pause(self, ctx: commands.Context):
        """Pause or resume playback."""
        player = await self.control(ctx)
        if player.paused:
            player.resume()
            await ctx.send("Resumed.")
        else:
            player.pause()
            await ctx.send("Paused.")

    @commands.command()
    async def resume(self, ctx: commands.Context):
        """Resume playback."""
        (await self.control(ctx)).resume()
        await ctx.send("Resumed.")

    @commands.command()
    async def skip(self, ctx: commands.Context, skip_to: int = 1):
        """Skip the current track, or skip to a queue position."""
        player = await self.control(ctx, dj=False)
        if not player.current:
            raise Feedback("Nothing playing.")
        vote = await self.config.guild(ctx.guild).vote_percent()
        if not await self.is_dj(ctx) and ctx.author.id != player.current.requester:
            if not vote:
                raise Feedback("You need the DJ role to skip other people's tracks.")
            listeners = [m for m in player.vc.channel.members if not m.bot]
            player.votes.add(ctx.author.id)
            needed = max(1, -(-len(listeners) * vote // 100))
            if len(player.votes) < needed:
                return await ctx.send(f"Vote to skip: {len(player.votes)}/{needed}.")
            skip_to = 1
        if skip_to > 1:
            del player.queue[: skip_to - 1]
        await ctx.send(f"Skipped **{esc(player.current.title)}**.")
        await player.skip()

    @commands.command()
    async def prev(self, ctx: commands.Context):
        """Play the previous track again."""
        player = await self.control(ctx)
        if not await player.previous():
            raise Feedback("No previous track.")

    @commands.command()
    async def stop(self, ctx: commands.Context):
        """Stop playback, clear the queue and leave the voice channel."""
        player = await self.control(ctx)
        await player.destroy()
        self.players.pop(ctx.guild.id, None)
        await ctx.send("Stopped, cleared the queue and left the channel.")

    @commands.command(aliases=["dc"])
    async def disconnect(self, ctx: commands.Context):
        """Disconnect from voice."""
        player = await self.control(ctx)
        await player.destroy()
        self.players.pop(ctx.guild.id, None)
        await ctx.send("Disconnected.")

    @commands.command()
    async def summon(self, ctx: commands.Context):
        """Move the bot to your voice channel."""
        if not await self.is_dj(ctx):
            raise Feedback("You need the DJ role to do that.")
        await self.connect(ctx)
        await ctx.tick()

    @commands.command()
    async def seek(self, ctx: commands.Context, position: str):
        """Seek to a position (`1:30`, `90`) or by an offset (`+30`, `-10`)."""
        player = await self.control(ctx)
        if not player.current or not player.current.duration:
            raise Feedback("Nothing seekable playing.")
        try:
            secs = sum(float(p) * 60 ** i for i, p in enumerate(reversed(position.lstrip("+-").split(":"))))
        except ValueError:
            raise Feedback("Use a time like `1:30`, `90`, `+30` or `-10`.")
        if position[0] in "+-":
            secs = player.position + (secs if position[0] == "+" else -secs)
        if secs >= player.current.duration:
            return await player.skip()
        await player.seek(secs)
        await ctx.send(f"Seeked to `{fmt_time(secs)}`.")

    @commands.command()
    async def volume(self, ctx: commands.Context, vol: Optional[int] = None):
        """Show or set the volume."""
        if vol is None:
            return await ctx.send(f"Volume: {await self.config.guild(ctx.guild).volume()}%")
        player = await self.control(ctx) if ctx.guild.voice_client else None
        if not player and not await self.is_dj(ctx):
            raise Feedback("You need the DJ role to do that.")
        vol = max(0, min(vol, await self.config.guild(ctx.guild).max_volume()))
        await self.config.guild(ctx.guild).volume.set(vol)
        if player:
            await player.set_volume(vol)
        await ctx.send(f"Volume set to {vol}%.")

    @commands.command()
    async def repeat(self, ctx: commands.Context):
        """Toggle repeating the queue."""
        if not await self.is_dj(ctx):
            raise Feedback("You need the DJ role to do that.")
        on = not await self.config.guild(ctx.guild).repeat()
        await self.config.guild(ctx.guild).repeat.set(on)
        await ctx.send(f"Repeat {'enabled' if on else 'disabled'}.")

    @commands.command()
    async def shuffle(self, ctx: commands.Context):
        """Toggle shuffle mode."""
        if not await self.is_dj(ctx):
            raise Feedback("You need the DJ role to do that.")
        on = not await self.config.guild(ctx.guild).shuffle()
        await self.config.guild(ctx.guild).shuffle.set(on)
        await ctx.send(f"Shuffle {'enabled' if on else 'disabled'}.")
        if player := self.players.get(ctx.guild.id):
            player.schedule_prefetch()

    @commands.command()
    async def remove(self, ctx: commands.Context, index_or_user: Union[int, discord.Member]):
        """Remove a track by queue position, or all tracks queued by a user."""
        player = await self.control(ctx)
        if isinstance(index_or_user, int):
            if not 1 <= index_or_user <= len(player.queue):
                raise Feedback("No track at that position.")
            t = player.queue.pop(index_or_user - 1)
            player.schedule_prefetch()
            return await ctx.send(f"Removed **{esc(t.title)}**.")
        before = len(player.queue)
        player.queue = [t for t in player.queue if t.requester != index_or_user.id]
        player.schedule_prefetch()
        await ctx.send(f"Removed {before - len(player.queue)} tracks queued by {esc(index_or_user.display_name)}.")

    @commands.command()
    async def bump(self, ctx: commands.Context, index: int):
        """Move a queued track to the front."""
        player = await self.control(ctx)
        if not 1 <= index <= len(player.queue):
            raise Feedback("No track at that position.")
        player.queue.insert(0, player.queue.pop(index - 1))
        player._shuffle_pick = player.queue[0]
        player.schedule_prefetch()
        await ctx.send(f"Moved **{esc(player.queue[0].title)}** to the front.")

    @commands.command()
    async def percent(self, ctx: commands.Context):
        """Show who queued how much."""
        player = self.players.get(ctx.guild.id)
        if not player or not player.queue:
            raise Feedback("The queue is empty.")
        counts = Counter(t.requester for t in player.queue)
        lines = [f"{esc(getattr(ctx.guild.get_member(uid), 'display_name', str(uid)))}: {n / len(player.queue):.0%} ({n})"
                 for uid, n in counts.most_common()]
        await ctx.send(embed=discord.Embed(colour=await ctx.embed_colour(), title="Queue share", description="\n".join(lines)))

    @commands.command()
    @commands.is_owner()
    async def audiostats(self, ctx: commands.Context):
        """Where the bot is playing right now."""
        lines = [f"{esc(p.guild.name)}: {esc(p.current.title) if p.current else 'idle'} ({len(p.queue)} queued)"
                 for p in self.players.values() if p.vc]
        await ctx.send("\n".join(lines) or "Not connected anywhere.")

    @commands.group(invoke_without_command=True)
    async def queue(self, ctx: commands.Context):
        """Show the queue."""
        player = self.players.get(ctx.guild.id)
        if not player or (not player.current and not player.queue):
            raise Feedback("The queue is empty.")
        total = sum(t.duration or 0 for t in player.queue)
        head = (f"**Playing:** {track_line(player.current)}\n\n" if player.current else "")
        lines = [f"`{i}.` {track_line(t)}" for i, t in enumerate(player.queue, 1)] or ["Nothing queued."]
        pages, colour = [], await ctx.embed_colour()
        chunks = [lines[i:i + 10] for i in range(0, len(lines), 10)]
        for n, chunk in enumerate(chunks, 1):
            e = discord.Embed(colour=colour, title=f"Queue for {ctx.guild.name}", description=head + "\n".join(chunk))
            e.set_footer(text=f"Page {n}/{len(chunks)} | {len(player.queue)} tracks | {fmt_time(total)} total")
            pages.append(e)
        await menu(ctx, pages)

    @queue.command(name="clear")
    async def queue_clear(self, ctx: commands.Context):
        """Clear the queue."""
        (await self.control(ctx)).queue.clear()
        await ctx.send("Queue cleared.")

    @queue.command(name="clean")
    async def queue_clean(self, ctx: commands.Context):
        """Remove tracks queued by people who left the voice channel."""
        player = await self.control(ctx)
        present = {m.id for m in player.vc.channel.members}
        before = len(player.queue)
        player.queue = [t for t in player.queue if t.requester in present]
        await ctx.send(f"Removed {before - len(player.queue)} tracks.")

    @queue.command(name="cleanself")
    async def queue_cleanself(self, ctx: commands.Context):
        """Remove all tracks you queued."""
        player = await self.control(ctx, dj=False)
        before = len(player.queue)
        player.queue = [t for t in player.queue if t.requester != ctx.author.id]
        await ctx.send(f"Removed {before - len(player.queue)} of your tracks.")

    @queue.command(name="search")
    async def queue_search(self, ctx: commands.Context, *, text: str):
        """Find tracks in the queue."""
        player = self.players.get(ctx.guild.id)
        hits = [f"`{i}.` {track_line(t)}" for i, t in enumerate(player.queue if player else [], 1)
                if text.lower() in f"{t.title} {t.author}".lower()]
        await ctx.send("\n".join(hits[:15]) or "No matches.", suppress_embeds=True)

    @queue.command(name="shuffle")
    async def queue_shuffle(self, ctx: commands.Context):
        """Shuffle the queue once."""
        player = await self.control(ctx)
        random.shuffle(player.queue)
        player.schedule_prefetch()
        await ctx.send("Queue shuffled.")

    async def _playlist(self, ctx, name: str, *, owner_only=False) -> dict:
        playlists = await self.config.guild(ctx.guild).playlists()
        pl = playlists.get(name.lower())
        if not pl:
            raise Feedback(f"No playlist named `{name}`.")
        if owner_only and pl["author"] != ctx.author.id and not await self.bot.is_mod(ctx.author):
            raise Feedback("Only the playlist's creator or a mod can change it.")
        return pl

    @commands.group()
    async def playlist(self, ctx: commands.Context):
        """Saved server playlists."""

    @playlist.command(name="create")
    async def pl_create(self, ctx: commands.Context, name: str, *, query: Optional[str] = None):
        """Create a playlist, optionally from a URL (YouTube/Spotify playlist, track, ...)."""
        async with self.config.guild(ctx.guild).playlists() as pls:
            if name.lower() in pls:
                raise Feedback("A playlist with that name already exists.")
        tracks = []
        if query:
            async with ctx.typing():
                tracks, _ = await self.resolve(query, ctx.author.id)
        async with self.config.guild(ctx.guild).playlists() as pls:
            pls[name.lower()] = {"name": name, "author": ctx.author.id, "tracks": [t.to_dict() for t in tracks]}
        await ctx.send(f"Created playlist `{name}` with {len(tracks)} tracks.")

    @playlist.command(name="append")
    async def pl_append(self, ctx: commands.Context, name: str, *, query: str):
        """Add a track (or a whole playlist URL) to a playlist."""
        await self._playlist(ctx, name, owner_only=True)
        async with ctx.typing():
            tracks, _ = await self.resolve(query, ctx.author.id)
        if not tracks:
            raise Feedback("Nothing found.")
        async with self.config.guild(ctx.guild).playlists() as pls:
            pls[name.lower()]["tracks"].extend(t.to_dict() for t in tracks)
        await ctx.send(f"Added {len(tracks)} tracks to `{name}`.")

    @playlist.command(name="queue")
    async def pl_queue(self, ctx: commands.Context, name: str):
        """Save the current queue (including the playing track) as a new playlist."""
        player = self.players.get(ctx.guild.id)
        tracks = ([player.current] if player and player.current else []) + (player.queue if player else [])
        if not tracks:
            raise Feedback("The queue is empty.")
        async with self.config.guild(ctx.guild).playlists() as pls:
            if name.lower() in pls:
                raise Feedback("A playlist with that name already exists.")
            pls[name.lower()] = {"name": name, "author": ctx.author.id, "tracks": [t.to_dict() for t in tracks]}
        await ctx.send(f"Saved {len(tracks)} tracks as `{name}`.")

    @playlist.command(name="remove")
    async def pl_remove(self, ctx: commands.Context, name: str, index: int):
        """Remove a track from a playlist by position."""
        pl = await self._playlist(ctx, name, owner_only=True)
        if not 1 <= index <= len(pl["tracks"]):
            raise Feedback("No track at that position.")
        async with self.config.guild(ctx.guild).playlists() as pls:
            t = pls[name.lower()]["tracks"].pop(index - 1)
        await ctx.send(f"Removed **{esc(t['title'])}** from `{name}`.")

    @playlist.command(name="start", aliases=["play"])
    async def pl_start(self, ctx: commands.Context, name: str):
        """Queue a saved playlist."""
        if not await self.is_dj(ctx):
            raise Feedback("You need the DJ role to queue tracks.")
        pl = await self._playlist(ctx, name)
        player = await self.connect(ctx)
        await self.enqueue(ctx, player, [Track.from_dict(t, ctx.author.id) for t in pl["tracks"]], pl["name"])

    @playlist.command(name="list")
    async def pl_list(self, ctx: commands.Context):
        """List saved playlists."""
        pls = await self.config.guild(ctx.guild).playlists()
        if not pls:
            raise Feedback("No playlists yet. Create one with `playlist create`.")
        lines = [f"`{p['name']}`: {len(p['tracks'])} tracks, by "
                 f"{esc(getattr(ctx.guild.get_member(p['author']), 'display_name', 'unknown'))}" for p in pls.values()]
        for page in pagify("\n".join(lines)):
            await ctx.send(page)

    @playlist.command(name="info")
    async def pl_info(self, ctx: commands.Context, name: str):
        """Show a playlist's tracks."""
        pl = await self._playlist(ctx, name)
        lines = [f"`{i}.` {track_line(Track.from_dict(t))}" for i, t in enumerate(pl["tracks"], 1)] or ["Empty."]
        colour = await ctx.embed_colour()
        pages = [discord.Embed(colour=colour, title=f"Playlist {pl['name']}", description="\n".join(lines[i:i + 10]))
                 for i in range(0, len(lines), 10)]
        await menu(ctx, pages)

    @playlist.command(name="rename")
    async def pl_rename(self, ctx: commands.Context, name: str, new_name: str):
        """Rename a playlist."""
        await self._playlist(ctx, name, owner_only=True)
        async with self.config.guild(ctx.guild).playlists() as pls:
            if new_name.lower() in pls and new_name.lower() != name.lower():
                raise Feedback("A playlist with that name already exists.")
            pl = pls.pop(name.lower())
            pl["name"] = new_name
            pls[new_name.lower()] = pl
        await ctx.send(f"Renamed `{name}` to `{new_name}`.")

    @playlist.command(name="delete")
    async def pl_delete(self, ctx: commands.Context, name: str):
        """Delete a playlist."""
        await self._playlist(ctx, name, owner_only=True)
        async with self.config.guild(ctx.guild).playlists() as pls:
            pls.pop(name.lower())
        await ctx.send(f"Deleted `{name}`.")

    @commands.group()
    @commands.admin_or_permissions(manage_guild=True)
    async def audioset(self, ctx: commands.Context):
        """Music settings."""

    @audioset.command(name="settings")
    async def as_settings(self, ctx: commands.Context):
        """Show the current settings."""
        s = await self.config.guild(ctx.guild).all()
        role = ctx.guild.get_role(s["dj_role"]) if s["dj_role"] else None
        g = await self.config.all()
        text = (
            f"DJ mode:          {s['dj_enabled']} (role: {role.name if role else 'not set'})\n"
            f"Vote skip:        {str(s['vote_percent']) + '%' if s['vote_percent'] else 'off'}\n"
            f"Empty disconnect: {str(s['empty_dc']) + 's' if s['empty_dc'] else 'off'}\n"
            f"Leave at end:     {s['dc_at_end']}\n"
            f"Max length:       {fmt_time(s['maxlength']) if s['maxlength'] else 'off'}\n"
            f"Notify:           {s['notify']}\n"
            f"Volume / max:     {s['volume']}% / {s['max_volume']}%\n"
            f"Repeat/Shuffle/Autoplay: {s['repeat']}/{s['shuffle']}/{s['autoplay']}\n"
            f"Playlists:        {len(s['playlists'])}\n\n"
            f"{self.deps.status()}\n"
            f"Auto-update:      {g['auto_update']}"
        )
        await ctx.send(box(text, lang="yaml"))

    @audioset.command(name="dj")
    async def as_dj(self, ctx: commands.Context):
        """Toggle DJ mode (only the DJ role, mods and admins can control music)."""
        on = not await self.config.guild(ctx.guild).dj_enabled()
        if on and not await self.config.guild(ctx.guild).dj_role():
            raise Feedback("Set a DJ role first with `audioset role`.")
        await self.config.guild(ctx.guild).dj_enabled.set(on)
        await ctx.send(f"DJ mode {'enabled' if on else 'disabled'}.")

    @audioset.command(name="role")
    async def as_role(self, ctx: commands.Context, *, role: discord.Role):
        """Set the DJ role."""
        await self.config.guild(ctx.guild).dj_role.set(role.id)
        await ctx.send(f"DJ role set to {esc(role.name)}.")

    @audioset.command(name="vote")
    async def as_vote(self, ctx: commands.Context, percent: int):
        """Let non-DJs vote-skip; percentage of listeners needed (0 disables)."""
        await self.config.guild(ctx.guild).vote_percent.set(max(0, min(percent, 100)))
        await ctx.send(f"Vote skip {'set to ' + str(percent) + '%' if percent > 0 else 'disabled'}.")

    @audioset.command(name="emptydisconnect")
    async def as_emptydc(self, ctx: commands.Context, seconds: int):
        """Leave after this many seconds alone in the channel (0 disables)."""
        await self.config.guild(ctx.guild).empty_dc.set(max(0, seconds))
        await ctx.send(f"Empty disconnect {'set to ' + str(seconds) + 's' if seconds > 0 else 'disabled'}.")

    @audioset.command(name="dc")
    async def as_dc(self, ctx: commands.Context):
        """Toggle leaving voice when the queue ends."""
        on = not await self.config.guild(ctx.guild).dc_at_end()
        await self.config.guild(ctx.guild).dc_at_end.set(on)
        await ctx.send(f"Leave at queue end {'enabled' if on else 'disabled'}.")

    @audioset.command(name="maxlength")
    async def as_maxlength(self, ctx: commands.Context, seconds: int):
        """Maximum track length in seconds (0 disables)."""
        await self.config.guild(ctx.guild).maxlength.set(max(0, seconds))
        await ctx.send(f"Max length {'set to ' + fmt_time(seconds) if seconds > 0 else 'disabled'}.")

    @audioset.command(name="maxvolume")
    async def as_maxvolume(self, ctx: commands.Context, percent: int):
        """Maximum volume users can set (up to 200)."""
        await self.config.guild(ctx.guild).max_volume.set(max(1, min(percent, 200)))
        await ctx.send(f"Max volume set to {max(1, min(percent, 200))}%.")

    @audioset.command(name="notify")
    async def as_notify(self, ctx: commands.Context):
        """Toggle "Now Playing" messages."""
        on = not await self.config.guild(ctx.guild).notify()
        await self.config.guild(ctx.guild).notify.set(on)
        await ctx.send(f"Now Playing messages {'enabled' if on else 'disabled'}.")

    @audioset.group(name="ytdlp")
    @commands.is_owner()
    async def as_ytdlp(self, ctx: commands.Context):
        """yt-dlp settings (bot owner)."""

    @as_ytdlp.command(name="update")
    async def ytdlp_update(self, ctx: commands.Context):
        """Update yt-dlp and check all dependencies now."""
        async with ctx.typing():
            await self.deps.ensure(update=True)
        await ctx.send(box(self.deps.status()))

    @as_ytdlp.command(name="autoupdate")
    async def ytdlp_autoupdate(self, ctx: commands.Context):
        """Toggle the daily yt-dlp update."""
        on = not await self.config.auto_update()
        await self.config.auto_update.set(on)
        await ctx.send(f"Daily yt-dlp update {'enabled' if on else 'disabled'}.")

    @as_ytdlp.command(name="path")
    async def ytdlp_path(self, ctx: commands.Context, path: Optional[str] = None):
        """Use another yt-dlp executable (no argument goes back to the built-in one)."""
        await self.config.ytdlp_path.set(path)
        async with ctx.typing():
            await self.deps.ensure()
        await ctx.send(box(self.deps.status()))

    @as_ytdlp.command(name="jsruntime")
    async def ytdlp_js(self, ctx: commands.Context, runtime: Optional[str] = None):
        """Set yt-dlp's JS runtime, e.g. `deno:/usr/bin/deno`; `none` disables, no argument picks one automatically."""
        if runtime is None:
            await self.config.js_runtime.clear()
        else:
            await self.config.js_runtime.set("" if runtime.lower() == "none" else runtime)
        async with ctx.typing():
            await self.deps.ensure()
        await ctx.send(box(self.deps.status()))
