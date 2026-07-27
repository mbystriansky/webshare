# -*- coding: utf-8 -*-
"""Kodi plugin for browsing TMDB catalogue and streaming from webshare.cz."""

import json
import os
import re
import sys
from urllib.parse import parse_qsl, urlencode, quote_plus

import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

from resources.lib.tmdb import TMDB, TMDBError, is_unrenderable
from resources.lib.webshare import WebshareAPI, WebshareAPIError

ADDON = xbmcaddon.Addon()
HANDLE = int(sys.argv[1])
BASE_URL = sys.argv[0]
PROFILE_DIR = xbmcvfs.translatePath(ADDON.getAddonInfo('profile'))
HISTORY_FILE = os.path.join(PROFILE_DIR, 'search_history.json')
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
    if _tmdb is not None:
        return _tmdb

    # 1) settings  2) file on disk
    api_key = ADDON.getSetting('tmdb_api_key') or _read_api_key_from_file()

    if not api_key:
        xbmcgui.Dialog().ok(
            'Webshare.cz',
            'Zadajte TMDB API kľúč v nastaveniach doplnku,\n'
            'alebo ho uložte do súboru tmdb_api_key.txt\n'
            '(napr. /sdcard/tmdb_api_key.txt na Androide).\n\n'
            'Kľúč získate zadarmo na themoviedb.org.'
        )
        ADDON.openSettings()
        api_key = ADDON.getSetting('tmdb_api_key') or _read_api_key_from_file()
        if not api_key:
            return None
    lang = ADDON.getSetting('tmdb_language') or 'cs-CZ'
    _tmdb = TMDB(api_key, language=lang)
    return _tmdb


def get_webshare():
    global _webshare
    if _webshare is not None:
        return _webshare
    api = WebshareAPI()
    username = ADDON.getSetting('username')
    password = ADDON.getSetting('password')
    if not username or not password:
        xbmcgui.Dialog().ok(
            'Webshare.cz',
            'Zadajte prihlasovacie údaje v nastaveniach doplnku.'
        )
        ADDON.openSettings()
        username = ADDON.getSetting('username')
        password = ADDON.getSetting('password')
        if not username or not password:
            return None
    try:
        api.login(username, password)
    except WebshareAPIError as e:
        xbmcgui.Dialog().ok('Webshare.cz - Chyba', str(e))
        return None
    _webshare = api
    return api


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def build_url(action, **kwargs):
    kwargs['action'] = action
    return '{}?{}'.format(BASE_URL, urlencode(kwargs))


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
    os.makedirs(PROFILE_DIR, exist_ok=True)
    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history[:MAX_HISTORY], f, ensure_ascii=False)


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
        os.makedirs(PROFILE_DIR, exist_ok=True)
        with open(CREDITS_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False)
    except (IOError, OSError):
        pass


def _distill_credits(data):
    """Keep only what list items need, so the cache stays small."""
    directors = [c['name'] for c in data.get('crew', [])
                 if c.get('job') == 'Director' and c.get('name')]
    cast = [{'name': c.get('name', ''), 'character': c.get('character', ''),
             'profile_path': c.get('profile_path')}
            for c in data.get('cast', [])[:15] if c.get('name')]
    return {'directors': directors, 'cast': cast}


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
    lang = ADDON.getSetting('tmdb_language') or 'cs-CZ'
    keys = {(mt, tid): '{}:{}:{}'.format(mt, tid, lang) for mt, tid in wanted}

    out = {}
    missing = []
    for mt, tid in wanted:
        cached = cache.get(keys[(mt, tid)])
        if cached is None:
            missing.append((mt, tid))
        else:
            out[tid] = cached

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
                out[entry[1]] = credits_
                cache[keys[entry]] = credits_
        _save_credits_cache(cache)

    return out


def _apply_credits(li, credits_, info):
    """Attach director + cast from prefetched credits to a list item."""
    if not credits_:
        return
    if credits_.get('directors'):
        info['director'] = ', '.join(credits_['directors'])
    cast = [{'name': c['name'], 'role': c.get('character', ''),
             'thumbnail': TMDB.poster_url(c.get('profile_path'), 'w185'),
             'order': i}
            for i, c in enumerate(credits_.get('cast', []))]
    if cast:
        li.setCast(cast)


