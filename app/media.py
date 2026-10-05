import hashlib
import html
import io
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from fractions import Fraction
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from PIL import Image

AUDIO = {'.mp3', '.m4a', '.m4b', '.flac', '.ogg', '.opus', '.wav', '.aac', '.mp4'}


def run(args, timeout=172800):
    result = subprocess.run(args, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
    if result.returncode:
        raise ValueError(f'{args[0]} failed: {result.stderr[-2000:]}')
    return result.stdout


def probe(path):
    info = json.loads(run(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-show_chapters', '-of', 'json', str(path)], timeout=120))
    audio = [s for s in info.get('streams', []) if s.get('codec_type') == 'audio']
    if len(audio) != 1:
        raise ValueError('Expected exactly one audio stream; review this source outside the automatic pipeline')
    stream = audio[0]
    duration = float(info.get('format', {}).get('duration') or stream.get('duration') or 0)
    duration_note = ''
    # Some long AAC MP4 files report a duration capped at the signed 32-bit
    # boundary. Validate against actual packet timing instead of trusting it.
    if stream['codec_name'] == 'aac' and int(stream.get('duration_ts') or 0) == 2 ** 31:
        time_base = Fraction(stream['time_base'])
        if time_base <= 0:
            raise ValueError('Invalid audio time base')
        packets = run(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_packets',
                       '-show_entries', 'packet=pts,duration', '-of', 'csv=p=0', str(path)], timeout=600)
        start, end, total, count, previous_end = None, 0, 0, 0, None
        for line in io.StringIO(packets):
            values = line.split(',')
            if len(values) < 2:
                continue  # ffprobe may include a separate side-data line
            try:
                pts, length = int(values[0]), int(values[1])
            except ValueError:
                raise ValueError('Cannot validate audio packet timing for this duration header')
            if length <= 0:
                raise ValueError('Invalid audio packet timing')
            if previous_end is not None and abs(pts - previous_end) > 1:
                raise ValueError('Audio packet timing is discontinuous; review the source before processing')
            previous_end = pts + length
            start, end = pts if start is None else min(start, pts), max(end, pts + length)
            total += length
            count += 1
        if not count or abs(float(start * time_base)) > 1 or abs(float((total - (end - start)) * time_base)) > max(2, float(total * time_base) * .0001):
            raise ValueError('Audio packet timing is discontinuous; review the source before processing')
        if stream.get('nb_frames') and count != int(stream['nb_frames']):
            raise ValueError('Audio packet count disagrees with the source; review before processing')
        end = float(end * time_base)
        if end > duration + 2:
            duration_note = f'Source duration header capped at 2^31 ticks; verified {count} audio packets: {duration:.3f}s corrected to {end:.3f}s'
            duration = end
    if duration <= 0:
        raise ValueError('Could not determine positive audio duration')
    tags = {k.lower(): v for k, v in info.get('format', {}).get('tags', {}).items()}
    return {'duration': duration, 'duration_note': duration_note, 'codec': stream['codec_name'], 'sample_rate': stream.get('sample_rate'),
            'channels': stream.get('channels'), 'profile': stream.get('profile'), 'tags': tags,
            'chapters': info.get('chapters', []), 'cover': any(s.get('disposition', {}).get('attached_pic') for s in info.get('streams', []))}


def contained(path, root):
    path, root = Path(path), Path(root).resolve()
    # Reject symlinks/junctions even when their targets happen to be inside the mount.
    for p in [path, *path.parents]:
        if p == root.parent:
            break
        if p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()):
            raise ValueError('Symlinks and junctions are not accepted')
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError('Path escapes its configured root')
    return resolved


def natural(value):
    return [int(x) if x.isdigit() else x.casefold() for x in re.split(r'(\d+)', str(value))]


def inspect_package(source, root):
    source = contained(source, root)
    paths = [source] if source.is_file() else sorted(source.rglob('*'), key=lambda p: natural(p.as_posix()))
    files = []
    for p in paths:
        if p.suffix.lower() not in AUDIO or not p.is_file():
            continue
        contained(p, root)
        info = probe(p)
        rel = p.relative_to(source) if source.is_dir() else Path(p.name)
        info.update(path=str(p), relative=str(rel), size=p.stat().st_size, mtime=p.stat().st_mtime_ns,
                    warning='Short audio or sample/extras: confirm inclusion' if info['duration'] < 120 or re.search(r'\b(sample|extras?|bonus)\b', str(rel), re.I) else '')
        files.append(info)
    if not files:
        raise ValueError('No supported audio found; archives must be extracted into a separate completed source folder')
    return files


def embedded(files, fallback):
    tags = files[0]['tags']
    result = {k: '' for k in ['title', 'author', 'narrator', 'year', 'series', 'series_number', 'genre', 'description', 'publisher', 'copyright', 'isbn', 'asin', 'language', 'cover_url']}
    result.update(title=tags.get('album') or tags.get('title') or fallback,
                  author=tags.get('album_artist') or tags.get('artist', ''), narrator=tags.get('composer', ''),
                  year=tags.get('date', '')[:4], series=tags.get('series', ''), series_number=tags.get('series-part', tags.get('series_number', '')),
                  description=tags.get('description', tags.get('comment', '')), genre=tags.get('genre', ''),
                  publisher=tags.get('publisher', ''), copyright=tags.get('copyright', ''), asin=tags.get('asin', ''), isbn=tags.get('isbn', ''), language=tags.get('language', ''),
                  duration=sum(f['duration'] for f in files))
    return result


def safe_component(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value)).strip(' .')[:120]
    if value in {'.', '..'}:
        return '_'
    if re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', value, re.I):
        value = '_' + value
    return value


