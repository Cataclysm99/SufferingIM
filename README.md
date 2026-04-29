# SufferingIM

A **rigged** Discord music bot that shuffles a playlist of local audio files and secretly has a **1-in-10 chance** of playing a designated "rigged" song.

## Features

| Feature | Details |
|---|---|
| Shuffle playlist | All songs in the library are shuffled and played in random order. |
| Rigged song | With 1-in-10 probability the bot silently plays a pre-set song instead of the queued one. |
| Persistent controller | A button panel posted in a Discord channel survives bot restarts. |
| Song library | Songs are tracked in SQLite with id, name, author, filename, and times-played. |
| File management | Audio files are stored in `songs/` and managed via Discord commands. |

## Controller Buttons

**Row 1 – Playback**
| Button | Action |
|---|---|
| ▶ Join & Play | Join the user's voice channel and start the shuffled queue. |
| ⏸ Pause | Pause the current song. |
| ▶ Resume | Resume a paused song. |
| ⏭ Skip | Skip to the next song. |

**Row 2 – Library**
| Button | Action |
|---|---|
| 📋 Song List | Show the song table (id, name, author, plays). |
| ➕ Add Song | Open a modal to register a file already in `songs/`. |
| 🗑 Delete Song | Open a modal to delete a song by id (also removes the file). |

## Slash Commands

| Command | Description |
|---|---|
| `/controller` | Post (or re-post) the controller panel in the current channel. |
| `/upload_song` | Upload an audio file attachment and add it to the library. |
| `/set_rigged song_id` | Set which song is secretly rigged (0 to disable). |
| `/now_playing` | Show the song currently playing. |
| `/songs` | Display the full song library table. |

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
# Edit .env and fill in DISCORD_TOKEN, CONTROLLER_CHANNEL_ID, RIGGED_SONG_ID

# 5. (Optional) Pre-populate the songs folder
cp /path/to/my_song.mp3 songs/

# 6. Run the bot
python bot.py
```

## Adding Songs

**Option A – Upload via Discord**
```
/upload_song  file:<attach audio>  name:Song Name  author:Artist
```
The file is saved to `songs/` and registered in the database automatically.

**Option B – Copy file manually then register**
1. Copy the audio file to the `songs/` directory on the host machine.
2. Click **➕ Add Song** in the controller and fill in the modal.

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