def _usable_original(title):
    """Original title, unless it is in a script Kodi cannot display."""
    return '' if is_unrenderable(title) else (title or '')


def _search_context_items(title, original='', year='', season=None, episode=None):
    """Context menu entries for playing / browsing webshare files."""
    original = _usable_original(original)
    extra = {}
    if season is not None and episode is not None:
        extra = {'season': season, 'episode': episode}
    items = [
        ('Prehrať – vybrať súbor', 'RunPlugin({})'.format(
            build_url('play_pick', title=title, original_title=original,
                      year=year, **extra))),
        ('Prehliadať súbory na Webshare', 'Container.Update({})'.format(
            build_url('ws_search_title', title=title, year=year, **extra))),
    ]
    if original and original.lower() != title.lower():
        items.append(('Prehliadať súbory (originál)', 'Container.Update({})'.format(
            build_url('ws_search_title', title=original, year=year, **extra))))
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
    info = {'title': label, 'plot': item.get('overview', ''),
            'originaltitle': _usable_original(item.get('original_title')),
            'year': int(year) if year.isdigit() else 0,
            'rating': rating, 'votes': item.get('vote_count', 0),
            'mediatype': 'movie'}
    _apply_credits(li, credits_, info)
    li.setInfo('video', info)

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
    info = {'title': label, 'plot': item.get('overview', ''),
            'originaltitle': _usable_original(item.get('original_name')),
            'year': int(year) if year.isdigit() else 0,
            'rating': rating, 'votes': item.get('vote_count', 0),
            'mediatype': 'tvshow'}
    _apply_credits(li, credits_, info)
    li.setInfo('video', info)

    poster = TMDB.poster_url(item.get('poster_path'))
    fanart = TMDB.fanart_url(item.get('backdrop_path'))
    li.setArt({'thumb': poster, 'poster': poster, 'fanart': fanart})
    original = item.get('original_name', '')
    ctx = [('Informácie', 'RunPlugin({})'.format(
        build_url('tv_info', tmdb_id=item['id'], title=title, year=year)))]
    ctx += _search_context_items(title, original, year)
    li.addContextMenuItems(ctx)
    return li, title, year


def _enable_sort_methods():
    """Enable sorting by title, year and rating in Kodi's view menu."""
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_UNSORTED)
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_TITLE)
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_VIDEO_YEAR)
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_VIDEO_RATING)


# TMDB discover sort options — maps UI labels to TMDB API sort_by values
SORT_OPTIONS = [
    ('Popularita', 'popularity.desc'),
    ('Hodnotenie', 'vote_average.desc'),
    ('Počet hodnotení', 'vote_count.desc'),
    ('Rok (najnovšie)', 'primary_release_date.desc'),
    ('Rok (najstaršie)', 'primary_release_date.asc'),
]

SORT_OPTIONS_TV = [
    ('Popularita', 'popularity.desc'),
    ('Hodnotenie', 'vote_average.desc'),
    ('Počet hodnotení', 'vote_count.desc'),
    ('Rok (najnovšie)', 'first_air_date.desc'),
    ('Rok (najstaršie)', 'first_air_date.asc'),
]


def _sort_label(media_type, sort_by):
    """Return human-readable label for a TMDB sort_by value."""
    options = SORT_OPTIONS if media_type == 'movie' else SORT_OPTIONS_TV
    for label, value in options:
        if value == sort_by:
            return label
    return 'Popularita'


def _pick_sort(media_type, current_sort='popularity.desc'):
    """Show a sort selection dialog. Returns TMDB sort_by value or None if cancelled."""
    options = SORT_OPTIONS if media_type == 'movie' else SORT_OPTIONS_TV
    labels = [o[0] for o in options]
    # pre-select current
    preselect = 0
    for i, o in enumerate(options):
        if o[1] == current_sort:
            preselect = i
            break
    idx = xbmcgui.Dialog().select('Zoradiť podľa', labels, preselect=preselect)
    if idx < 0:
        return None
    return options[idx][1]


def _add_page_items(data, action, extra_params=None):
    """Add next/previous page navigation items."""
    page = data.get('page', 1)
    total = data.get('total_pages', 1)
    params = extra_params or {}
    if page < total:
        li = xbmcgui.ListItem('Ďalšia strana ({}/{})'.format(page + 1, total))
        li.setArt({'icon': 'DefaultFolder.png'})
        xbmcplugin.addDirectoryItem(
            HANDLE, build_url(action, page=page + 1, **params), li, isFolder=True)


