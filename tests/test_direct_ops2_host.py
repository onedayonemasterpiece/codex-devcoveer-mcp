from __future__ import annotations

import asyncio
import hashlib
import http.server
from pathlib import Path
import socketserver
import sys
import threading
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[1]
RUNTIME=ROOT/"runtime"
sys.path.insert(0,str(RUNTIME))
import direct_ops2_host as host


def run(coro): return asyncio.run(coro)


def test_registered_root_read_stat_hash(tmp_path: Path):
    root=tmp_path/"root"; root.mkdir(); (root/"a.txt").write_text("hello\nTOKEN=secret\n",encoding="utf-8")
    binding={"roots":{"ops":str(root)}}
    stat=host.root_file_stat(binding,{"root":"ops","path":"a.txt"})
    assert stat["type"]=="file" and stat["size"]>0
    digest=host.root_file_sha256(binding,{"root":"ops","path":"a.txt"})
    assert digest["sha256"]==hashlib.sha256((root/"a.txt").read_bytes()).hexdigest()
    read=host.root_read_text(binding,{"root":"ops","path":"a.txt"})
    assert "<redacted>" in read["text"] and "secret" not in read["text"]


def test_process_tree_reads_no_environment(monkeypatch):
    async def fake_show(_cfg): return {"MainPID":str(__import__("os").getpid())}
    monkeypatch.setattr(host,"_systemctl_show",fake_show)
    out=run(host.process_tree({"services":{"self":{"unit":"self.service","scope":"user"}}},{"service":"self","limit":8}))
    assert out["main_pid"]>0 and out["environment_read"] is False
    assert out["processes"] and "argv_shape" in out["processes"][0]


def test_network_dns_and_loopback_http():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.send_header("Content-Type","text/plain"); self.end_headers(); self.wfile.write(b"OK")
        def log_message(self,*_args): pass
    server=socketserver.TCPServer(("127.0.0.1",0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    try:
        port=server.server_address[1]
        dns=run(host.network_probe({"mode":"dns","host":"localhost"}))
        assert any(row["loopback"] for row in dns["addresses"])
        http_result=run(host.network_probe({"mode":"http","url":f"http://127.0.0.1:{port}/","method":"GET"}))
        assert http_result["status"]==200 and http_result["body"]=="OK" and http_result["followed_redirect"] is False
    finally:
        server.shutdown(); server.server_close()


def test_mcp_manifest_returns_version_tools_and_digest(tmp_path: Path):
    bridge=tmp_path/"bridge.py"
    bridge.write_text('BRIDGE_VERSION = "x"\nTOOLS=[types.Tool(name="a", input_schema=A_SCHEMA),types.Tool(name="b", input_schema=B_SCHEMA)]\n',encoding="utf-8")
    out=host.mcp_manifest({"runtime_files":{"bridge":str(bridge)}},{"file":"bridge"})
    assert out["version"]=="x" and out["tool_count"]==2
    assert [x["name"] for x in out["tools"]]==["a","b"] and len(out["catalog_digest"])==64


def test_daemon_reload_requires_registered_user_service(monkeypatch):
    seen={}
    async def fake_run(argv,**kwargs):
        seen["argv"]=argv
        return SimpleNamespace(stdout=b"",stderr=b"",exit_code=0,truncated=False,duration_ms=1)
    monkeypatch.setattr(host,"_run_process",fake_run)
    assert run(host.daemon_reload({"services":{"app":{"unit":"app.service","scope":"user"}}}))["status"]=="ok"
    assert seen["argv"][-1]=="daemon-reload"
    with pytest.raises(host.ProbeError):
        run(host.daemon_reload({"services":{}}))


def test_delete_registered_file_is_alias_and_sha_guarded(tmp_path: Path):
    p=tmp_path/"dropin.conf"; p.write_text("x",encoding="utf-8")
    sha=hashlib.sha256(b"x").hexdigest()
    binding={"host_files":{"dropin":{"path":str(p)}}}
    out=host.delete_registered_file(binding,{"file":"dropin","expected_sha256":sha})
    assert out["old_sha256"]==sha and not p.exists()
