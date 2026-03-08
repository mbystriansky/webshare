# -*- coding: utf-8 -*-
"""Kodi plugin for searching and playing videos from webshare.cz."""

import sys
from urllib.parse import parse_qsl, urlencode

import xbmcaddon
import xbmcgui
import xbmcplugin

from resources.lib.webshare import WebshareAPI, WebshareAPIError

ADDON = xbmcaddon.Addon()
HANDLE = int(sys.argv[1])
BASE_URL = sys.argv[0]

VIDEO_EXTENSIONS = (
    '.avi', '.mkv', '.mp4', '.m4v', '.mov', '.wmv', '.flv',
    '.mpg', '.mpeg', '.ts', '.vob', '.divx', '.webm', '.3gp',
)

_api = None


def get_api():
    """Get authenticated WebshareAPI instance."""
    global _api
    if _api is not None:
        return _api

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

    _api = api
    return api


def build_url(action, **kwargs):
    """Build a plugin URL with the given action and parameters."""
    kwargs['action'] = action
    return '{}?{}'.format(BASE_URL, urlencode(kwargs))


def main_menu():
    """Show the main menu."""
    xbmcplugin.setContent(HANDLE, 'videos')

    # Search item
    li = xbmcgui.ListItem('Vyhľadávanie')
    li.setArt({'icon': 'DefaultAddonsSearch.png'})
    xbmcplugin.addDirectoryItem(
        HANDLE, build_url('search_input'), li, isFolder=True
    )

    # New search
    li = xbmcgui.ListItem('Nové vyhľadávanie')
    li.setArt({'icon': 'DefaultAddonsSearch.png'})
    xbmcplugin.addDirectoryItem(
        HANDLE, build_url('new_search'), li, isFolder=True
    )

    xbmcplugin.endOfDirectory(HANDLE)


def search_input():
    """Show keyboard for search input."""
    kb = xbmcgui.Dialog()
    query = kb.input('Hľadať na Webshare.cz')
    if query:
        do_search(query, offset=0)


def new_search():
    """Always prompt for a new search query."""
    search_input()


def do_search(query, offset=0):
    """Perform search and display results."""
    api = get_api()
    if api is None:
        return

    limit = 25
    try:
        results, total = api.search(query, offset=int(offset), limit=limit)
    except WebshareAPIError as e:
        xbmcgui.Dialog().ok('Webshare.cz - Chyba', str(e))
        return

    xbmcplugin.setContent(HANDLE, 'videos')

    for item in results:
        name = item['name']

        # Only show video files
        if not any(name.lower().endswith(ext) for ext in VIDEO_EXTENSIONS):
            continue

        li = xbmcgui.ListItem(name)
        li.setInfo('video', {
            'title': name,
            'size': int(item['size'] * 1024 * 1024),
        })
        li.setProperty('IsPlayable', 'true')

        if item['img']:
            li.setArt({'thumb': item['img'], 'icon': item['img']})

        # Label2 shows size
        li.setLabel2(item['size_str'])

        url = build_url('play', ident=item['ident'], name=name)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=False)

    # Pagination - next page
    current_offset = int(offset)
    if current_offset + limit < total:
        next_li = xbmcgui.ListItem('Ďalšia strana ({}/{})'.format(
            (current_offset // limit) + 2,
            (total + limit - 1) // limit
        ))
        next_li.setArt({'icon': 'DefaultFolder.png'})
        next_url = build_url('search', query=query, offset=current_offset + limit)
        xbmcplugin.addDirectoryItem(HANDLE, next_url, next_li, isFolder=True)

    xbmcplugin.endOfDirectory(HANDLE)


def play_video(ident, name=''):
    """Resolve file link and play video."""
    api = get_api()
    if api is None:
        return

    try:
        link = api.get_file_link(ident)
    except WebshareAPIError as e:
        xbmcgui.Dialog().ok('Webshare.cz - Chyba', str(e))
        return

    li = xbmcgui.ListItem(name or 'Video', path=link)
    xbmcplugin.setResolvedUrl(HANDLE, True, li)


def router():
    """Route plugin actions based on URL parameters."""
    params = dict(parse_qsl(sys.argv[2][1:]))
    action = params.get('action')

    if action is None:
        main_menu()
    elif action == 'search_input':
        search_input()
    elif action == 'new_search':
        new_search()
    elif action == 'search':
        do_search(params.get('query', ''), offset=params.get('offset', 0))
    elif action == 'play':
        play_video(params.get('ident', ''), params.get('name', ''))
    else:
        main_menu()


if __name__ == '__main__':
    router()
