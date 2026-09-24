import os
import tempfile
from pathlib import Path

import pytest

# Set mount roots before application modules are imported.
_root = Path(tempfile.mkdtemp(prefix='funnel-tests-'))
for name in ('source', 'work', 'library', 'torrents', 'data'):
    os.environ['AF_' + name.upper()] = str(_root / name)

from app import state


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setattr(state, 'DATA', tmp_path / 'config')
    paths = {}
    for key, root in state.ROOTS.items():
        path = root / tmp_path.name
        path.mkdir(parents=True, exist_ok=True)
        paths[key.replace('torrents', 'torrent') + '_path'] = str(path)
    state.init()
    settings = state.Settings(**paths)
    with state.db() as c:
        c.execute('UPDATE settings SET body=?', (settings.model_dump_json(),))
    return settings
