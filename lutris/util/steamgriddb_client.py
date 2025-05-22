# lutris/util/steamgriddb_client.py

import os
import sys
from dotenv import load_dotenv

# Avoid shadowing standard libs by repo files like http.py
sys.path = [p for p in sys.path if p != os.getcwd()]

from steamgrid import SteamGridDB, ImageType

# Load API key from .env or shell
load_dotenv()
_API_KEY = os.environ.get("STEAMGRIDDB_API_KEY")
_client = SteamGridDB(_API_KEY)


def search_game(name):
    results = _client.search_game(name)
    return results[0] if results else None


def get_cover_url(game_id):
    """Fetch the first cover URL (static image) for a given game_id"""
    covers = _client.get_grids_by_gameid(
        game_ids=[game_id],       
    )
    return covers[0].url if covers else None


def get_cover_url_by_name(name):
    game = search_game(name)
    if game:
        return get_cover_url(game.id)
    return None

if __name__ == "__main__":
    """Self-test, search for Hollow Knight and fetch its cover URL"""
    name = "Hollow Knight"
    url = get_cover_url_by_name(name)
    if url:
        print(f"[{name}] cover URL: {url}")
    else:
        print(f"No cover found for: {name}")
