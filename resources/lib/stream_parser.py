# -*- coding: utf-8 -*-
"""Parse scene release filenames to extract stream metadata.

Reconstructed from resources/lib/__pycache__/stream_parser.cpython-38.opt-1.pyc
(decompyle3 + manual control-flow repair) — the original source was written on
another machine and never committed. Behaviour verified against the bytecode.
"""

import re
import unicodedata

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


# --- Matching a file name against the title we searched for ---------------

_WORDS_RE = re.compile(r'[^a-z0-9]+')

# Every way a file name states which episode it holds
_EP_TAG_RES = [
    re.compile(r's(\d{1,2}) ?e(\d{1,3})'),
    re.compile(r'(?<!\d)(\d{1,2})x(\d{2})(?!\d)'),
    re.compile(r'(?:season|serie|seria|series) (\d{1,2}) '
               r'(?:episode|epizoda|epizody|dil|cast|ep) (\d{1,3})'),
]

# Shortest word that may match by prefix, and how much two forms of the
# same word may differ in length ('odhaleni' vs 'odhalenia')
PREFIX_MIN = 4
PREFIX_SLACK = 2


def fold(text):
    """Lowercase ASCII form of a title: 'Pelíšky' -> 'pelisky'."""
    return unicodedata.normalize('NFKD', text or '') \
        .encode('ascii', 'ignore').decode('ascii').lower()


def _words(text):
    return [w for w in _WORDS_RE.split(fold(text)) if w]


def _word_matches(want, words):
    """One title word against a file's words.

    Near-identical forms count as the same word, so Slovak 'odhalenia' and
    Czech 'odhaleni' are not treated as different films. The forms must be
    within a couple of characters of each other: 'smrt' is its own word,
    not a stand-in for 'smrtelne'.
    """
    if want in words:
        return True
    if len(want) < PREFIX_MIN:
        return False
    return any(len(w) >= PREFIX_MIN
               and abs(len(w) - len(want)) <= PREFIX_SLACK
               and (w.startswith(want) or want.startswith(w))
               for w in words)


def episode_tags(name):
    """Every (season, episode) pair a file name announces, in any of the
    notations releases use: S01E02, 1x02, 'Season 1 Episode 2'."""
    flat = ' '.join(_words(name))
    tags = set()
    for pattern in _EP_TAG_RES:
        for m in pattern.finditer(flat):
            tags.add((int(m.group(1)), int(m.group(2))))
    return tags


def title_relevance(name, title, year='', season=None, episode=None):
    """How well a Webshare file name matches what was searched for.

    Returns None when the file is about something else — Webshare answers a
    search for 'Smrtelné zlo: V plamenech' with 'Aljaška v plamenech' too,
    and that must not be offered as the film — otherwise a rank:

      3  every word of the title, and the year it was released
      2  every word of the title
      1  most of the title, or the title but another year

    A file has to carry the title's most distinctive word — its longest —
    plus half the words, and never fewer than two unless the title is a
    single word. Requiring every word would throw away the many files that
    drop a franchise prefix ('Mandalorian a Grogu' for 'Star Wars:
    Mandalorian a Grogu'); requiring fewer lets one shared ordinary word
    stand in for the film ('Autogrotesky - fantastický odhalení').
    """
    words = _words(name)
    wanted = [w for w in _words(title) if len(w) > 1] or _words(title)
    if not wanted:
        return 2

    if not _word_matches(max(wanted, key=len), words):
        return None
    hits = sum(1 for w in wanted if _word_matches(w, words))
    if hits * 2 < len(wanted) or hits < min(2, len(wanted)):
        return None

    if season is not None and episode is not None:
        tags = episode_tags(name)
        if tags and (int(season), int(episode)) not in tags:
            return None  # a different episode of the right series
        if tags:
            return 3

    if hits < len(wanted):
        return 1

    named_years = {w for w in words
                   if len(w) == 4 and w.isdigit() and 1900 < int(w) < 2100}
    if not year or not named_years:
        return 2
    # A file naming some other year is likely another film of the series
    return 3 if str(year) in named_years else 1


def sort_streams(results, max_rank=0):
    """Sort stream dicts: closest to what was asked for first, then by
    resolution and size (both desc). With max_rank set, streams above that
    resolution rank sort below everything else — never hidden, so a title
    that only exists in 4K stays playable."""
    def key(r):
        rank = r.get('_parsed', {}).get('resolution_rank', 0)
        return (0 if max_rank and rank > max_rank else 1,
                r.get('_relevance', 3),
                rank,
                r.get('size_bytes', 0))
    return sorted(results, key=key, reverse=True)


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