# ---------------------------------------------------------------------------
# Webshare search for a title
# ---------------------------------------------------------------------------

def _build_search_queries(title, year='', season=None, episode=None):
    """Build search queries for webshare from a title."""
    clean = re.sub(r'[^\w\s]', '', title).strip()
    queries = []
    if season is not None and episode is not None:
        ep_tag = 'S{:02d}E{:02d}'.format(int(season), int(episode))
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
        except WebshareAPIError:
            continue
        for item in results:
            if item['ident'] not in seen_idents:
                name = item['name']
                if any(name.lower().endswith(ext) for ext in VIDEO_EXTENSIONS):
                    all_results.append(item)
                    seen_idents.add(item['ident'])
    return all_results


def search_webshare_for_title(title, year='', season=None, episode=None):
    """Search webshare for a movie/episode title and list results as directory."""
    all_results = _ws_collect_results(title, year, season, episode)
    if all_results is None:
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return

    if not all_results:
        xbmcgui.Dialog().ok('Webshare.cz', 'Žiadne výsledky pre: {}'.format(title))
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return

    xbmcplugin.setContent(HANDLE, 'videos')
    for r in all_results:
        label = '{} [{}]'.format(r['name'], r['size_str'])
        li = xbmcgui.ListItem(label)
        li.setInfo('video', {'title': r['name']})
        li.setProperty('IsPlayable', 'true')
        if r['img']:
            li.setArt({'thumb': r['img'], 'icon': r['img']})
        url = build_url('play', ident=r['ident'], name=r['name'])
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=False)

    xbmcplugin.endOfDirectory(HANDLE)


def _add_stream_headers(link):
    """Append HTTP headers to a streaming URL for Kodi (pipe syntax)."""
    headers = (
        'User-Agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        '&Referer=https://webshare.cz/'
    )
    return '{}|{}'.format(link, headers)


def _play_direct(ident, name=''):
    """Resolve webshare link and play directly via xbmc.Player (for dialog flows)."""
    import xbmc
    ws = get_webshare()
    if ws is None:
        return
    try:
        link = ws.get_file_link(ident)
    except WebshareAPIError as e:
        xbmcgui.Dialog().ok('Webshare.cz - Chyba', str(e))
        return
    stream_url = _add_stream_headers(link)
    li = xbmcgui.ListItem(name or 'Video', path=stream_url)
    xbmc.Player().play(stream_url, li)


def play_webshare(ident, name=''):
    """Resolve and play a webshare file (for IsPlayable items via setResolvedUrl)."""
    ws = get_webshare()
    if ws is None:
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
        return
    try:
        link = ws.get_file_link(ident)
    except WebshareAPIError as e:
        xbmcgui.Dialog().ok('Webshare.cz - Chyba', str(e))
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
        return

    stream_url = _add_stream_headers(link)
    li = xbmcgui.ListItem(name or 'Video', path=stream_url)
    xbmcplugin.setResolvedUrl(HANDLE, True, li)


def play_pick(title, original_title='', year='', season=None, episode=None):
    """Search webshare, let the user pick a file in a dialog and play it.

    Reached both from the info dialog's Play button (Kodi waits for
    setResolvedUrl) and from context menus via RunPlugin (no handle).
    """
    def abort():
        if HANDLE >= 0:
            xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())

    progress = xbmcgui.DialogProgressBG()
    progress.create('Webshare.cz', 'Hľadám súbory: {}'.format(title))
    try:
        results = _ws_collect_results(title, year, season, episode)
        if results is None:
            abort()
            return
        if not results and original_title \
                and original_title.lower() != title.lower():
            progress.update(50, message='Skúšam originálny názov: {}'.format(
                original_title))
            results = _ws_collect_results(original_title, year,
                                          season, episode) or []
    finally:
        progress.close()

    if not results:
        xbmcgui.Dialog().ok('Webshare.cz',
                            'Žiadne výsledky pre: {}'.format(title))
        abort()
        return

    labels = ['{} [{}]'.format(r['name'], r['size_str']) for r in results]
    idx = xbmcgui.Dialog().select('Vyber súbor: {}'.format(title), labels)
    if idx < 0:
        abort()
        return

    ident, name = results[idx]['ident'], results[idx]['name']
    if HANDLE >= 0:
        play_webshare(ident, name)
    else:
        _play_direct(ident, name)


# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------

def main_menu():
    xbmcplugin.setContent(HANDLE, 'videos')
    items = [
        ('[B]Hľadať[/B]', 'search_input', 'DefaultAddonsSearch.png'),
        ('Trending', 'trending', 'DefaultRecentlyAddedMovies.png'),
        ('Novinky v kinách', 'movies_now_playing', 'DefaultRecentlyAddedMovies.png'),
        ('Populárne filmy', 'movies_popular', 'DefaultMovies.png'),
        ('Top hodnotené filmy', 'movies_top_rated', 'DefaultMovies.png'),
        ('Populárne seriály', 'tv_popular', 'DefaultTVShows.png'),
        ('Top hodnotené seriály', 'tv_top_rated', 'DefaultTVShows.png'),
        ('Žánre - filmy', 'genres_movies', 'DefaultGenre.png'),
        ('Žánre - seriály', 'genres_tv', 'DefaultGenre.png'),
        ('Podľa roku - filmy', 'years_movies', 'DefaultYear.png'),
        ('Podľa roku - seriály', 'years_tv', 'DefaultYear.png'),
        ('Webshare - priame hľadanie', 'ws_search_input', 'DefaultAddonsSearch.png'),
    ]
    for label, action, icon in items:
        li = xbmcgui.ListItem(label)
        li.setArt({'icon': icon})
        xbmcplugin.addDirectoryItem(HANDLE, build_url(action), li, isFolder=True)

    # Search history
    history = load_history()
    if history:
        li = xbmcgui.ListItem('[I]--- História ---[/I]')
        li.setArt({'icon': 'DefaultAddonsSearch.png'})
        xbmcplugin.addDirectoryItem(HANDLE, build_url('history'), li, isFolder=True)

    xbmcplugin.endOfDirectory(HANDLE)


# ---------------------------------------------------------------------------
# TMDB catalogue screens
# ---------------------------------------------------------------------------

def show_trending(page=1):
    tmdb = get_tmdb()
    if tmdb is None:
        return
    try:
        data = tmdb.trending(page=int(page))
    except TMDBError as e:
        xbmcgui.Dialog().ok('TMDB Chyba', str(e))
        return

    xbmcplugin.setContent(HANDLE, 'videos')
    creds = prefetch_credits(data.get('results', []))
    for item in data.get('results', []):
        mt = item.get('media_type', 'movie')
        if mt == 'movie':
            li, title, year = _movie_listitem(item, creds.get(item['id']))
            url = build_url('movie_detail', tmdb_id=item['id'],
                            title=title, year=year)
            xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)
        elif mt == 'tv':
            li, title, year = _tv_listitem(item, creds.get(item['id']))
            url = build_url('tv_detail', tmdb_id=item['id'],
                            title=title, year=year)
            xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)
    _add_page_items(data, 'trending')
    _enable_sort_methods()
    xbmcplugin.endOfDirectory(HANDLE)


def show_movies(category, page=1):
    tmdb = get_tmdb()
    if tmdb is None:
        return
    method = {'now_playing': tmdb.movies_now_playing,
              'popular': tmdb.movies_popular,
              'top_rated': tmdb.movies_top_rated}
    try:
        data = method[category](page=int(page))
    except TMDBError as e:
        xbmcgui.Dialog().ok('TMDB Chyba', str(e))
        return

    xbmcplugin.setContent(HANDLE, 'movies')
    creds = prefetch_credits(data.get('results', []), 'movie')
    for item in data.get('results', []):
        li, title, year = _movie_listitem(item, creds.get(item['id']))
        url = build_url('movie_detail', tmdb_id=item['id'],
                        title=title, year=year)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)
    _add_page_items(data, 'movies_' + category)
    _enable_sort_methods()
    xbmcplugin.endOfDirectory(HANDLE)


