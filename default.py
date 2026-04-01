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

from resources.lib.tmdb import TMDB, TMDBError
from resources.lib.webshare import WebshareAPI, WebshareAPIError

ADDON = xbmcaddon.Addon()
HANDLE = int(sys.argv[1])
BASE_URL = sys.argv[0]
PROFILE_DIR = xbmcvfs.translatePath(ADDON.getAddonInfo('profile'))
HISTORY_FILE = os.path.join(PROFILE_DIR, 'search_history.json')
MAX_HISTORY = 20

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

def _movie_listitem(item):
    """Create a ListItem from a TMDB movie dict."""
    title = item.get('title') or item.get('original_title', '')
    year = (item.get('release_date') or '')[:4]
    label = '{} ({})'.format(title, year) if year else title

    li = xbmcgui.ListItem(label)
    info = {'title': title, 'plot': item.get('overview', ''),
            'year': int(year) if year.isdigit() else 0,
            'rating': item.get('vote_average', 0)}
    li.setInfo('video', info)

    poster = TMDB.poster_url(item.get('poster_path'))
    fanart = TMDB.fanart_url(item.get('backdrop_path'))
    li.setArt({'thumb': poster, 'poster': poster, 'fanart': fanart})
    return li, title, year


def _tv_listitem(item):
    """Create a ListItem from a TMDB tv dict."""
    title = item.get('name') or item.get('original_name', '')
    year = (item.get('first_air_date') or '')[:4]
    label = '{} ({})'.format(title, year) if year else title

    li = xbmcgui.ListItem(label)
    info = {'title': title, 'plot': item.get('overview', ''),
            'year': int(year) if year.isdigit() else 0,
            'rating': item.get('vote_average', 0)}
    li.setInfo('video', info)

    poster = TMDB.poster_url(item.get('poster_path'))
    fanart = TMDB.fanart_url(item.get('backdrop_path'))
    li.setArt({'thumb': poster, 'poster': poster, 'fanart': fanart})
    return li, title, year


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


def search_webshare_for_title(title, year='', season=None, episode=None):
    """Search webshare for a movie/episode title and show stream selection."""
    ws = get_webshare()
    if ws is None:
        return

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

    if not all_results:
        xbmcgui.Dialog().ok('Webshare.cz', 'Žiadne výsledky pre: {}'.format(title))
        return

    # Let user pick a stream
    labels = ['{} [{}]'.format(r['name'], r['size_str']) for r in all_results]
    idx = xbmcgui.Dialog().select('Vyber kvalitu / súbor', labels)
    if idx < 0:
        return

    chosen = all_results[idx]
    _play_direct(chosen['ident'], chosen['name'])


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
    li = xbmcgui.ListItem(name or 'Video', path=link)
    xbmc.Player().play(link, li)


def play_webshare(ident, name=''):
    """Resolve and play a webshare file (for IsPlayable items via setResolvedUrl)."""
    ws = get_webshare()
    if ws is None:
        return
    try:
        link = ws.get_file_link(ident)
    except WebshareAPIError as e:
        xbmcgui.Dialog().ok('Webshare.cz - Chyba', str(e))
        return

    li = xbmcgui.ListItem(name or 'Video', path=link)
    xbmcplugin.setResolvedUrl(HANDLE, True, li)


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
    for item in data.get('results', []):
        mt = item.get('media_type', 'movie')
        if mt == 'movie':
            li, title, year = _movie_listitem(item)
            url = build_url('movie_detail', tmdb_id=item['id'],
                            title=title, year=year)
            xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)
        elif mt == 'tv':
            li, title, year = _tv_listitem(item)
            url = build_url('tv_detail', tmdb_id=item['id'],
                            title=title, year=year)
            xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)
    _add_page_items(data, 'trending')
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
    for item in data.get('results', []):
        li, title, year = _movie_listitem(item)
        url = build_url('movie_detail', tmdb_id=item['id'],
                        title=title, year=year)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)
    _add_page_items(data, 'movies_' + category)
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
    for item in data.get('results', []):
        li, title, year = _tv_listitem(item)
        url = build_url('tv_detail', tmdb_id=item['id'],
                        title=title, year=year)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)
    _add_page_items(data, 'tv_' + category)
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


