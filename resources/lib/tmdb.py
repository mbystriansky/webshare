# -*- coding: utf-8 -*-
"""TMDB (The Movie Database) API client."""

import requests

API_BASE = 'https://api.themoviedb.org/3'
IMG_BASE = 'https://image.tmdb.org/t/p/'

# Poradie jazykov, z ktorých sa doplní popis, keď v nastavenom jazyku chýba:
# sk a cs si navzájom vypomôžu, angličtina je posledná záchrana
FALLBACK_CHAINS = {
    'sk': ['cs-CZ', 'en-US'],
    'cs': ['sk-SK', 'en-US'],
}
DEFAULT_FALLBACK = ['en-US']

# Písma v Kodi skinoch pokrývajú latinku, gréčtinu a cyriliku (do U+04FF).
# Názvy v inom písme (barmčina, CJK, arabčina…) sa vykreslia ako prázdne
# štvorčeky, tak ich nahrádzame anglickým názvom.
MAX_RENDERABLE_CODEPOINT = 0x04FF


def is_unrenderable(text):
    """True, keď je text prevažne v písme, ktoré Kodi nevie zobraziť."""
    letters = [c for c in text or '' if c.isalpha()]
    if not letters:
        return False
    exotic = sum(1 for c in letters if ord(c) > MAX_RENDERABLE_CODEPOINT)
    return exotic > len(letters) / 2


def _title_key(item):
    """'title' pre filmy, 'name' pre seriály."""
    return 'name' if 'name' in item and 'title' not in item else 'title'


def _media_type(item):
    mt = item.get('media_type')
    if mt in ('movie', 'tv'):
        return mt
    return 'tv' if _title_key(item) == 'name' else 'movie'


class TMDBError(Exception):
    pass


