# SufferingIM

Discord music bot with:
- weighted blind song selection
- rigged-song pool
- ad intermissions (~30 minutes)
- DJ events (~60 minutes) with intro/outro 6-hour cycle
- persistent controller buttons + slash commands
- Default `CollectorIM` branding, with Sunday `HeavenIN` / Monday `SufferFML` day-cycle branding

---

## Add the bot to a server (least-privilege setup)

### 1) Create app + bot
1. Go to https://discord.com/developers/applications
2. Create application → **Bot** tab → create bot
3. Copy token into `.env` as `DISCORD_TOKEN`

### 2) Enable gateway intents
In **Bot** tab, enable:
- **Message Content Intent** (required by this bot)

`Voice States` is not a privileged toggle in the portal; it is requested by code and used at runtime.

### 3) Invite URL (OAuth2)
Use **OAuth2 → URL Generator**:
- Scopes: `bot`, `applications.commands`
- Recommended bot permissions (minimal for full current feature set):
  - View Channels
  - Send Messages
  - Embed Links
  - Add Reactions
  - Read Message History
  - Connect
  - Speak
  - Use Voice Activity
  - Attach Files (for `/upload_song`)
  - Manage Messages (for clearing reactions after delete confirmation)

> Expansion note: if future features require role edits, channel creation, or moderation, add those permissions explicitly at that time rather than granting Administrator now.

---

## Setup

```bash
git clone https://github.com/Cataclysm99/SufferingIM.git
cd SufferingIM
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
python bot.py
```

FFmpeg resolution order at runtime:
- `FFMPEG_EXECUTABLE` from `.env` (if set)
- system `ffmpeg` on PATH
- bundled binary from `imageio-ffmpeg` dependency

If you already have ffmpeg installed globally, no extra setup is required.

---

## Runtime folders

```text
songs/               # normal songs
ads/                 # ad audio clips
dj_events/
  monday/
    intro.mp3
    outro.mp3
    ... hourly random DJ clips ...
  tuesday/
  ...
  sunday/
```

All audio extensions in config are supported (`.mp3`, `.wav`, `.ogg`, `.flac`, `.m4a`, `.aac`, `.opus`, `.webm`).

---

## Core bot modules

- `bot.py` – thin entrypoint / bootstrap
- `app_bot.py` – `MusicBot` class, controller + persona state logic
- `bot_commands.py` – slash command and modal registration
- `views/` – modular UI layer (`controller.py`, `modals.py`, `helpers.py`, etc.)

---

## Current architecture decisions

### Playback flow (priority)
On every transition to next track:
1. Forced/manual intermission clip (if requested)
2. DJ intro at cycle start
3. DJ outro when 6-hour cycle expires (then cycle restarts)
4. Ad if due (~`AD_INTERVAL_MINUTES`)
5. DJ hourly event if due (~`DJ_EVENT_INTERVAL_MINUTES`)
6. Song selection

### DJ events model
- Old sequential weekly broadcast state removed
- No `radio_state.json` tracking needed for DJ events
- Intro first, outro at cycle boundary, random hourly events per weekday

### Song selection model
- No persistent queue
- If rigged check hits (`1 / RIGGED_CHANCE`), choose randomly from `RIGGED_SONG_IDS`
- Otherwise, songs with a negative `vote_score` are excluded (suppressed)
- Songs with a positive `vote_score` (net-disliked) receive the maximum weight
- Remaining songs: weight = `max_played - times_played + 1` (less played → higher chance)
- When a song plays its `vote_score` resets to 0

### Feedback model
- 👍 **Like** a song: `vote_score -= 1` — if score goes negative the song is suppressed and won't play until the next DJ cycle reset
- 👎 **Dislike** a song: `vote_score += 1` — if score goes positive the song gets maximum selection probability
- Cooldown: **one vote per user per hour** — each new vote replaces that user's prior cooldown row
- Votes are cast via `/like` / `/dislike` commands or the 👍/👎 controller buttons
- Cycle reset (DJ outro → restart) resets all negative `vote_score` values back to 0

### Branding model
- Sunday: switch toward HeavenFM config values/assets
- Monday onward: switch back to SufferingFM config values/assets
- Name/avatar/banner paths are optional config values

### Database model
- `init_db()` now creates required tables directly:
  - `songs` (includes comma-separated `genres` and `vote_score` fields)
  - `ads` (name/sponsor/uploader/play counters)
  - `broadcasts` (day/slot/name/sponsor/uploader/play counters)
  - `vote_cooldowns` (unique user_id cooldown tracking)
  - `genre_filter_state` / `genre_filter_entries` (daily genre include/exclude controls)

