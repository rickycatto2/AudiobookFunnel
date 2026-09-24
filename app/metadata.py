import html
import re
from difflib import SequenceMatcher
from urllib.parse import urlparse

import httpx

FIELDS = ['title', 'author', 'narrator', 'year', 'series', 'series_number', 'genre', 'description', 'publisher', 'copyright', 'isbn', 'asin', 'language', 'cover_url']


def plain(text):
    return html.unescape(re.sub('<[^>]+>', '', str(text or '')))


def asin_from(value):
    value = value.strip()
    if re.fullmatch(r'[A-Za-z0-9]{10}', value):
        return value.upper()
    url = urlparse(value)
    if url.scheme not in ('http', 'https') or not re.fullmatch(r'(?:www\.)?audible\.(?:com|co\.uk|com\.au|ca|de|fr|it|es|co\.jp|in)', url.hostname or ''):
        raise ValueError('Paste an ASIN or an Audible book URL')
    matches = re.findall(r'(?:/)([A-Za-z0-9]{10})(?=/|$)', url.path)
    if not matches:
        raise ValueError('No ASIN found in Audible URL')
    return matches[-1].upper()


def similarity(a, b):
    norm = lambda x: ' '.join(re.findall(r'\w+', str(x).casefold()))
    return SequenceMatcher(None, norm(a), norm(b)).ratio() if a and b else 0


def score(source, candidate):
    signals = []
    for field, weight in [('title', 35), ('author', 25), ('narrator', 10), ('series', 5), ('series_number', 5)]:
        ratio = similarity(source.get(field), candidate.get(field))
        signals.append({'field': field, 'points': round(weight * ratio, 1), 'maximum': weight,
                        'reason': 'missing evidence' if not source.get(field) or not candidate.get(field) else f'{ratio:.0%} similarity'})
    a, b = source.get('duration', 0), candidate.get('duration', 0)
    ratio = max(0, 1 - abs(a - b) / max(a, b) / .15) if a and b else 0
    signals.append({'field': 'duration', 'points': round(15 * ratio, 1), 'maximum': 15, 'reason': f'{a:.0f}s source / {b:.0f}s provider' if a and b else 'missing evidence'})
    identifiers = [k for k in ('asin', 'isbn') if source.get(k) and candidate.get(k)]
    conflict = any(str(source[k]).upper() != str(candidate[k]).upper() for k in identifiers)
    exact = bool(identifiers) and not conflict
    signals.append({'field': 'identifier', 'points': 5 if exact else 0, 'maximum': 5, 'reason': 'conflict: automation blocked' if conflict else 'exact' if exact else 'missing evidence'})
    return {'total': round(sum(x['points'] for x in signals), 1), 'signals': signals, 'conflict': conflict}


def may_automate(source, candidates, settings, grouping_confirmed):
    if not settings.auto_approve or not grouping_confirmed or not candidates:
        return False
    best = candidates[0]
    scoring = best['confidence']
    return (best['provider'] == 'Audible' and not scoring['conflict'] and scoring['total'] >= settings.confidence_threshold
            and scoring['total'] - (candidates[1]['confidence']['total'] if len(candidates) > 1 else 0) >= settings.confidence_margin
            and similarity(source.get('title'), best.get('title')) >= .9
            and similarity(source.get('author'), best.get('author')) >= .9
            and bool(best.get('duration')))


def audible_product(p):
    names = lambda field: '; '.join(x.get('name', '') for x in p.get(field, []))
    series = (p.get('series') or [{}])[0]
    images = p.get('product_images') or {}
    cover = images.get('2400') or images.get('1000') or images.get('500') or next(iter(images.values()), '')
    genres = []
    for ladder in p.get('category_ladders', []):
        for category in ladder.get('ladder', []):
            if category.get('name') and category['name'] not in genres:
                genres.append(category['name'])
    return dict(title=p.get('title', ''), author=names('authors'), narrator=names('narrators'),
                year=(p.get('release_date') or '')[:4], series=series.get('title', ''), series_number=series.get('sequence', ''),
                description=plain(p.get('publisher_summary')), publisher=p.get('publisher_name', ''),
                copyright=plain(p.get('copyright')), asin=p.get('asin', ''), isbn=p.get('isbn', ''), language=p.get('language', ''),
                duration=float(p.get('runtime_length_min') or 0) * 60, genre='; '.join(genres), cover_url=cover, provider='Audible')


def search(provider, query, author='', region='com', asin=''):
    with httpx.Client(timeout=25, follow_redirects=True) as client:
        if provider == 'Audible':
            params = {'response_groups': 'category_ladders,contributors,media,product_desc,product_extended_attrs,product_attrs,series,product_details', 'image_sizes': '500,1000,2400'}
            base = f'https://api.audible.{region}/1.0/catalog/products'
            if asin:
                response = client.get(base + '/' + asin_from(asin), params=params)
                response.raise_for_status()
                return [audible_product(response.json()['product'])]
            params.update({'title': query, 'author': author, 'num_results': 8, 'products_sort_by': 'Relevance'})
            response = client.get(base, params=params)
            response.raise_for_status()
            return [audible_product(p) for p in response.json().get('products', [])]
        if provider == 'Google Books':
            response = client.get('https://www.googleapis.com/books/v1/volumes', params={'q': f'intitle:{query} inauthor:{author}' if author else query, 'maxResults': 8})
            response.raise_for_status()
            result = []
            for item in response.json().get('items', []):
                v = item['volumeInfo']
                result.append(dict(title=v.get('title', ''), author='; '.join(v.get('authors', [])), year=v.get('publishedDate', '')[:4],
                                   description=plain(v.get('description')), publisher=v.get('publisher', ''), language=v.get('language', ''),
                                   isbn=next((x['identifier'] for x in v.get('industryIdentifiers', []) if x['type'] == 'ISBN_13'), ''),
                                   cover_url=v.get('imageLinks', {}).get('thumbnail', '').replace('http://', 'https://'), provider=provider))
            return result
        if provider == 'Open Library':
            response = client.get('https://openlibrary.org/search.json', params={'title': query, 'author': author, 'limit': 8, 'fields': 'key,title,author_name,first_publish_year,isbn,cover_i'})
            response.raise_for_status()
            return [dict(title=v.get('title', ''), author='; '.join(v.get('author_name', [])), year=str(v.get('first_publish_year', '')),
                         isbn=next(iter(v.get('isbn', [])), ''), cover_url=f"https://covers.openlibrary.org/b/id/{v['cover_i']}-L.jpg" if v.get('cover_i') else '', provider=provider)
                    for v in response.json().get('docs', [])]
        raise ValueError('Unknown metadata provider')


def ranked(source, candidates):
    for candidate in candidates:
        candidate['confidence'] = score(source, candidate)
    return sorted(candidates, key=lambda x: x['confidence']['total'], reverse=True)
