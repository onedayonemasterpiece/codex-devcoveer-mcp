from __future__ import annotations

import asyncio
import ctypes
import json
import os
import signal
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


OPENCODE_BINARY = Path("/home/dev/.opencode/bin/opencode")
MAX_WIRE_LINE = 1_000_000
MAX_STDERR_BYTES = 131_072
DEFAULT_TIMEOUT_SECONDS = 180.0

# Linux Landlock (asm-generic syscall numbers, shared by x86_64/aarch64).
_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1

_EXECUTE = 1 << 0
_WRITE_FILE = 1 << 1
_READ_FILE = 1 << 2
_READ_DIR = 1 << 3
_REMOVE_DIR = 1 << 4
_REMOVE_FILE = 1 << 5
_MAKE_CHAR = 1 << 6
_MAKE_DIR = 1 << 7
_MAKE_REG = 1 << 8
_MAKE_SOCK = 1 << 9
_MAKE_FIFO = 1 << 10
_MAKE_BLOCK = 1 << 11
_MAKE_SYM = 1 << 12
_REFER = 1 << 13
_TRUNCATE = 1 << 14

_PR_SET_NO_NEW_PRIVS = 38


class IsolatedAcpError(RuntimeError):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class IsolatedAcpResult:
    text: str
    stop_reason: str
    usage: dict[str, Any] | None
    duration_ms: int


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]


def _syscall(number: int, *args: Any) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    result = int(libc.syscall(number, *args))
    if result < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return result


def landlock_abi() -> int:
    try:
        return _syscall(
            _SYS_LANDLOCK_CREATE_RULESET,
            ctypes.c_void_p(),
            ctypes.c_size_t(0),
            ctypes.c_uint32(_LANDLOCK_CREATE_RULESET_VERSION),
        )
    except OSError as exc:
        raise IsolatedAcpError(
            "sandbox_unavailable",
            f"Landlock is unavailable on this host: {exc}",
        ) from exc


def _handled_fs(abi: int) -> int:
    access = (
        _EXECUTE
        | _WRITE_FILE
        | _READ_FILE
        | _READ_DIR
        | _REMOVE_DIR
        | _REMOVE_FILE
        | _MAKE_CHAR
        | _MAKE_DIR
        | _MAKE_REG
        | _MAKE_SOCK
        | _MAKE_FIFO
        | _MAKE_BLOCK
        | _MAKE_SYM
    )
    if abi >= 2:
        access |= _REFER
    if abi >= 3:
        access |= _TRUNCATE
    return access


def _allow_path(ruleset_fd: int, path: Path, access: int) -> None:
    if not path.exists():
        return
    flags = getattr(os, "O_PATH", os.O_RDONLY) | os.O_CLOEXEC
    fd = os.open(path, flags)
    try:
        attr = _PathBeneathAttr(
            allowed_access=access,
            parent_fd=fd,
        )
        _syscall(
            _SYS_LANDLOCK_ADD_RULE,
            ctypes.c_int(ruleset_fd),
            ctypes.c_int(_LANDLOCK_RULE_PATH_BENEATH),
            ctypes.byref(attr),
            ctypes.c_uint32(0),
        )
    finally:
        os.close(fd)


def _apply_landlock(root: Path, opencode_binary: Path) -> int:
    abi = landlock_abi()
    handled = _handled_fs(abi)
    ruleset = _RulesetAttr(handled_access_fs=handled)
    try:
        ruleset_fd = _syscall(
            _SYS_LANDLOCK_CREATE_RULESET,
            ctypes.byref(ruleset),
            ctypes.sizeof(ruleset),
            ctypes.c_uint32(0),
        )
    except OSError as exc:
        raise IsolatedAcpError(
            "sandbox_unavailable",
            f"Landlock ruleset creation failed: {exc}",
        ) from exc

    read_only = _READ_FILE | _READ_DIR
    read_exec = read_only | _EXECUTE
    full_without_exec = handled & ~_EXECUTE

    try:
        # The only writable filesystem tree visible to the model process.
        _allow_path(ruleset_fd, root, full_without_exec)

        # OpenCode itself must remain executable. Nothing else under /home/dev
        # is allowed by the ruleset.
        _allow_path(ruleset_fd, opencode_binary.parent, read_exec)

        # Runtime libraries/configuration required by a standalone Linux binary.
        # System files are readable, never writable; ordinary executables under
        # /usr and /bin are deliberately not executable.
        for path in (Path("/usr"), Path("/etc")):
            _allow_path(ruleset_fd, path, read_only)
        for path in (Path("/lib"), Path("/lib64")):
            _allow_path(ruleset_fd, path, read_exec)

        for path in (
            Path("/dev/null"),
            Path("/dev/urandom"),
            Path("/dev/random"),
        ):
            _allow_path(ruleset_fd, path, _READ_FILE | _WRITE_FILE)

        # Bun/OpenCode may inspect its own executable/process metadata. The
        # environment is sanitized before the sandbox is entered.
        _allow_path(ruleset_fd, Path("/proc/self"), read_only)
        _allow_path(ruleset_fd, Path("/run/systemd/resolve"), read_only)
        _allow_path(ruleset_fd, Path("/sys/devices/system/cpu"), read_only)
        _allow_path(ruleset_fd, Path("/sys/devices/system/node"), read_only)

        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))
        _syscall(
            _SYS_LANDLOCK_RESTRICT_SELF,
            ctypes.c_int(ruleset_fd),
            ctypes.c_uint32(0),
        )
    except OSError as exc:
        raise IsolatedAcpError(
            "sandbox_unavailable",
            f"Landlock policy installation failed: {exc}",
        ) from exc
    finally:
        os.close(ruleset_fd)
    return abi


