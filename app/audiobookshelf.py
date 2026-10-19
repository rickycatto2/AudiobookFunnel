"""On-demand ABS playlist operations. Never include upstream bodies/errors in responses."""
import posixpath
from contextlib import contextmanager
from urllib.parse import quote

import httpx
from fastapi import HTTPException


@contextmanager
def connection(settings):
    if not settings.abs_url or not settings.abs_token or not settings.abs_library_id:
        raise HTTPException(400, 'Configure Audiobookshelf URL, API token and library ID in Settings first.')
    try:
        with httpx.Client(base_url=settings.abs_url.rstrip('/') + '/',
                          headers={'Authorization': 'Bearer ' + settings.abs_token}, timeout=30) as client:
            yield client
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise HTTPException(502, 'Audiobookshelf is unavailable or returned an invalid response. Check its address, permissions and connection.') from None


def request(client, method, path, **kwargs):
    response = client.request(method, 'api/' + path, **kwargs)
    response.raise_for_status()
    return response.json()


def identifier(value):
    return quote(str(value), safe='')


def playlists(client):
    result = request(client, 'GET', 'playlists')
    rows = result if isinstance(result, list) else result['playlists']
    # Only expose fields needed by the picker, never upstream user/credential data.
    return [{'id': row['id'], 'name': row['name']} for row in rows]


def normalized(path):
    return posixpath.normpath(str(path).replace('\\', '/'))


def resolve(client, settings, job):
    plan = job['body'].get('publication')
    # A plan is frozen BEFORE publication, so it alone is not proof of success.
    if not plan or not job.get('output'):
        raise HTTPException(409, 'This book has not been successfully published yet.')
    relative = normalized(plan['relative'])
    absolute = normalized(plan['library_path'] + '/' + plan['relative'])
    matches = set()
    page = 0
    while True:
        result = request(client, 'GET', f'libraries/{identifier(settings.abs_library_id)}/items',
                         # ABS minified items retain id, libraryId, path and relPath;
                         # resolution has no metadata/name-search fallback.
                         params={'page': page, 'limit': 200, 'minified': 1})
        rows = result['results']
        for item in rows:
            if item.get('libraryId', settings.abs_library_id) != settings.abs_library_id:
                continue
            if ((item.get('path') and normalized(item['path']) == absolute)
                    or (item.get('relPath') and normalized(item['relPath']) == relative)):
                matches.add(item['id'])
                if len(matches) >= 2:
                    raise HTTPException(409, 'Published path is ambiguous in Audiobookshelf. Check the library folders before trying again.')
        if (page + 1) * 200 >= result['total'] or not rows:
            break
        page += 1
    if not matches:
        raise HTTPException(409, 'Not yet in Audiobookshelf — try again after the library scan. Run a library scan in Audiobookshelf if needed, then retry lookup.')
    return matches.pop()
