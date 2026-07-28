# -*- coding: utf-8 -*-
"""File-per-key JSON cache with TTL.

Writes are atomic (temp file + os.replace) and tolerate concurrent plugin
invocations — Kodi can run the plugin from widgets while the user browses.
A failed read or write degrades to a cache miss, never to an error.
"""

import hashlib
import json
import os
import time


class Cache:
    def __init__(self, directory):
        self.directory = directory

    def _path(self, key):
        name = hashlib.md5(key.encode('utf-8')).hexdigest()
        return os.path.join(self.directory, name + '.json')

    def get(self, key, ttl):
        """Return the cached value, or None when missing or older than ttl."""
        path = self._path(key)
        try:
            if time.time() - os.path.getmtime(path) > ttl:
                return None
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def set(self, key, value):
        path = self._path(key)
        tmp = '{}.{}.tmp'.format(path, os.getpid())
        try:
            os.makedirs(self.directory, exist_ok=True)
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(value, f, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def prune(self, max_age):
        """Delete entries older than max_age seconds."""
        try:
            names = os.listdir(self.directory)
        except OSError:
            return
        cutoff = time.time() - max_age
        for name in names:
            path = os.path.join(self.directory, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass
