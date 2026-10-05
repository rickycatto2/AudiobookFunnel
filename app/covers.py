"""Bounded image imports; remote connections are pinned to validated public IPs."""
import http.client
import ipaddress
import io
import socket
import ssl
from urllib.parse import urljoin, urlsplit

from PIL import Image, UnidentifiedImageError

LIMIT = 15 * 1024 * 1024


def public_target(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError('Use a direct HTTPS image URL on the standard HTTPS port')
    addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('Cover URLs must point to a public internet image, not a local/private address')
    return parsed, addresses[0][4][0]


def fetch(url):
    for _ in range(5):
        parsed, address = public_target(url)
        connection = http.client.HTTPSConnection(parsed.hostname, timeout=30)
        def connect():
            raw = socket.create_connection((address, 443), timeout=30)
            try:
                connection.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=parsed.hostname)
            except Exception:
                raw.close()
                raise
        connection.connect = connect
        try:
            target = parsed.path or '/'
            if parsed.query:
                target += '?' + parsed.query
            connection.request('GET', target, headers={'User-Agent': 'AudiobookFunnel/1.0', 'Accept': 'image/*'})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location')
                if not location:
                    raise ValueError('Cover redirect has no destination')
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise ValueError(f'Cover server returned HTTP {response.status}; try another URL or upload the image')
            data = response.read(LIMIT + 1)
            if len(data) > LIMIT:
                raise ValueError('Cover exceeds 15 MB')
            return data
        finally:
            connection.close()
    raise ValueError('Cover URL redirects too many times')


def normalize(data):
    if not data or len(data) > LIMIT:
        raise ValueError('Choose an image up to 15 MB')
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in {'JPEG', 'PNG', 'WEBP'} or image.width * image.height > 20_000_000:
                raise ValueError('Use a JPEG, PNG, or WebP cover under 20 megapixels')
            image.load()
            if image.mode in ('RGBA', 'LA') or 'transparency' in image.info:
                rgba = image.convert('RGBA')
                clean = Image.new('RGB', image.size, 'white')
                clean.paste(rgba, mask=rgba.getchannel('A'))
            else:
                clean = image.convert('RGB')
            output = io.BytesIO()
            clean.save(output, format='JPEG', quality=95)
            return output.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError('This is not a readable image. Use a direct image URL or upload a JPEG, PNG, or WebP file.') from exc
