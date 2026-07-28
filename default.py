# -*- coding: utf-8 -*-
"""Kodi plugin for browsing TMDB catalogue and streaming from webshare.cz."""

import hashlib
import json
import os
import re
import sys
import traceback
import uuid
from urllib.parse import parse_qsl, urlencode

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

from resources.lib.cache import Cache
from resources.lib.stream_parser import (
    format_stream_label, parse_stream_info, sort_streams, title_relevance)
from resources.lib.tmdb import TMDB, TMDBError, is_unrenderable
from resources.lib.webshare import WebshareAPI, WebshareAPIError

ADDON = xbmcaddon.Addon()
# Per-invocation values; router() refreshes them because with
# reuselanguageinvoker the module lives across invocations
HANDLE = -1
BASE_URL = ''
PROFILE_DIR = xbmcvfs.translatePath(ADDON.getAddonInfo('profile'))
HISTORY_FILE = os.path.join(PROFILE_DIR, 'search_history.json')
WS_TOKEN_FILE = os.path.join(PROFILE_DIR, 'ws_token.json')
TMDB_CACHE_DIR = os.path.join(PROFILE_DIR, 'cache')
MAX_HISTORY = 20

# Cast/crew shown in list items so Kodi's built-in info dialog has them
CREDITS_CACHE_FILE = os.path.join(PROFILE_DIR, 'credits_cache.json')
CREDITS_CACHE_MAX = 600
CREDITS_WORKERS = 10

VIDEO_EXTENSIONS = (
    '.avi', '.mkv', '.mp4', '.m4v', '.mov', '.wmv', '.flv',
    '.mpg', '.mpeg', '.ts', '.vob', '.divx', '.webm', '.3gp',
)

_webshare = None
_tmdb = None


def L(string_id):
    """Localized string from resources/language/*/strings.po."""
    return ADDON.getLocalizedString(string_id)


# ---------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------

