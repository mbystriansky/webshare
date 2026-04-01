# -*- coding: utf-8 -*-
"""TMDB (The Movie Database) API client."""

import requests

API_BASE = 'https://api.themoviedb.org/3'
IMG_BASE = 'https://image.tmdb.org/t/p/'


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

    # --- Movies ---

    def trending(self, media_type='all', time_window='week', page=1):
        return self._get('/trending/{}/{}'.format(media_type, time_window),
                         {'page': page})

    def movies_now_playing(self, page=1):
        return self._get('/movie/now_playing', {'page': page, 'region': 'CZ'})

    def movies_popular(self, page=1):
        return self._get('/movie/popular', {'page': page})

    def movies_top_rated(self, page=1):
        return self._get('/movie/top_rated', {'page': page})

    def movie_detail(self, movie_id):
        return self._get('/movie/{}'.format(movie_id),
                         {'append_to_response': 'credits'})

    # --- TV ---

    def tv_popular(self, page=1):
        return self._get('/tv/popular', {'page': page})

    def tv_top_rated(self, page=1):
        return self._get('/tv/top_rated', {'page': page})

    def tv_detail(self, tv_id):
        return self._get('/tv/{}'.format(tv_id),
                         {'append_to_response': 'credits'})

    def tv_season(self, tv_id, season_number):
        return self._get('/tv/{}/season/{}'.format(tv_id, season_number))

    # --- Search ---

    def search_movie(self, query, page=1):
        return self._get('/search/movie', {'query': query, 'page': page})

    def search_tv(self, query, page=1):
        return self._get('/search/tv', {'query': query, 'page': page})

    def search_multi(self, query, page=1):
        return self._get('/search/multi', {'query': query, 'page': page})

    # --- Genres ---

    def movie_genres(self):
        return self._get('/genre/movie/list')

    def tv_genres(self):
        return self._get('/genre/tv/list')

    # --- Discover ---

    def discover_movies(self, page=1, **kwargs):
        kwargs['page'] = page
        return self._get('/discover/movie', kwargs)

    def discover_tv(self, page=1, **kwargs):
        kwargs['page'] = page
        return self._get('/discover/tv', kwargs)

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
