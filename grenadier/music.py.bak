"""grenadier/music.py — YouTube music for Grenadier Bot.

/play <song or link>   queue and play a YouTube song (search by name or paste a link)
/pause, /resume        pause / resume the current song
/skip                  skip to the next song
/stop                  stop, clear the queue and leave the voice channel
/queue                 show what's coming up
/nowplaying            show the current song

Notes:
  * Audio is streamed through yt-dlp + ffmpeg; nothing is saved to disk.
  * Single videos only. A playlist link plays just the video it points at.
  * Only YouTube links are accepted (other sites are rejected on purpose).
  * Anyone in the bot's voice channel can control playback.
  * Optional env var YTDLP_COOKIES: contents of a Netscape cookies.txt, used when
    YouTube blocks the server's IP with a "confirm you're not a bot" check.
"""

import asyncio
import os
import shutil
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urlparse

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands

EMBED_COLOR = 0xC0392B
IDLE_DISCONNECT_SECONDS = 300          # leave after 5 min with nothing to play
EMPTY_CHANNEL_DISCONNECT_SECONDS = 60  # leave after 1 min alone in the channel
MAX_QUEUE = 100
MAX_DURATION_SECONDS = 3 * 60 * 60     # refuse songs longer than 3 hours
STREAM_URL_MAX_AGE = 30 * 60           # re-resolve a queued song's stream link after 30 min
EXTRACT_TIMEOUT = 45

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"

YOUTUBE_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtu.be", "www.youtu.be",
}


def _write_cookie_file():
    raw = os.getenv("YTDLP_COOKIES")
    if not raw:
        return None
    try:
        fd, path = tempfile.mkstemp(prefix="yt_cookies_", suffix=".txt")
        with os.fdopen(fd, "w") as f:
            f.write(raw)
        return path
    except Exception as e:
        print(f"[GRENADIER DEBUG] couldn't write cookie file: {e}")
        return None


COOKIE_FILE = _write_cookie_file()


def _ffmpeg_exe():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _is_youtube_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in YOUTUBE_HOSTS


SOUNDCLOUD_HOSTS = {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com", "on.soundcloud.com"}


def _is_allowed_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in YOUTUBE_HOSTS or host in SOUNDCLOUD_HOSTS


def _fmt_duration(seconds) -> str:
    if not seconds:
        return "?:??"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class TrackError(Exception):
    """An error whose message is safe to show to users."""


class SourceBlocked(TrackError):
    """YouTube (or SoundCloud) refused the request - worth trying another source."""


class NoResults(Exception):
    pass


@dataclass
class Track:
    title: str
    page_url: str
    stream_url: str
    duration: int
    thumbnail: str
    requested_by: str
    resolved_at: float = field(default_factory=time.time)


class GuildPlayer:
    def __init__(self):
        self.queue: deque = deque()
        self.current = None
        self.text_channel = None
        self.lock = asyncio.Lock()
        self.idle_task = None


def _ydl_opts() -> dict:
    opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "skip_download": True,
        "socket_timeout": 15,
        "cachedir": False,
    }
    if COOKIE_FILE:
        opts["cookiefile"] = COOKIE_FILE
    return opts


def _extract(target: str) -> dict:
    """Blocking — always run in an executor."""
    with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
        info = ydl.extract_info(target, download=False)
    if info and "entries" in info:
        entries = [e for e in (info.get("entries") or []) if e]
        info = entries[0] if entries else None
    if not info:
        raise NoResults("No results found.")
    return info


