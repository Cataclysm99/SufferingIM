# SufferingIM

A **rigged** Discord music bot that shuffles a playlist of local audio files and secretly has a **1-in-10 chance** of playing a designated "rigged" song.

## Features

| Feature | Details |
|---|---|
| Shuffle playlist | All songs in the library are shuffled and played in random order. |
| Rigged song | With 1-in-10 probability the bot silently plays a pre-set song instead of the queued one. |
| Persistent controller | A button panel posted in a Discord channel survives bot restarts. |
| Song library | Songs are tracked in SQLite with id, name, artist, added\_by, filename, times\_played, and available flag. |
| File management | Audio files are stored in `songs/` and managed via Discord commands. |
| Role-gated management | Add, delete, toggle, upload, and rig commands are restricted to a configurable **Music Manager** role (see `MUSIC_MANAGER_ROLE_ID`). |

## Permissions

Set `MUSIC_MANAGER_ROLE_ID` in `.env` to the Discord role ID that should be allowed to manage the song library.  
When set to `0` (the default) everyone can manage songs, which is useful for testing.

| Action | Who can do it |
|---|---|
| Play, Pause, Skip, Leave | Everyone |
| View playlist (active songs) | Everyone |
| View full library (including deactivated) | Everyone |
| Upload / Add / Delete / Toggle songs | Music Manager role (or server Administrator) |
| Set rigged song | Music Manager role (or server Administrator) |

## Controller Buttons

**Row 1 – Playback**
| Button | Action |
|---|---|
| ▶ Play / Resume | Join the user's voice channel and start the queue, or resume if paused. |
| ⏸ Pause | Pause the current song. |
| ⏭ Skip | Skip to the next song. |
| 📞 Leave | Disconnect the bot from the voice channel. |

**Row 2 – Library**
| Button | Who | Action |
|---|---|---|
| 📋 Playlist | Everyone | Show only the currently active (available) songs. |
| 📚 Full Library | Everyone | Show all songs including deactivated ones — useful for requesting a re-enable. |
| ➕ Add Song | Music Manager | Open a modal to register a file already in `songs/`. |
| 🗑 Delete Song | Music Manager | Open a modal to start the two-step delete by song name. |

## Slash Commands

| Command | Who | Description |
|---|---|---|
| `/controller` | Everyone | Post (or re-post) the controller panel in the current channel. |
| `/search` | Everyone | Search the library by name, artist, added\_by user ID, or song id. |
| `/songs` | Everyone | Display active (available) songs only. |
| `/songs_all` | Everyone | Display all songs including deactivated ones. |
| `/now_playing` | Everyone | Show the song currently playing. |
| `/upload_song` | Music Manager | Upload an audio file attachment and add it to the library. |
| `/toggle_song` | Music Manager | Activate or deactivate a song by name. |
| `/toggle_song_id` | Music Manager | Activate or deactivate a song by its unique id. |
| `/delete_song_id` | Music Manager | Start the two-step delete confirmation for a specific song id. |
| `/set_rigged song_id` | Music Manager | Set which song is secretly rigged (0 to disable). |

## Prerequisites

- Python 3.10+
- **FFmpeg** installed and on `PATH` (required for audio playback).
- A Discord bot with the following intents enabled in the Developer Portal:
  - **Message Content Intent**
  - **Voice States** (automatically enabled for bots)

## Setup

```bash
# 1. Clone the repository
git clone https://github.com/Cataclysm99/SufferingIM.git
cd SufferingIM

# 2. Create a virtual environment (recommended)
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment variables
cp .env.example .env
# Edit .env and fill in DISCORD_TOKEN, CONTROLLER_CHANNEL_ID, RIGGED_SONG_ID,
# and optionally MUSIC_MANAGER_ROLE_ID (right-click a role → Copy Role ID)

# 5. (Optional) Pre-populate the songs folder
cp /path/to/my_song.mp3 songs/

# 6. Run the bot
python bot.py
```

## Adding Songs

**Option A – Upload via Discord**
```
/upload_song  file:<attach audio>  name:Song Name  artist:Artist Name
```
The file is saved to `songs/` and registered in the database automatically.

**Option B – Copy file manually then register**
1. Copy the audio file to the `songs/` directory on the host machine.
2. Click **➕ Add Song** in the controller and fill in the modal.

## Managing Songs

### Deleting a song (two-step confirmation)

Click **🗑 Delete Song** or run `/delete_song_id <id>`.  
The bot posts a confirmation message and adds two reactions:

| Reaction | Effect |
|---|---|
| ✅ | **Deactivate** – removes the song from the playlist queue but keeps the database row and audio file. Use `/toggle_song` or `/toggle_song_id` to re-enable it later. |
| 🗑️ | **Permanently delete** – removes the row from the database **and** deletes the audio file from `songs/`. |

Only the user who triggered the deletion can react to confirm it.

**Duplicate song names:** if more than one song shares the same name the bot shows each match with its unique ID, artist, and the username of who added it, then asks you to use `/delete_song_id <id>` to target the correct one. Reacting on that informational message does *not* delete anything.

### Temporarily removing a song

```
/toggle_song  name:Song Name
/toggle_song_id  song_id:3
```

Toggles `available` between active and deactivated.  Deactivated songs are hidden from the queue and `/search` but their audio file stays on disk, making them easy to re-add without re-uploading.

### Searching the library

```
/search  field:Artist  query:Queen
/search  field:Name    query:Bohemian
/search  field:ID      query:3
```

If nothing matches you'll get a plain-English clarification, e.g. *"Sorry, there is no artist named Bloopity Bloop bloop."*

## Rigged Song

The rigged song is stored in the library like any other song.  Set it with:
```
/set_rigged song_id:3
```
Every time the bot picks the next song to play it rolls a 10-sided die.
On a roll of 1 the rigged song plays instead of whatever was queued next — silently and without any indication to listeners.

## File Structure

```
SufferingIM/
├── bot.py          # Entry point
├── config.py       # Environment / path configuration
├── database.py     # SQLite CRUD helpers
├── player.py       # MusicPlayer (queue, rigged mechanic, FFmpeg)
├── views.py        # Persistent Discord UI (buttons + modals)
├── requirements.txt
├── .env.example
└── songs/          # Audio files (not tracked in git)
    └── .gitkeep
```