def _read_api_key_from_file():
    """Try to read TMDB API key from a text file.

    Checks these locations (first found wins):
      1. <addon_profile>/tmdb_api_key.txt
      2. <addon_dir>/tmdb_api_key.txt
      3. /sdcard/tmdb_api_key.txt  (handy on Android)
    """
    locations = [
        os.path.join(PROFILE_DIR, 'tmdb_api_key.txt'),
        os.path.join(
            xbmcvfs.translatePath(ADDON.getAddonInfo('path')),
            'tmdb_api_key.txt'),
        '/sdcard/tmdb_api_key.txt',
    ]
    for path in locations:
        if os.path.isfile(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    key = f.read().strip()
                if key:
                    return key
            except OSError:
                pass
    return ''


def get_tmdb():
    global _tmdb

    # 1) settings  2) file on disk
    api_key = ADDON.getSetting('tmdb_api_key') or _read_api_key_from_file()

    if not api_key:
        xbmcgui.Dialog().ok('Webshare.cz', L(30160))
        ADDON.openSettings()
        api_key = ADDON.getSetting('tmdb_api_key') or _read_api_key_from_file()
        if not api_key:
            return None
    lang = ADDON.getSetting('tmdb_language') or 'cs-CZ'
    if (_tmdb is None or _tmdb.api_key != api_key
            or _tmdb.language != lang):
        _tmdb = TMDB(api_key, language=lang, cache=Cache(TMDB_CACHE_DIR))
    return _tmdb


def _cred_hash(username, password):
    return hashlib.sha1(
        '{}:{}'.format(username, password).encode('utf-8')).hexdigest()


def _device_uuid():
    """Stable per-installation device id for Webshare's slot tracking."""
    path = os.path.join(PROFILE_DIR, 'device_uuid.txt')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            value = f.read().strip()
        if value:
            return value
    except OSError:
        pass
    value = str(uuid.uuid4())
    try:
        os.makedirs(PROFILE_DIR, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(value)
    except OSError:
        pass
    return value


class _WebshareClient:
    """WebshareAPI with a persisted token and one re-login on rejection.

    The token webshare returns with keep_logged_in lives for weeks, so a
    fresh login on every plugin invocation would be two wasted round-trips
    per click. The stored token is tried first; only when the server turns
    a request down is a login done and the token replaced.
    """

    def __init__(self, username, password):
        self.username = username
        self.password = password
        self.api = WebshareAPI(token=self._stored_token(),
                               device_uuid=_device_uuid())

    def _stored_token(self):
        try:
            with open(WS_TOKEN_FILE, 'r', encoding='utf-8') as f:
                stored = json.load(f)
            if stored.get('creds') == _cred_hash(self.username, self.password):
                return stored.get('token', '')
        except (OSError, ValueError):
            pass
        return ''

    def _login(self):
        try:
            self.api.login(self.username, self.password)
        except WebshareAPIError as e:
            if e.network:
                raise
            raise WebshareAPIError(L(30162).format(e), code='LOGIN_FAILED')
        try:
            _atomic_write_json(WS_TOKEN_FILE, {
                'creds': _cred_hash(self.username, self.password),
                'token': self.api.token,
            })
        except OSError:
            pass

    def _call(self, fn, *args, **kwargs):
        if not self.api.token:
            self._login()
            return fn(*args, **kwargs)
        try:
            return fn(*args, **kwargs)
        except WebshareAPIError as e:
            if e.network:
                raise
            # Anything else may be an expired token — one login, one retry
            self._login()
            return fn(*args, **kwargs)

    def search(self, *args, **kwargs):
        return self._call(self.api.search, *args, **kwargs)

    def get_file_link(self, *args, **kwargs):
        return self._call(self.api.get_file_link, *args, **kwargs)


def get_webshare():
    global _webshare
    username = ADDON.getSetting('username')
    password = ADDON.getSetting('password')
    if not username or not password:
        xbmcgui.Dialog().ok('Webshare.cz', L(30161))
        ADDON.openSettings()
        username = ADDON.getSetting('username')
        password = ADDON.getSetting('password')
        if not username or not password:
            return None
    if (_webshare is None or _webshare.username != username
            or _webshare.password != password):
        _webshare = _WebshareClient(username, password)
    return _webshare


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def build_url(action, **kwargs):
    kwargs['action'] = action
    return '{}?{}'.format(BASE_URL, urlencode(kwargs))


def _int(value, default=0):
    """int() for URL parameters — tolerant of junk from stale bookmarks."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _fail_directory():
    """Terminate the pending directory listing, if any."""
    if HANDLE >= 0:
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)


def _atomic_write_json(path, data):
    """Write JSON so a concurrent run or a kill never leaves half a file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = '{}.{}.tmp'.format(path, os.getpid())
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Search history
# ---------------------------------------------------------------------------

def load_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def save_history(history):
    try:
        _atomic_write_json(HISTORY_FILE, history[:MAX_HISTORY])
    except OSError:
        pass


def add_to_history(query):
    history = load_history()
    if query in history:
        history.remove(query)
    history.insert(0, query)
    save_history(history)


# ---------------------------------------------------------------------------
# List item helpers
# ---------------------------------------------------------------------------

def _load_credits_cache():
    try:
        with open(CREDITS_CACHE_FILE, 'r', encoding='utf-8') as f:
            cache = json.load(f)
        return cache if isinstance(cache, dict) else {}
    except (IOError, OSError, ValueError):
        return {}


def _save_credits_cache(cache):
    if len(cache) > CREDITS_CACHE_MAX:
        # Drop oldest-inserted entries; dicts keep insertion order
        for key in list(cache)[:len(cache) - CREDITS_CACHE_MAX]:
            del cache[key]
    try:
        _atomic_write_json(CREDITS_CACHE_FILE, cache)
    except OSError:
        pass


def _distill_credits(data):
    """Keep only what list items need, so the cache stays small."""
    directors = [c['name'] for c in data.get('crew', [])
                 if c.get('job') == 'Director' and c.get('name')]
    cast = [{'name': c.get('name', ''), 'character': c.get('character', ''),
             'profile_path': c.get('profile_path')}
            for c in data.get('cast', [])[:15] if c.get('name')]
    return {'directors': directors, 'cast': cast}


def _credits_key(media_type, tmdb_id):
    lang = ADDON.getSetting('tmdb_language') or 'cs-CZ'
    return '{}:{}:{}'.format(media_type, tmdb_id, lang)


def prefetch_credits(results, media_type=None):
    """Fetch cast/crew for a page of TMDB results, in parallel and cached.

    Returns {tmdb_id: distilled credits}. Failures are skipped silently — the
    listing still works, it just shows no cast for those entries.
    """
    tmdb = get_tmdb()
    if tmdb is None:
        return {}

    wanted = []
    for item in results:
        mt = media_type or item.get('media_type')
        if mt in ('movie', 'tv') and item.get('id'):
            wanted.append((mt, item['id']))
    if not wanted:
        return {}

    cache = _load_credits_cache()
    keys = {(mt, tid): _credits_key(mt, tid) for mt, tid in wanted}

    out = {}
    missing = []
    for mt, tid in wanted:
        cached = cache.get(keys[(mt, tid)])
        if cached is None:
            missing.append((mt, tid))
        else:
            out[(mt, tid)] = cached

    if missing:
        from concurrent.futures import ThreadPoolExecutor

        def fetch(entry):
            mt, tid = entry
            getter = tmdb.movie_credits if mt == 'movie' else tmdb.tv_credits
            try:
                return entry, _distill_credits(getter(tid))
            except Exception:  # network/API hiccup — listing must not break
                return entry, None

        workers = min(CREDITS_WORKERS, len(missing))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for entry, credits_ in pool.map(fetch, missing):
                if credits_ is None:
                    continue
                out[entry] = credits_
                cache[keys[entry]] = credits_
        _save_credits_cache(cache)

    return out


def _credits_cast(credits_):
    """Cast from distilled credits, as plain dicts (JSON-safe)."""
    return [{'name': c['name'], 'role': c.get('character', ''),
             'thumbnail': TMDB.poster_url(c.get('profile_path'), 'w185'),
             'order': i}
            for i, c in enumerate((credits_ or {}).get('cast') or [])]


def _names(value):
    """A list of names from either a list or a comma-joined string."""
    if isinstance(value, str):
        return [s.strip() for s in value.split(',') if s.strip()]
    return list(value or [])


def _set_video_info(li, info, cast_=None):
    """Fill the item's InfoTagVideo — the Kodi 20+ replacement for the
    deprecated ListItem.setInfo()/setCast().

    `info` is the same plain dict the plugin has always built; `cast_` is a
    list of dicts with name/role/thumbnail/order.
    """
    tag = li.getVideoInfoTag()
    if info.get('mediatype'):
        tag.setMediaType(info['mediatype'])
    if info.get('title'):
        tag.setTitle(str(info['title']))
    if info.get('originaltitle'):
        tag.setOriginalTitle(info['originaltitle'])
    if info.get('plot'):
        tag.setPlot(info['plot'])
    if _int(info.get('year')):
        tag.setYear(_int(info.get('year')))
    if info.get('rating'):
        tag.setRating(float(info['rating']))
    if _int(info.get('votes')):
        tag.setVotes(_int(info.get('votes')))
    if info.get('duration'):
        tag.setDuration(_int(info['duration']))
    if info.get('genre'):
        tag.setGenres(_names(info['genre']))
    if info.get('director'):
        tag.setDirectors(_names(info['director']))
    if info.get('writer'):
        tag.setWriters(_names(info['writer']))
    if info.get('studio'):
        tag.setStudios(_names(info['studio']))
    if info.get('tvshowtitle'):
        tag.setTvShowTitle(info['tvshowtitle'])
    if info.get('season') is not None:
        tag.setSeason(_int(info['season']))
    if info.get('episode') is not None:
        tag.setEpisode(_int(info['episode']))
    if info.get('imdbnumber'):
        tag.setIMDBNumber(info['imdbnumber'])
    uniqueids = dict(info.get('uniqueids') or {})
    if info.get('imdbnumber'):
        uniqueids.setdefault('imdb', info['imdbnumber'])
    if uniqueids:
        tag.setUniqueIDs(uniqueids, 'tmdb' if 'tmdb' in uniqueids else '')
    if cast_:
        tag.setCast([
            xbmc.Actor(c.get('name', ''), c.get('role', ''),
                       _int(c.get('order'), i), c.get('thumbnail', ''))
            for i, c in enumerate(cast_)])


def _apply_credits(info, credits_):
    """Merge director + cast from prefetched credits; returns cast list."""
    if not credits_:
        return []
    if credits_.get('directors'):
        info['director'] = ', '.join(credits_['directors'])
    return _credits_cast(credits_)


def _usable_original(title):
    """Original title, unless it is in a script Kodi cannot display."""
    return '' if is_unrenderable(title) else (title or '')


def _search_context_items(title, original='', year='', season=None,
                          episode=None):
    """Context menu entries for picking a webshare file to play."""
    original = _usable_original(original)
    extra = {}
    if season is not None and episode is not None:
        extra = {'season': season, 'episode': episode}
    items = [
        (L(30140), 'RunPlugin({})'.format(
            build_url('play_pick', title=title, original_title=original,
                      year=year, **extra))),
    ]
    if original and original.lower() != title.lower():
        # Webshare names files by the original title at least as often
        items.append((L(30142), 'RunPlugin({})'.format(
            build_url('play_pick', title=original, year=year, **extra))))
    return items


def _tmdb_cast(cast, limit=15):
    """Convert TMDB cast/guest_stars entries to Kodi setCast() format."""
    return [{'name': c.get('name', ''),
             'role': c.get('character', ''),
             'thumbnail': TMDB.poster_url(c.get('profile_path'), 'w185'),
             'order': i}
            for i, c in enumerate(cast[:limit]) if c.get('name')]


def _movie_listitem(item, credits_=None):
    """Create a ListItem from a TMDB movie dict."""
    title = item.get('title') or item.get('original_title', '')
    year = (item.get('release_date') or '')[:4]
    rating = item.get('vote_average', 0)
    label = '{} ({})'.format(title, year) if year else title
    if rating:
        votes = item.get('vote_count', 0)
        label += '  [COLOR gold]\u2605 {:.1f} ({:,})[/COLOR]'.format(rating, votes)

    li = xbmcgui.ListItem(label)
    info = {'title': title, 'plot': item.get('overview', ''),
            'originaltitle': _usable_original(item.get('original_title')),
            'rating': rating, 'votes': str(item.get('vote_count', 0)),
            'mediatype': 'movie'}
    if year.isdigit():
        info['year'] = int(year)
    if item.get('id'):
        info['uniqueids'] = {'tmdb': str(item['id'])}
    cast_ = _apply_credits(info, credits_)
    _set_video_info(li, info, cast_)

    poster = TMDB.poster_url(item.get('poster_path'))
    fanart = TMDB.fanart_url(item.get('backdrop_path'))
    li.setArt({'thumb': poster, 'poster': poster, 'fanart': fanart})
    li.addContextMenuItems(_search_context_items(
        title, item.get('original_title', ''), year))
    return li, title, year


def _tv_listitem(item, credits_=None):
    """Create a ListItem from a TMDB tv dict."""
    title = item.get('name') or item.get('original_name', '')
    year = (item.get('first_air_date') or '')[:4]
    rating = item.get('vote_average', 0)
    label = '{} ({})'.format(title, year) if year else title
    if rating:
        votes = item.get('vote_count', 0)
        label += '  [COLOR gold]\u2605 {:.1f} ({:,})[/COLOR]'.format(rating, votes)

    li = xbmcgui.ListItem(label)
    info = {'title': title, 'plot': item.get('overview', ''),
            'originaltitle': _usable_original(item.get('original_name')),
            'rating': rating, 'votes': str(item.get('vote_count', 0)),
            'mediatype': 'tvshow'}
    if year.isdigit():
        info['year'] = int(year)
    if item.get('id'):
        info['uniqueids'] = {'tmdb': str(item['id'])}
    cast_ = _apply_credits(info, credits_)
    _set_video_info(li, info, cast_)

    poster = TMDB.poster_url(item.get('poster_path'))
    fanart = TMDB.fanart_url(item.get('backdrop_path'))
    li.setArt({'thumb': poster, 'poster': poster, 'fanart': fanart})
    li.addContextMenuItems(_search_context_items(
        title, item.get('original_name', ''), year))
    return li, title, year


def _add_movie_item(item, credits_=None):
    """Add a movie to the current listing.

    Not a folder and not playable, so clicking it makes Kodi run us as a plain
    script instead of waiting for a directory or a resolved URL. Only from that
    context is it safe to open the info screen — see show_movie_info.
    """
    li, title, year = _movie_listitem(item, credits_)
    url = build_url('movie_info', tmdb_id=item['id'], title=title, year=year)
    xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=False)


def _add_tv_item(item, credits_=None):
    """Add a series to the current listing; it opens the season list."""
    li, title, year = _tv_listitem(item, credits_)
    url = build_url('tv_detail', tmdb_id=item['id'], title=title, year=year)
    xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)


