import json
import os
import time
from typing import Iterable

from lutris import settings
from lutris.api import get_api_games, get_game_installers
from lutris.database.games import get_games
from lutris.game import Game
from lutris.gui.widgets import NotificationSource
from lutris.installer.errors import MissingGameDependencyError
from lutris.installer.interpreter import ScriptInterpreter
from lutris.services.lutris import download_lutris_media
from lutris.util import cache_single
from lutris.util.jobs import AsyncCall
from lutris.util.log import logger
from lutris.util.strings import slugify

GAME_PATH_CACHE_PATH = os.path.join(settings.CACHE_DIR, "game-paths.json")


def get_game_slugs_and_folders(dirname):
    """Scan a directory for games previously installed with lutris"""
    folders = os.listdir(dirname)
    game_folders = {}
    for folder in folders:
        if not os.path.isdir(os.path.join(dirname, folder)):
            continue
        game_folders[slugify(folder)] = folder
    return game_folders


def find_game_folder(dirname, api_game, slugs_map):
    if api_game["slug"] in slugs_map:
        game_folder = os.path.join(dirname, slugs_map[api_game["slug"]])
        if os.path.exists(game_folder):
            return game_folder
    for alias in api_game["aliases"]:
        if alias["slug"] in slugs_map:
            game_folder = os.path.join(dirname, slugs_map[alias["slug"]])
            if os.path.exists(game_folder):
                return game_folder


def detect_game_from_installer(game_folder, installer):
    try:
        exe_path = installer["script"]["game"].get("exe")
    except KeyError:
        exe_path = installer["script"].get("exe")
    if not exe_path:
        try:
            exe_path = installer["script"]["game"].get("main_file")
        except KeyError:
            pass
    if not exe_path:
        return None
    exe_path = exe_path.replace("$GAMEDIR/", "")
    full_path = os.path.join(game_folder, exe_path)
    if os.path.exists(full_path):
        return full_path


def find_game(game_folder, api_game):
    installers = get_game_installers(api_game["slug"])
    for installer in installers:
        full_path = detect_game_from_installer(game_folder, installer)
        if full_path:
            return full_path, installer
    return None, None


def get_used_directories():
    directories = set()
    for game in get_games():
        if game['directory']:
            directories.add(game['directory'])
    return directories


def install_game(installer, game_folder):
    interpreter = ScriptInterpreter(installer)
    interpreter.target_path = game_folder
    interpreter.installer.save()


def scan_directory(dirname):
    slugs_map = get_game_slugs_and_folders(dirname)
    directories = get_used_directories()
    api_games = get_api_games(list(slugs_map.keys()))
    slugs_seen = set()
    slugs_installed = set()
    for api_game in api_games:
        if api_game["slug"] in slugs_seen:
            continue
        slugs_seen.add(api_game["slug"])
        game_folder = find_game_folder(dirname, api_game, slugs_map)
        if game_folder in directories:
            slugs_installed.add(api_game["slug"])
            continue
        full_path, installer = find_game(game_folder, api_game)
        if full_path:
            logger.info("Found %s in %s", api_game["name"], full_path)
            try:
                install_game(installer, game_folder)
            except MissingGameDependencyError as ex:
                logger.error("Skipped %s: %s", api_game["name"], ex)
            download_lutris_media(installer["game_slug"])
            slugs_installed.add(api_game["slug"])

    installed_map = {slug: folder for slug, folder in slugs_map.items() if slug in slugs_installed}
    missing_map = {slug: folder for slug, folder in slugs_map.items() if slug not in slugs_installed}
    return installed_map, missing_map


def get_path_from_config(game):
    """Return the path of the main entry point for a game"""
    if not game.config:
        logger.warning("Game %s has no configuration", game)
        return ""
    game_config = game.config.game_config

    # Skip MAME roms referenced by their ID
    if game.runner_name == "mame":
        if "main_file" in game_config and "." not in game_config["main_file"]:
            return ""

    for key in ["exe", "main_file", "iso", "rom", "disk-a", "path", "files"]:
        if key in game_config:
            path = game_config[key]
            if key == "files":
                path = path[0]

            if path:
                path = os.path.expanduser(path)
                if not path.startswith("/"):
                    path = os.path.join(game.directory, path)
                return path

    logger.warning("No path found in %s", game.config)
    return ""


def get_game_paths():
    game_paths = {}
    all_games = get_games(filters={'installed': 1})
    for db_game in all_games:
        game = Game(db_game["id"])
        if game.runner_name in ("steam", "web"):
            continue
        path = get_path_from_config(game)
        if not path:
            continue
        game_paths[db_game["id"]] = path
    return game_paths


