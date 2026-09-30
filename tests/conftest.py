"""Keep source verification out of the installed operator's runtime state."""
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))

@pytest.fixture(autouse=True)
def isolated_process_state(monkeypatch, tmp_path):
    import process_recovery
    import direct_probe_core
    monkeypatch.setattr(process_recovery, 'DB_PATH', tmp_path / 'operator-state/process.sqlite3')
    monkeypatch.setattr(direct_probe_core, 'DIRECT_RECEIPT_LOG', tmp_path / 'operator-state/receipts.jsonl')
    token = process_recovery.CONTEXT.set({})
    yield
    process_recovery.CONTEXT.reset(token)
