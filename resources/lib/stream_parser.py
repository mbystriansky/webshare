# -*- coding: utf-8 -*-
"""Parse scene release filenames to extract stream metadata.

Reconstructed from resources/lib/__pycache__/stream_parser.cpython-38.opt-1.pyc
(decompyle3 + manual control-flow repair) — the original source was written on
another machine and never committed. Behaviour verified against the bytecode.
"""

import re

_TOKEN = r'(?:^|[\.\s\-_\[\(])'
_END = r'(?:[\.\s\-_\]\)]|$)'

_RESOLUTION_MAP = [
    (re.compile(_TOKEN + r'(2160p|4K|UHD)' + _END, re.I), '2160p', 4),
    (re.compile(_TOKEN + r'(1080[pi])' + _END, re.I), '1080p', 3),
    (re.compile(_TOKEN + r'(720p)' + _END, re.I), '720p', 2),
    (re.compile(_TOKEN + r'(480p|576p)' + _END, re.I), '480p', 1),
]

_VIDEO_CODEC_MAP = [
    (re.compile(_TOKEN + r'(x265|H\.?265|HEVC)' + _END, re.I), 'x265'),
    (re.compile(_TOKEN + r'(x264|H\.?264|AVC)' + _END, re.I), 'x264'),
    (re.compile(_TOKEN + r'(XviD)' + _END, re.I), 'XviD'),
    (re.compile(_TOKEN + r'(DivX)' + _END, re.I), 'DivX'),
    (re.compile(_TOKEN + r'(AV1)' + _END, re.I), 'AV1'),
    (re.compile(_TOKEN + r'(VP9)' + _END, re.I), 'VP9'),
]

_AUDIO_CODEC_MAP = [
    (re.compile(_TOKEN + r'(TrueHD)' + _END, re.I), 'TrueHD'),
    (re.compile(_TOKEN + r'(Atmos)' + _END, re.I), 'Atmos'),
    (re.compile(_TOKEN + r'(DTS[\-\.]?HD[\.\s]?MA)' + _END, re.I), 'DTS-HD MA'),
    (re.compile(_TOKEN + r'(DTS[\-\.]?HD)' + _END, re.I), 'DTS-HD'),
    (re.compile(_TOKEN + r'(DTS)' + _END, re.I), 'DTS'),
    (re.compile(_TOKEN + r'(EAC3|E[\-\.]AC[\-\.]?3|DD[P\+])' + _END, re.I), 'DD+'),
    (re.compile(_TOKEN + r'(AC3|DD)' + _END, re.I), 'AC3'),
    (re.compile(_TOKEN + r'(AAC)' + _END, re.I), 'AAC'),
    (re.compile(_TOKEN + r'(FLAC)' + _END, re.I), 'FLAC'),
    (re.compile(_TOKEN + r'(MP3)' + _END, re.I), 'MP3'),
]

_CHANNELS_RE = re.compile(r'(?<!\d)(7\.1|5\.1|2\.0|2\.1|1\.0)(?!\d)')

_SOURCE_MAP = [
    (re.compile(_TOKEN + r'(Blu[\-\.]?Ray|BDRip|BRRip)' + _END, re.I), 'BluRay'),
    (re.compile(_TOKEN + r'(WEB[\-\.]?DL)' + _END, re.I), 'WEB-DL'),
    (re.compile(_TOKEN + r'(WEB[\-\.]?Rip)' + _END, re.I), 'WEBRip'),
    (re.compile(_TOKEN + r'(HDRip)' + _END, re.I), 'HDRip'),
    (re.compile(_TOKEN + r'(DVD[\-\.]?Rip)' + _END, re.I), 'DVDRip'),
    (re.compile(_TOKEN + r'(HDTV)' + _END, re.I), 'HDTV'),
    (re.compile(_TOKEN + r'(DVDScr)' + _END, re.I), 'DVDScr'),
    (re.compile(_TOKEN + r'(CAM(?:Rip)?)' + _END, re.I), 'CAM'),
    (re.compile(_TOKEN + r'(TELESYNC|TS)' + _END, re.I), 'TS'),
]

