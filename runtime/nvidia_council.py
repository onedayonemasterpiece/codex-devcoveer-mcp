"""Direct, provided-context-only NVIDIA council transport with two credential slots.

This module never exposes credential values. It discovers distinct NVIDIA API keys
from the trusted OpenCode provider EnvironmentFile, coordinates two cross-process
slot locks, and invokes NVIDIA's OpenAI-compatible chat completion endpoint.
"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Any
import urllib.error
import urllib.request


NVIDIA_ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"
PROVIDER_ENVS = (
    Path("/home/dev/.config/opencode/devcoveer-providers.env"),
    Path("/home/dev/.local/state/regional-knowledge-base/service.env"),
)
SLOT_ROOT = Path("/home/dev/.local/share/openai-codex-mcp/nvidia-council-slots")
MAX_SLOTS = 2
TRANSIENT_RETRY_DELAYS = (5.0, 15.0, 30.0, 60.0)
_KEY_NAME_RE = re.compile(r"^(?:NVIDIA[A-Z0-9_]*KEY[A-Z0-9_]*|NVCF[A-Z0-9_]*KEY[A-Z0-9_]*)$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{1,180}$")
_IN_PROCESS = set()
_IN_PROCESS_LOCK = threading.Lock()

MODEL_PROFILES: dict[str, dict[str, Any]] = {
    "moonshotai/kimi-k3": {
        "temperature": 1.0,
        "max_tokens": 16384,
        "reasoning_effort": "max",
    },
    "deepseek-ai/deepseek-v4.1-flash": {
        "temperature": 1.0,
        "top_p": 0.95,
        "max_tokens": 16384,
    },
}


class NvidiaCouncilError(RuntimeError):
    def __init__(self, category: str, message: str, *, status: int | None = None):
        super().__init__(message)
        self.category = category
        self.status = status


def _private_regular(path: Path) -> None:
    info = path.lstat()
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(info.st_mode) & 0o077:
        raise NvidiaCouncilError(
            "configuration",
            "NVIDIA provider EnvironmentFile must be a private regular file",
        )


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value


def _source_paths(
    paths: Path | tuple[Path, ...] | list[Path],
) -> tuple[Path, ...]:
    return (paths,) if isinstance(paths, Path) else tuple(paths)


def _candidate_keys(
    paths: Path | tuple[Path, ...] | list[Path] = PROVIDER_ENVS,
) -> list[tuple[str, str]]:
    source_paths = _source_paths(paths)
    found: list[tuple[str, str, str]] = []
    seen_fingerprints: set[str] = set()
    for path in source_paths:
        if not path.exists():
            continue
        try:
            _private_regular(path)
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise NvidiaCouncilError(
                "configuration",
                f"NVIDIA provider EnvironmentFile is unavailable: {path.name}",
            ) from exc

        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            name, value = line.split("=", 1)
            name = name.strip().upper()
            if not _KEY_NAME_RE.fullmatch(name):
                continue
            value = _unquote(value)
            if not value or len(value) > 4096:
                continue
            fingerprint = hashlib.sha256(value.encode("utf-8")).hexdigest()
            if fingerprint in seen_fingerprints:
                continue
            seen_fingerprints.add(fingerprint)
            found.append((path.name, name, value))

    found.sort(key=lambda item: (item[0], item[1]))
    return [(f"{source}:{name}", value) for source, name, value in found[:MAX_SLOTS]]


def slot_metadata(
    paths: Path | tuple[Path, ...] | list[Path] = PROVIDER_ENVS,
) -> dict[str, Any]:
    source_paths = _source_paths(paths)
    slots = _candidate_keys(source_paths)
    return {
        "configured": len(slots) >= MAX_SLOTS,
        "slotCount": len(slots),
        "requiredSlots": MAX_SLOTS,
        "sourceNames": [name for name, _value in slots],
        "configuredSourceFiles": [path.name for path in source_paths if path.exists()],
        "credentialValuesExposed": False,
    }


def _slot_root() -> Path:
    SLOT_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(SLOT_ROOT, 0o700)
    return SLOT_ROOT


def _try_acquire_slot(keys: list[tuple[str, str]]) -> tuple[int, int, str] | None:
    root = _slot_root()
    for index, (_name, key) in enumerate(keys):
        with _IN_PROCESS_LOCK:
            if index in _IN_PROCESS:
                continue
            _IN_PROCESS.add(index)
        fd = -1
        try:
            path = root / f"slot-{index}.lock"
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
            os.chmod(path, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                fd = -1
                with _IN_PROCESS_LOCK:
                    _IN_PROCESS.discard(index)
                continue
            return index, fd, key
        except BaseException:
            if fd >= 0:
                os.close(fd)
            with _IN_PROCESS_LOCK:
                _IN_PROCESS.discard(index)
            raise
    return None


def _release_slot(index: int, fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
        with _IN_PROCESS_LOCK:
            _IN_PROCESS.discard(index)


def _acquire_slot(
    keys: list[tuple[str, str]], *, timeout_seconds: float
) -> tuple[int, int, str, int]:
    started = time.monotonic()
    deadline = started + max(1.0, timeout_seconds)
    while True:
        result = _try_acquire_slot(keys)
        if result is not None:
            index, fd, key = result
            return index, fd, key, round((time.monotonic() - started) * 1000)
        if time.monotonic() >= deadline:
            raise NvidiaCouncilError(
                "capacity",
                "Both NVIDIA council credential slots are busy",
            )
        time.sleep(0.1)


def _category(status: int) -> str:
    if status in {401, 403}:
        return "credential"
    if status == 429:
        return "rate_limit"
    if status >= 500:
        return "provider_unavailable"
    return "provider_error"


def _extract_event_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    delta = first.get("delta")
    if isinstance(delta, dict) and isinstance(delta.get("content"), str):
        return delta["content"]
    message = first.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    return ""


def _invoke_once(
    *, key: str, model: str, prompt: str, timeout_seconds: float
) -> tuple[str, dict[str, Any] | None]:
    profile = dict(MODEL_PROFILES.get(model, {}))
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "temperature": float(profile.pop("temperature", 0.6)),
        "max_tokens": int(profile.pop("max_tokens", 8192)),
        **profile,
    }
    request = urllib.request.Request(
        NVIDIA_ENDPOINT,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": "Bearer " + key,
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": "devcoveer-nvidia-council/1",
        },
    )
    chunks: list[str] = []
    usage: dict[str, Any] | None = None
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                value = line[5:].strip()
                if value == "[DONE]":
                    break
                try:
                    event = json.loads(value)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                error = event.get("error")
                if isinstance(error, dict):
                    raise NvidiaCouncilError(
                        "provider_error",
                        str(error.get("message") or "NVIDIA provider error")[:500],
                    )
                text = _extract_event_text(event)
                if text:
                    chunks.append(text)
                if isinstance(event.get("usage"), dict):
                    usage = dict(event["usage"])
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        raise NvidiaCouncilError(
            _category(status),
            f"NVIDIA chat completion failed with HTTP {status}",
            status=status,
        ) from exc
    except urllib.error.URLError as exc:
        raise NvidiaCouncilError(
            "provider_unavailable", "NVIDIA chat completion transport failed"
        ) from exc

    text = "".join(chunks).strip()
    if not text:
        raise NvidiaCouncilError("empty_output", "NVIDIA model returned no visible output")
    return text, usage


def _invoke_blocking(
    *,
    model: str,
    prompt: str,
    capacity_timeout_seconds: float,
    request_timeout_seconds: float,
) -> dict[str, Any]:
    if not _MODEL_RE.fullmatch(model):
        raise NvidiaCouncilError("validation", "Invalid NVIDIA model identifier")
    clean_prompt = str(prompt or "").strip()
    if not clean_prompt or len(clean_prompt.encode("utf-8")) > 180_000:
        raise NvidiaCouncilError("validation", "NVIDIA council prompt is empty or too large")

    keys = _candidate_keys()
    if len(keys) < MAX_SLOTS:
        raise NvidiaCouncilError(
            "configuration",
            "Two distinct NVIDIA council credentials are required",
        )

    attempts = 0
    total_wait_ms = 0
    started = time.monotonic()
    last_error: NvidiaCouncilError | None = None
    delays = (0.0,) + TRANSIENT_RETRY_DELAYS
    for attempt, delay in enumerate(delays, start=1):
        if delay:
            time.sleep(delay)
        index, fd, key, waited_ms = _acquire_slot(
            keys, timeout_seconds=capacity_timeout_seconds
        )
        total_wait_ms += waited_ms
        attempts = attempt
        try:
            text, usage = _invoke_once(
                key=key,
                model=model,
                prompt=clean_prompt,
                timeout_seconds=request_timeout_seconds,
            )
            return {
                "text": text,
                "usage": usage,
                "slot": index,
                "attempts": attempts,
                "capacityWaitMs": total_wait_ms,
                "latencyMs": round((time.monotonic() - started) * 1000),
            }
        except NvidiaCouncilError as exc:
            last_error = exc
            if exc.category not in {"rate_limit", "provider_unavailable"}:
                raise
            if attempt >= len(delays):
                raise
        finally:
            _release_slot(index, fd)
    assert last_error is not None
    raise last_error


async def invoke_nvidia(
    *,
    model: str,
    prompt: str,
    capacity_timeout_seconds: float = 900.0,
    request_timeout_seconds: float = 900.0,
) -> dict[str, Any]:
    return await asyncio.to_thread(
        _invoke_blocking,
        model=model,
        prompt=prompt,
        capacity_timeout_seconds=capacity_timeout_seconds,
        request_timeout_seconds=request_timeout_seconds,
    )