def _to_track(info: dict, requested_by: str) -> Track:
    if info.get("is_live"):
        raise TrackError("Live streams aren't supported.")
    duration = int(info.get("duration") or 0)
    if duration > MAX_DURATION_SECONDS:
        raise TrackError("That's too long (the limit is 3 hours).")
    stream_url = info.get("url")
    if not stream_url:
        raise TrackError("I couldn't get an audio stream for that video.")
    return Track(
        title=info.get("title") or "Unknown title",
        page_url=info.get("webpage_url") or "",
        stream_url=stream_url,
        duration=duration,
        thumbnail=info.get("thumbnail") or "",
        requested_by=requested_by,
    )


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players = {}  # guild_id -> GuildPlayer
        self.ffmpeg = _ffmpeg_exe()
        if not self.ffmpeg:
            print("[GRENADIER DEBUG] ffmpeg was not found — /play will not work until it's installed.")
        if COOKIE_FILE:
            print("[GRENADIER DEBUG] YouTube cookies loaded from YTDLP_COOKIES.")

    async def cog_unload(self):
        for vc in list(self.bot.voice_clients):
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _player(self, guild_id: int) -> GuildPlayer:
        return self.players.setdefault(guild_id, GuildPlayer())

    @staticmethod
    def _embed(title: str, description: str = "") -> discord.Embed:
        return discord.Embed(title=title, description=description, color=EMBED_COLOR)

    def _track_embed(self, heading: str, track: Track, extra: str = "") -> discord.Embed:
        embed = self._embed(heading)
        embed.description = f"**[{discord.utils.escape_markdown(_clip(track.title, 200))}]({track.page_url})**"
        if extra:
            embed.description += f"\n{extra}"
        embed.add_field(name="Length", value=_fmt_duration(track.duration))
        embed.add_field(name="Requested by", value=discord.utils.escape_markdown(track.requested_by))
        if track.thumbnail.startswith("http"):
            embed.set_thumbnail(url=track.thumbnail)
        return embed

    async def _reply(self, interaction: discord.Interaction, embed: discord.Embed, ephemeral: bool = False):
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=ephemeral)

    async def _say(self, player: GuildPlayer, embed: discord.Embed):
        if player.text_channel is None:
            return
        try:
            await player.text_channel.send(embed=embed)
        except discord.HTTPException:
            pass

    async def _control_vc(self, interaction: discord.Interaction):
        """For pause/resume/skip/stop: the bot must be connected and the user in the same channel."""
        vc = interaction.guild.voice_client
        if vc is None or not vc.is_connected():
            await self._reply(interaction, self._embed("Not Playing", "I'm not in a voice channel."), True)
            return None
        user_channel = getattr(interaction.user.voice, "channel", None)
        if user_channel is None or user_channel.id != vc.channel.id:
            await self._reply(interaction, self._embed("Join My Channel", f"Join {vc.channel.mention} to control the music."), True)
            return None
        return vc

    async def _resolve(self, query: str, requested_by: str) -> Track:
        query = query.strip()
        if query.lower().startswith(("http://", "https://")):
            if not _is_allowed_url(query):
                raise TrackError("Only YouTube and SoundCloud links are supported. You can also just type a song name.")
            return await self._resolve_target(query, requested_by)

        try:
            return await self._resolve_target(f"ytsearch1:{query}", requested_by)
        except SourceBlocked as yt_error:
            print(f"[GRENADIER DEBUG] YouTube failed ({yt_error}); trying SoundCloud for: {query!r}")
            try:
                return await self._resolve_target(f"scsearch1:{query}", requested_by)
            except TrackError:
                raise yt_error  # SoundCloud had nothing either: report the original problem

    async def _resolve_target(self, target: str, requested_by: str) -> Track:
        loop = asyncio.get_running_loop()
        try:
            info = await asyncio.wait_for(loop.run_in_executor(None, _extract, target), timeout=EXTRACT_TIMEOUT)
        except asyncio.TimeoutError:
            raise TrackError("YouTube took too long to respond. Try again in a moment.")
        except NoResults:
            raise TrackError("I couldn't find anything for that.")
        except yt_dlp.utils.DownloadError as e:
            print(f"[GRENADIER DEBUG] yt-dlp error: {e}")
            text = str(e)
            if "Sign in to confirm" in text or "not a bot" in text:
                raise SourceBlocked("YouTube is blocking this server right now (bot check). The bot owner needs to add YouTube cookies.")
            reason = text.replace("ERROR: ", "").strip()[:200]
            raise SourceBlocked(f"I couldn't load that video. Reason: {reason}")
        except Exception as e:
            print(f"[GRENADIER DEBUG] extract failed: {type(e).__name__}: {e}")
            raise TrackError("Something went wrong while looking that up.")

        return _to_track(info, requested_by)

    # ------------------------------------------------------------------ #
    # Playback engine
    # ------------------------------------------------------------------ #

    def _after(self, guild_id: int, error):
        # Runs on the audio thread — hand control back to the event loop.
        if error:
            print(f"[GRENADIER DEBUG] player error: {error}")
        asyncio.run_coroutine_threadsafe(self._on_track_end(guild_id), self.bot.loop)

    async def _on_track_end(self, guild_id: int):
        player = self.players.get(guild_id)
        if player is not None:
            player.current = None
        await self._advance(guild_id)

    async def _start(self, guild: discord.Guild, vc: discord.VoiceClient, track: Track):
        if time.time() - track.resolved_at > STREAM_URL_MAX_AGE:
            fresh = await self._resolve(track.page_url, track.requested_by)
            track.stream_url = fresh.stream_url
            track.resolved_at = fresh.resolved_at
        source = discord.FFmpegOpusAudio(
            track.stream_url,
            executable=self.ffmpeg,
            before_options=FFMPEG_BEFORE,
            options="-vn",
        )
        vc.play(source, after=lambda err, gid=guild.id: self._after(gid, err))

    async def _advance(self, guild_id: int, announce: bool = True):
        player = self.players.get(guild_id)
        guild = self.bot.get_guild(guild_id)
        if player is None or guild is None:
            return

        async with player.lock:
            vc = guild.voice_client
            if vc is None or not vc.is_connected():
                player.current = None
                player.queue.clear()
                return
            if vc.is_playing() or vc.is_paused():
                return

            while player.queue:
                track = player.queue.popleft()
                try:
                    await self._start(guild, vc, track)
                except TrackError as e:
                    await self._say(player, self._embed("Couldn't Play", f"Skipping **{discord.utils.escape_markdown(_clip(track.title, 100))}**: {e}"))
                    continue
                except Exception as e:
                    print(f"[GRENADIER DEBUG] start failed: {type(e).__name__}: {e}")
                    await self._say(player, self._embed("Couldn't Play", f"Skipping **{discord.utils.escape_markdown(_clip(track.title, 100))}** (playback error)."))
                    continue
                player.current = track
                if announce:
                    await self._say(player, self._track_embed("Now Playing", track))
                return

            player.current = None

        self._schedule_idle(guild_id, IDLE_DISCONNECT_SECONDS)

    # ------------------------------------------------------------------ #
    # Idle / cleanup
    # ------------------------------------------------------------------ #

    def _cancel_idle(self, player: GuildPlayer):
        if player.idle_task is not None and not player.idle_task.done():
            player.idle_task.cancel()
        player.idle_task = None

    def _schedule_idle(self, guild_id: int, delay: int):
        player = self.players.get(guild_id)
        if player is None:
            return
        self._cancel_idle(player)
        player.idle_task = asyncio.create_task(self._idle_disconnect(guild_id, delay))

    async def _idle_disconnect(self, guild_id: int, delay: int):
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        guild = self.bot.get_guild(guild_id)
        player = self.players.get(guild_id)
        if guild is None or player is None:
            return
        vc = guild.voice_client
        if vc is None or not vc.is_connected():
            return
        alone = not [m for m in vc.channel.members if not m.bot]
        idle = not vc.is_playing() and not vc.is_paused() and not player.queue
        if alone or idle:
            player.idle_task = None  # don't let teardown cancel this very task
            await self._teardown(guild)

    async def _teardown(self, guild: discord.Guild):
        player = self.players.pop(guild.id, None)
        if player is not None:
            self._cancel_idle(player)
            player.queue.clear()
            player.current = None
        vc = guild.voice_client
        if vc is not None:
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        guild = member.guild
        if member.id == self.bot.user.id:
            # The bot itself was disconnected/kicked — drop our state.
            if before.channel is not None and after.channel is None:
                player = self.players.pop(guild.id, None)
                if player is not None:
                    self._cancel_idle(player)
                    player.queue.clear()
                    player.current = None
            return

        vc = guild.voice_client
        player = self.players.get(guild.id)
        if vc is None or not vc.is_connected() or player is None:
            return
        humans = [m for m in vc.channel.members if not m.bot]
        if not humans:
            self._schedule_idle(guild.id, EMPTY_CHANNEL_DISCONNECT_SECONDS)
        elif vc.is_playing() or vc.is_paused() or player.queue:
            self._cancel_idle(player)

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #

    @app_commands.command(name="play", description="Play a YouTube song (type a name or paste a link).")
    @app_commands.describe(query="Song name or YouTube link")
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, query: str):
        user_channel = getattr(interaction.user.voice, "channel", None)
        if user_channel is None:
            return await self._reply(interaction, self._embed("Join a Voice Channel", "Join a voice channel first, then run `/play`."), True)
        if not self.ffmpeg:
            return await self._reply(interaction, self._embed("Not Available", "ffmpeg isn't installed on the host, so I can't play audio yet."), True)

        guild = interaction.guild
        perms = user_channel.permissions_for(guild.me)
        if not (perms.connect and perms.speak):
            return await self._reply(interaction, self._embed("Missing Permissions", f"I need **Connect** and **Speak** in {user_channel.mention}."), True)

        vc = guild.voice_client
        if vc is not None and vc.is_connected() and vc.channel.id != user_channel.id and (vc.is_playing() or vc.is_paused()):
            return await self._reply(interaction, self._embed("Busy", f"I'm already playing in {vc.channel.mention}."), True)

        player = self._player(guild.id)
        if len(player.queue) >= MAX_QUEUE:
            return await self._reply(interaction, self._embed("Queue Full", f"The queue is full ({MAX_QUEUE} songs)."), True)

        await interaction.response.defer()

        try:
            track = await self._resolve(query, interaction.user.display_name)
        except TrackError as e:
            return await self._reply(interaction, self._embed("Couldn't Play That", str(e)), True)

        try:
            if vc is None or not vc.is_connected():
                if vc is not None:
                    await vc.disconnect(force=True)
                vc = await user_channel.connect(self_deaf=True)
            elif vc.channel.id != user_channel.id:
                await vc.move_to(user_channel)
        except (discord.ClientException, discord.HTTPException, asyncio.TimeoutError) as e:
            print(f"[GRENADIER DEBUG] voice connect failed: {type(e).__name__}: {e}")
            return await self._reply(interaction, self._embed("Couldn't Join", "I couldn't connect to your voice channel. Try again in a moment."), True)

        player.text_channel = interaction.channel
        self._cancel_idle(player)
        was_idle = player.current is None and not vc.is_playing() and not vc.is_paused()
        player.queue.append(track)

        if was_idle:
            embed = self._track_embed("Now Playing", track)
        else:
            embed = self._track_embed("Added to Queue", track, f"Position **#{len(player.queue)}**")
        await interaction.followup.send(embed=embed)

        if was_idle:
            await self._advance(guild.id, announce=False)

    @app_commands.command(name="pause", description="Pause the current song.")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction):
        vc = await self._control_vc(interaction)
        if vc is None:
            return
        if not vc.is_playing():
            return await self._reply(interaction, self._embed("Nothing Playing", "There's nothing playing right now."), True)
        vc.pause()
        await self._reply(interaction, self._embed("Paused", "Use `/resume` to continue."))

    @app_commands.command(name="resume", description="Resume the paused song.")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction):
        vc = await self._control_vc(interaction)
        if vc is None:
            return
        if not vc.is_paused():
            return await self._reply(interaction, self._embed("Not Paused", "Nothing is paused right now."), True)
        vc.resume()
        await self._reply(interaction, self._embed("Resumed"))

    @app_commands.command(name="skip", description="Skip the current song.")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction):
        vc = await self._control_vc(interaction)
        if vc is None:
            return
        player = self.players.get(interaction.guild.id)
        if not (vc.is_playing() or vc.is_paused()) or player is None or player.current is None:
            return await self._reply(interaction, self._embed("Nothing Playing", "There's nothing to skip."), True)
        title = discord.utils.escape_markdown(_clip(player.current.title, 100))
        vc.stop()  # triggers the after-callback, which starts the next song
        await self._reply(interaction, self._embed("Skipped", f"**{title}**"))

    @app_commands.command(name="stop", description="Stop the music, clear the queue and leave the voice channel.")
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction):
        vc = await self._control_vc(interaction)
        if vc is None:
            return
        await self._teardown(interaction.guild)
        await self._reply(interaction, self._embed("Stopped", "Cleared the queue and left the voice channel."))

    @app_commands.command(name="queue", description="Show the song queue.")
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction):
        player = self.players.get(interaction.guild.id)
        if player is None or (player.current is None and not player.queue):
            return await self._reply(interaction, self._embed("Queue", "The queue is empty. Use `/play` to add a song."), True)

        lines = []
        if player.current is not None:
            lines.append(f"**Now playing:** {discord.utils.escape_markdown(_clip(player.current.title, 80))} `{_fmt_duration(player.current.duration)}`")
        upcoming = list(player.queue)
        if upcoming:
            lines.append("")
            for i, t in enumerate(upcoming[:10], start=1):
                lines.append(f"`{i}.` {discord.utils.escape_markdown(_clip(t.title, 80))} `{_fmt_duration(t.duration)}`")
            if len(upcoming) > 10:
                lines.append(f"…and {len(upcoming) - 10} more")
        await self._reply(interaction, self._embed(f"Queue ({len(upcoming)} up next)", "\n".join(lines)))

    @app_commands.command(name="nowplaying", description="Show the song that's playing.")
    @app_commands.guild_only()
    async def nowplaying(self, interaction: discord.Interaction):
        player = self.players.get(interaction.guild.id)
        if player is None or player.current is None:
            return await self._reply(interaction, self._embed("Nothing Playing", "There's nothing playing right now."), True)
        await self._reply(interaction, self._track_embed("Now Playing", player.current))


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
