import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from string import Formatter
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, model_validator

ROOTS = {name: Path(os.getenv('AF_' + name.upper(), default)).resolve() for name, default in
         [('source', '/source'), ('work', '/work'), ('library', '/library'), ('torrents', '/torrents')]}
DATA = Path(os.getenv('AF_DATA', './data/config'))
SECRET_FIELDS = {'qbit_password', 'abs_token'}


class Settings(BaseModel):
    source_path: str = str(ROOTS['source'])
    work_path: str = str(ROOTS['work'])
    library_path: str = str(ROOTS['library'])
    torrent_path: str = str(ROOTS['torrents'])
    monitor_enabled: bool = False
    auto_approve: bool = False
    confidence_threshold: int = Field(85, ge=0, le=100)
    confidence_margin: int = Field(12, ge=0, le=100)
    folder_template: str = '{author}/{series}/{year_prefix}{title}{series_suffix}'
    file_template: str = '{title}{year_suffix}{series_suffix} - {author}'
    timezone: str = 'America/Chicago'
    heavy_start: int = Field(1, ge=0, le=23)
    heavy_end: int = Field(7, ge=0, le=23)
    aac_bitrate: int = Field(96, ge=48, le=320)
    ffmpeg_threads: int = Field(2, ge=1, le=16)
    audible_region: str = 'com'
    qbit_enabled: bool = False
    qbit_url: str = 'http://host.docker.internal:8080'
    qbit_username: str = ''
    qbit_password: str = ''
    qbit_save_path: str = '/downloads/audiobooks/raw'
    abs_enabled: bool = False
    abs_url: str = ''
    abs_token: str = ''
    abs_library_id: str = ''
    write_description: bool = True
    write_reader: bool = True

    @model_validator(mode='after')
    def validate_settings(self):
        ZoneInfo(self.timezone)
        if self.audible_region not in {'com', 'co.uk', 'com.au', 'ca', 'de', 'fr', 'it', 'es', 'co.jp', 'in'}:
            raise ValueError('Unsupported Audible region')
        paths = []
        for key in ROOTS:
            p = Path(getattr(self, key.replace('torrents', 'torrent') + '_path')).resolve()
            if not p.is_relative_to(ROOTS[key]):
                raise ValueError(f'{key} path must be inside mounted root {ROOTS[key]}')
            paths.append(p)
        for i, p in enumerate(paths):
            if any(p.is_relative_to(q) or q.is_relative_to(p) for q in paths[i + 1:]):
                raise ValueError('Source, work, library and torrent paths must not overlap')
        allowed = {'title', 'author', 'narrator', 'year', 'series', 'series_number', 'year_prefix', 'year_suffix', 'series_suffix', 'asin'}
        for template in (self.folder_template, self.file_template):
            if not template or template.startswith(('/', '\\')) or '..' in template:
                raise ValueError('Naming templates must be relative and cannot contain ..')
            for _, field, spec, conversion in Formatter().parse(template):
                if field is not None and (field not in allowed or spec or conversion):
                    raise ValueError('Unsupported naming field or format')
        if '/' in self.file_template or '\\' in self.file_template:
            raise ValueError('Filename template cannot contain folders')
        for url in (self.qbit_url, self.abs_url):
            if url and not url.startswith(('http://', 'https://')):
                raise ValueError('Integration URLs must use http or https')
        return self


@contextmanager
def db():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATA / 'funnel.db', timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init():
    with db() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS packages (id TEXT PRIMARY KEY, source TEXT NOT NULL UNIQUE, status TEXT NOT NULL, error TEXT, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, package_id TEXT NOT NULL REFERENCES packages(id), status TEXT NOT NULL, body TEXT NOT NULL, error TEXT, output TEXT, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, job_id TEXT, message TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS torrents (digest TEXT PRIMARY KEY, name TEXT NOT NULL, submitted REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS scan_requests (id INTEGER PRIMARY KEY, status TEXT NOT NULL, error TEXT, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS runtime (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        c.execute('INSERT OR IGNORE INTO settings VALUES (1, ?)', (Settings().model_dump_json(),))


def settings():
    with db() as c:
        return Settings.model_validate_json(c.execute('SELECT body FROM settings WHERE id=1').fetchone()[0])


def public_settings():
    data = settings().model_dump()
    for key in SECRET_FIELDS:
        data[key + '_configured'] = bool(data[key])
        data[key] = ''
    return data


def event(c, job_id, message):
    c.execute('INSERT INTO events(job_id,message,created) VALUES (?,?,?)', (job_id, message, time.time()))


def unpack(row):
    if row is None:
        raise KeyError('Job not found')
    obj = dict(row)
    obj['body'] = json.loads(obj['body'])
    return obj


def job(job_id):
    with db() as c:
        return unpack(c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())


def update_job(job_id, status, body=None, error=None, output=None, message=None):
    with db() as c:
        c.execute('UPDATE jobs SET status=?,body=COALESCE(?,body),error=?,output=COALESCE(?,output),updated=? WHERE id=?',
                  (status, json.dumps(body) if body is not None else None, error, output, time.time(), job_id))
        event(c, job_id, message or status)


def runtime(key, value):
    with db() as c:
        c.execute('INSERT OR REPLACE INTO runtime VALUES (?,?)', (key, str(value)))
