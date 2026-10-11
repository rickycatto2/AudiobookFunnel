import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

STATIC_JS = Path(__file__).parent.parent / 'app' / 'static' / 'app.js'
HARNESS = Path(__file__).with_name('ui_harness.cjs')


def test_review_queue_static_ui_contract():
    """The static UI keeps the review affordances the dashboard relies on."""
    client = TestClient(app)
    assert client.get('/').status_code == 200
    assert '<option>REVIEW</option>' in client.get('/static/index.html').text
    js = client.get('/static/app.js').text
    # The needs-review total is a real button, so it is reachable by keyboard.
    assert 'class="count count-filter" data-count-filter="REVIEW"' in js
    assert 'aria-label="Needs review: ${total}' in js
    assert '<div class="count">${box}</div>' in js
    # Approval stays one explicit code path that always sends force_now.
    assert js.count('id="approve"') == 1
    assert js.count('id="approve-top"') == 1
    assert js.count('/approve`, "POST", { force_now: force }') == 1
    assert js.count('approve(false)') == 2  # bottom and top Save & approve buttons
    # Other navigation still resets the viewport on purpose.
    assert 'function screen(name) {' in js
    assert js.count('window.scrollTo(0, 0)') == 1  # only screen() jumps to top
    # Test scaffolding must never ship inside the served script.
    assert 'globalThis.__hooks' not in js


@pytest.mark.skipif(shutil.which('node') is None, reason='node is required for the UI harness')
def test_review_queue_ui_interactions():
    """Exercise the real app.js against a stub DOM: counts, filter, scroll."""
    result = subprocess.run(
        ['node', str(HARNESS)], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'FAIL' not in result.stdout
    assert 'all UI checks passed' in result.stdout