def _enable_sort_methods():
    """Enable sorting by title, year and rating in Kodi's view menu."""
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_UNSORTED)
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_TITLE)
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_VIDEO_YEAR)
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_VIDEO_RATING)


def _notify_no_content():
    xbmcgui.Dialog().notification('Webshare.cz', L(30165),
                                  xbmcgui.NOTIFICATION_INFO)


# TMDB discover sort options — maps label string ids to TMDB sort_by values
SORT_OPTIONS = [
    (30120, 'popularity.desc'),
    (30121, 'vote_average.desc'),
    (30122, 'vote_count.desc'),
    (30123, 'primary_release_date.desc'),
    (30124, 'primary_release_date.asc'),
]

SORT_OPTIONS_TV = [
    (30120, 'popularity.desc'),
    (30121, 'vote_average.desc'),
    (30122, 'vote_count.desc'),
    (30123, 'first_air_date.desc'),
    (30124, 'first_air_date.asc'),
]


def _sort_label(media_type, sort_by):
    """Return human-readable label for a TMDB sort_by value."""
    options = SORT_OPTIONS if media_type == 'movie' else SORT_OPTIONS_TV
    for label_id, value in options:
        if value == sort_by:
            return L(label_id)
    return L(30120)


def _pick_sort(media_type, current_sort='popularity.desc'):
    """Show a sort selection dialog. Returns TMDB sort_by value or None if cancelled."""
    options = SORT_OPTIONS if media_type == 'movie' else SORT_OPTIONS_TV
    labels = [L(o[0]) for o in options]
    # pre-select current
    preselect = 0
    for i, o in enumerate(options):
        if o[1] == current_sort:
            preselect = i
            break
    idx = xbmcgui.Dialog().select(L(30125), labels, preselect=preselect)
    if idx < 0:
        return None
    return options[idx][1]


MAX_TMDB_PAGE = 500  # TMDB rejects page > 500 with HTTP 400


def _add_page_items(data, action, extra_params=None):
    """Add the next-page navigation item."""
    page = _int(data.get('page', 1), 1)
    total = min(_int(data.get('total_pages', 1), 1), MAX_TMDB_PAGE)
    params = extra_params or {}
    if page < total:
        li = xbmcgui.ListItem(L(30181).format(page + 1, total))
        li.setArt({'icon': 'DefaultFolder.png'})
        li.setProperty('SpecialSort', 'bottom')
        xbmcplugin.addDirectoryItem(
            HANDLE, build_url(action, page=page + 1, **params), li, isFolder=True)


# ---------------------------------------------------------------------------
# Webshare search for a title
# ---------------------------------------------------------------------------

def _build_search_queries(title, year='', season=None, episode=None):
    """Build search queries for webshare from a title.

    Diacritics are left alone: Webshare folds them server-side, so
    'Želary' and 'Zelary' return identical results (verified).
    """
    clean = re.sub(r'[^\w\s]', '', title).strip()
    queries = []
    if season is not None and episode is not None:
        ep_tag = 'S{:02d}E{:02d}'.format(_int(season), _int(episode))
        queries.append('{} {}'.format(clean, ep_tag))
    if year:
        queries.append('{} {}'.format(clean, year))
    queries.append(clean)
    return queries


def _ws_collect_results(title, year='', season=None, episode=None):
    """Search webshare and return deduplicated video-file results.

    Returns None when the Webshare client is not configured.
    """
    ws = get_webshare()
    if ws is None:
        return None

    queries = _build_search_queries(title, year, season, episode)

    all_results = []
    seen_idents = set()
    for query in queries:
        try:
            results, _ = ws.search(query, limit=50)
        except WebshareAPIError as e:
            if e.code == 'LOGIN_FAILED':
                raise  # bad credentials, not a bad query
            continue
        for item in results:
            if item['ident'] in seen_idents:
                continue
            name = item['name']
            if not any(name.lower().endswith(ext) for ext in VIDEO_EXTENSIONS):
                continue
            # Webshare matches loosely: a search for the title comes back
            # with every file sharing a word with it
            relevance = title_relevance(name, title, year, season, episode)
            if relevance is None:
                continue
            item['_relevance'] = relevance
            item['_parsed'] = parse_stream_info(name)
            all_results.append(item)
            seen_idents.add(item['ident'])
    # Closest match first, best quality within that — so the top of every
    # picker and listing is the file to play. Files over the device's
    # resolution cap sort last (kept visible as a fallback).
    return sort_streams(all_results, _max_resolution_rank())