def show_tv(category, page=1):
    tmdb = get_tmdb()
    if tmdb is None:
        return
    method = {'popular': tmdb.tv_popular,
              'top_rated': tmdb.tv_top_rated}
    try:
        data = method[category](page=int(page))
    except TMDBError as e:
        xbmcgui.Dialog().ok('TMDB Chyba', str(e))
        return

    xbmcplugin.setContent(HANDLE, 'tvshows')
    creds = prefetch_credits(data.get('results', []), 'tv')
    for item in data.get('results', []):
        li, title, year = _tv_listitem(item, creds.get(item['id']))
        url = build_url('tv_detail', tmdb_id=item['id'],
                        title=title, year=year)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)
    _add_page_items(data, 'tv_' + category)
    _enable_sort_methods()
    xbmcplugin.endOfDirectory(HANDLE)


# ---------------------------------------------------------------------------
# Genres
# ---------------------------------------------------------------------------

def show_genres(media_type):
    tmdb = get_tmdb()
    if tmdb is None:
        return
    try:
        data = (tmdb.movie_genres() if media_type == 'movie'
                else tmdb.tv_genres())
    except TMDBError as e:
        xbmcgui.Dialog().ok('TMDB Chyba', str(e))
        return

    xbmcplugin.setContent(HANDLE, 'videos')
    for genre in data.get('genres', []):
        li = xbmcgui.ListItem(genre['name'])
        li.setArt({'icon': 'DefaultGenre.png'})
        url = build_url('genre_list', media_type=media_type,
                        genre_id=genre['id'], genre_name=genre['name'])
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)
    xbmcplugin.endOfDirectory(HANDLE)


def show_genre_list(media_type, genre_id, genre_name, page=1,
                    sort_by='popularity.desc'):
    tmdb = get_tmdb()
    if tmdb is None:
        return
    discover_params = {'with_genres': genre_id, 'sort_by': sort_by}
    if sort_by == 'vote_average.desc':
        discover_params['vote_count.gte'] = 50
    try:
        if media_type == 'movie':
            data = tmdb.discover_movies(page=int(page), **discover_params)
        else:
            data = tmdb.discover_tv(page=int(page), **discover_params)
    except TMDBError as e:
        xbmcgui.Dialog().ok('TMDB Chyba', str(e))
        return

    content = 'movies' if media_type == 'movie' else 'tvshows'
    xbmcplugin.setContent(HANDLE, content)

    # Sort button at the top
    sort_li = xbmcgui.ListItem('[B]Zoradiť: {}[/B]'.format(
        _sort_label(media_type, sort_by)))
    sort_li.setArt({'icon': 'DefaultAddSource.png'})
    xbmcplugin.addDirectoryItem(
        HANDLE, build_url('genre_sort', media_type=media_type,
                          genre_id=genre_id, genre_name=genre_name,
                          current_sort=sort_by),
        sort_li, isFolder=True)

    creds = prefetch_credits(data.get('results', []), media_type)
    for item in data.get('results', []):
        if media_type == 'movie':
            li, title, year = _movie_listitem(item, creds.get(item['id']))
            url = build_url('movie_detail', tmdb_id=item['id'],
                            title=title, year=year)
        else:
            li, title, year = _tv_listitem(item, creds.get(item['id']))
            url = build_url('tv_detail', tmdb_id=item['id'],
                            title=title, year=year)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)
    _add_page_items(data, 'genre_list',
                    {'media_type': media_type, 'genre_id': genre_id,
                     'genre_name': genre_name, 'sort_by': sort_by})
    _enable_sort_methods()
    xbmcplugin.endOfDirectory(HANDLE)


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


