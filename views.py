"""
views.py – Persistent Discord UI components for the music bot.

MusicControlView
    Persistent button panel (timeout=None) that survives bot restarts.
    All button custom_ids are stable strings prefixed with "music:".

    Row 0 – playback controls : Play/Resume | Pause | Skip | Leave
    Row 1 – library controls  : Song List  | Add Song | Delete Song

AddSongModal
    Modal form to download audio from a YouTube URL and register it.
    The uploader's Discord user ID is captured automatically.

DeleteSongModal
    Two-step delete by song name.
    Step 1 – modal: user enters song name.
    Step 2 – bot posts a non-ephemeral confirmation in the channel with
             ✅ (deactivate, keep file) and 🗑️ (hard-delete + remove file)
             reactions.  on_raw_reaction_add in bot.py handles the action.
"""
from __future__ import annotations

import asyncio
import logging

import discord

from config import ALLOWED_EXTENSIONS, MUSIC_MANAGER_ROLE_ID, SONGS_DIR
from database import (
    add_song,
    get_all_songs,
    get_all_songs_admin,
    get_songs_by_name,
)
from media_utils import download_youtube_audio, extract_urls, is_youtube_url

log = logging.getLogger(__name__)

# Column widths used in the song-table display.
_COL_ID = 4
_COL_NAME = 20
_COL_ARTIST = 15
_COL_ADDED_BY = 14
# Number of history messages to scan when searching for an existing controller.
CONTROLLER_SEARCH_LIMIT = 30

# Reaction emojis for the two-step delete confirmation.
REACT_DEACTIVATE = "✅"
REACT_HARD_DELETE = "🗑️"

# ---------------------------------------------------------------------------
# Permission helper
# ---------------------------------------------------------------------------

def is_music_manager(interaction: discord.Interaction) -> bool:
    """Return True if the interacting user may manage the song library.

    Access is granted when ANY of the following hold:
    * ``MUSIC_MANAGER_ROLE_ID`` is 0 (unrestricted / default).
    * The user has the Administrator permission in the guild.
    * The user holds the role whose ID matches ``MUSIC_MANAGER_ROLE_ID``.

    Always returns True in DM contexts when the role ID is 0.
    """
    if not MUSIC_MANAGER_ROLE_ID:
        return True
    member = interaction.user
    if not isinstance(member, discord.Member):
        # DM context — guild roles cannot be checked.
        return False
    if member.guild_permissions.administrator:
        return True
    return any(r.id == MUSIC_MANAGER_ROLE_ID for r in member.roles)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _resolve_username(
    client: discord.Client,
    user_id_str: str,
    guild: discord.Guild | None = None,
) -> str:
    """Return a display name (preferring server nickname) for a Discord user ID.

    Resolution order:
    1. Guild member's ``display_name`` — server nickname if set, otherwise
       the member's global display name (requires *guild* to be provided).
    2. Global ``User.display_name``.
    3. Raw user ID string (fallback when the user cannot be found at all).
    """
    if not user_id_str:
        return "Unknown"
    try:
        uid = int(user_id_str)
    except ValueError:
        return user_id_str

    # Prefer guild member so we get the server-specific nickname.
    if guild is not None:
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except Exception:
                member = None
        if member is not None:
            return member.display_name

    # Fall back to the global User object.
    user = client.get_user(uid)
    if user is None:
        try:
            user = await client.fetch_user(uid)
        except Exception:
            return user_id_str
    return user.display_name


async def _build_delete_confirm_message(
    song: dict,
    requester_id: int,
    client: discord.Client,
    guild: discord.Guild | None = None,
) -> str:
    """Build the non-ephemeral confirmation message used by both the
    name-based delete modal and the ``/delete_song_id`` command."""
    added_by_display = await _resolve_username(client, song.get("added_by", ""), guild)
    status = "✅ active" if song.get("available", 1) else "⛔ deactivated"
    return (
        f"🎵 **{song['name']}** by **{song['artist']}** "
        f"(ID: `{song['id']}`, {status})\n"
        f"Added by: **{added_by_display}**\n\n"
        f"React with {REACT_DEACTIVATE} to **deactivate** "
        f"— removes from playlist but keeps the audio file "
        f"(re-enable later with `/toggle_song` or `/toggle_song_id`).\n"
        f"React with {REACT_HARD_DELETE} to **permanently delete** "
        f"— removes from the database **and** deletes the audio file.\n\n"
        f"Only <@{requester_id}> can confirm this action."
    )