def search_webshare_for_title(title, original_title='', year='',
                              season=None, episode=None):
    """Search webshare for a movie/episode title and list results as directory."""
    all_results = _ws_collect_results(title, year, season, episode)
    if all_results is None:
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return

    if not all_results and original_title \
            and original_title.lower() != title.lower():
        all_results = _ws_collect_results(original_title, year,
                                          season, episode) or []

    if not all_results:
        # A notification, not a modal dialog: Kodi is still holding its busy
        # dialog open while it waits for this listing.
        xbmcgui.Dialog().notification(
            'Webshare.cz', L(30163).format(title),
            xbmcgui.NOTIFICATION_INFO)
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return

    _render_stream_list(all_results, {}, {}, [])


WINDOW_OK_DIALOG = 12002


def _cancel_playback():
    """Tell Kodi no URL is coming, when it is waiting for one.

    Kodi answers a declined resolve with a modal "Playback failed" — seen on
    Android, where it stays on screen until dismissed. Nothing here is a
    failure the user needs to acknowledge: either they closed the file
    picker themselves, or they have already been told what went wrong. So
    wait for that dialog to appear and take it away again.
    """
    if HANDLE < 0:
        return
    xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
    monitor = xbmc.Monitor()
    for _ in range(20):
        if monitor.waitForAbort(0.1):
            return
        if xbmcgui.getCurrentWindowDialogId() == WINDOW_OK_DIALOG:
            xbmc.executebuiltin('Dialog.Close(okdialog, true)')
            return


def _notify(message):
    xbmcgui.Dialog().notification('Webshare.cz', message,
                                  xbmcgui.NOTIFICATION_INFO)


def _download_type():
    """video_stream (default) or file_download per the user's setting."""
    return ('file_download'
            if ADDON.getSetting('download_type') == 'file_download'
            else 'video_stream')


_MAX_RESOLUTION_RANKS = {'1080': 3, '720': 2}


def _max_resolution_rank():
    """resolution_rank cap from the max_resolution setting, 0 = no limit."""
    return _MAX_RESOLUTION_RANKS.get(ADDON.getSetting('max_resolution'), 0)


def _stream_context_items(result):
    """Context menu for a Webshare file row."""
    return [
        (L(30143), 'RunPlugin({})'.format(
            build_url('play_direct', ident=result['ident'],
                      name=result['name'], dt='file_download'))),
    ]


def _add_stream_headers(link):
    """Append HTTP headers to a streaming URL for Kodi (pipe syntax)."""
    headers = (
        'User-Agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        '&Referer=https://webshare.cz/'
    )
    return '{}|{}'.format(link, headers)


def _play_direct(ident, name='', download_type=''):
    """Resolve webshare link and play directly via xbmc.Player (for dialog flows)."""
    ws = get_webshare()
    if ws is None:
        return
    try:
        link = ws.get_file_link(ident, download_type or _download_type())
    except WebshareAPIError as e:
        xbmcgui.Dialog().ok(L(30177), str(e))
        return
    stream_url = _add_stream_headers(link)
    li = xbmcgui.ListItem(name or 'Video', path=stream_url)
    xbmc.Player().play(stream_url, li)


def play_webshare(ident, name=''):
    """Resolve and play a webshare file (for IsPlayable items via setResolvedUrl)."""
    ws = get_webshare()
    if ws is None:
        _cancel_playback()
        return
    try:
        link = ws.get_file_link(ident, _download_type())
    except WebshareAPIError as e:
        _notify(str(e))
        _cancel_playback()
        return

    stream_url = _add_stream_headers(link)
    li = xbmcgui.ListItem(name or 'Video', path=stream_url)
    xbmcplugin.setResolvedUrl(HANDLE, True, li)


def play_pick(title, original_title='', year='', season=None, episode=None):
    """Search webshare, let the user pick a file in a dialog and play it.

    Reached two ways, and both end here: from a context menu via RunPlugin
    (no handle, so the file is handed to xbmc.Player), and as the path of a
    playable item — the info screen's Play button — where Kodi is waiting
    for a resolved URL and gets one once the user has chosen.
    """
    progress = xbmcgui.DialogProgressBG()
    progress.create('Webshare.cz', L(30167).format(title))
    try:
        results = _ws_collect_results(title, year, season, episode)
        if results is None:
            _cancel_playback()
            return
        if not results and original_title \
                and original_title.lower() != title.lower():
            progress.update(50, message=L(30168).format(original_title))
            results = _ws_collect_results(original_title, year,
                                          season, episode) or []
    finally:
        progress.close()

    if not results:
        # A notification rather than a modal: with playback pending the
        # "Playback failed" dialog is on its way and two stacked modals
        # would need two dismissals
        _cancel_playback()
        _notify(L(30163).format(title))
        return

    choices = []
    for r in results:
        li = xbmcgui.ListItem(r['name'])
        li.setLabel2(format_stream_label(r))
        li.setArt({'thumb': r['img']})
        choices.append(li)
    idx = xbmcgui.Dialog().select(L(30169).format(title), choices,
                                  useDetails=True)
    if idx < 0:
        _cancel_playback()
        return

    chosen = results[idx]
    if HANDLE >= 0:
        play_webshare(chosen['ident'], chosen['name'])
    else:
        _play_direct(chosen['ident'], chosen['name'])


# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------

def main_menu():
    xbmcplugin.setContent(HANDLE, 'videos')
    items = [
        ('[B]{}[/B]'.format(L(30100)), 'search_input', 'DefaultAddonsSearch.png'),
        (L(30101), 'trending', 'DefaultRecentlyAddedMovies.png'),
        (L(30102), 'movies_now_playing', 'DefaultRecentlyAddedMovies.png'),
        (L(30103), 'movies_popular', 'DefaultMovies.png'),
        (L(30104), 'movies_top_rated', 'DefaultMovies.png'),
        (L(30105), 'tv_popular', 'DefaultTVShows.png'),
        (L(30106), 'tv_top_rated', 'DefaultTVShows.png'),
        (L(30107), 'genres_movies', 'DefaultGenre.png'),
        (L(30108), 'genres_tv', 'DefaultGenre.png'),
        (L(30109), 'years_movies', 'DefaultYear.png'),
        (L(30110), 'years_tv', 'DefaultYear.png'),
        (L(30111), 'ws_search_input', 'DefaultAddonsSearch.png'),
    ]
    for label, action, icon in items:
        li = xbmcgui.ListItem(label)
        li.setArt({'icon': icon})
        xbmcplugin.addDirectoryItem(HANDLE, build_url(action), li, isFolder=True)

    # Search history
    history = load_history()
    if history:
        li = xbmcgui.ListItem('[I]--- {} ---[/I]'.format(L(30112)))
        li.setArt({'icon': 'DefaultAddonsSearch.png'})
        xbmcplugin.addDirectoryItem(HANDLE, build_url('history'), li, isFolder=True)

    xbmcplugin.endOfDirectory(HANDLE)


# ---------------------------------------------------------------------------
# TMDB catalogue screens
# ---------------------------------------------------------------------------

def show_trending(page=1):
    tmdb = get_tmdb()
    if tmdb is None:
        _fail_directory()
        return
    data = tmdb.trending(page=_int(page, 1))
    if not data.get('results'):
        _notify_no_content()

    xbmcplugin.setContent(HANDLE, 'videos')
    creds = prefetch_credits(data.get('results', []))
    for item in data.get('results', []):
        mt = item.get('media_type', 'movie')
        if mt == 'movie':
            _add_movie_item(item, creds.get((mt, item['id'])))
        elif mt == 'tv':
            _add_tv_item(item, creds.get((mt, item['id'])))
    _add_page_items(data, 'trending')
    _enable_sort_methods()
    xbmcplugin.endOfDirectory(HANDLE)


def _cinema_region():
    """Region for now-playing listings: setting, else from the language."""
    region = ADDON.getSetting('tmdb_region')
    if region:
        return region.upper()
    lang = ADDON.getSetting('tmdb_language') or 'cs-CZ'
    return lang.split('-')[-1].upper() if '-' in lang else 'CZ'


def show_movies(category, page=1):
    tmdb = get_tmdb()
    if tmdb is None:
        _fail_directory()
        return
    if category == 'now_playing':
        data = tmdb.movies_now_playing(page=_int(page, 1),
                                       region=_cinema_region())
    else:
        method = {'popular': tmdb.movies_popular,
                  'top_rated': tmdb.movies_top_rated}
        data = method[category](page=_int(page, 1))
    if not data.get('results'):
        _notify_no_content()

    xbmcplugin.setContent(HANDLE, 'movies')
    creds = prefetch_credits(data.get('results', []), 'movie')
    for item in data.get('results', []):
        _add_movie_item(item, creds.get(('movie', item['id'])))
    _add_page_items(data, 'movies_' + category)
    _enable_sort_methods()
    xbmcplugin.endOfDirectory(HANDLE)


def show_tv(category, page=1):
    tmdb = get_tmdb()
    if tmdb is None:
        _fail_directory()
        return
    method = {'popular': tmdb.tv_popular,
              'top_rated': tmdb.tv_top_rated}
    data = method[category](page=_int(page, 1))
    if not data.get('results'):
        _notify_no_content()

    xbmcplugin.setContent(HANDLE, 'tvshows')
    creds = prefetch_credits(data.get('results', []), 'tv')
    for item in data.get('results', []):
        _add_tv_item(item, creds.get(('tv', item['id'])))
    _add_page_items(data, 'tv_' + category)
    _enable_sort_methods()
    xbmcplugin.endOfDirectory(HANDLE)


# ---------------------------------------------------------------------------
# Genres
# ---------------------------------------------------------------------------

def show_genres(media_type):
    tmdb = get_tmdb()
    if tmdb is None:
        _fail_directory()
        return
    data = (tmdb.movie_genres() if media_type == 'movie'
            else tmdb.tv_genres())

    xbmcplugin.setContent(HANDLE, 'videos')
    for genre in data.get('genres', []):
        li = xbmcgui.ListItem(genre['name'])
        li.setArt({'icon': 'DefaultGenre.png'})
        url = build_url('genre_list', media_type=media_type,
                        genre_id=genre['id'], genre_name=genre['name'])
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)
    xbmcplugin.endOfDirectory(HANDLE)


def show_genre_list(media_type, genre_id, genre_name, page=1,
                    sort_by='popularity.desc', update_listing=False):
    tmdb = get_tmdb()
    if tmdb is None:
        _fail_directory()
        return
    discover_params = {'with_genres': genre_id, 'sort_by': sort_by}
    if sort_by == 'vote_average.desc':
        discover_params['vote_count.gte'] = 50
    if media_type == 'movie':
        data = tmdb.discover_movies(page=_int(page, 1), **discover_params)
    else:
        data = tmdb.discover_tv(page=_int(page, 1), **discover_params)
    if not data.get('results'):
        _notify_no_content()

    content = 'movies' if media_type == 'movie' else 'tvshows'
    xbmcplugin.setContent(HANDLE, content)

    # Sort button pinned to the top
    sort_li = xbmcgui.ListItem('[B]{}[/B]'.format(
        L(30126).format(_sort_label(media_type, sort_by))))
    sort_li.setArt({'icon': 'DefaultAddSource.png'})
    sort_li.setProperty('SpecialSort', 'top')
    xbmcplugin.addDirectoryItem(
        HANDLE, build_url('genre_sort', media_type=media_type,
                          genre_id=genre_id, genre_name=genre_name,
                          current_sort=sort_by),
        sort_li, isFolder=True)

    creds = prefetch_credits(data.get('results', []), media_type)
    for item in data.get('results', []):
        if media_type == 'movie':
            _add_movie_item(item, creds.get((media_type, item['id'])))
        else:
            _add_tv_item(item, creds.get((media_type, item['id'])))
    _add_page_items(data, 'genre_list',
                    {'media_type': media_type, 'genre_id': genre_id,
                     'genre_name': genre_name, 'sort_by': sort_by})
    _enable_sort_methods()
    xbmcplugin.endOfDirectory(HANDLE, updateListing=update_listing)


# ---------------------------------------------------------------------------
# Year browsing
# ---------------------------------------------------------------------------

def show_years(media_type):
    """Show a list of years (current year down to 1970) for browsing."""
    import datetime
    current_year = datetime.datetime.now().year
    xbmcplugin.setContent(HANDLE, 'videos')
    for year in range(current_year, 1969, -1):
        li = xbmcgui.ListItem(str(year))
        li.setArt({'icon': 'DefaultYear.png'})
        url = build_url('year_list', media_type=media_type, year=year)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)
    xbmcplugin.endOfDirectory(HANDLE)


def show_year_list(media_type, year, page=1, sort_by='popularity.desc',
                   update_listing=False):
    """Show movies or TV shows from a specific year using TMDB discover."""
    tmdb = get_tmdb()
    if tmdb is None:
        _fail_directory()
        return
    discover_params = {'sort_by': sort_by}
    if sort_by == 'vote_average.desc':
        discover_params['vote_count.gte'] = 50
    if media_type == 'movie':
        discover_params['primary_release_year'] = _int(year)
    else:
        discover_params['first_air_date_year'] = _int(year)
    if media_type == 'movie':
        data = tmdb.discover_movies(page=_int(page, 1), **discover_params)
    else:
        data = tmdb.discover_tv(page=_int(page, 1), **discover_params)
    if not data.get('results'):
        _notify_no_content()

    content = 'movies' if media_type == 'movie' else 'tvshows'
    xbmcplugin.setContent(HANDLE, content)

    # Sort button pinned to the top
    sort_li = xbmcgui.ListItem('[B]{}[/B]'.format(
        L(30126).format(_sort_label(media_type, sort_by))))
    sort_li.setArt({'icon': 'DefaultAddSource.png'})
    sort_li.setProperty('SpecialSort', 'top')
    xbmcplugin.addDirectoryItem(
        HANDLE, build_url('year_sort', media_type=media_type,
                          year=year, current_sort=sort_by),
        sort_li, isFolder=True)

    creds = prefetch_credits(data.get('results', []), media_type)
    for item in data.get('results', []):
        if media_type == 'movie':
            _add_movie_item(item, creds.get((media_type, item['id'])))
        else:
            _add_tv_item(item, creds.get((media_type, item['id'])))
    _add_page_items(data, 'year_list',
                    {'media_type': media_type, 'year': year,
                     'sort_by': sort_by})
    _enable_sort_methods()
    xbmcplugin.endOfDirectory(HANDLE, updateListing=update_listing)


