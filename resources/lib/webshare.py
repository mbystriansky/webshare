# -*- coding: utf-8 -*-
"""Webshare.cz API client for Kodi plugin."""

import hashlib
import xml.etree.ElementTree as ET

import requests

from resources.lib.md5crypt import md5crypt

API_BASE = 'https://webshare.cz/api/'

HEADERS = {
    'X-Requested-With': 'XMLHttpRequest',
    'Accept': 'text/xml; charset=UTF-8',
    'Referer': 'https://webshare.cz/',
}


class WebshareAPIError(Exception):
    pass


class WebshareAPI:
    def __init__(self):
        self.token = ''

    def _post(self, endpoint, data=None):
        url = API_BASE + endpoint + '/'
        resp = requests.post(url, data=data, headers=HEADERS)
        resp.raise_for_status()
        return ET.fromstring(resp.content)

    def _check_status(self, xml, context=''):
        status = xml.findtext('status', '')
        if status != 'OK':
            msg = xml.findtext('message', 'Unknown error')
            raise WebshareAPIError('{}: {} ({})'.format(context, msg, status))

    def login(self, username, password):
        """Login to webshare.cz and obtain authentication token."""
        if not username or not password:
            raise WebshareAPIError('Používateľské meno a heslo sú povinné')

        # Get salt
        xml = self._post('salt', {'username_or_email': username})
        self._check_status(xml, 'Salt')
        salt = xml.findtext('salt', '')

        # Hash password: SHA1(md5crypt(password, salt))
        encrypted = md5crypt(password, salt)
        password_hash = hashlib.sha1(encrypted.encode('utf-8')).hexdigest()

        # Digest: MD5(username:Webshare:password)
        digest = hashlib.md5(
            (username + ':Webshare:' + password).encode('utf-8')
        ).hexdigest()

        # Login
        xml = self._post('login', {
            'username_or_email': username,
            'password': password_hash,
            'digest': digest,
            'keep_logged_in': 1,
        })
        self._check_status(xml, 'Login')
        self.token = xml.findtext('token', '')
        return True

    def search(self, query, offset=0, limit=25, category='video'):
        """Search for files on webshare.cz.

        Returns (results_list, total_count).
        Each result is a dict with keys: name, ident, size, img, type.
        """
        xml = self._post('search', {
            'what': query,
            'offset': offset,
            'limit': limit,
            'category': category,
            'sort': '',
            'wst': self.token,
        })
        self._check_status(xml, 'Search')

        total = int(xml.findtext('total', '0'))
        results = []

        for f in xml.findall('file'):
            size_bytes = int(f.findtext('size', '0'))
            size_mb = size_bytes / (1024 * 1024)

            img = f.findtext('img', '')
            if img and not img.startswith('http'):
                img = 'https://webshare.cz/' + img.lstrip('/')

            results.append({
                'name': f.findtext('name', ''),
                'ident': f.findtext('ident', ''),
                'size': size_mb,
                'size_str': '{:.0f} MB'.format(size_mb),
                'img': img,
                'type': f.findtext('type', ''),
            })

        return results, total

    def get_file_link(self, ident, download_type='video_stream'):
        """Get streaming/download link for a file.

        Args:
            ident: File identifier.
            download_type: One of 'video_stream', 'audio_stream', 'file_download'.

        Returns:
            Direct link URL string.
        """
        xml = self._post('file_link', {
            'ident': ident,
            'wst': self.token,
            'download_type': download_type,
            'force_https': 1,
        })
        self._check_status(xml, 'FileLink')
        link = xml.findtext('link', '')
        if not link:
            raise WebshareAPIError('Nepodarilo sa získať odkaz na súbor')
        return link