def build_path_cache(recreate=False):
    """Generate a new cache path"""
    if os.path.exists(GAME_PATH_CACHE_PATH) and not recreate:
        return
    start_time = time.time()
    with open(GAME_PATH_CACHE_PATH, "w", encoding="utf-8") as cache_file:
        game_paths = get_game_paths()
        json.dump(game_paths, cache_file, indent=2)
    end_time = time.time()
    get_path_cache.cache_clear()
    logger.debug("Game path cache built in %0.2f seconds", end_time - start_time)


def add_to_path_cache(game):
    """Add or update the path of a game in the cache"""
    logger.debug("Adding %s to path cache", game)
    path = get_path_from_config(game)
    if not path:
        logger.warning("No path for %s", game)
        return
    current_cache = read_path_cache()
    current_cache[game.id] = path
    with open(GAME_PATH_CACHE_PATH, "w", encoding="utf-8") as cache_file:
        json.dump(current_cache, cache_file, indent=2)
    get_path_cache.cache_clear()


def remove_from_path_cache(game):
    logger.debug("Removing %s from path cache", game)
    current_cache = read_path_cache()
    if game.id not in current_cache:
        logger.warning("Game %s (id=%s) not in cache path", game, game.id)
        return
    del current_cache[game.id]
    with open(GAME_PATH_CACHE_PATH, "w", encoding="utf-8") as cache_file:
        json.dump(current_cache, cache_file, indent=2)
    get_path_cache.cache_clear()


@cache_single
def get_path_cache():
    """Return the contents of the path cache file; this
    dict is cached, so do not modify it."""
    return read_path_cache()


def read_path_cache():
    """Read the contents of the path cache file, and does not cache it."""
    with open(GAME_PATH_CACHE_PATH, encoding="utf-8") as cache_file:
        try:
            return json.load(cache_file)
        except json.JSONDecodeError:
            return {}


class MissingGames:
    """This class is a singleton that holds a set of game-ids for games whose directories
    are missing. It is updated on a background thread, but there's a NotificationSource ('updated')
    that fires when that thread has made changes and exited, so that the UI cab update then."""
    CHECK_STALL = 3

    def __init__(self):
        self.updated = NotificationSource()
        self.missing_game_ids = set()
        self._check_game_ids = None
        self._check_game_queue = []
        self._check_all_games = False
        self._changed = False

    def update_all_missing(self):
        """This starts the check for all games; the actual list of game-ids will be obtained
        on the worker thread, and this method will start it."""
        self._check_all_games = True
        self.update_missing()

    def update_missing(self, game_ids: Iterable[str] = None):
        """Starts checking the missing status on a list of games. This starts the worker
        thread, but if it is running already, it queues the games. The worker will pause
        briefly before processing extra games just to limit the rate of filesystem accesses."""

        # The presence of this set indicates that the worker is running
        start_fetch = self._check_game_ids is None

        if not self._check_game_ids:
            self._check_game_ids = set()

        if game_ids:
            self._check_game_ids |= set(game_ids)

        if start_fetch:
            initial_delay = self.CHECK_STALL if game_ids else 0
            AsyncCall(self._fetch, None, initial_delay)

    def _fetch(self, initial_delay=0):
        """This is the method that runs on the worker thread; it continues until all
        games are processed, even extras added while it is running."""
        time.sleep(initial_delay)
        logger.debug("Checking for missing games")
        try:
            while True:
                game_id = self._next_game_id()
                if not game_id:
                    break

                path = get_path_cache().get(game_id)

                if path:
                    if os.path.exists(path):
                        if game_id in self.missing_game_ids:
                            self.missing_game_ids.discard(game_id)
                            self._changed = True
                    elif game_id not in self.missing_game_ids:
                        self.missing_game_ids.add(game_id)
                        self._changed = True
        except Exception as ex:
            logger.exception("Unable to detect missing games: %s", ex)
        finally:
            # Clearing out _check_game_ids is how we know the worker is no longer running
            self._check_game_ids = None
            self._notify_changed()

    def _notify_changed(self):
        """Fires the 'updated' notification if changes have been made since the last time
        this method was called."""
        if self._changed:
            self._changed = False
            self.updated.fire()

    def _next_game_id(self):
        """Returns the next game-id to check, or None if we're done. This will detect
        additional games once the queue empties, and moves hem to the queue, but fires
        the notification then too."""
        if self._check_all_games:
            self._check_all_games = False
            path_cache = get_path_cache()
            self._check_game_queue = list(path_cache)
            self._check_game_ids.clear()

        if not self._check_game_queue and self._check_game_ids:
            # If more ids have arrived while fetching the old ones, we do not
            # just check them too. We notify immediate and wait a little,
            # then continue checking. This will
            self._notify_changed()
            time.sleep(self.CHECK_STALL)
            self._check_game_queue = list(self._check_game_ids)
            self._check_game_ids.clear()

        if self._check_game_queue:
            return self._check_game_queue.pop()

        return None


MISSING_GAMES = MissingGames()