# ---------------------------------------------------------------------------
# Movie / TV detail
# ---------------------------------------------------------------------------

def _quiet(fn, *args):
    """Run fn, turning a network or API failure into no data.

    The info screen is worth showing with a missing plot or an empty file list;
    it is not worth replacing with a traceback.
    """
    try:
        return fn(*args)
    except Exception as e:
        xbmc.log('plugin.video.webshare: {} failed: {}'.format(
            getattr(fn, '__name__', fn), e), xbmc.LOGWARNING)
        return None


def _info_listitem(label, play_url, info, art, cast_):
    """A playable item for Kodi's info screen.

    Its Play button follows the path, which searches Webshare and offers the
    files it found — the same picker the context menu opens. Kodi waits for a
    resolved URL the whole time, so that path must always answer: with the
    chosen file, or with a declined resolve (see _cancel_playback).
    """
    li = xbmcgui.ListItem(label, path=play_url)
    li.setProperty('IsPlayable', 'true')
    _set_video_info(li, info, cast_)
    if art:
        li.setArt(art)
    return li


def _render_stream_list(results, info, art, cast_):
    """List Webshare files as a directory, each carrying the title's metadata.

    A row is identified by its file name, so that overrides the title from
    TMDB — otherwise every row shows the film's name. The film's runtime goes
    too: repeated on every row it only says the same thing. The size is left
    to Kodi, which shows it in its own column.
    """
    xbmcplugin.setContent(HANDLE, 'videos')
    shared = {k: v for k, v in info.items() if k != 'duration'}
    for r in results:
        li = xbmcgui.ListItem(r['name'])
        # Parsed quality summary (1080p | BluRay | CZ dabing | 8.5 GB)
        # goes to label2; skins with a second line show it right away
        li.setLabel2(format_stream_label(r))
        _set_video_info(li, dict(shared, title=r['name']), cast_)
        li.setArt(dict(art, thumb=r['img'] or art.get('thumb', '')))
        li.setProperty('IsPlayable', 'true')
        li.addContextMenuItems(_stream_context_items(r))
        url = build_url('play', ident=r['ident'], name=r['name'])
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=False)

    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_UNSORTED)
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_SIZE)
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_LABEL)
    xbmcplugin.endOfDirectory(HANDLE)


def _movie_info_dict(detail, title, year):
    """Build the info dict and artwork shared by every file of one movie."""
    play_title = detail.get('title') or title
    play_year = (detail.get('release_date') or '')[:4] or year
    runtime = detail.get('runtime', 0)
    info = {'title': play_title,
            'originaltitle': _usable_original(detail.get('original_title')),
            'plot': detail.get('overview', ''),
            'genre': ', '.join(g['name'] for g in detail.get('genres', [])),
            'year': int(play_year) if str(play_year).isdigit() else 0,
            'rating': detail.get('vote_average', 0),
            'votes': str(detail.get('vote_count', 0)),
            'duration': runtime * 60 if runtime else 0,
            'mediatype': 'movie'}
    if detail.get('id'):
        info['uniqueids'] = {'tmdb': str(detail['id'])}
    if detail.get('imdb_id'):
        # Unlocks subtitle addons (OpenSubtitles keys off imdbnumber)
        info['imdbnumber'] = detail['imdb_id']
    credits_ = detail.get('credits', {})
    directors = [c['name'] for c in credits_.get('crew', [])
                 if c.get('job') == 'Director']
    if directors:
        info['director'] = ', '.join(directors)
    writers = [c['name'] for c in credits_.get('crew', [])
               if c.get('department') == 'Writing']
    if writers:
        info['writer'] = ', '.join(writers[:3])
    studios = [s['name'] for s in detail.get('production_companies', [])]
    if studios:
        info['studio'] = ', '.join(studios[:3])
    art = {}
    poster = TMDB.poster_url(detail.get('poster_path'))
    fanart = TMDB.fanart_url(detail.get('backdrop_path'))
    if poster:
        art.update({'thumb': poster, 'poster': poster})
    if fanart:
        art['fanart'] = fanart
    return info, art, _tmdb_cast(credits_.get('cast', []))


def _fetch_movie_detail(tmdb_id):
    tmdb = get_tmdb()
    return tmdb.movie_detail(tmdb_id) if tmdb is not None else {}


def show_movie_info(tmdb_id, title, year):
    """Open Kodi's info screen for a movie.

    Reached by clicking a non-folder, non-playable list item, so Kodi runs us
    as a script rather than waiting for us — no busy dialog is up and opening
    the info screen here is safe. Its Play button picks a file to play.

    A stale favourite can still invoke this as a playable path; then Kodi is
    waiting for a URL, so go straight to the picker.
    """
    if HANDLE >= 0:
        play_pick(title, year=year)
        return

    progress = xbmcgui.DialogProgressBG()
    progress.create('Webshare.cz', L(30170).format(title))
    try:
        detail = _quiet(_fetch_movie_detail, tmdb_id) or {}
    finally:
        progress.close()

    info, art, cast_ = _movie_info_dict(detail, title, year)
    play_url = build_url(
        'play_pick', title=info['title'],
        original_title=_usable_original(detail.get('original_title')),
        year=(detail.get('release_date') or '')[:4] or year)

    li = _info_listitem(info['title'], play_url, info, art, cast_)
    xbmcgui.Dialog().info(li)