async def _song_table_embed(
    songs: list[dict],
    title: str = "🎵 Song Library",
    *,
    client: discord.Client | None = None,
    guild: discord.Guild | None = None,
    compact: bool = False,
) -> discord.Embed:
    """Build a nicely formatted embed table for the song library.

    When *compact* is True only ID, Name and Artist are shown (no "Added By"
    or "Plays"), keeping each row to a single line for normal users.

    When *compact* is False all columns are included and deactivated songs are
    prefixed with ``[inactive]`` so admins can see status at a glance.
    """
    embed = discord.Embed(title=title, colour=discord.Colour.blue())

    if not songs:
        embed.description = "*No songs in the library yet.*"
        return embed

    if compact:
        col_id = _COL_ID
        col_name = _COL_NAME
        col_artist = _COL_ARTIST

        header = (
            f"{'ID':<{col_id}} "
            f"{'Name':<{col_name}} "
            f"{'Artist':<{col_artist}}"
        )
        divider = "─" * (col_id + col_name + col_artist + 2)

        rows: list[str] = []
        for s in songs:
            rows.append(
                f"{s['id']:<{col_id}} "
                f"{s['name'][:col_name]:<{col_name}} "
                f"{s['artist'][:col_artist]:<{col_artist}}"
            )
    else:
        col_id = _COL_ID
        col_name = _COL_NAME
        col_artist = _COL_ARTIST
        col_added_by = _COL_ADDED_BY

        show_inactive_marker = any(not s.get("available", 1) for s in songs)

        header = (
            f"{'ID':<{col_id}} "
            f"{'Name':<{col_name}} "
            f"{'Artist':<{col_artist}} "
            f"{'Added By':<{col_added_by}} "
            f"Plays"
        )
        divider = "─" * (col_id + col_name + col_artist + col_added_by + 18)

        # Resolve all "added by" display names concurrently.
        added_by_ids = [str(s.get("added_by", "")) for s in songs]
        if client:
            display_names: list[str] = list(
                await asyncio.gather(
                    *[_resolve_username(client, uid, guild) for uid in added_by_ids]
                )
            )
        else:
            display_names = added_by_ids

        rows = []
        for s, added_by_str in zip(songs, display_names):
            name_str = s["name"]
            if show_inactive_marker and not s.get("available", 1):
                name_str = f"[inactive] {name_str}"
            name_str = name_str[:col_name]

            rows.append(
                f"{s['id']:<{col_id}} "
                f"{name_str:<{col_name}} "
                f"{s['artist'][:col_artist]:<{col_artist}} "
                f"{added_by_str[:col_added_by]:<{col_added_by}} "
                f"{s['times_played']}"
            )

    # Keep embed description within Discord's 4096-character limit.
    max_desc_len = 4096
    shown_rows: list[str] = []
    for row in rows:
        candidate = "```\n" + "\n".join([header, divider, *shown_rows, row]) + "\n```"
        if len(candidate) > max_desc_len:
            break
        shown_rows.append(row)

    hidden_count = len(rows) - len(shown_rows)
    suffix_line = f"... ({hidden_count} more song(s) not shown)"
    if hidden_count:
        while True:
            lines = [header, divider, *shown_rows, suffix_line]
            candidate = "```\n" + "\n".join(lines) + "\n```"
            if len(candidate) <= max_desc_len:
                break
            if not shown_rows:
                # Extremely defensive fallback; header+divider+suffix should fit.
                lines = [header, divider]
                candidate = "```\n" + "\n".join(lines) + "\n```"
                break
            shown_rows.pop()

    final_lines = [header, divider, *shown_rows]
    if hidden_count:
        final_lines.append(suffix_line)

    embed.description = "```\n" + "\n".join(final_lines) + "\n```"
    if hidden_count:
        embed.set_footer(text=f"{len(songs)} song(s) total • showing {len(shown_rows)}")
    else:
        embed.set_footer(text=f"{len(songs)} song(s) total")
    return embed


# ---------------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------------