def show_genre_list(media_type, genre_id, genre_name, page=1):
    tmdb = get_tmdb()
    if tmdb is None:
        return
    try:
        if media_type == 'movie':
            data = tmdb.discover_movies(page=int(page),
                                        with_genres=genre_id,
                                        sort_by='popularity.desc')
        else:
            data = tmdb.discover_tv(page=int(page),
                                    with_genres=genre_id,
                                    sort_by='popularity.desc')
    except TMDBError as e:
        xbmcgui.Dialog().ok('TMDB Chyba', str(e))
        return

    content = 'movies' if media_type == 'movie' else 'tvshows'
    xbmcplugin.setContent(HANDLE, content)
    for item in data.get('results', []):
        if media_type == 'movie':
            li, title, year = _movie_listitem(item)
            url = build_url('movie_detail', tmdb_id=item['id'],
                            title=title, year=year)
        else:
            li, title, year = _tv_listitem(item)
            url = build_url('tv_detail', tmdb_id=item['id'],
                            title=title, year=year)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)
    _add_page_items(data, 'genre_list',
                    {'media_type': media_type, 'genre_id': genre_id,
                     'genre_name': genre_name})
    xbmcplugin.endOfDirectory(HANDLE)


# ---------------------------------------------------------------------------
# Movie / TV detail
# ---------------------------------------------------------------------------

def show_movie_detail(tmdb_id, title, year):
    """Show movie info and option to search on Webshare."""
    tmdb = get_tmdb()
    if tmdb is None:
        return

    try:
        detail = tmdb.movie_detail(tmdb_id)
    except TMDBError:
        detail = {}

    xbmcplugin.setContent(HANDLE, 'movies')

    # "Play" item - searches webshare
    play_title = detail.get('title') or title
    play_year = (detail.get('release_date') or '')[:4] or year
    original = detail.get('original_title', '')

    li = xbmcgui.ListItem('[B]Hľadať na Webshare: {}[/B]'.format(play_title))
    plot = detail.get('overview', '')
    genres = ', '.join(g['name'] for g in detail.get('genres', []))
    runtime = detail.get('runtime', 0)
    info = {'title': play_title, 'plot': plot, 'genre': genres,
            'year': int(play_year) if play_year.isdigit() else 0,
            'rating': detail.get('vote_average', 0),
            'duration': runtime * 60 if runtime else 0}
    credits_ = detail.get('credits', {})
    directors = [c['name'] for c in credits_.get('crew', [])
                 if c.get('job') == 'Director']
    if directors:
        info['director'] = ', '.join(directors)
    cast_ = [c['name'] for c in credits_.get('cast', [])[:10]]
    if cast_:
        info['cast'] = cast_
    li.setInfo('video', info)

    poster = TMDB.poster_url(detail.get('poster_path'))
    fanart = TMDB.fanart_url(detail.get('backdrop_path'))
    li.setArt({'thumb': poster, 'poster': poster, 'fanart': fanart})

    url = build_url('ws_search_title', title=play_title, year=play_year)
    xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=False)

    # Also search by original title if different
    if original and original.lower() != play_title.lower():
        li2 = xbmcgui.ListItem('Hľadať originálny názov: {}'.format(original))
        li2.setArt({'thumb': poster, 'poster': poster, 'fanart': fanart})
        url2 = build_url('ws_search_title', title=original, year=play_year)
        xbmcplugin.addDirectoryItem(HANDLE, url2, li2, isFolder=False)

    xbmcplugin.endOfDirectory(HANDLE)


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
        })
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
    xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=False)

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
        li.setInfo('video', {
            'title': ep_title,
            'plot': ep.get('overview', ''),
            'tvshowtitle': title,
            'season': snum,
            'episode': enum,
            'rating': ep.get('vote_average', 0),
        })
        still = TMDB.fanart_url(ep.get('still_path'), 'w780')
        if still:
            li.setArt({'thumb': still, 'fanart': still})

        # Search on webshare with S01E01 pattern
        search_title = original_title or title
        url = build_url('ws_search_episode', title=search_title,
                        season=snum, episode=enum)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=False)

    xbmcplugin.endOfDirectory(HANDLE)


# ---------------------------------------------------------------------------
# TMDB search
# ---------------------------------------------------------------------------

def search_input():
    query = xbmcgui.Dialog().input('Hľadať filmy a seriály')
    if query:
        add_to_history(query)
        do_search(query)


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
    for item in data.get('results', []):
        mt = item.get('media_type', '')
        if mt == 'movie':
            li, title, year = _movie_listitem(item)
            url = build_url('movie_detail', tmdb_id=item['id'],
                            title=title, year=year)
            xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)
        elif mt == 'tv':
            li, title, year = _tv_listitem(item)
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
                        params.get('page', 1))

    # TMDB detail
    elif action == 'movie_detail':
        show_movie_detail(params.get('tmdb_id'),
                          params.get('title', ''),
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
