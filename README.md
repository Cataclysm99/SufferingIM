# SufferingIM

Discord music bot with:
- weighted blind song selection
- rigged-song pool
- ad intermissions (~30 minutes)
- DJ events (~60 minutes) with intro/outro 6-hour cycle
- persistent controller buttons + slash commands
- Sunday `HeavenFM` branding / Monday `SufferingFM` branding

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

Also install FFmpeg and ensure it is on PATH.

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

All audio extensions in config are supported (`.mp3`, `.wav`, `.ogg`, `.flac`, `.m4a`, `.aac`, `.opus`).

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
- Cooldown: **one vote per user per song per hour** — a user may vote on many different songs in the same hour, but cannot vote on the same song twice within the hour
- Votes are cast via `/like` / `/dislike` commands or the 👍/👎 controller buttons
- Cycle reset (DJ outro → restart) resets all negative `vote_score` values back to 0

### Branding model
- Sunday: switch toward HeavenFM config values/assets
- Monday onward: switch back to SufferingFM config values/assets
- Name/avatar/banner paths are optional config values

### Database model
- Since deployment starts from fresh DB, migration logic was removed
- `init_db()` now creates required tables directly:
  - `songs` (includes `vote_score` field)
  - `ads`
  - `vote_cooldowns` (composite PK: user_id + song_id)

---

## Commands (core)

- `/controller` – post control panel
- `/upload_song` – upload + register a song (Music Manager)
- `/songs`, `/songs_all`, `/search`
- `/toggle_song`, `/toggle_song_id` (Music Manager)
- `/delete_song_id` (Music Manager, reaction-confirmed)
- `/set_rigged_pool` (Music Manager, comma-separated IDs)
- `/play_dj_event` (Music Manager)
- `/now_playing`
- `/like`, `/dislike`

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