class TMDB:
    def __init__(self, api_key, language='cs-CZ'):
        self.api_key = api_key
        self.language = language

    def _get(self, path, params=None):
        if not self.api_key:
            raise TMDBError('TMDB API kľúč nie je nastavený')
        p = {'api_key': self.api_key, 'language': self.language}
        if params:
            p.update(params)
        resp = requests.get(API_BASE + path, params=p, timeout=15)
        if resp.status_code == 401:
            raise TMDBError('Neplatný TMDB API kľúč')
        resp.raise_for_status()
        return resp.json()

    def _fallback_langs(self):
        """Poradie náhradných jazykov podľa nastaveného primárneho jazyka."""
        iso = self.language.split('-')[0].lower()
        chain = FALLBACK_CHAINS.get(iso)
        if chain is None:
            chain = [] if iso == 'en' else DEFAULT_FALLBACK
        return [l for l in chain if not l.lower().startswith(iso)]

    def _get_list(self, path, params=None):
        """GET zoznamového endpointu.

        Chýbajúce popisy a názvy v nezobraziteľnom písme doplní z náhradných
        jazykov (posledný v poradí je vždy angličtina).
        """
        data = self._get(path, params)
        results = data.get('results') or []
        for lang in self._fallback_langs():
            no_plot = [r for r in results if not r.get('overview')]
            bad_title = [r for r in results
                         if is_unrenderable(r.get(_title_key(r)))]
            if not no_plot and not bad_title:
                break
            p = dict(params or {})
            p['language'] = lang
            try:
                fb_results = self._get(path, p).get('results') or []
            except (TMDBError, requests.RequestException):
                continue
            by_id = {r.get('id'): r for r in fb_results}
            for r in no_plot:
                fb = by_id.get(r.get('id')) or {}
                r['overview'] = fb.get('overview') or r.get('overview', '')
            for r in bad_title:
                key = _title_key(r)
                fb_title = (by_id.get(r.get('id')) or {}).get(key)
                if fb_title and not is_unrenderable(fb_title):
                    r[key] = fb_title
        self._repair_items(results)
        return data

    def _needs_repair(self, item):
        return (not item.get('overview')
                or is_unrenderable(item.get(_title_key(item))))

    def _repair_items(self, results):
        """Per-item translation lookups for what the list-level fallback missed.

        Discover pages differ slightly between languages, so an item is not
        guaranteed to appear in the fallback page at all.
        """
        broken = [r for r in results if r.get('id') and self._needs_repair(r)]
        if not broken or not self._fallback_langs():
            return
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(8, len(broken))) as pool:
            list(pool.map(self._repair_item, broken))

    def _repair_item(self, item):
        try:
            data = self._get('/{}/{}/translations'.format(
                _media_type(item), item['id']))
        except (TMDBError, requests.RequestException):
            return
        by_iso = {}
        for tr in data.get('translations') or []:
            iso = tr.get('iso_639_1')
            if iso and iso not in by_iso:
                by_iso[iso] = tr.get('data') or {}

        key = _title_key(item)
        need_plot = not item.get('overview')
        need_title = is_unrenderable(item.get(key))
        for lang in self._fallback_langs():
            tr = by_iso.get(lang.split('-')[0].lower()) or {}
            if need_plot and tr.get('overview'):
                item['overview'] = tr['overview']
                need_plot = False
            tr_title = tr.get('title') or tr.get('name')
            if need_title and tr_title and not is_unrenderable(tr_title):
                item[key] = tr_title
                need_title = False
            if not need_plot and not need_title:
                break

    def _fill_from_translations(self, data):
        """Doplní chýbajúci popis a nezobraziteľný názov z prekladov v odpovedi."""
        key = _title_key(data)
        need_plot = not data.get('overview')
        need_title = is_unrenderable(data.get(key))
        if not need_plot and not need_title:
            return data

        by_iso = {}
        for tr in (data.get('translations') or {}).get('translations') or []:
            iso = tr.get('iso_639_1')
            if iso and iso not in by_iso:
                by_iso[iso] = tr.get('data') or {}

        for lang in self._fallback_langs():
            tr = by_iso.get(lang.split('-')[0].lower()) or {}
            if need_plot and tr.get('overview'):
                data['overview'] = tr['overview']
                need_plot = False
            tr_title = tr.get('title') or tr.get('name')
            if need_title and tr_title and not is_unrenderable(tr_title):
                data[key] = tr_title
                need_title = False
            if not need_plot and not need_title:
                break
        return data

    # --- Movies ---

    def trending(self, media_type='all', time_window='week', page=1):
        return self._get_list('/trending/{}/{}'.format(media_type, time_window),
                              {'page': page})

    def movies_now_playing(self, page=1):
        return self._get_list('/movie/now_playing', {'page': page, 'region': 'CZ'})

    def movies_popular(self, page=1):
        return self._get_list('/movie/popular', {'page': page})

    def movies_top_rated(self, page=1):
        return self._get_list('/movie/top_rated', {'page': page})

    def movie_credits(self, movie_id):
        return self._get('/movie/{}/credits'.format(movie_id))

    def movie_detail(self, movie_id):
        data = self._get('/movie/{}'.format(movie_id),
                         {'append_to_response': 'credits,translations'})
        return self._fill_from_translations(data)

    # --- TV ---

    def tv_popular(self, page=1):
        return self._get_list('/tv/popular', {'page': page})

    def tv_top_rated(self, page=1):
        return self._get_list('/tv/top_rated', {'page': page})

    def tv_credits(self, tv_id):
        """Cast plus creators — TMDB has no director for a series as a whole,
        so creators are reported in their place."""
        data = self._get('/tv/{}'.format(tv_id),
                         {'append_to_response': 'credits'})
        creds = data.get('credits') or {}
        crew = list(creds.get('crew') or [])
        crew += [{'name': c.get('name'), 'job': 'Director'}
                 for c in data.get('created_by') or [] if c.get('name')]
        return {'cast': creds.get('cast') or [], 'crew': crew}

    def tv_detail(self, tv_id):
        data = self._get('/tv/{}'.format(tv_id),
                         {'append_to_response': 'credits,translations'})
        return self._fill_from_translations(data)

    def tv_season(self, tv_id, season_number):
        path = '/tv/{}/season/{}'.format(tv_id, season_number)
        data = self._get(path)
        episodes = data.get('episodes') or []
        for lang in self._fallback_langs():
            if data.get('overview') and all(e.get('overview') for e in episodes):
                break
            try:
                fb = self._get(path, {'language': lang})
            except (TMDBError, requests.RequestException):
                continue
            if not data.get('overview'):
                data['overview'] = fb.get('overview', '')
            fb_eps = {e.get('episode_number'): e.get('overview')
                      for e in fb.get('episodes') or []}
            for e in episodes:
                if not e.get('overview'):
                    e['overview'] = fb_eps.get(e.get('episode_number')) or ''
        return data

    # --- Search ---

    def search_movie(self, query, page=1):
        return self._get_list('/search/movie', {'query': query, 'page': page})

    def search_tv(self, query, page=1):
        return self._get_list('/search/tv', {'query': query, 'page': page})

    def search_multi(self, query, page=1):
        return self._get_list('/search/multi', {'query': query, 'page': page})

    # --- Genres ---

    def movie_genres(self):
        return self._get('/genre/movie/list')

    def tv_genres(self):
        return self._get('/genre/tv/list')

    # --- Discover ---

    def discover_movies(self, page=1, **kwargs):
        kwargs['page'] = page
        return self._get_list('/discover/movie', kwargs)

    def discover_tv(self, page=1, **kwargs):
        kwargs['page'] = page
        return self._get_list('/discover/tv', kwargs)

    # --- Helpers ---

    @staticmethod
    def poster_url(path, size='w500'):
        if not path:
            return ''
        return IMG_BASE + size + path

    @staticmethod
    def fanart_url(path, size='w1280'):
        if not path:
            return ''
        return IMG_BASE + size + path
