from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[1]
RUNTIME=ROOT/"runtime"
sys.path.insert(0,str(RUNTIME))
import direct_ops2_android as android


def run(coro): return asyncio.run(coro)


def result(stdout=b"",stderr=b"",code=0):
    return SimpleNamespace(stdout=stdout,stderr=stderr,exit_code=code,truncated=False,duration_ms=1)


def test_adb_devices_parses_safe_inventory(monkeypatch):
    monkeypatch.setattr(android,"_adb",lambda:"/usr/bin/adb")
    async def fake(argv,**kwargs):
        return result(b"List of devices attached\nemulator-5554 device product:sdk model:Pixel_9 device:emu transport_id:1\n")
    monkeypatch.setattr(android,"_run_process",fake)
    out=run(android.adb_devices({}))
    assert out["devices"]==[{"serial":"emulator-5554","state":"device","product":"sdk","model":"Pixel_9","device":"emu","transport_id":"1"}]


def test_package_state_uses_only_bounded_pm_and_pidof(monkeypatch):
    calls=[]
    monkeypatch.setattr(android,"_adb",lambda:"/usr/bin/adb")
    async def fake(argv,**kwargs):
        calls.append(argv)
        if "pidof" in argv: return result(b"123 124\n")
        return result(b"package:/data/app/base.apk\n")
    monkeypatch.setattr(android,"_run_process",fake)
    out=run(android.adb_package_state({"serial":"emulator-5554","package":"com.example.app"}))
    assert out["installed"] is True and out["pids"]==[123,124]
    assert all("-c" not in call for call in calls)


def test_adb_install_requires_repository_apk(tmp_path: Path,monkeypatch):
    apk=tmp_path/"app.apk"; apk.write_bytes(b"APK")
    monkeypatch.setattr(android,"_adb",lambda:"/usr/bin/adb")
    async def fake(argv,**kwargs):
        assert argv[-1]==str(apk)
        return result(b"Success\n")
    monkeypatch.setattr(android,"_run_process",fake)
    out=run(android.adb_action(tmp_path,{"operation":"install","apk":"app.apk"}))
    assert out["status"]=="ok"
    bad=tmp_path/"x.txt"; bad.write_text("x")
    with pytest.raises(android.ProbeError):
        run(android.adb_action(tmp_path,{"operation":"install","apk":"x.txt"}))


def test_start_activity_binds_component_and_typed_extras(monkeypatch,tmp_path: Path):
    seen={}
    monkeypatch.setattr(android,"_adb",lambda:"/usr/bin/adb")
    async def fake(argv,**kwargs):
        seen["argv"]=argv
        return result(b"Status: ok\n")
    monkeypatch.setattr(android,"_run_process",fake)
    out=run(android.adb_action(tmp_path,{
        "operation":"start_activity","package":"com.example.app",
        "component":"com.example.app/.MainActivity",
        "extras":{"backendUrl":"https://example.invalid","debug":True,"count":2},
    }))
    assert out["status"]=="ok"
    assert "--es" in seen["argv"] and "--ez" in seen["argv"] and "--ei" in seen["argv"]
    with pytest.raises(android.ProbeError):
        run(android.adb_action(tmp_path,{
            "operation":"start_activity","package":"com.example.app",
            "component":"other.app/.MainActivity",
        }))


def test_clear_data_requires_explicit_confirmation(monkeypatch,tmp_path: Path):
    monkeypatch.setattr(android,"_adb",lambda:"/usr/bin/adb")
    with pytest.raises(android.ProbeError) as exc:
        run(android.adb_action(tmp_path,{"operation":"clear_data","package":"com.example.app"}))
    assert exc.value.category=="confirmation_required"


def test_package_logcat_pins_package_pid_and_redacts(monkeypatch):
    monkeypatch.setattr(android,"_adb",lambda:"/usr/bin/adb")
    calls=[]
    async def fake(argv,**kwargs):
        calls.append(argv)
        if "pidof" in argv: return result(b"321\n")
        return result(b"hello TOKEN=supersecret\n")
    monkeypatch.setattr(android,"_run_process",fake)
    out=run(android.adb_package_logcat({"package":"com.example.app","lines":50}))
    assert out["pids"]==[321]
    assert "<redacted>" in out["text"] and "supersecret" not in out["text"]
    assert any("--pid" in call for call in calls)

def test_adb_operation_missing_specific_fields_is_typed_invalid_input(monkeypatch,tmp_path: Path):
    monkeypatch.setattr(android,"_adb",lambda:"/usr/bin/adb")
    for payload in [
        {"operation":"install"},
        {"operation":"uninstall"},
        {"operation":"start_activity","package":"com.example.app"},
        {"operation":"instrument","package":"com.example.app"},
    ]:
        with pytest.raises(android.ProbeError) as exc:
            run(android.adb_action(tmp_path,payload))
        assert exc.value.category=="invalid_input"