def _sandbox_environment(root: Path) -> dict[str, str]:
    home = root / "home"
    tmp = root / "tmp"
    for path in (
        home,
        home / ".config",
        home / ".local" / "share",
        home / ".cache",
        tmp,
    ):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "TMPDIR": str(tmp),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
        "TERM": "dumb",
    }


def sandbox_exec(root: Path, work: Path, opencode_binary: Path = OPENCODE_BINARY) -> None:
    root = root.resolve(strict=True)
    work = work.resolve(strict=True)
    binary = opencode_binary.resolve(strict=True)
    if work.parent != root:
        raise IsolatedAcpError("sandbox_invalid", "Work directory is outside sandbox root")
    if binary != OPENCODE_BINARY.resolve(strict=True):
        raise IsolatedAcpError("sandbox_invalid", "Unexpected OpenCode executable")
    env = _sandbox_environment(root)
    os.chdir(work)
    _apply_landlock(root, binary)
    os.execve(str(binary), [str(binary), "acp"], env)


async def _drain_stderr(stream: asyncio.StreamReader) -> bytes:
    data = bytearray()
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        if len(data) < MAX_STDERR_BYTES:
            data.extend(chunk[: MAX_STDERR_BYTES - len(data)])
    return bytes(data)


class _AcpWire:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.next_id = 1
        self.assistant_chunks: list[str] = []
        self.tool_attempted = False
        self.cancel_sent = False
        self.usage: dict[str, Any] | None = None

    async def _write(self, value: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise IsolatedAcpError("transport", "ACP stdin is unavailable")
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_WIRE_LINE:
            raise IsolatedAcpError("transport", "ACP request is too large")
        self.process.stdin.write(raw + b"\n")
        await self.process.stdin.drain()

    async def _read(self) -> dict[str, Any]:
        if self.process.stdout is None:
            raise IsolatedAcpError("transport", "ACP stdout is unavailable")
        raw = await self.process.stdout.readline()
        if not raw:
            raise IsolatedAcpError("transport", "ACP process closed stdout")
        if len(raw) > MAX_WIRE_LINE:
            raise IsolatedAcpError("transport", "ACP response line is too large")
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise IsolatedAcpError("transport", "ACP returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise IsolatedAcpError("transport", "ACP returned a non-object message")
        return value

    async def _cancel(self, session_id: str) -> None:
        if self.cancel_sent:
            return
        self.cancel_sent = True
        await self._write(
            {
                "jsonrpc": "2.0",
                "method": "session/cancel",
                "params": {"sessionId": session_id},
            }
        )

    async def _handle_inbound(self, message: dict[str, Any], session_id: str | None) -> None:
        method = message.get("method")
        if method == "session/request_permission" and "id" in message:
            self.tool_attempted = True
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {"outcome": {"outcome": "cancelled"}},
                }
            )
            if session_id:
                await self._cancel(session_id)
            return
        if method != "session/update":
            if method and "id" in message:
                await self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {"code": -32601, "message": "Unsupported ACP callback"},
                    }
                )
            return
        params = message.get("params")
        update = params.get("update") if isinstance(params, dict) else None
        if not isinstance(update, dict):
            return
        kind = str(update.get("sessionUpdate") or "")
        if kind == "agent_message_chunk":
            content = update.get("content")
            if isinstance(content, dict) and content.get("type") == "text":
                text = content.get("text")
                if isinstance(text, str):
                    self.assistant_chunks.append(text)
        elif kind in {"tool_call", "tool_call_update"}:
            self.tool_attempted = True
            if session_id:
                await self._cancel(session_id)
        elif kind == "usage_update":
            self.usage = dict(update)

    async def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        session_id: str | None = None,
        timeout: float,
    ) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        await self._write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )

        async def wait() -> dict[str, Any]:
            while True:
                message = await self._read()
                if message.get("id") == request_id and "method" not in message:
                    if "error" in message:
                        error = message.get("error")
                        raise IsolatedAcpError(
                            "acp_error",
                            str(error.get("message") if isinstance(error, dict) else error),
                        )
                    result = message.get("result")
                    if not isinstance(result, dict):
                        raise IsolatedAcpError("transport", f"ACP {method} returned no result")
                    return result
                await self._handle_inbound(message, session_id)

        return await asyncio.wait_for(wait(), timeout=timeout)