def show_tv_detail(tmdb_id, title, year):
    """Show TV series seasons."""
    tmdb = get_tmdb()
    if tmdb is None:
        _fail_directory()
        return

    detail = tmdb.tv_detail(tmdb_id)

    xbmcplugin.setContent(HANDLE, 'seasons')
    series_title = detail.get('name') or title
    original = detail.get('original_name', '')
    poster = TMDB.poster_url(detail.get('poster_path'))
    fanart = TMDB.fanart_url(detail.get('backdrop_path'))

    for season in detail.get('seasons', []):
        snum = season.get('season_number', 0)
        if snum == 0:
            continue  # skip "Specials"
        label = L(30182).format(snum)
        ep_count = season.get('episode_count', 0)
        if ep_count:
            label += ' ' + L(30183).format(ep_count)

        li = xbmcgui.ListItem(label)
        _set_video_info(li, {
            'title': label,
            'plot': season.get('overview', '') or detail.get('overview', ''),
            'tvshowtitle': series_title,
            'season': snum,
            'mediatype': 'season',
            'uniqueids': {'tmdb': str(tmdb_id)},
            'imdbnumber': (detail.get('external_ids') or {}).get('imdb_id', ''),
        }, _tmdb_cast(detail.get('credits', {}).get('cast', [])))
        sp = TMDB.poster_url(season.get('poster_path'))
        li.setArt({'thumb': sp or poster, 'poster': sp or poster, 'fanart': fanart})
        url = build_url('tv_season', tmdb_id=tmdb_id,
                        season=snum, title=series_title,
                        original_title=original)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)

    # Fallback: search entire series on webshare
    li = xbmcgui.ListItem('[B]{}[/B]'.format(L(30185)))
    li.setProperty('SpecialSort', 'bottom')
    li.setArt({'thumb': poster, 'poster': poster, 'fanart': fanart})
    url = build_url('ws_search_title', title=series_title, year=year,
                    original_title=_usable_original(original))
    xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)

    xbmcplugin.endOfDirectory(HANDLE)


def show_tv_season(tmdb_id, season, title, original_title=''):
    """Show episodes in a season."""
    tmdb = get_tmdb()
    if tmdb is None:
        _fail_directory()
        return

    snum = _int(season, 1)
    data = tmdb.tv_season(tmdb_id, snum)

    xbmcplugin.setContent(HANDLE, 'episodes')

    for ep in data.get('episodes', []):
        enum = ep.get('episode_number', 0)
        ep_title = ep.get('name', L(30184).format(enum))
        label = 'S{:02d}E{:02d} - {}'.format(snum, enum, ep_title)

        li = xbmcgui.ListItem(label)
        ep_info = {
            'title': ep_title,
            'plot': ep.get('overview', ''),
            'tvshowtitle': title,
            'season': snum,
            'episode': enum,
            'rating': ep.get('vote_average', 0),
            'mediatype': 'episode',
        }
        ep_directors = [c['name'] for c in ep.get('crew', [])
                        if c.get('job') == 'Director']
        if ep_directors:
            ep_info['director'] = ', '.join(ep_directors)
        _set_video_info(li, ep_info, _tmdb_cast(ep.get('guest_stars', [])))
        still = TMDB.fanart_url(ep.get('still_path'), 'w780')
        if still:
            li.setArt({'thumb': still, 'fanart': still})

        # Webshare names episodes by the original title far more often
        search_title = original_title or title
        li.addContextMenuItems(_search_context_items(
            search_title, '', season=snum, episode=enum))
        url = build_url('episode_info', tmdb_id=tmdb_id, season=snum,
                        episode=enum, title=search_title, show_title=title)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=False)

    xbmcplugin.endOfDirectory(HANDLE)


def _fetch_episode(tmdb_id, season, episode):
    tmdb = get_tmdb()
    if tmdb is None:
        return {}
    for ep in tmdb.tv_season(tmdb_id, season).get('episodes') or []:
        if ep.get('episode_number') == episode:
            return ep
    return {}


def show_episode_info(tmdb_id, season, episode, title, show_title=''):
    """Open Kodi's info screen for one episode.

    Same shape as show_movie_info — see there.
    """
    snum, enum = _int(season, 1), _int(episode, 1)
    if HANDLE >= 0:
        play_pick(title, season=snum, episode=enum)
        return

    tag = 'S{:02d}E{:02d}'.format(snum, enum)
    name = '{} {}'.format(show_title or title, tag)
    progress = xbmcgui.DialogProgressBG()
    progress.create('Webshare.cz', L(30170).format(name))
    try:
        detail = _quiet(_fetch_episode, tmdb_id, snum, enum) or {}
    finally:
        progress.close()

    ep_title = detail.get('name') or L(30184).format(enum)
    info = {'title': '{} - {}'.format(tag, ep_title),
            'plot': detail.get('overview', ''),
            'tvshowtitle': show_title or title,
            'season': snum, 'episode': enum,
            'rating': detail.get('vote_average', 0),
            'mediatype': 'episode'}
    directors = [c['name'] for c in detail.get('crew') or []
                 if c.get('job') == 'Director']
    if directors:
        info['director'] = ', '.join(directors)

    # Series regulars first (cached when the show was listed), guests after
    cast_ = _credits_cast(_load_credits_cache().get(_credits_key('tv', tmdb_id)))
    cast_ += _tmdb_cast(detail.get('guest_stars') or [])
    for i, member in enumerate(cast_):
        member['order'] = i
    still = TMDB.fanart_url(detail.get('still_path'), 'w780')
    art = {'thumb': still, 'fanart': still} if still else {}

    play_url = build_url('play_pick', title=title, season=snum, episode=enum)
    li = _info_listitem(info['title'], play_url, info, art, cast_)
    xbmcgui.Dialog().info(li)


# ---------------------------------------------------------------------------
# TMDB search
# ---------------------------------------------------------------------------

def search_input():
    query = xbmcgui.Dialog().input(L(30171))
    if query:
        add_to_history(query)
        do_search(query)
    else:
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)


def do_search(query, page=1):
    tmdb = get_tmdb()
    if tmdb is None:
        _fail_directory()
        return

    data = tmdb.search_multi(query, page=_int(page, 1))

    xbmcplugin.setContent(HANDLE, 'videos')
    added = 0
    creds = prefetch_credits(data.get('results', []))
    for item in data.get('results', []):
        mt = item.get('media_type', '')
        if mt == 'movie':
            _add_movie_item(item, creds.get((mt, item['id'])))
        elif mt == 'tv':
            _add_tv_item(item, creds.get((mt, item['id'])))
        else:
            continue  # persons are not playable content
        added += 1

    if not added:
        xbmcgui.Dialog().notification(
            'Webshare.cz', L(30163).format(query),
            xbmcgui.NOTIFICATION_INFO)

    _add_page_items(data, 'tmdb_search', {'query': query})
    _enable_sort_methods()
    xbmcplugin.endOfDirectory(HANDLE)


# ---------------------------------------------------------------------------
# Webshare direct search
# ---------------------------------------------------------------------------

def ws_search_input():
    query = xbmcgui.Dialog().input(L(30172))
    if query:
        do_ws_search(query)
    else:
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)