class AddSongModal(discord.ui.Modal, title="Add Song"):
    """Collect a YouTube URL and register downloaded audio in the database."""

    song_name = discord.ui.TextInput(
        label="Song Name (optional)",
        placeholder="Overrides title for 1st track only; leave blank for playlists",
        max_length=100,
        required=False,
    )
    artist = discord.ui.TextInput(
        label="Artist (optional)",
        placeholder="Defaults to YouTube",
        max_length=100,
        required=False,
    )
    youtube_url = discord.ui.TextInput(
        label="YouTube Link (video or playlist)",
        placeholder="https://www.youtube.com/watch?v=... or playlist?list=...",
        max_length=500,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not is_music_manager(interaction):
            await interaction.response.send_message(
                "❌ You need the **Music Manager** role to add songs.", ephemeral=True
            )
            return

        urls = extract_urls(self.youtube_url.value or "")
        if not urls:
            await interaction.response.send_message(
                "❌ Please provide a valid YouTube URL.",
                ephemeral=True,
            )
            return

        youtube_url = urls[0]
        if not is_youtube_url(youtube_url):
            await interaction.response.send_message(
                "❌ Only YouTube links are supported here.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            downloaded = await asyncio.to_thread(download_youtube_audio, youtube_url, SONGS_DIR, False)
        except Exception as exc:
            await interaction.followup.send(f"❌ Could not download from YouTube: {exc}", ephemeral=True)
            return

        if not downloaded:
            await interaction.followup.send(
                f"❌ Downloaded file type is not supported.\nAllowed: {', '.join(ALLOWED_EXTENSIONS)}",
                ephemeral=True,
            )
            return

        added_by = str(interaction.user.id)
        artist = (self.artist.value or "").strip() or "YouTube"
        custom_name = (self.song_name.value or "").strip()
        lines: list[str] = []
        for idx, track in enumerate(downloaded):
            name = custom_name if idx == 0 and custom_name else track.stem
            song_id = add_song(name, artist, track.name, added_by)
            lines.append(f"✅ Added **{name}** (ID: `{song_id}`)")
        await interaction.followup.send("\n".join(lines), ephemeral=True)


class DeleteSongModal(discord.ui.Modal, title="Delete Song"):
    """
    Two-step delete by song name.

    After the user submits the song name the bot:
      1. Looks up the song (case-insensitive, all availability states).
      2. Posts a *non-ephemeral* confirmation message in the channel.
      3. Adds ✅ and 🗑️ reactions so the user can choose the delete type.
      4. Stores the pending action in ``interaction.client.pending_deletes``.

    The reaction handler (``on_raw_reaction_add`` in bot.py) completes
    the action when the requesting user reacts.
    """

    song_name = discord.ui.TextInput(
        label="Song Name",
        placeholder="e.g. Bohemian Rhapsody",
        max_length=100,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not is_music_manager(interaction):
            await interaction.response.send_message(
                "❌ You need the **Music Manager** role to delete songs.", ephemeral=True
            )
            return

        query = self.song_name.value
        matches = get_songs_by_name(query)

        if not matches:
            await interaction.response.send_message(
                f"Sorry, there is no song named **{query}**.",
                ephemeral=True,
            )
            return

        if len(matches) > 1:
            # Resolve all "added by" display names concurrently.
            usernames = await asyncio.gather(
                *[_resolve_username(interaction.client, m.get("added_by", ""), interaction.guild) for m in matches]
            )

            lines = []
            for song, username in zip(matches, usernames):
                status = "✅" if song.get("available", 1) else "⛔"
                lines.append(
                    f"{status} ID `{song['id']}` — **{song['name']}** "
                    f"by **{song['artist']}** — added by **{username}**"
                )

            embed = discord.Embed(
                title=f'🔎 Multiple songs named "{query}"',
                description="\n".join(lines),
                colour=discord.Colour.orange(),
            )
            embed.add_field(
                name="What to do",
                value=(
                    "Identify the song you want to remove from the list above, "
                    "then use:\n"
                    "**`/delete_song_id <id>`** — to start the deletion confirmation.\n\n"
                    "⚠️ Reacting to this message will **not** delete any songs."
                ),
                inline=False,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        song = matches[0]
        msg_content = await _build_delete_confirm_message(
            song, interaction.user.id, interaction.client, interaction.guild
        )

        # Must be non-ephemeral so we can attach reactions.
        await interaction.response.send_message(msg_content)
        msg = await interaction.original_response()

        await msg.add_reaction(REACT_DEACTIVATE)
        await msg.add_reaction(REACT_HARD_DELETE)

        # Register the pending delete keyed on the confirmation message id.
        interaction.client.pending_deletes[msg.id] = {  # type: ignore[attr-defined]
            "user_id": interaction.user.id,
            "song": song,
        }


# ---------------------------------------------------------------------------
# Paginated song list view
# ---------------------------------------------------------------------------

_SONGS_PER_PAGE = 20


def _song_page_embed(songs: list[dict], page: int, total_pages: int) -> discord.Embed:
    """Build a compact single-page embed for the paginated playlist.

    Only ID, Name and Artist are shown (one song per line, no wrapping).
    """
    col_id = _COL_ID
    col_name = _COL_NAME
    col_artist = _COL_ARTIST

    header = (
        f"{'ID':<{col_id}} "
        f"{'Name':<{col_name}} "
        f"{'Artist':<{col_artist}}"
    )
    divider = "─" * (col_id + col_name + col_artist + 2)

    rows = [
        f"{s['id']:<{col_id}} "
        f"{s['name'][:col_name]:<{col_name}} "
        f"{s['artist'][:col_artist]:<{col_artist}}"
        for s in songs
    ]

    body = "```\n" + "\n".join([header, divider, *rows]) + "\n```"
    embed = discord.Embed(
        title="🎵 Song Playlist",
        description=body,
        colour=discord.Colour.blue(),
    )
    embed.set_footer(text=f"Page {page}/{total_pages} • {len(songs)} song(s) on this page")
    return embed


class SongListView(discord.ui.View):
    """Ephemeral paginated playlist viewer.

    Presents songs 20 per page with Prev / Next navigation buttons.
    Because this is always sent ephemerally its custom_ids do not need to be
    globally stable — it has a 10-minute timeout so idle views expire cleanly.
    """

    def __init__(self, songs: list[dict]) -> None:
        super().__init__(timeout=600)
        self._songs = songs
        self._page = 1
        self._total_pages = max(1, -(-len(songs) // _SONGS_PER_PAGE))  # ceil division

    def _current_page_songs(self) -> list[dict]:
        start = (self._page - 1) * _SONGS_PER_PAGE
        return self._songs[start : start + _SONGS_PER_PAGE]

    def _build_embed(self) -> discord.Embed:
        return _song_page_embed(
            self._current_page_songs(), self._page, self._total_pages
        )

    def _update_buttons(self) -> None:
        self.prev_button.disabled = self._page <= 1
        self.next_button.disabled = self._page >= self._total_pages

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary, row=0)
    async def prev_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self._page > 1:
            self._page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary, row=0)
    async def next_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self._page < self._total_pages:
            self._page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)


# ---------------------------------------------------------------------------
# Persistent controller view
# ---------------------------------------------------------------------------

class MusicControlView(discord.ui.View):
    """
    Persistent button panel for music playback and library management.

    Because this view has ``timeout=None`` and all custom_ids are static
    it survives bot restarts when re-registered with ``bot.add_view()``.
    The player is retrieved at interaction time via ``interaction.client.player``
    so no state needs to be stored on the view itself.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self._play_resume_lock = asyncio.Lock()

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[discord.ui.View],
    ) -> None:
        log.error(
            "Unhandled controller view error (item=%s user_id=%s guild_id=%s)",
            getattr(item, "custom_id", None),
            getattr(interaction.user, "id", None),
            getattr(interaction.guild, "id", None),
            exc_info=(type(error), error, error.__traceback__),
        )
        msg = "❌ Something went wrong while handling that button. Please try again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            # Do not surface a second error if Discord no longer accepts responses.
            return

    # ------------------------------------------------------------------
    # Row 0 – playback
    # ------------------------------------------------------------------

    @discord.ui.button(
        label="▶ Play",
        style=discord.ButtonStyle.success,
        custom_id="music:play_resume",
        row=0,
    )
    async def play_resume(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        player = interaction.client.player  # type: ignore[attr-defined]
        async with self._play_resume_lock:
            # If paused, just resume — no voice channel join needed.
            if player.is_paused():
                player.resume()
                await interaction.response.send_message("▶ Resumed.", ephemeral=True)
                await interaction.client.refresh_controller_status()  # type: ignore[attr-defined]
                return

            # Already playing — nothing to do.
            if player.is_playing():
                await interaction.response.send_message(
                    "▶ Music is already playing.", ephemeral=True
                )
                return

            # Not active — need to join a voice channel.
            if not interaction.user.voice:  # type: ignore[union-attr]
                await interaction.response.send_message(
                    "❌ You must be in a voice channel first!", ephemeral=True
                )
                return

            voice_channel = interaction.user.voice.channel  # type: ignore[union-attr]
            try:
                await player.connect(voice_channel)
            except TimeoutError:
                await interaction.response.send_message(
                    "❌ Timed out joining voice. Please press Play again.",
                    ephemeral=True,
                )
                return
            except discord.ClientException:
                await interaction.response.send_message(
                    "❌ Could not join voice right now. Please try again.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True)
            song = await player.play_next()

            if song:
                await interaction.followup.send(
                    f"🎵 Now playing: **{song['name']}** by **{song['artist']}**",
                    ephemeral=True,
                )
            elif player.is_playing():
                # An intermission clip (ad/DJ) started instead of a regular song.
                await interaction.followup.send("▶ Playback started.", ephemeral=True)
            else:
                await interaction.followup.send(
                    "❌ The song library is empty. Add songs with **Add Song**.",
                    ephemeral=True,
                )

    @discord.ui.button(
        label="⏸ Pause",
        style=discord.ButtonStyle.primary,
        custom_id="music:pause",
        row=0,
    )
    async def pause(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        player = interaction.client.player  # type: ignore[attr-defined]
        if player.pause():
            await interaction.response.send_message("⏸ Paused.", ephemeral=True)
            await interaction.client.refresh_controller_status()  # type: ignore[attr-defined]
        else:
            await interaction.response.send_message(
                "❌ Nothing is playing right now.", ephemeral=True
            )

    @discord.ui.button(
        label="⏭ Skip",
        style=discord.ButtonStyle.secondary,
        custom_id="music:skip",
        row=0,
    )
    async def skip(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        player = interaction.client.player  # type: ignore[attr-defined]
        if player.skip():
            await interaction.response.send_message("⏭ Skipped.", ephemeral=True)
        else:
            await interaction.response.send_message(
                "❌ Nothing to skip.", ephemeral=True
            )

    @discord.ui.button(
        label="📞 Leave",
        style=discord.ButtonStyle.danger,
        custom_id="music:leave",
        row=0,
    )
    async def leave(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        player = interaction.client.player  # type: ignore[attr-defined]
        if not player.is_connected():
            await interaction.response.send_message(
                "❌ Not currently in a voice channel.", ephemeral=True
            )
            return
        await player.disconnect()
        await interaction.client.clear_controller_now_playing()  # type: ignore[attr-defined]
        await interaction.response.send_message(
            "📞 Left the voice channel.", ephemeral=True
        )

    # ------------------------------------------------------------------
    # Row 1 – library management
    # ------------------------------------------------------------------

    @discord.ui.button(
        label="📋 Playlist",
        style=discord.ButtonStyle.secondary,
        custom_id="music:song_list",
        row=1,
    )
    async def song_list(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        songs = get_all_songs()
        if not songs:
            await interaction.response.send_message(
                "*No songs in the library yet.*", ephemeral=True
            )
            return
        view = SongListView(songs)
        view._update_buttons()
        await interaction.response.send_message(
            embed=view._build_embed(), view=view, ephemeral=True
        )

    @discord.ui.button(
        label="➕ Add Song",
        style=discord.ButtonStyle.success,
        custom_id="music:add_song",
        row=1,
    )
    async def add_song(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not is_music_manager(interaction):
            await interaction.response.send_message(
                "❌ You need the **Music Manager** role to add songs.", ephemeral=True
            )
            return
        await interaction.response.send_modal(AddSongModal())

    @discord.ui.button(
        label="🗑 Delete Song",
        style=discord.ButtonStyle.danger,
        custom_id="music:delete_song",
        row=1,
    )
    async def delete_song(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not is_music_manager(interaction):
            await interaction.response.send_message(
                "❌ You need the **Music Manager** role to delete songs.", ephemeral=True
            )
            return
        await interaction.response.send_modal(DeleteSongModal())

    # ------------------------------------------------------------------
    # Row 2 – feedback
    # ------------------------------------------------------------------

    @discord.ui.button(
        label="👍",
        style=discord.ButtonStyle.success,
        custom_id="music:like_current",
        row=2,
    )
    async def like_current(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        ok, msg = await interaction.client.submit_current_song_feedback(  # type: ignore[attr-defined]
            interaction, is_like=True
        )
        prefix = "👍" if ok else "❌"
        await interaction.response.send_message(f"{prefix} {msg}", ephemeral=True)

    @discord.ui.button(
        label="👎",
        style=discord.ButtonStyle.secondary,
        custom_id="music:dislike_current",
        row=2,
    )
    async def dislike_current(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        ok, msg = await interaction.client.submit_current_song_feedback(  # type: ignore[attr-defined]
            interaction, is_like=False
        )
        prefix = "👎" if ok else "❌"
        await interaction.response.send_message(f"{prefix} {msg}", ephemeral=True)