def show_year_list(media_type, year, page=1, sort_by='popularity.desc'):
    """Show movies or TV shows from a specific year using TMDB discover."""
    tmdb = get_tmdb()
    if tmdb is None:
        return
    discover_params = {'sort_by': sort_by}
    if sort_by == 'vote_average.desc':
        discover_params['vote_count.gte'] = 50
    if media_type == 'movie':
        discover_params['primary_release_year'] = int(year)
    else:
        discover_params['first_air_date_year'] = int(year)
    try:
        if media_type == 'movie':
            data = tmdb.discover_movies(page=int(page), **discover_params)
        else:
            data = tmdb.discover_tv(page=int(page), **discover_params)
    except TMDBError as e:
        xbmcgui.Dialog().ok('TMDB Chyba', str(e))
        return

    content = 'movies' if media_type == 'movie' else 'tvshows'
    xbmcplugin.setContent(HANDLE, content)

    # Sort button at the top
    sort_li = xbmcgui.ListItem('[B]Zoradiť: {}[/B]'.format(
        _sort_label(media_type, sort_by)))
    sort_li.setArt({'icon': 'DefaultAddSource.png'})
    xbmcplugin.addDirectoryItem(
        HANDLE, build_url('year_sort', media_type=media_type,
                          year=year, current_sort=sort_by),
        sort_li, isFolder=True)

    creds = prefetch_credits(data.get('results', []), media_type)
    for item in data.get('results', []):
        if media_type == 'movie':
            li, title, yr = _movie_listitem(item, creds.get(item['id']))
            url = build_url('movie_detail', tmdb_id=item['id'],
                            title=title, year=yr)
        else:
            li, title, yr = _tv_listitem(item, creds.get(item['id']))
            url = build_url('tv_detail', tmdb_id=item['id'],
                            title=title, year=yr)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)
    _add_page_items(data, 'year_list',
                    {'media_type': media_type, 'year': year,
                     'sort_by': sort_by})
    _enable_sort_methods()
    xbmcplugin.endOfDirectory(HANDLE)


# ---------------------------------------------------------------------------
# Movie / TV detail
# ---------------------------------------------------------------------------

def show_movie_detail(tmdb_id, title, year):
    """Open Kodi's video info dialog for a movie; Play resolves via webshare.

    Credits are fetched here rather than when building the list, so browsing
    stays fast.
    """
    tmdb = get_tmdb()
    if tmdb is None:
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return

    progress = xbmcgui.DialogProgressBG()
    progress.create('TMDB', 'Načítavam: {}'.format(title))
    try:
        detail = tmdb.movie_detail(tmdb_id)
    except TMDBError:
        detail = {}
    finally:
        progress.close()

    play_title = detail.get('title') or title
    play_year = (detail.get('release_date') or '')[:4] or year
    original = _usable_original(detail.get('original_title'))

    url = build_url('play_pick', title=play_title, original_title=original,
                    year=play_year)
    li = xbmcgui.ListItem(play_title, path=url)
    li.setProperty('IsPlayable', 'true')

    runtime = detail.get('runtime', 0)
    info = {'title': play_title,
            'originaltitle': original,
            'plot': detail.get('overview', ''),
            'genre': ', '.join(g['name'] for g in detail.get('genres', [])),
            'year': int(play_year) if play_year.isdigit() else 0,
            'rating': detail.get('vote_average', 0),
            'votes': str(detail.get('vote_count', 0)),
            'duration': runtime * 60 if runtime else 0,
            'mediatype': 'movie'}
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
    li.setInfo('video', info)
    cast_ = _tmdb_cast(credits_.get('cast', []))
    if cast_:
        li.setCast(cast_)

    poster = TMDB.poster_url(detail.get('poster_path'))
    fanart = TMDB.fanart_url(detail.get('backdrop_path'))
    li.setArt({'thumb': poster, 'poster': poster, 'fanart': fanart})

    xbmcgui.Dialog().info(li)
    # Nothing to list — the dialog above is the whole interaction
    xbmcplugin.endOfDirectory(HANDLE, succeeded=False)


def show_tv_info(tmdb_id, title, year):
    """Open Kodi's video info dialog for a TV series (context menu action)."""
    tmdb = get_tmdb()
    if tmdb is None:
        return

    progress = xbmcgui.DialogProgressBG()
    progress.create('TMDB', 'Načítavam: {}'.format(title))
    try:
        detail = tmdb.tv_detail(tmdb_id)
    except TMDBError:
        detail = {}
    finally:
        progress.close()

    series_title = detail.get('name') or title
    first_air = (detail.get('first_air_date') or '')[:4] or year

    li = xbmcgui.ListItem(series_title)
    info = {'title': series_title,
            'originaltitle': _usable_original(detail.get('original_name')),
            'plot': detail.get('overview', ''),
            'genre': ', '.join(g['name'] for g in detail.get('genres', [])),
            'year': int(first_air) if str(first_air).isdigit() else 0,
            'rating': detail.get('vote_average', 0),
            'votes': str(detail.get('vote_count', 0)),
            'status': detail.get('status', ''),
            'mediatype': 'tvshow'}
    creators = [c['name'] for c in detail.get('created_by', [])]
    if creators:
        info['director'] = ', '.join(creators)
    studios = [n['name'] for n in detail.get('networks', [])]
    if studios:
        info['studio'] = ', '.join(studios[:3])
    li.setInfo('video', info)
    cast_ = _tmdb_cast(detail.get('credits', {}).get('cast', []))
    if cast_:
        li.setCast(cast_)

    poster = TMDB.poster_url(detail.get('poster_path'))
    fanart = TMDB.fanart_url(detail.get('backdrop_path'))
    li.setArt({'thumb': poster, 'poster': poster, 'fanart': fanart})

    xbmcgui.Dialog().info(li)


