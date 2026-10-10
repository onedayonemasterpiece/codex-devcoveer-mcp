"""The stable gateway exposes only bounded, passive native history reads."""
import asyncio
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

import direct_ops_gateway as gateway
import direct_probes
import native_thread_links

UUID = "01a121f0-9d2b-7da0-9d06-cffd2c97e871"


def test_native_thread_gateway_schema_and_discovery():
    result = gateway.describe_plane("host_probe", "native_thread_read")
    assert "native_thread_read" in result["operations"]
    validator = Draft202012Validator(result["payload_schema"])
    assert not list(validator.iter_errors({"thread": UUID, "cursor": 16}))
    for bad in ({"thread": UUID, "path": "/private"},
                {"thread": UUID, "cursor": -1},
                {"thread": UUID, "limit": 101},
                {"thread": UUID, "snapshot": {"bytes": 2}}):
        assert list(validator.iter_errors(bad))


def test_native_thread_host_dispatch_is_passive(monkeypatch):
    seen = {}
    def fake(reference, **kwargs):
        seen.update(reference=reference, **kwargs)
        return {"status": "ok", "complete": True, "history": []}
    monkeypatch.setattr(native_thread_links, "read_native_thread", fake)
    data, source = asyncio.run(direct_probes._one_host({}, {
        "kind": "native_thread_read", "thread": UUID,
    }))
    assert source == "verified-native-visible-history"
    assert data["complete"] is True
    assert seen == {"reference": UUID, "detail": "full", "cursor": 0,
                    "limit": 40, "include_tools": False, "snapshot": None}


def test_gateway_forwards_snapshot_and_tools(monkeypatch, tmp_path: Path):
    seen = {}
    async def fake(root, project, checks, receipt_surface="typed"):
        seen.update(root=root, project=project, checks=checks)
        return {"execution": "direct"}
    monkeypatch.setattr(gateway, "run_host_probe", fake)
    token = {"version": 1, "bytes": 12, "sha256": "a" * 64,
             "threadId": UUID, "includeTools": True}
    result = asyncio.run(gateway.run_direct_ops_v2(
        str(tmp_path), "operator", "host_probe", "native_thread_read",
        {"thread": UUID, "cursor": 16, "include_tools": True, "snapshot": token}))
    assert result["execution"] == "direct"
    assert seen["checks"][0]["snapshot"] == token
    assert seen["checks"][0]["cursor"] == 16
    assert seen["project"] == "operator"


def test_host_dispatch_preserves_reader_errors(monkeypatch):
    def fake(*args, **kwargs):
        raise native_thread_links.NativeThreadError("snapshot_mismatch", "Snapshot changed")
    monkeypatch.setattr(native_thread_links, "read_native_thread", fake)
    with pytest.raises(direct_probes.ProbeError) as error:
        asyncio.run(direct_probes._one_host({}, {"kind": "native_thread_read", "thread": UUID}))
    assert error.value.category == "snapshot_mismatch"
