"""Test isolation for the live trade journal.

Several tests import ggpbot, which opens the configured SQLite trade journal at
import time and may persist drawdown-cooldown state. Redirect that path to a
temporary directory so the repository's real ``data/ggp_trade_journal.sqlite3``
is never read or modified by the test suite. Production behaviour is unchanged.
"""
import atexit
import os
import shutil
import tempfile

import pytest

_TEST_JOURNAL_DIR = tempfile.mkdtemp(prefix="ggp-pytest-journal-")
atexit.register(lambda: shutil.rmtree(_TEST_JOURNAL_DIR, ignore_errors=True))
os.environ["TRADE_JOURNAL_PATH"] = os.path.join(_TEST_JOURNAL_DIR, "ggp_trade_journal.sqlite3")


@pytest.fixture(autouse=True)
def _profit_brain_legacy_test_default(monkeypatch):
    """Keep legacy-focused tests on the explicit fallback path.

    The production default is Brain ON / Shadow OFF. Tests that exercise the
    Brain explicitly override these attributes; the rest keep their historical
    legacy-Board expectations.
    """
    import ggpbot
    monkeypatch.setattr(ggpbot, "ENABLE_UNIFIED_PROFIT_BRAIN", False)
    monkeypatch.setattr(ggpbot, "PROFIT_BRAIN_SHADOW_MODE", True)