def show_tv_detail(tmdb_id, title, year):
    """Show TV series seasons."""
    tmdb = get_tmdb()
    if tmdb is None:
        return

    try:
        detail = tmdb.tv_detail(tmdb_id)
    except TMDBError as e:
        xbmcgui.Dialog().ok('TMDB Chyba', str(e))
        return

    xbmcplugin.setContent(HANDLE, 'seasons')
    series_title = detail.get('name') or title
    original = detail.get('original_name', '')
    poster = TMDB.poster_url(detail.get('poster_path'))
    fanart = TMDB.fanart_url(detail.get('backdrop_path'))

    for season in detail.get('seasons', []):
        snum = season.get('season_number', 0)
        if snum == 0:
            continue  # skip "Specials"
        label = 'Séria {}'.format(snum)
        ep_count = season.get('episode_count', 0)
        if ep_count:
            label += ' ({} epizód)'.format(ep_count)

        li = xbmcgui.ListItem(label)
        li.setInfo('video', {
            'title': label,
            'plot': season.get('overview', '') or detail.get('overview', ''),
            'tvshowtitle': series_title,
            'season': snum,
            'mediatype': 'season',
        })
        show_cast = _tmdb_cast(detail.get('credits', {}).get('cast', []))
        if show_cast:
            li.setCast(show_cast)
        sp = TMDB.poster_url(season.get('poster_path'))
        li.setArt({'thumb': sp or poster, 'poster': sp or poster, 'fanart': fanart})
        url = build_url('tv_season', tmdb_id=tmdb_id,
                        season=snum, title=series_title,
                        original_title=original)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)

    # Fallback: search entire series on webshare
    li = xbmcgui.ListItem('[B]Hľadať celý seriál na Webshare[/B]')
    li.setArt({'thumb': poster, 'poster': poster, 'fanart': fanart})
    url = build_url('ws_search_title', title=series_title, year=year)
    xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)

    xbmcplugin.endOfDirectory(HANDLE)


def show_tv_season(tmdb_id, season, title, original_title=''):
    """Show episodes in a season."""
    tmdb = get_tmdb()
    if tmdb is None:
        return

    try:
        data = tmdb.tv_season(tmdb_id, int(season))
    except TMDBError as e:
        xbmcgui.Dialog().ok('TMDB Chyba', str(e))
        return

    xbmcplugin.setContent(HANDLE, 'episodes')
    snum = int(season)

    for ep in data.get('episodes', []):
        enum = ep.get('episode_number', 0)
        ep_title = ep.get('name', 'Epizóda {}'.format(enum))
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
        li.setInfo('video', ep_info)
        guests = _tmdb_cast(ep.get('guest_stars', []))
        if guests:
            li.setCast(guests)
        still = TMDB.fanart_url(ep.get('still_path'), 'w780')
        if still:
            li.setArt({'thumb': still, 'fanart': still})

        # Search on webshare with S01E01 pattern
        search_title = original_title or title
        li.addContextMenuItems(_search_context_items(
            search_title, '', season=snum, episode=enum))
        url = build_url('ws_search_episode', title=search_title,
                        season=snum, episode=enum)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)

    xbmcplugin.endOfDirectory(HANDLE)


# ---------------------------------------------------------------------------
# TMDB search
# ---------------------------------------------------------------------------

def search_input():
    query = xbmcgui.Dialog().input('Hľadať filmy a seriály')
    if query:
        add_to_history(query)
        do_search(query)
    else:
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)