_LANG_QUALIFIED = [
    (re.compile(_TOKEN + r'CZ[\.\-_\s]?(?:dabing|dab)' + _END, re.I), 'CZ dabing'),
    (re.compile(_TOKEN + r'CZ[\.\-_\s]?(?:titulky|tit)' + _END, re.I), 'CZ titulky'),
    (re.compile(_TOKEN + r'SK[\.\-_\s]?(?:dabing|dab)' + _END, re.I), 'SK dabing'),
    (re.compile(_TOKEN + r'SK[\.\-_\s]?(?:titulky|tit)' + _END, re.I), 'SK titulky'),
]

_LANG_CODES = ['CZ', 'SK', 'EN', 'DE', 'HU', 'PL', 'FR', 'ES', 'IT', 'RU']
_LANG_CODES_RE = re.compile(
    _TOKEN + '(' + '|'.join(_LANG_CODES) + r')(?=[\.\s\-_\]\)]|$)')

_HDR_RE = re.compile(
    _TOKEN + r'(HDR10\+?|HDR|DV|Dolby[\.\s]?Vision|HLG)' + _END, re.I)


def parse_stream_info(filename):
    """Parse a scene release filename and return extracted metadata dict."""
    info = {
        'resolution': '',
        'resolution_rank': 0,
        'video_codec': '',
        'audio_codec': '',
        'audio_channels': '',
        'languages': [],
        'source': '',
        'hdr': '',
    }

    for pattern, label, rank in _RESOLUTION_MAP:
        if pattern.search(filename):
            info['resolution'] = label
            info['resolution_rank'] = rank
            break

    for pattern, label in _VIDEO_CODEC_MAP:
        if pattern.search(filename):
            info['video_codec'] = label
            break

    for pattern, label in _AUDIO_CODEC_MAP:
        if pattern.search(filename):
            info['audio_codec'] = label
            break

    m = _CHANNELS_RE.search(filename)
    if m:
        info['audio_channels'] = m.group(1)

    for pattern, label in _SOURCE_MAP:
        if pattern.search(filename):
            info['source'] = label
            break

    m = _HDR_RE.search(filename)
    if m:
        val = m.group(1)
        if val.upper().startswith('DV') or 'VISION' in val.upper():
            info['hdr'] = 'DV'
        else:
            info['hdr'] = val.upper()

    langs = []
    for pattern, label in _LANG_QUALIFIED:
        if pattern.search(filename):
            langs.append(label)
    for m in _LANG_CODES_RE.finditer(filename):
        code = m.group(1).upper()
        if not any(l.startswith(code) for l in langs):
            langs.append(code)
    info['languages'] = langs

    return info


def sort_streams(results):
    """Sort stream result dicts by resolution (desc), then size (desc)."""
    return sorted(
        results,
        key=lambda r: (r.get('_parsed', {}).get('resolution_rank', 0),
                       r.get('size_bytes', 0)),
        reverse=True)


def format_stream_label(result):
    """Format a Webshare result dict into a stream picker label string."""
    p = result.get('_parsed', {})
    parts = []
    if p.get('resolution'):
        parts.append(p['resolution'])
    if p.get('source'):
        parts.append(p['source'])
    if p.get('video_codec'):
        parts.append(p['video_codec'])
    langs = p.get('languages', [])
    if langs:
        parts.append(', '.join(langs))
    audio_parts = []
    if p.get('audio_codec'):
        audio_parts.append(p['audio_codec'])
    if p.get('audio_channels'):
        audio_parts.append(p['audio_channels'])
    if audio_parts:
        parts.append(' '.join(audio_parts))
    if p.get('hdr'):
        parts.append(p['hdr'])
    size_bytes = result.get('size_bytes', 0)
    if size_bytes > 0:
        if size_bytes >= 1073741824:
            parts.append('{:.1f} GB'.format(size_bytes / 1073741824))
        else:
            parts.append('{:.0f} MB'.format(size_bytes / 1048576))
    pos = result.get('positive_votes', 0)
    neg = result.get('negative_votes', 0)
    if pos or neg:
        parts.append('+{}/-{}'.format(pos, neg))
    if parts:
        return ' | '.join(parts)
    return result.get('name', 'Unknown')