def names(meta, settings):
    values = {k: safe_component(meta.get(k, '')) for k in ['title', 'author', 'narrator', 'year', 'series', 'series_number', 'asin']}
    if not values['title'] or not values['author']:
        raise ValueError('Title and author are required')
    values['year_prefix'] = values['year'] + ' - ' if values['year'] else ''
    values['year_suffix'] = ' (' + values['year'] + ')' if values['year'] else ''
    values['series_suffix'] = ' [' + values['series'] + (' ' + values['series_number'] if values['series_number'] else '') + ']' if values['series'] else ''
    folders = [safe_component(x) for x in settings.folder_template.format(**values).replace('\\', '/').split('/') if x.strip()]
    filename = safe_component(settings.file_template.format(**values))
    if not folders or not filename or any(not x for x in folders):
        raise ValueError('Naming template produced an empty path')
    return Path(*folders), filename + '.m4b'


def copy_mode(files):
    return all(f['codec'] == 'aac' for f in files) and len({(f['sample_rate'], f['channels'], f['profile']) for f in files}) == 1


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def cover_download(url, dest):
    from app import covers
    dest.write_bytes(covers.normalize(covers.fetch(url)))


def validate_cover(path):
    with Image.open(path) as image:
        if image.format not in ('JPEG', 'PNG'):
            raise ValueError('Cover must be JPEG or PNG')
        image.verify()


def write_tags(path, meta, cover):
    book = MP4(path)
    mapping = {'title': '\xa9nam', 'author': 'aART', 'narrator': '\xa9wrt', 'year': '\xa9day', 'genre': '\xa9gen', 'description': 'desc', 'copyright': 'cprt'}
    for field, atom in mapping.items():
        if meta.get(field):
            book[atom] = [str(meta[field])]
    book['\xa9alb'] = [meta['title']]
    book['\xa9ART'] = [meta.get('narrator') or meta['author']]
    book['stik'] = [2]
    for field in ('series', 'series_number', 'publisher', 'asin', 'isbn', 'language'):
        if meta.get(field):
            key = 'SERIES-PART' if field == 'series_number' else field.upper()
            book['----:com.apple.iTunes:' + key] = [MP4FreeForm(str(meta[field]).encode())]
    if cover:
        raw = cover.read_bytes()
        book['covr'] = [MP4Cover(raw, imageformat=MP4Cover.FORMAT_PNG if raw.startswith(b'\x89PNG') else MP4Cover.FORMAT_JPEG)]
    book.save()