def do_search(query, page=1):
    tmdb = get_tmdb()
    if tmdb is None:
        return

    try:
        data = tmdb.search_multi(query, page=int(page))
    except TMDBError as e:
        xbmcgui.Dialog().ok('TMDB Chyba', str(e))
        return

    xbmcplugin.setContent(HANDLE, 'videos')
    creds = prefetch_credits(data.get('results', []))
    for item in data.get('results', []):
        mt = item.get('media_type', '')
        if mt == 'movie':
            li, title, year = _movie_listitem(item, creds.get(item['id']))
            url = build_url('movie_detail', tmdb_id=item['id'],
                            title=title, year=year)
            xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)
        elif mt == 'tv':
            li, title, year = _tv_listitem(item, creds.get(item['id']))
            url = build_url('tv_detail', tmdb_id=item['id'],
                            title=title, year=year)
            xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)

    _add_page_items(data, 'tmdb_search', {'query': query})
    xbmcplugin.endOfDirectory(HANDLE)


# ---------------------------------------------------------------------------
# Webshare direct search
# ---------------------------------------------------------------------------

def ws_search_input():
    query = xbmcgui.Dialog().input('Hľadať na Webshare.cz')
    if query:
        do_ws_search(query)
    else:
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)


def do_ws_search(query, offset=0):
    ws = get_webshare()
    if ws is None:
        return

    limit = 25
    try:
        results, total = ws.search(query, offset=int(offset), limit=limit)
    except WebshareAPIError as e:
        xbmcgui.Dialog().ok('Webshare.cz - Chyba', str(e))
        return

    xbmcplugin.setContent(HANDLE, 'videos')
    for item in results:
        name = item['name']
        if not any(name.lower().endswith(ext) for ext in VIDEO_EXTENSIONS):
            continue
        li = xbmcgui.ListItem(name)
        li.setInfo('video', {'title': name, 'size': int(item['size'] * 1024 * 1024)})
        li.setProperty('IsPlayable', 'true')
        if item['img']:
            li.setArt({'thumb': item['img'], 'icon': item['img']})
        li.setLabel2(item['size_str'])
        url = build_url('play', ident=item['ident'], name=name)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=False)

    current_offset = int(offset)
    if current_offset + limit < total:
        li = xbmcgui.ListItem('Ďalšia strana ({}/{})'.format(
            (current_offset // limit) + 2, (total + limit - 1) // limit))
        li.setArt({'icon': 'DefaultFolder.png'})
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
        cm = [('Odstrániť z histórie',
               'RunPlugin({})'.format(build_url('remove_history', query=query)))]
        li.addContextMenuItems(cm)
        url = build_url('tmdb_search', query=query, page=1)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)

    if history:
        li = xbmcgui.ListItem('[I]Vymazať históriu[/I]')
        li.setArt({'icon': 'DefaultIconInfo.png'})
        xbmcplugin.addDirectoryItem(HANDLE, build_url('clear_history'), li, isFolder=False)

    xbmcplugin.endOfDirectory(HANDLE)


def remove_history(query):
    history = load_history()
    if query in history:
        history.remove(query)
        save_history(history)
    import xbmc
    xbmc.executebuiltin('Container.Refresh')


def clear_history():
    if xbmcgui.Dialog().yesno('Webshare.cz', 'Vymazať celú históriu vyhľadávaní?'):
        save_history([])
        import xbmc
        xbmc.executebuiltin('Container.Refresh')


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def router():
    params = dict(parse_qsl(sys.argv[2][1:]))
    action = params.get('action')

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
                            page=1, sort_by=new_sort)
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
                           page=1, sort_by=new_sort)

    # TMDB detail
    elif action == 'movie_detail':
        show_movie_detail(params.get('tmdb_id'),
                          params.get('title', ''),
                          params.get('year', ''))
    elif action == 'tv_info':
        show_tv_info(params.get('tmdb_id'), params.get('title', ''),
                     params.get('year', ''))
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
                                  params.get('year', ''))
    elif action == 'ws_search_episode':
        search_webshare_for_title(params.get('title', ''),
                                  season=params.get('season'),
                                  episode=params.get('episode'))

    # Webshare direct search
    elif action == 'ws_search_input':
        ws_search_input()
    elif action == 'ws_search':
        do_ws_search(params.get('query', ''), params.get('offset', 0))

    # Playback
    elif action == 'play':
        play_webshare(params.get('ident', ''), params.get('name', ''))
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

    else:
        main_menu()


if __name__ == '__main__':
    router()