def do_ws_search(query, offset=0):
    ws = get_webshare()
    if ws is None:
        _fail_directory()
        return

    limit = 25
    results, total = ws.search(query, offset=_int(offset), limit=limit)

    xbmcplugin.setContent(HANDLE, 'videos')
    for item in results:
        name = item['name']
        if not any(name.lower().endswith(ext) for ext in VIDEO_EXTENSIONS):
            continue
        li = xbmcgui.ListItem(name)
        _set_video_info(li, {'title': name})
        li.setProperty('IsPlayable', 'true')
        li.addContextMenuItems(_stream_context_items(item))
        if item['img']:
            li.setArt({'thumb': item['img'], 'icon': item['img']})
        li.setLabel2(item['size_str'])
        url = build_url('play', ident=item['ident'], name=name)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=False)

    current_offset = _int(offset)
    if current_offset + limit < total:
        # No page numbers: `total` counts non-video files too, so a
        # computed page count would only mislead
        li = xbmcgui.ListItem(L(30180))
        li.setArt({'icon': 'DefaultFolder.png'})
        li.setProperty('SpecialSort', 'bottom')
        url = build_url('ws_search', query=query, offset=current_offset + limit)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)

    xbmcplugin.endOfDirectory(HANDLE)


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def show_history():
    xbmcplugin.setContent(HANDLE, 'videos')
    history = load_history()
    for query in history:
        li = xbmcgui.ListItem(query)
        li.setArt({'icon': 'DefaultAddonsSearch.png'})
        cm = [(L(30144),
               'RunPlugin({})'.format(build_url('remove_history', query=query)))]
        li.addContextMenuItems(cm)
        url = build_url('tmdb_search', query=query, page=1)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)

    if history:
        li = xbmcgui.ListItem('[I]{}[/I]'.format(L(30173)))
        li.setProperty('SpecialSort', 'bottom')
        li.setArt({'icon': 'DefaultIconInfo.png'})
        xbmcplugin.addDirectoryItem(HANDLE, build_url('clear_history'), li, isFolder=False)

    xbmcplugin.endOfDirectory(HANDLE)


def remove_history(query):
    history = load_history()
    if query in history:
        history.remove(query)
        save_history(history)
    xbmc.executebuiltin('Container.Refresh')


def clear_history():
    if xbmcgui.Dialog().yesno('Webshare.cz', L(30174)):
        save_history([])
        xbmc.executebuiltin('Container.Refresh')


def clear_cache():
    """Wipe cached TMDB data (settings button). History stays."""
    Cache(TMDB_CACHE_DIR).prune(0)
    try:
        os.remove(CREDITS_CACHE_FILE)
    except OSError:
        pass
    xbmcgui.Dialog().notification('Webshare.cz', L(30175),
                                  xbmcgui.NOTIFICATION_INFO)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def _dispatch(action, params):
    if action is None:
        main_menu()

    # TMDB browsing
    elif action == 'trending':
        show_trending(params.get('page', 1))
    elif action == 'movies_now_playing':
        show_movies('now_playing', params.get('page', 1))
    elif action == 'movies_popular':
        show_movies('popular', params.get('page', 1))
    elif action == 'movies_top_rated':
        show_movies('top_rated', params.get('page', 1))
    elif action == 'tv_popular':
        show_tv('popular', params.get('page', 1))
    elif action == 'tv_top_rated':
        show_tv('top_rated', params.get('page', 1))
    elif action == 'genres_movies':
        show_genres('movie')
    elif action == 'genres_tv':
        show_genres('tv')
    elif action == 'genre_list':
        show_genre_list(params.get('media_type', 'movie'),
                        params.get('genre_id', ''),
                        params.get('genre_name', ''),
                        params.get('page', 1),
                        params.get('sort_by', 'popularity.desc'))
    elif action == 'genre_sort':
        mt = params.get('media_type', 'movie')
        new_sort = _pick_sort(mt, params.get('current_sort', 'popularity.desc'))
        if new_sort:
            show_genre_list(mt, params.get('genre_id', ''),
                            params.get('genre_name', ''),
                            page=1, sort_by=new_sort, update_listing=True)
        else:
            _fail_directory()
    elif action == 'years_movies':
        show_years('movie')
    elif action == 'years_tv':
        show_years('tv')
    elif action == 'year_list':
        show_year_list(params.get('media_type', 'movie'),
                       params.get('year', ''),
                       params.get('page', 1),
                       params.get('sort_by', 'popularity.desc'))
    elif action == 'year_sort':
        mt = params.get('media_type', 'movie')
        new_sort = _pick_sort(mt, params.get('current_sort', 'popularity.desc'))
        if new_sort:
            show_year_list(mt, params.get('year', ''),
                           page=1, sort_by=new_sort, update_listing=True)
        else:
            _fail_directory()

    # TMDB detail
    elif action == 'movie_info':
        show_movie_info(params.get('tmdb_id'),
                        params.get('title', ''),
                        params.get('year', ''))
    elif action == 'episode_info':
        show_episode_info(params.get('tmdb_id'),
                          params.get('season', 1),
                          params.get('episode', 1),
                          params.get('title', ''),
                          params.get('show_title', ''))
    elif action == 'tv_detail':
        show_tv_detail(params.get('tmdb_id'),
                       params.get('title', ''),
                       params.get('year', ''))
    elif action == 'tv_season':
        show_tv_season(params.get('tmdb_id'), params.get('season', 1),
                       params.get('title', ''),
                       params.get('original_title', ''))

    # TMDB search
    elif action == 'search_input':
        search_input()
    elif action == 'tmdb_search':
        do_search(params.get('query', ''), params.get('page', 1))

    # Webshare title search (from movie/tv detail)
    elif action == 'ws_search_title':
        search_webshare_for_title(params.get('title', ''),
                                  params.get('original_title', ''),
                                  params.get('year', ''),
                                  params.get('season'),
                                  params.get('episode'))
    # Webshare direct search
    elif action == 'ws_search_input':
        ws_search_input()
    elif action == 'ws_search':
        do_ws_search(params.get('query', ''), params.get('offset', 0))

    # Playback
    elif action == 'play':
        play_webshare(params.get('ident', ''), params.get('name', ''))
    elif action == 'play_direct':
        _play_direct(params.get('ident', ''), params.get('name', ''),
                     params.get('dt', ''))
    elif action == 'play_pick':
        play_pick(params.get('title', ''),
                  params.get('original_title', ''),
                  params.get('year', ''),
                  params.get('season'), params.get('episode'))

    # History
    elif action == 'history':
        show_history()
    elif action == 'remove_history':
        remove_history(params.get('query', ''))
    elif action == 'clear_history':
        clear_history()

    # Maintenance (reachable from settings)
    elif action == 'clear_cache':
        clear_cache()

    else:
        main_menu()


def router():
    # With reuselanguageinvoker the module survives between invocations,
    # so anything derived from sys.argv must be refreshed here
    global HANDLE, BASE_URL
    try:
        HANDLE = int(sys.argv[1])
    except (IndexError, ValueError):
        HANDLE = -1
    BASE_URL = sys.argv[0] if sys.argv else ''

    params = dict(parse_qsl(sys.argv[2][1:] if len(sys.argv) > 2 else ''))
    action = params.get('action')
    try:
        _dispatch(action, params)
    except (TMDBError, WebshareAPIError) as e:
        # A notification, not a modal: Kodi may be holding its busy dialog
        # open while it waits for this listing.
        xbmcgui.Dialog().notification('Webshare.cz', str(e),
                                      xbmcgui.NOTIFICATION_ERROR)
        _fail_directory()
    except Exception:
        xbmc.log('plugin.video.webshare: unhandled error in action {}\n{}'
                 .format(action, traceback.format_exc()), xbmc.LOGERROR)
        xbmcgui.Dialog().notification('Webshare.cz', L(30176),
                                      xbmcgui.NOTIFICATION_ERROR)
        _fail_directory()


if __name__ == '__main__':
    router()
