"""
bot.py – Entry point for the SufferingFM bot.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import re
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from broadcast import DAYS as DJ_DAYS, DJEventScheduler
from config import (
    ADS_DIR,
    ALLOWED_EXTENSIONS,
    CONTROLLER_CHANNEL_ID,
    DJ_EVENTS_DIR,
    HEAVEN_AVATAR_PATH,
    HEAVEN_BANNER_PATH,
    HEAVEN_BOT_NAME,
    RIGGED_SONG_IDS,
    SONGS_DIR,
    SUFFERING_AVATAR_PATH,
    SUFFERING_BANNER_PATH,
    SUFFERING_BOT_NAME,
    TOKEN,
)
from database import (
    activate_song,
    add_song,
    apply_song_feedback,
    deactivate_song,
    get_all_songs,
    get_all_songs_admin,
    get_song,
    get_songs_by_name,
    hard_delete_song,
    init_db,
    search_songs,
    sync_ads_from_disk,
)
from media_utils import download_youtube_audio, extract_urls, is_youtube_url
from player import MusicPlayer
from views import (
    CONTROLLER_SEARCH_LIMIT,
    REACT_DEACTIVATE,
    REACT_HARD_DELETE,
    MusicControlView,
    ShadyControlView,
    _build_delete_confirm_message,
    _resolve_username,
    _song_table_embed,
    is_music_manager,
)

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

SONGS_DIR.mkdir(parents=True, exist_ok=True)
ADS_DIR.mkdir(parents=True, exist_ok=True)

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

def _controller_embed(
    song: dict | None = None,
    label: str | None = None,
    added_by: str | None = None,
    is_paused: bool = False,
) -> discord.Embed:
    """Return the controller embed, optionally showing the current track."""
    embed = discord.Embed(title="🎵 SufferingFM", colour=discord.Colour.purple())
    status = "⏸ Paused" if is_paused else "▶ Playing"
    if song:
        embed.add_field(
            name=status,
            value=f"**{song['name']}** — **{song['artist']}**",
            inline=False,
        )
        embed.add_field(name="Added by", value=added_by or "Unknown", inline=False)
    elif label:
        label_map = {
            "ad": "📢 Advertisement",
            "dj intro": "🎤 DJ Intro",
            "dj outro": "🎤 DJ Outro",
            "dj event": "🎤 DJ Event",
            "intermission": "📢 Intermission",
        }
        display = label_map.get(label.lower(), f"🎵 {label.title()}")
        embed.add_field(name=status, value=display, inline=False)
    else:
        embed.description = "*Not currently playing. Use ▶ Play to start.*"
    return embed


def _shady_controller_embed() -> discord.Embed:
    return discord.Embed(
        title="🕶️ The Collector",
        description=(
            "Drop songs here and keep the stash growing.\n"
            "Use **➕ Add Song** to feed the vault, **📋 Playlist** to inspect the haul, "
            "and **⛔ Disable Song** to bury tracks."
        ),
        colour=discord.Colour.dark_purple(),
    )


def _help_tutorial_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🎧 SufferingFM Quick Start",
        description="Mini guide for the controller buttons and slash commands.",
        colour=discord.Colour.blurple(),
    )
    embed.add_field(
        name="1) Start playback",
        value=(
            "Join a voice channel, then press **▶ Play** on the controller.\n"
            "Use **⏸ Pause**, **⏭ Skip**, and **📞 Leave** as needed."
        ),
        inline=False,
    )
    embed.add_field(
        name="2) Browse songs",
        value=(
            "Use **📋 Playlist** on the controller for the active song list.\n"
            "Slash commands: **`/songs`**, **`/songs_all`** (Manager), **`/search`**."
        ),
        inline=False,
    )
    embed.add_field(
        name="3) Check the current track / send feedback",
        value=(
            "Use **`/now_playing`** to see the current song.\n"
            "Send feedback with **👍 / 👎** buttons or **`/like`** / **`/dislike`**."
        ),
        inline=False,
    )
    embed.add_field(
        name="4) Add music",
        value=(
            "Music Managers can use **`/upload_song`** for attachments or YouTube links.\n"
            "The **➕ Add Song** button can add a song directly from a YouTube link."
        ),
        inline=False,
    )
    embed.add_field(
        name="5) Manager tools",
        value=(
            "**`/toggle_song`**, **`/toggle_song_id`**, **`/delete_song_id`**,\n"
            "**`/set_rigged_pool`**, **`/play_dj_event`**."
        ),
        inline=False,
    )
    embed.set_footer(text="Some commands/buttons require the Music Manager role.")
    return embed


def _read_optional_bytes(path: str) -> bytes | None:
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    return p.read_bytes()


def _unique_path(directory: Path, filename: str) -> Path:
    safe = Path(filename).name
    base = Path(safe).stem or "audio"
    ext = Path(safe).suffix.lower()
    candidate = directory / f"{base}{ext}"
    n = 1
    while candidate.exists():
        candidate = directory / f"{base}_{n}{ext}"
        n += 1
    return candidate


def _infer_target(source: str | None, selected: str | None) -> str:
    if selected in {"song", "ad"}:
        return selected
    if source:
        m = re.search(r"\b(?:target|type|kind)\s*:\s*(song|ad)\b", source, flags=re.IGNORECASE)
        if m:
            return m.group(1).lower()
    return "song"


class MusicBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=intents)
        self.player = MusicPlayer(self)
        self.pending_deletes: dict[int, dict] = {}
        self._controller_mode: str = "shady"
        self._branding_override: str | None = None
        self._branding_mode: str | None = None
        self._branding_task: asyncio.Task | None = None
        self.controller_message: discord.Message | None = None
        self._controller_song: dict | None = None
        self._controller_label: str | None = None

    async def setup_hook(self) -> None:
        self.add_view(MusicControlView())
        self.add_view(ShadyControlView())
        await self.tree.sync()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)  # type: ignore[union-attr]
        init_db()
        sync_ads_from_disk()
        self.player.set_rigged_songs(list(RIGGED_SONG_IDS))

        DJ_EVENTS_DIR.mkdir(parents=True, exist_ok=True)
        for day in DJ_DAYS:
            (DJ_EVENTS_DIR / day).mkdir(parents=True, exist_ok=True)
        self.player.set_dj_events(DJEventScheduler(DJ_EVENTS_DIR))

        await self._apply_branding_for_day()
        if self._branding_task is None or self._branding_task.done():
            self._branding_task = asyncio.create_task(self._branding_loop())

        self.player.on_track_start = self.update_controller_now_playing
        await self._ensure_controller()

    async def _ensure_controller(self) -> None:
        if not CONTROLLER_CHANNEL_ID:
            return
        channel = self.get_channel(CONTROLLER_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            return
        async for msg in channel.history(limit=CONTROLLER_SEARCH_LIMIT):
            if msg.author == self.user and msg.components:
                self.controller_message = msg
                await self.refresh_controller_status()
                return
        msg = await channel.send(
            embed=self._active_controller_embed(),
            view=self._active_controller_view(),
        )
        self.controller_message = msg
        log.info("Controller posted in #%s", channel.name)

    async def _branding_loop(self) -> None:
        while True:
            try:
                await self._apply_branding_for_day()
            except Exception as exc:
                log.warning("Branding check failed: %s", exc)
            await asyncio.sleep(300)

    async def _apply_branding_for_day(self) -> None:
        if self.user is None:
            return
        if self._branding_override == "heaven":
            target = "heaven"
        else:
            # Monday=0, Sunday=6; Sunday triggers heaven mode.
            weekday = datetime.datetime.now(datetime.UTC).weekday()
            target = "heaven" if weekday == 6 else "suffering"
        if target == self._branding_mode:
            return

        if target == "heaven":
            username = HEAVEN_BOT_NAME
            avatar = _read_optional_bytes(HEAVEN_AVATAR_PATH)
            banner = _read_optional_bytes(HEAVEN_BANNER_PATH)
        else:
            username = SUFFERING_BOT_NAME
            avatar = _read_optional_bytes(SUFFERING_AVATAR_PATH)
            banner = _read_optional_bytes(SUFFERING_BANNER_PATH)

        kwargs: dict = {"username": username}
        if avatar is not None:
            kwargs["avatar"] = avatar
        if banner is not None:
            kwargs["banner"] = banner

        try:
            await self.user.edit(**kwargs)
            self._branding_mode = target
            log.info("Branding switched to %s mode.", target)
        except Exception as exc:
            log.warning("Could not apply %s branding: %s", target, exc)

    def _active_controller_view(self) -> discord.ui.View:
        if self._controller_mode == "shady":
            return ShadyControlView()
        return MusicControlView()

    def _active_controller_embed(self) -> discord.Embed:
        if self._controller_mode == "shady":
            return _shady_controller_embed()
        guild = self.controller_message.guild if self.controller_message else None
        if self._controller_song:
            return _controller_embed(
                song=self._controller_song,
                added_by=None,
                is_paused=self.player.is_paused(),
            )
        if self._controller_label:
            return _controller_embed(
                label=self._controller_label,
                is_paused=self.player.is_paused(),
            )
        return _controller_embed()

    async def submit_current_song_feedback(
        self, interaction: discord.Interaction, is_like: bool
    ) -> tuple[bool, str]:
        song = self.player.current_song
        if not song:
            return False, "Nothing is currently playing."
        ok, msg = apply_song_feedback(song["id"], interaction.user.id, is_like)
        if not ok:
            return False, msg
        fresh = get_song(song["id"])
        if fresh:
            self.player.current_song = fresh
        return True, msg

    async def refresh_controller_status(self) -> None:
        """Refresh the tracked controller message using cached current-track state."""
        if self.controller_message is None:
            return
        if self._controller_mode == "shady":
            embed = _shady_controller_embed()
        else:
            guild = self.controller_message.guild
            if self._controller_song:
                added_by = await _resolve_username(
                    self, self._controller_song.get("added_by", ""), guild
                )
                embed = _controller_embed(
                    song=self._controller_song,
                    added_by=added_by,
                    is_paused=self.player.is_paused(),
                )
            elif self._controller_label:
                embed = _controller_embed(
                    label=self._controller_label,
                    is_paused=self.player.is_paused(),
                )
            else:
                embed = _controller_embed()
        try:
            await self.controller_message.edit(
                embed=embed,
                view=self._active_controller_view(),
            )
        except Exception as exc:
            log.warning("Failed to update controller embed: %s", exc)

    async def clear_controller_now_playing(self) -> None:
        self._controller_song = None
        self._controller_label = None
        await self.refresh_controller_status()

    async def update_controller_now_playing(
        self, song: dict | None, label: str
    ) -> None:
        """Update cached current-track state and refresh controller embed."""
        if song:
            self._controller_song = song
            self._controller_label = None
        else:
            self._controller_song = None
            self._controller_label = label
        await self.refresh_controller_status()

    async def on_raw_reaction_add(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        if self.user and payload.user_id == self.user.id:
            return

        pending = self.pending_deletes.get(payload.message_id)
        if not pending:
            return
        if payload.user_id != pending["user_id"]:
            return

        emoji = str(payload.emoji)
        song = pending["song"]
        channel = self.get_channel(payload.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        msg = await channel.fetch_message(payload.message_id)

        if emoji == REACT_DEACTIVATE:
            deactivate_song(song["id"])
            await msg.edit(
                content=(
                    f"⛔ **{song['name']}** by **{song['artist']}** has been deactivated.\n"
                    "Use `/toggle_song` or `/toggle_song_id` to re-enable it."
                )
            )
            await msg.clear_reactions()
            del self.pending_deletes[payload.message_id]
        elif emoji == REACT_HARD_DELETE:
            hard_delete_song(song["id"])
            song_path = SONGS_DIR / song["filename"]
            if song_path.exists():
                song_path.unlink()
            await msg.edit(
                content=(
                    f"🗑️ **{song['name']}** by **{song['artist']}** has been permanently deleted."
                )
            )
            await msg.clear_reactions()
            del self.pending_deletes[payload.message_id]


bot = MusicBot()


@bot.tree.command(name="controller", description="Post the controller panel.")
async def cmd_controller(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        embed=bot._active_controller_embed(),
        view=bot._active_controller_view(),
    )


@bot.tree.command(name="help", description="DM a quick tutorial and command list.")
async def cmd_help(interaction: discord.Interaction) -> None:
    try:
        await interaction.user.send(embed=_help_tutorial_embed())
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ I couldn't DM you. Please enable direct messages from server members and try again.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        "📬 I sent you a quick tutorial and command list in DMs.",
        ephemeral=True,
    )


@bot.tree.command(
    name="upload_song",
    description="Add media from attachment, YouTube link(s), or both.",
)
@app_commands.describe(
    source="Optional text with YouTube link(s). You can include kind:song or kind:ad.",
    file="Optional audio file attachment",
    target="Destination type (Song or Ad). Defaults to Song.",
    name="Optional display name override for attached file song",
    artist="Optional artist override for attached file song",
)
@app_commands.choices(
    target=[
        app_commands.Choice(name="Song", value="song"),
        app_commands.Choice(name="Ad", value="ad"),
    ]
)
async def cmd_upload_song(
    interaction: discord.Interaction,
    source: str | None = None,
    file: discord.Attachment | None = None,
    target: app_commands.Choice[str] | None = None,
    name: str | None = None,
    artist: str | None = None,
) -> None:
    if not is_music_manager(interaction):
        await interaction.response.send_message(
            "❌ You need the **Music Manager** role to upload songs.", ephemeral=True
        )
        return

    raw_target = _infer_target(source, target.value if target else None)
    target_dir = SONGS_DIR if raw_target == "song" else ADS_DIR
    urls = extract_urls(source or "")
    youtube_urls = [u for u in urls if is_youtube_url(u)]

    if file is None and not youtube_urls:
        await interaction.response.send_message(
            "❌ Provide an attachment, a YouTube link, or both.",
            ephemeral=True,
        )
        return

    if file is not None:
        ext = "." + file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
        if ext not in ALLOWED_EXTENSIONS:
            await interaction.response.send_message(
                f"❌ Unsupported file type `{ext}`.\nAllowed: {', '.join(ALLOWED_EXTENSIONS)}",
                ephemeral=True,
            )
            return

    await interaction.response.defer(ephemeral=True)

    added_song_ids: list[int] = []
    added_song_names: list[str] = []
    added_ad_files: list[str] = []
    ignored_urls = [u for u in urls if u not in youtube_urls]
    failed_urls: list[str] = []

    for url in youtube_urls:
        try:
            downloaded = await asyncio.to_thread(download_youtube_audio, url, target_dir, False)
        except Exception as exc:
            log.warning("yt-dlp download failed for %s: %s", url, exc)
            failed_urls.append(url)
            continue
        for p in downloaded:
            if raw_target == "song":
                sid = add_song(p.stem, "YouTube", p.name, str(interaction.user.id))
                added_song_ids.append(sid)
                added_song_names.append(p.stem)
            else:
                added_ad_files.append(p.name)

    if file is not None:
        dest = _unique_path(target_dir, file.filename)
        await file.save(dest)
        if raw_target == "song":
            display_name = (name or dest.stem).strip() or dest.stem
            display_artist = (artist or "Unknown").strip() or "Unknown"
            sid = add_song(display_name, display_artist, dest.name, str(interaction.user.id))
            added_song_ids.append(sid)
            added_song_names.append(display_name)
        else:
            added_ad_files.append(dest.name)

    if raw_target == "ad" and (added_ad_files or youtube_urls):
        sync_ads_from_disk()

    lines: list[str] = []
    if added_song_ids:
        lines.append(f"✅ Added {len(added_song_ids)} song(s). IDs: `{', '.join(map(str, added_song_ids))}`")
    if added_ad_files:
        lines.append(f"✅ Added {len(added_ad_files)} ad file(s).")
    if ignored_urls:
        lines.append(f"⚠️ Ignored {len(ignored_urls)} non-YouTube URL(s).")
    if failed_urls:
        lines.append(f"⚠️ Failed to download {len(failed_urls)} YouTube URL(s).")
    if not lines:
        lines.append("❌ No media was added.")

    await interaction.followup.send("\n".join(lines), ephemeral=True)


@bot.tree.command(name="search", description="Search songs by name, artist, uploader, or id.")
@app_commands.describe(field="Field", query="Search term")
@app_commands.choices(field=[
    app_commands.Choice(name="Name", value="name"),
    app_commands.Choice(name="Artist", value="artist"),
    app_commands.Choice(name="Added By (user ID)", value="added_by"),
    app_commands.Choice(name="ID", value="id"),
])
async def cmd_search(
    interaction: discord.Interaction,
    field: app_commands.Choice[str],
    query: str,
) -> None:
    results = search_songs(field.value, query)
    if not results:
        await interaction.response.send_message("No matching songs found.", ephemeral=True)
        return
    embed = await _song_table_embed(results, title=f'🔎 Results: {field.name} = "{query}"', client=interaction.client, guild=interaction.guild)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="toggle_song", description="Activate/deactivate a song by name.")
@app_commands.describe(name="Song name")
async def cmd_toggle_song(interaction: discord.Interaction, name: str) -> None:
    if not is_music_manager(interaction):
        await interaction.response.send_message(
            "❌ You need the **Music Manager** role to toggle songs.", ephemeral=True
        )
        return
    matches = get_songs_by_name(name)
    if not matches:
        await interaction.response.send_message(
            f"Sorry, there is no song named **{name}**.", ephemeral=True
        )
        return
    if len(matches) > 1:
        embed = await _song_table_embed(matches, title=f'🔎 Multiple songs named "{name}"', client=interaction.client, guild=interaction.guild)
        embed.add_field(
            name="What to do",
            value="Use **`/toggle_song_id <id>`** with the ID shown above.",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    song = matches[0]
    if song.get("available", 1):
        deactivate_song(song["id"])
        await interaction.response.send_message(f"⛔ Deactivated **{song['name']}**.", ephemeral=True)
    else:
        activate_song(song["id"])
        await interaction.response.send_message(f"✅ Re-activated **{song['name']}**.", ephemeral=True)


@bot.tree.command(name="toggle_song_id", description="Activate/deactivate by song id.")
@app_commands.describe(song_id="Song ID")
async def cmd_toggle_song_id(interaction: discord.Interaction, song_id: int) -> None:
    if not is_music_manager(interaction):
        await interaction.response.send_message(
            "❌ You need the **Music Manager** role to toggle songs.", ephemeral=True
        )
        return
    song = get_song(song_id)
    if not song:
        await interaction.response.send_message(
            f"Sorry, there is no song with ID **{song_id}**.", ephemeral=True
        )
        return
    if song.get("available", 1):
        deactivate_song(song_id)
        await interaction.response.send_message(f"⛔ Deactivated **{song['name']}**.", ephemeral=True)
    else:
        activate_song(song_id)
        await interaction.response.send_message(f"✅ Re-activated **{song['name']}**.", ephemeral=True)


@bot.tree.command(name="delete_song_id", description="Begin two-step delete by song id.")
@app_commands.describe(song_id="Song ID")
async def cmd_delete_song_id(interaction: discord.Interaction, song_id: int) -> None:
    if not is_music_manager(interaction):
        await interaction.response.send_message(
            "❌ You need the **Music Manager** role to delete songs.", ephemeral=True
        )
        return
    song = get_song(song_id)
    if not song:
        await interaction.response.send_message(
            f"Sorry, there is no song with ID **{song_id}**.", ephemeral=True
        )
        return
    msg_content = await _build_delete_confirm_message(
        song, interaction.user.id, interaction.client, interaction.guild
    )
    await interaction.response.send_message(msg_content)
    msg = await interaction.original_response()
    await msg.add_reaction(REACT_DEACTIVATE)
    await msg.add_reaction(REACT_HARD_DELETE)
    bot.pending_deletes[msg.id] = {"user_id": interaction.user.id, "song": song}


@bot.tree.command(name="songs", description="Show active songs.")
async def cmd_songs(interaction: discord.Interaction) -> None:
    embed = await _song_table_embed(get_all_songs(), compact=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="songs_all", description="Show all songs including deactivated (Music Manager only).")
async def cmd_songs_all(interaction: discord.Interaction) -> None:
    if not is_music_manager(interaction):
        await interaction.response.send_message(
            "❌ You need the **Music Manager** role to view the full library.", ephemeral=True
        )
        return
    embed = await _song_table_embed(get_all_songs_admin(), title="🎵 Song Library (All)", client=interaction.client, guild=interaction.guild)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="set_rigged_pool",
    description="Set rigged song IDs as comma-separated list (empty/0 to clear).",
)
@app_commands.describe(song_ids="Example: 3,7,12")
async def cmd_set_rigged_pool(interaction: discord.Interaction, song_ids: str) -> None:
    if not is_music_manager(interaction):
        await interaction.response.send_message(
            "❌ You need the **Music Manager** role.", ephemeral=True
        )
        return
    raw = [p.strip() for p in song_ids.split(",")]
    ids: list[int] = []
    for p in raw:
        if not p or p == "0":
            continue
        try:
            ids.append(int(p))
        except ValueError:
            continue
    bot.player.set_rigged_songs(ids)
    if not ids:
        await interaction.response.send_message("🎭 Rigged song pool cleared.", ephemeral=True)
    else:
        await interaction.response.send_message(
            f"🎭 Rigged song pool set to IDs: {', '.join(str(i) for i in ids)}",
            ephemeral=True,
        )


@bot.tree.command(name="now_playing", description="Show current song.")
async def cmd_now_playing(interaction: discord.Interaction) -> None:
    song = bot.player.current_song
    if not song:
        await interaction.response.send_message("❌ Nothing is playing right now.", ephemeral=True)
        return
    embed = discord.Embed(title="🎵 Now Playing", colour=discord.Colour.green())
    embed.add_field(name="Song", value=song["name"], inline=True)
    embed.add_field(name="Artist", value=song["artist"], inline=True)
    embed.add_field(name="Plays", value=str(song.get("times_played", 0)), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="play_dj_event", description="Play a random DJ event clip now.")
@app_commands.describe(day="Optional day abbreviation: MON TUE WED THU FRI SAT SUN")
async def cmd_play_dj_event(
    interaction: discord.Interaction,
    day: str | None = None,
) -> None:
    if not is_music_manager(interaction):
        await interaction.response.send_message(
            "❌ You need the **Music Manager** role to trigger DJ events.", ephemeral=True
        )
        return
    if not bot.player.is_connected():
        await interaction.response.send_message(
            "❌ Bot is not connected to voice. Press Play/Resume first.", ephemeral=True
        )
        return
    target_day = None
    if day:
        day_map = {
            "MON": "monday", "TUE": "tuesday", "WED": "wednesday",
            "THU": "thursday", "FRI": "friday", "SAT": "saturday", "SUN": "sunday",
        }
        target_day = day_map.get(day.upper())
        if target_day is None:
            await interaction.response.send_message(
                "❌ Invalid day. Use MON TUE WED THU FRI SAT SUN.", ephemeral=True
            )
            return
    started = await bot.player.play_dj_event_now(day=target_day)
    if started:
        await interaction.response.send_message("🎙️ Playing DJ event now.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ No DJ event clip available.", ephemeral=True)


@bot.tree.command(name="like", description="Send a like for the current song.")
async def cmd_like(interaction: discord.Interaction) -> None:
    ok, msg = await bot.submit_current_song_feedback(interaction, is_like=True)
    prefix = "👍" if ok else "❌"
    await interaction.response.send_message(f"{prefix} {msg}", ephemeral=True)


@bot.tree.command(name="dislike", description="Send a dislike for the current song.")
async def cmd_dislike(interaction: discord.Interaction) -> None:
    ok, msg = await bot.submit_current_song_feedback(interaction, is_like=False)
    prefix = "👎" if ok else "❌"
    await interaction.response.send_message(f"{prefix} {msg}", ephemeral=True)


@bot.tree.command(name="pardon", description="Switch to the limited shady controller mode.")
async def cmd_pardon(interaction: discord.Interaction) -> None:
    if not is_music_manager(interaction):
        await interaction.response.send_message(
            "❌ You need the **Music Manager** role.", ephemeral=True
        )
        return
    bot._controller_mode = "shady"
    await bot.refresh_controller_status()
    await interaction.response.send_message(
        "🕶️ Entered shady collection mode (Playlist/Add/Disable only).",
        ephemeral=True,
    )


@bot.tree.command(name="damn", description="Switch to full mode with default day-based persona.")
async def cmd_damn(interaction: discord.Interaction) -> None:
    if not is_music_manager(interaction):
        await interaction.response.send_message(
            "❌ You need the **Music Manager** role.", ephemeral=True
        )
        return
    bot._controller_mode = "full"
    bot._branding_override = None
    await bot._apply_branding_for_day()
    await bot.refresh_controller_status()
    await interaction.response.send_message(
        "🔥 Full mode enabled. Default day-based persona restored.",
        ephemeral=True,
    )


@bot.tree.command(name="save", description="Switch to full mode and force Heaven persona.")
async def cmd_save(interaction: discord.Interaction) -> None:
    if not is_music_manager(interaction):
        await interaction.response.send_message(
            "❌ You need the **Music Manager** role.", ephemeral=True
        )
        return
    bot._controller_mode = "full"
    bot._branding_override = "heaven"
    await bot._apply_branding_for_day()
    await bot.refresh_controller_status()
    await interaction.response.send_message(
        "✨ Full mode enabled. Heaven persona forced until `/damn` is used.",
        ephemeral=True,
    )


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    bot.run(TOKEN)