def escape_ffmetadata(value):
    return str(value).replace('\\', '\\\\').replace('=', '\\=').replace(';', '\\;').replace('#', '\\#').replace('\n', ' ')


class DestinationExists(ValueError):
    """A naming collision, not proof that the audio is identical."""


def process(job, settings):
    body, job_id = job['body'], job['id']
    files, meta = body['files'], body['metadata']
    work = contained(Path(settings.work_path) / job_id, settings.work_path)
    work.mkdir(parents=True, exist_ok=True)
    relative, filename = names(meta, settings)
    # Freeze the publication location before touching audio. A settings change after
    # a crash must not publish the same job again under a different template.
    from app import state
    if not body.get('publication'):
        body['publication'] = {'library_path': settings.library_path, 'relative': str(relative), 'filename': filename}
        with state.db() as c:
            c.execute('UPDATE jobs SET body=? WHERE id=?', (json.dumps(body), job_id))
    plan = body['publication']
    library = contained(plan['library_path'], state.ROOTS['library'])
    filename = plan['filename']
    destination = contained(library / plan['relative'], library)
    if destination.exists():
        manifest = destination / 'funnel.json'
        if manifest.is_file():
            saved = json.loads(manifest.read_text('utf-8'))
            existing = destination / filename
            if saved.get('job_id') == job_id and existing.is_file() and saved.get('sha256') == digest(existing):
                return str(destination)
        raise DestinationExists('Destination already exists; nothing overwritten. This may be a duplicate or a naming collision. Review metadata/naming to keep a different edition.')
    sources = []
    for i, f in enumerate(files):
        src = contained(f['path'], settings.source_path)
        stat = src.stat()
        if stat.st_size != f['size'] or stat.st_mtime_ns != f['mtime']:
            raise ValueError('Source changed since inspection; re-import after confirming download completion')
        staged = work / f'input-{i:05}{src.suffix.lower()}'
        shutil.copy2(src, staged)
        if digest(src) != digest(staged):
            raise ValueError('Staging checksum mismatch')
        if f['codec'] == 'aac' and not f.get('duration_note'):
            checked = probe(staged)
            if checked.get('duration_note'):
                f['original_duration'] = f['duration']
                f['duration'] = checked['duration']
                f['duration_note'] = checked['duration_note']
                body['embedded']['duration'] = sum(part['duration'] for part in files)
                meta['duration'] = body['embedded']['duration']
                with state.db() as c:
                    c.execute('UPDATE jobs SET body=? WHERE id=?', (json.dumps(body), job_id))
                    state.event(c, job_id, checked['duration_note'])
        sources.append(staged)
    cover = None
    choice = body.get('cover_choice', 'embedded')
    if choice == 'manual':
        source_cover = contained(state.DATA / 'covers' / job_id / body['manual_cover']['filename'], state.DATA / 'covers' / job_id)
        cover = work / 'selected-cover'
        shutil.copy2(source_cover, cover)
        validate_cover(cover)
    elif choice == 'provider' and meta.get('cover_url'):
        cover = work / 'selected-cover'
        cover_download(meta['cover_url'], cover)
    elif choice.startswith('local:'):
        index = int(choice.split(':')[1])
        source_cover = contained(body['covers'][index], settings.source_path)
        cover = work / 'selected-cover'
        shutil.copy2(source_cover, cover)
        validate_cover(cover)
    elif choice == 'embedded' and files[0].get('cover'):
        cover = work / 'embedded-cover'
        run(['ffmpeg', '-v', 'error', '-y', '-i', str(sources[0]), '-map', '0:v:0', '-frames:v', '1', '-c:v', 'copy', '-f', 'image2', '-update', '1', str(cover)])
        validate_cover(cover)
    inputs = sources
    direct = copy_mode(files)
    if not direct:
        inputs = []
        for i, src in enumerate(sources):
            target = work / f'part-{i:05}.m4a'
            run(['ffmpeg', '-v', 'error', '-y', '-i', str(src), '-map', '0:a:0', '-vn', '-c:a', 'aac', '-b:a', f'{settings.aac_bitrate}k', '-ar', '44100', '-ac', '2', '-threads', str(settings.ffmpeg_threads), str(target)])
            inputs.append(target)
    concat = work / 'parts.txt'
    # Generated filenames contain no quotes and no user-controlled concat syntax.
    concat.write_text(''.join(f"file '{p.name}'\n" for p in inputs), encoding='utf-8')
    chapters = [';FFMETADATA1']
    offset = 0.0
    for i, f in enumerate(files):
        items = f.get('chapters') or [{'start_time': 0, 'end_time': f['duration'], 'tags': {'title': f'Part {i + 1}'}}]
        for chapter in items:
            start, end = offset + float(chapter['start_time']), offset + float(chapter['end_time'])
            chapters.extend(['[CHAPTER]', 'TIMEBASE=1/1000', f'START={round(start * 1000)}', f'END={round(end * 1000)}', 'title=' + escape_ffmetadata(chapter.get('tags', {}).get('title', f'Part {i + 1}'))])
        offset += f['duration']
    chapter_file = work / 'chapters.txt'
    chapter_file.write_text('\n'.join(chapters), encoding='utf-8')
    output = work / 'book.m4b'
    run(['ffmpeg', '-v', 'error', '-y', '-f', 'concat', '-safe', '1', '-i', str(concat), '-f', 'ffmetadata', '-i', str(chapter_file),
         '-map', '0:a:0', '-map_metadata', '-1', '-map_chapters', '1', '-c:a', 'copy', '-movflags', '+faststart', '-f', 'mp4', str(output)])
    write_tags(output, meta, cover)
    actual = probe(output)['duration']
    if abs(actual - offset) > max(2, offset * .005):
        raise ValueError(f'Output duration differs: expected {offset:.1f}s, got {actual:.1f}s')
    run(['ffmpeg', '-v', 'error', '-xerror', '-i', str(output), '-map', '0:a:0', '-f', 'null', '-'])
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = contained(library / ('.funnel-' + job_id + '-' + uuid.uuid4().hex[:8]), library)
    partial.mkdir()
    shutil.copy2(output, partial / filename)
    if digest(output) != digest(partial / filename):
        raise ValueError('Final copy checksum mismatch')
    if cover:
        ext = '.png' if cover.read_bytes().startswith(b'\x89PNG') else '.jpg'
        shutil.copy2(cover, partial / ('cover' + ext))
    if settings.write_description:
        (partial / 'desc.txt').write_text(meta.get('description', ''), encoding='utf-8')
    if settings.write_reader:
        rows = ''.join(f'<dt>{html.escape(k.replace("_", " ").title())}</dt><dd>{html.escape(str(v))}</dd>' for k, v in meta.items() if v and k not in ('cover_url', 'duration'))
        (partial / 'reader.html').write_text('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>' + html.escape(meta['title']) + '</title><style>body{font:18px/1.6 system-ui;max-width:850px;margin:3rem auto;padding:1rem}dt{font-weight:bold}dd{margin:0 0 1rem;white-space:pre-wrap}</style><h1>' + html.escape(meta['title']) + '</h1><dl>' + rows + '</dl></html>', encoding='utf-8')
    (partial / 'funnel.json').write_text(json.dumps({'job_id': job_id, 'metadata': meta, 'provenance': body['provenance'], 'files': [f['relative'] for f in files], 'sha256': digest(output), 'audio_mode': 'copy' if direct else 'AAC encode'}, indent=2), encoding='utf-8')
    # One worker owns publication. Never use replace(), which can overwrite destinations.
    if destination.exists():
        raise DestinationExists('Destination appeared while processing; nothing overwritten')
    os.rename(partial, destination)
    return str(destination)