### Upload request handling
- Upload requests are serialized through an in-memory queue to avoid concurrent
  playlist/song import races.

---

## YouTube authentication (private & restricted videos)

By default the bot downloads public YouTube videos without any sign-in. If a playlist
contains **private, age-restricted, or region-locked** videos, yt-dlp needs your YouTube
cookies so it can authenticate on your behalf. Without cookies those tracks are skipped
with a `⚠️ Skipped unavailable track` warning and the rest of the playlist still imports.

There are two ways to supply cookies. Pick whichever suits your setup.

---

### Option A – Browser cookie file (recommended for servers / headless hosts)

This exports a snapshot of your cookies from a browser you are already signed in to.

1. **Install a browser extension** that exports cookies in Netscape format:
   - Chrome / Edge / Brave: [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)
   - Firefox: [cookies.txt](https://addons.mozilla.org/en-US/firefox/addon/cookies-txt/)

2. **Sign in to YouTube** in that browser.

3. **Export cookies** for `youtube.com`:
   - In the extension, navigate to `youtube.com`, click the extension icon, and export.
   - Save the file somewhere on the host machine, e.g. `/home/user/yt-cookies.txt`.

4. **Set the env var** in your `.env`:
   ```
   YTDLP_COOKIES_FILE=/home/user/yt-cookies.txt
   ```

> **Tip:** Cookie files expire when your YouTube session expires. Re-export from the
> browser if downloads start failing again. Keep the file out of version control — add
> its path to `.gitignore`.

---

### Option B – Live browser cookie extraction (easiest for local machines)

yt-dlp can read cookies directly from an installed browser's profile on the same machine
the bot is running on. No file export needed, but the browser must be installed locally.

Set `YTDLP_COOKIES_FROM_BROWSER` in your `.env` to the name of your browser:

```
YTDLP_COOKIES_FROM_BROWSER=chrome
```

Supported values: `chrome`, `chromium`, `firefox`, `edge`, `opera`, `safari`, `brave`, `vivaldi`

When this is set it takes **priority over** `YTDLP_COOKIES_FILE`.

> **Note for Linux servers:** Chrome/Chromium live-cookie extraction requires a running
> display or a keyring daemon. If you hit errors, prefer Option A (cookie file) instead.

---

### Verifying it works

After setting either option, try adding a private or members-only playlist via the
**Add Song** modal or `/upload_song`. Songs that are still unavailable (deleted, blocked
in your region even with auth, etc.) will be skipped with a warning, while the rest
import normally.

For more detail on cookie export see the official yt-dlp docs:
- https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp
- https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies

---



- `/controller` – post control panel
- `/helpless` – DM a quick tutorial and command list
- `/helpless_manager` – DM the extended manager tutorial (Music Manager)
- `/upload_song` – add songs from attachment, YouTube link(s), or both (Music Manager)
- `/upload_ad` – add ads from attachment, YouTube link(s), or both (Music Manager)
- `/upload_broadcast` – add DJ broadcasts from attachment, YouTube link(s), or both (Music Manager)
- `/playlist`, `/playlist_all`, `/search`, `/genres_today`
- `/ad_list`, `/broadcast_list` (Music Manager)
- `/toggle_song`, `/toggle_song_id` (Music Manager)
- `/delete_song_id` (Music Manager, reaction-confirmed: ✅ deactivate / 🗑️ delete / 🚫 cancel)
- `/manage_songs` (Music Manager dropdown editor for song metadata)
- `/enable_genres`, `/disable_genres` (daily genre controls)
- `/set_rigged_pool` (Music Manager, comma-separated IDs)
- `/play_dj_event` (Music Manager, optional day or broadcast ID)
- `/like`, `/dislike`
- `!sync` – sync global slash commands and clear server-specific duplicate overrides
  (Manage Server)

Buttons mirror the same core actions.

---

## Config highlights (`.env`)

- `MUSIC_MANAGER_ROLE_ID=0` means unrestricted manager actions
- `RIGGED_SONG_IDS=3,7,12`
- `RIGGED_CHANCE=10`
- `AD_INTERVAL_MINUTES=30`
- `DJ_EVENT_INTERVAL_MINUTES=60`
- `DJ_CYCLE_HOURS=6`
- Sunday/Monday branding settings:
  - `SUFFERING_BOT_NAME`, `HEAVEN_BOT_NAME`
  - optional avatar/banner paths
- YouTube authentication (for private / restricted videos):
  - `YTDLP_COOKIES_FILE` – path to a Netscape cookies file
  - `YTDLP_COOKIES_FROM_BROWSER` – browser name (`chrome`, `firefox`, `edge`, etc.); takes priority over the file option
