"""Shared view-layer constants."""

from __future__ import annotations

# Column widths used in song-table displays.
COL_ID = 4
COL_NAME = 20
COL_ARTIST = 20
COL_ADDED_BY = 20

# Number of history messages to scan when searching for an existing controller.
CONTROLLER_SEARCH_LIMIT = 100

# Reaction emojis used by the legacy `/delete_song_id` confirmation flow.
REACT_DEACTIVATE = "✅"
REACT_HARD_DELETE = "🗑️"
REACT_CANCEL = "🚫"

# Song list pagination size.
SONGS_PER_PAGE = 20