async def run_isolated_acp_prompt(
    *,
    model: str,
    prompt: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    opencode_binary: Path = OPENCODE_BINARY,
) -> IsolatedAcpResult:
    clean_model = str(model or "").strip()
    clean_prompt = str(prompt or "").strip()
    if not clean_model.startswith("opencode/") or len(clean_model) > 220:
        raise IsolatedAcpError("validation", "Only exact OpenCode models are supported")
    if not clean_prompt or len(clean_prompt.encode("utf-8")) > 100_000:
        raise IsolatedAcpError("validation", "ACP prompt is empty or too large")
    if not opencode_binary.is_file() or opencode_binary.is_symlink():
        raise IsolatedAcpError("sandbox_unavailable", "OpenCode executable is unavailable")

    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="devcoveer-acp-isolated-") as temp:
        root = Path(temp).resolve()
        os.chmod(root, 0o700)
        work = root / "work"
        work.mkdir(mode=0o700)

        process = await asyncio.create_subprocess_exec(
            os.environ.get("PYTHON", "/usr/bin/python3"),
            str(Path(__file__).resolve()),
            "--sandbox-launch",
            str(root),
            str(work),
            str(opencode_binary),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(work),
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PYTHONUNBUFFERED": "1",
            },
            limit=MAX_WIRE_LINE + 1,
            start_new_session=True,
        )
        if process.stderr is None:
            raise IsolatedAcpError("transport", "ACP stderr is unavailable")
        stderr_task = asyncio.create_task(_drain_stderr(process.stderr))
        wire = _AcpWire(process)
        session_id: str | None = None
        try:
            init = await wire.request(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {},
                    "clientInfo": {
                        "name": "devcoveer-isolated-analysis",
                        "title": "DevCoveer Isolated Analysis",
                        "version": "1",
                    },
                },
                timeout=20.0,
            )
            if int(init.get("protocolVersion") or 0) != 1:
                raise IsolatedAcpError("transport", "Unsupported ACP protocol version")

            created = await wire.request(
                "session/new",
                {"cwd": str(work), "mcpServers": []},
                timeout=20.0,
            )
            raw_session = created.get("sessionId")
            if not isinstance(raw_session, str) or not raw_session:
                raise IsolatedAcpError("transport", "ACP session/new returned no sessionId")
            session_id = raw_session

            await wire.request(
                "session/set_config_option",
                {
                    "sessionId": session_id,
                    "configId": "model",
                    "value": clean_model,
                },
                session_id=session_id,
                timeout=20.0,
            )

            result = await wire.request(
                "session/prompt",
                {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": clean_prompt}],
                },
                session_id=session_id,
                timeout=timeout_seconds,
            )
            stop_reason = str(result.get("stopReason") or "")
            if wire.tool_attempted:
                raise IsolatedAcpError(
                    "isolation_violation",
                    "OpenCode attempted a tool call during provided-only analysis",
                )
            text = "".join(wire.assistant_chunks).strip()
            if stop_reason != "end_turn":
                raise IsolatedAcpError(
                    "model_error",
                    f"OpenCode ACP ended with stopReason={stop_reason or 'unknown'}",
                )
            if not text:
                raise IsolatedAcpError("empty_output", "OpenCode ACP returned no visible output")
            usage = result.get("usage")
            if not isinstance(usage, dict):
                usage = wire.usage
            return IsolatedAcpResult(
                text=text,
                stop_reason=stop_reason,
                usage=usage if isinstance(usage, dict) else None,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
        except asyncio.TimeoutError as exc:
            if session_id:
                try:
                    await wire._cancel(session_id)
                except Exception:
                    pass
            raise IsolatedAcpError("timeout", "OpenCode ACP prompt timed out") from exc
        finally:
            if process.stdin is not None:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
            try:
                await asyncio.wait_for(stderr_task, timeout=1.0)
            except asyncio.TimeoutError:
                stderr_task.cancel()


def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--sandbox-launch", action="store_true")
    parser.add_argument("root", nargs="?")
    parser.add_argument("work", nargs="?")
    parser.add_argument("opencode", nargs="?")
    args = parser.parse_args()
    if not args.sandbox_launch or not args.root or not args.work or not args.opencode:
        raise SystemExit("internal sandbox launcher only")
    sandbox_exec(Path(args.root), Path(args.work), Path(args.opencode))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
