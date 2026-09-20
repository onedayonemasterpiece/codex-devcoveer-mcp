"""Narrow allowlisted Street Story deployment authority for DevCoveer2.

This module is intentionally not a generic command runner.  It may deploy only
one repository/branch, one loopback service, and one public hostname.  Dynamic
release SHAs are accepted only when they equal the current remote branch HEAD.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


TARGET_REPOSITORY = "onedayonemasterpiece/street-story"
TARGET_BRANCH = "work/street-story-mvp-20260908"
TARGET_HOSTNAME = "street-story.kenigevents.ru"
TARGET_PORT = 8188
TARGET_SERVICE = "street-story.service"
PROJECT_ROOT = Path("/home/dev/projects/street-story")
STATE_ROOT = Path("/home/dev/.local/state/openai-codex-mcp/street-story-operator")
RELEASES_ROOT = Path("/home/dev/.local/share/street-story/releases")
CURRENT_LINK = Path("/home/dev/.local/share/street-story/current")
APP_STATE_ROOT = Path("/home/dev/.local/state/street-story")
VPN_ROOT = Path("/home/dev/projects/vpn-server")
YC_BINARY = Path("/home/dev/yandex-cloud/bin/yc")
CERTBOT_ROOT = Path("/home/dev/.local/state/my-data-hub-local-edge")
VIBE_SOURCE = Path("/home/dev/projects/vibepublish")
VIBE_PY = Path("/home/dev/.local/opt/vibepublish/bin/python")
VIBE_DB = Path("/home/dev/.local/state/vibepublish/vibepublish.sqlite3")
VIBE_OWNER_TOKEN_FILE = Path("/home/dev/.local/state/vibepublish/owner-token.txt")
VIBE_BASE_URL = "http://127.0.0.1:18765"
HOST_ENV = Path("/home/dev/.env")
GH_BINARY = Path("/usr/bin/gh")
DEVICE_TOKEN_FILE = APP_STATE_ROOT / "device-token"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")

_SECRET_PATTERNS = (
    re.compile(r"Bearer\s+[^\s]+", re.I),
    re.compile(r"(?:token|secret|key|password)=[^\s&]+", re.I),
)


class OperatorError(RuntimeError):
    """Safe capability error with secrets removed at the boundary."""


@dataclass(frozen=True)
class CommandResult:
    code: int
    output: str


def _redact(value: str) -> str:
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    return value[:2000]


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _safe_sha(value: str) -> str:
    value = value.strip().lower()
    if SHA_RE.fullmatch(value) is None:
        raise OperatorError("invalid Street Story release SHA")
    return value


class StreetStoryOperator:
    """One allowlisted, audit-logged host authority for Street Story."""

    def __init__(
        self,
        *,
        state_root: Path = STATE_ROOT,
        project_root: Path = PROJECT_ROOT,
        releases_root: Path = RELEASES_ROOT,
        current_link: Path = CURRENT_LINK,
        app_state_root: Path = APP_STATE_ROOT,
        vpn_root: Path = VPN_ROOT,
        user_unit_root: Path | None = None,
        yc_binary: Path = YC_BINARY,
        certbot_root: Path = CERTBOT_ROOT,
        vibe_source: Path = VIBE_SOURCE,
        vibe_py: Path = VIBE_PY,
        vibe_db: Path = VIBE_DB,
        vibe_owner_token_file: Path = VIBE_OWNER_TOKEN_FILE,
        host_env: Path = HOST_ENV,
        runner: Callable[[list[str], Path | None, Mapping[str, str] | None, int], CommandResult] | None = None,
    ) -> None:
        self.state_root = state_root
        self.project_root = project_root
        self.releases_root = releases_root
        self.current_link = current_link
        self.app_state_root = app_state_root
        self.vpn_root = vpn_root
        self.user_unit_root = user_unit_root or (Path.home() / ".config/systemd/user")
        self.yc_binary = yc_binary
        self.certbot_root = certbot_root
        self.vibe_source = vibe_source
        self.vibe_py = vibe_py
        self.vibe_db = vibe_db
        self.vibe_owner_token_file = vibe_owner_token_file
        self.host_env = host_env
        self._runner = runner or self._run
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_root, 0o700)
        self._lock_path = self.state_root / "operator.lock"
        self._lock_path.touch(mode=0o600, exist_ok=True)
        os.chmod(self._lock_path, 0o600)
        self._db_path = self.state_root / "receipts.sqlite3"
        self._initialize()

    def _initialize(self) -> None:
        with self._db() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS operations (
                    idempotency_key TEXT PRIMARY KEY,
                    operation TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS operation_attempts (
                    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    safe_error TEXT,
                    created_at INTEGER NOT NULL
                )"""
            )

    @contextlib.contextmanager
    def _locked(self):
        with self._lock_path.open("r+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @contextlib.contextmanager
    def _db(self):
        db = sqlite3.connect(self._db_path)
        try:
            yield db
            db.commit()
        finally:
            db.close()

    @staticmethod
    def _run(argv: list[str], cwd: Path | None, env: Mapping[str, str] | None, timeout: int) -> CommandResult:
        try:
            result = subprocess.run(
                argv,
                cwd=cwd,
                env=dict(env) if env else None,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OperatorError(f"host action unavailable: {type(exc).__name__}") from None
        output = result.stdout or ""
        if result.returncode != 0:
            raise OperatorError(f"host action failed ({result.returncode}): {_redact(output)}")
        return CommandResult(result.returncode, output)

    def _call(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int = 120,
    ) -> CommandResult:
        return self._runner(argv, cwd, env, timeout)

    @staticmethod
    def _validate_key(key: str) -> None:
        if KEY_RE.fullmatch(key) is None:
            raise OperatorError("invalid idempotency key")

    def _apply(
        self,
        operation: str,
        request: Mapping[str, Any],
        key: str,
        action: Callable[[], Mapping[str, Any]],
    ) -> dict[str, Any]:
        self._validate_key(key)
        digest = hashlib.sha256(_canonical_json(dict(request)).encode()).hexdigest()
        with self._locked(), self._db() as db:
            row = db.execute(
                "SELECT operation,request_sha256,receipt_json FROM operations WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if row:
                if row[0] != operation or row[1] != digest:
                    raise OperatorError("idempotency key conflicts with a changed request")
                receipt = json.loads(row[2])
                receipt["idempotent"] = True
                return receipt
            try:
                receipt = dict(action())
            except BaseException as exc:
                db.execute(
                    "INSERT INTO operation_attempts "
                    "(idempotency_key,operation,request_sha256,status,safe_error,created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (key, operation, digest, "failed", _redact(str(exc)), int(time.time())),
                )
                db.commit()
                raise
            receipt.update(
                {
                    "schema_version": 1,
                    "operation": operation,
                    "idempotency_key": key,
                    "request_sha256": digest,
                    "idempotent": False,
                }
            )
            encoded = _canonical_json(receipt)
            if any(marker in encoded.casefold() for marker in ("bearer", "service_token", "device_token", "private_key")):
                raise OperatorError("unsafe Street Story receipt projection")
            db.execute(
                "INSERT INTO operations VALUES (?,?,?,?,?,?)",
                (key, operation, digest, "succeeded", encoded, int(time.time())),
            )
            db.execute(
                "INSERT INTO operation_attempts "
                "(idempotency_key,operation,request_sha256,status,safe_error,created_at) "
                "VALUES (?,?,?,?,NULL,?)",
                (key, operation, digest, "succeeded", int(time.time())),
            )
            return receipt

    def _remote_head_and_tree(self) -> tuple[str, str]:
        fixed_ref = f"refs/remotes/origin/{TARGET_BRANCH}"
        self._call(
            [
                "git",
                "-C",
                str(self.project_root),
                "fetch",
                "--no-tags",
                "origin",
                f"+refs/heads/{TARGET_BRANCH}:{fixed_ref}",
            ],
            timeout=180,
        )
        head = self._call(["git", "-C", str(self.project_root), "rev-parse", fixed_ref], timeout=30).output.strip()
        tree = self._call(["git", "-C", str(self.project_root), "rev-parse", f"{head}^{{tree}}"], timeout=30).output.strip()
        return head, tree

    def _verify_approved_source(self, exact_sha: str) -> str:
        sha = _safe_sha(exact_sha)
        remote_head, remote_tree = self._remote_head_and_tree()
        if remote_head != sha:
            raise OperatorError("requested SHA is not the current Street Story PR branch HEAD")
        return remote_tree

    def materialize_release(
        self,
        *,
        repository: str,
        exact_sha: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if repository != TARGET_REPOSITORY:
            raise OperatorError("repository is not the allowlisted Street Story repository")
        sha = _safe_sha(exact_sha)
        request = {"repository": repository, "exact_sha": sha}

        def action() -> Mapping[str, Any]:
            tree = self._verify_approved_source(sha)
            self.releases_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.releases_root, 0o700)
            release = self.releases_root / sha
            manifest_value = {"repository": TARGET_REPOSITORY, "release_sha": sha, "tree_sha": tree}
            manifest = release / ".street-story-release.json"
            if release.is_dir() and not release.is_symlink():
                try:
                    current = json.loads(manifest.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError) as exc:
                    raise OperatorError("existing Street Story release is not verified") from exc
                if current != manifest_value:
                    raise OperatorError("existing Street Story release manifest mismatch")
                return {
                    "repository": TARGET_REPOSITORY,
                    "release_sha": sha,
                    "tree_sha": tree,
                    "materialized": True,
                    "reused": True,
                }

            stage = Path(tempfile.mkdtemp(prefix=".street-story-release-", dir=self.releases_root))
            archive = stage / ".source.tar"
            source = stage / "source"
            source.mkdir(mode=0o700)
            try:
                self._call(
                    ["git", "-C", str(self.project_root), "archive", "--format=tar", "-o", str(archive), sha],
                    timeout=180,
                )
                self._call(["tar", "-xf", str(archive), "-C", str(source)], timeout=120)
                archive.unlink(missing_ok=True)
                (stage / ".street-story-release.json").write_text(
                    _canonical_json(manifest_value) + "\n", encoding="utf-8"
                )
                os.chmod(stage / ".street-story-release.json", 0o600)
                os.chmod(stage, 0o700)
                os.replace(stage, release)
            except BaseException:
                shutil.rmtree(stage, ignore_errors=True)
                raise
            return {
                "repository": TARGET_REPOSITORY,
                "release_sha": sha,
                "tree_sha": tree,
                "materialized": True,
                "reused": False,
            }

        return self._apply("street_story_materialize_release", request, idempotency_key, action)

    def _release(self, sha: str) -> Path:
        release = self.releases_root / _safe_sha(sha)
        manifest = release / ".street-story-release.json"
        if not release.is_dir() or release.is_symlink():
            raise OperatorError("exact Street Story release has not been materialized")
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise OperatorError("Street Story release manifest is unavailable") from exc
        if data.get("repository") != TARGET_REPOSITORY or data.get("release_sha") != sha:
            raise OperatorError("Street Story release manifest mismatch")
        return release

    def _ensure_live_token(self) -> str:
        self.app_state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.app_state_root, 0o700)
        path = DEVICE_TOKEN_FILE
        if path.exists():
            st = path.lstat()
            if path.is_symlink() or stat.S_IMODE(st.st_mode) != 0o600:
                raise OperatorError("Street Story device token path is unsafe")
            token = path.read_text(encoding="utf-8").strip()
            if len(token) < 32:
                raise OperatorError("Street Story device token is invalid")
        else:
            token = secrets.token_urlsafe(48)
            fd, temporary = tempfile.mkstemp(prefix=".device-token-", dir=self.app_state_root)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(token + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary)

        if not GH_BINARY.is_file():
            raise OperatorError("GitHub CLI is unavailable for Street Story secret sync")
        try:
            result = subprocess.run(
                [
                    str(GH_BINARY),
                    "secret",
                    "set",
                    "STREET_STORY_LIVE_TOKEN",
                    "--repo",
                    TARGET_REPOSITORY,
                ],
                input=token + "\n",
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OperatorError(f"Street Story GitHub secret sync unavailable: {type(exc).__name__}") from None
        if result.returncode != 0:
            raise OperatorError(
                f"Street Story GitHub secret sync failed ({result.returncode}): {_redact(result.stdout or '')}"
            )
        return token

    def _read_host_env(self) -> dict[str, str]:
        try:
            text = self.host_env.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise OperatorError("DevCoveer provider environment is unavailable") from exc
        values: dict[str, str] = {}
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            values[key] = value
        return values

    @staticmethod
    def _write_private_env(path: Path, values: Mapping[str, str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        fd, temp = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for key, value in values.items():
                    handle.write(f"{key}={shlex.quote(str(value))}\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp)

    def _provider_env(self) -> Path:
        host = self._read_host_env()
        refs = [
            name for name in ("GOOGLE_API_KEY", "GOOGLE_API_KEY2", "GOOGLE_API_KEY3", "GOOGLE_API_KEY4", "GOOGLE_API_KEY5")
            if host.get(name)
        ]
        if not refs:
            raise OperatorError("registered Google API key pool is unavailable")
        quota_url = host.get("GOOGLE_AI_LIMITER_SUPABASE_URL") or host.get("SUPABASE_URL")
        quota_key = (
            host.get("GOOGLE_AI_LIMITER_SUPABASE_SERVICE_KEY")
            or host.get("SUPABASE_SERVICE_KEY")
            or host.get("SUPABASE_KEY")
        )
        if not quota_url or not quota_key:
            raise OperatorError("shared Google AI limiter credentials are unavailable")
        values = {name: host[name] for name in refs}
        values.update(
            {
                "GEMINI_API_KEY_REFS": json.dumps(refs, separators=(",", ":")),
                "GEMINI_MODEL": "gemini-3.1-flash-lite",
                "GEMINI_QUOTA_SUPABASE_URL": quota_url,
                "GEMINI_QUOTA_SUPABASE_KEY": quota_key,
            }
        )
        path = self.app_state_root / "providers.env"
        self._write_private_env(path, values)
        return path

    def _vibe_principal(self, sha: str) -> tuple[Path, Path]:
        token_file = self.app_state_root / "vibepublish-token"
        principal_file = self.app_state_root / "vibepublish-principal"
        self.app_state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.app_state_root, 0o700)
        if token_file.is_file() and principal_file.is_file():
            if token_file.is_symlink() or principal_file.is_symlink():
                raise OperatorError("Street Story Vibe credential paths are unsafe")
            if stat.S_IMODE(token_file.stat().st_mode) != 0o600 or stat.S_IMODE(principal_file.stat().st_mode) != 0o600:
                raise OperatorError("Street Story Vibe credential permissions are unsafe")
            if len(token_file.read_text(encoding="utf-8").strip()) < 20:
                raise OperatorError("Street Story Vibe token is invalid")
            return token_file, principal_file

        for required in (self.vibe_py, self.vibe_db, self.vibe_owner_token_file):
            if not required.exists():
                raise OperatorError("current VibePublish management runtime is unavailable")
        principal = f"street-story-runtime-{sha[:12]}"
        owner_token = self.vibe_owner_token_file.read_text(encoding="utf-8").strip()
        if len(owner_token) < 20:
            raise OperatorError("VibePublish owner token is invalid")
        admin_env = os.environ.copy()
        admin_env["VIBEPUBLISH_SERVICE_TOKEN"] = owner_token
        created = self._call(
            [
                str(self.vibe_py),
                "-m",
                "social_operations.cli",
                "--db",
                str(self.vibe_db),
                "principal",
                "--tenant",
                "devcoveer2",
                "--principal",
                principal,
            ],
            cwd=self.vibe_source,
            env=admin_env,
            timeout=60,
        ).output
        try:
            service_token = json.loads(created).get("service_token", "")
        except (ValueError, TypeError) as exc:
            raise OperatorError("VibePublish principal creation readback is invalid") from exc
        if not isinstance(service_token, str) or len(service_token) < 20:
            raise OperatorError("VibePublish principal creation failed")

        # Reads are read-only discovery; every authorization mutation below is
        # performed by the official VibePublish management CLI.
        db = sqlite3.connect(f"file:{self.vibe_db}?mode=ro", uri=True)
        try:
            row = db.execute(
                """
                select d.native_id, c.id
                from bindings b
                join destinations d on d.id=b.destination_id
                join connections c on c.id=d.connection_id
                where b.principal_id='owner' and b.alias='lovekenig_tg'
                  and b.active=1 and c.active=1 and c.provider='telegram'
                limit 1
                """
            ).fetchone()
        finally:
            db.close()
        if not row or not all(isinstance(value, str) and value for value in row):
            raise OperatorError("current owner lovekenig_tg binding is unavailable")
        self._call(
            [
                str(self.vibe_py),
                "-m",
                "social_operations.cli",
                "--db",
                str(self.vibe_db),
                "bind",
                "--principal",
                principal,
                "--alias",
                "lovekenig_tg",
                "--connection",
                row[1],
                "--native-id",
                row[0],
                "--label",
                "Polubit Kaliningrad Telegram",
            ],
            cwd=self.vibe_source,
            env=admin_env,
            timeout=60,
        )
        fd, temporary = tempfile.mkstemp(prefix=".vibepublish-token-", dir=self.app_state_root)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(service_token + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, token_file)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
        principal_file.write_text(principal + "\n", encoding="utf-8")
        os.chmod(principal_file, 0o600)
        return token_file, principal_file

    @staticmethod
    def _user_systemd_environment() -> dict[str, str]:
        runtime = f"/run/user/{os.getuid()}"
        bus = Path(runtime) / "bus"
        if not bus.exists() or not bus.is_socket():
            raise OperatorError("DevCoveer user-systemd bus is unavailable")
        return {
            "HOME": "/home/dev",
            "PATH": os.environ.get("PATH", ""),
            "XDG_RUNTIME_DIR": runtime,
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={bus}",
        }

    def _service_status(self) -> dict[str, str]:
        result = self._call(
            [
                "systemctl",
                "--user",
                "show",
                TARGET_SERVICE,
                "--property=ActiveState,SubState,MainPID,ExecMainStatus",
            ],
            env=self._user_systemd_environment(),
            timeout=30,
        )
        fields = dict(line.split("=", 1) for line in result.output.splitlines() if "=" in line)
        return {
            "service": TARGET_SERVICE,
            "active": fields.get("ActiveState", "unknown"),
            "substate": fields.get("SubState", "unknown"),
            "exec_status": fields.get("ExecMainStatus", "unknown"),
        }

    def _wait_local_health(self, sha: str, timeout: int = 90) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            try:
                raw = self._call(
                    [
                        "curl",
                        "--fail",
                        "--silent",
                        "--show-error",
                        "--max-time",
                        "3",
                        f"http://127.0.0.1:{TARGET_PORT}/healthz",
                    ],
                    timeout=5,
                ).output
                payload = json.loads(raw)
                if payload.get("ok") is True and payload.get("source_sha") == sha:
                    return {"ok": True, "source_sha": sha}
                last = "unexpected health payload"
            except (OperatorError, ValueError, TypeError) as exc:
                last = str(exc)
            time.sleep(1)
        raise OperatorError(f"Street Story local readiness failed: {_redact(last)}")

    def _local_capabilities(self, live_token: str) -> list[dict[str, Any]]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{TARGET_PORT}/v1/capabilities",
            headers={"Authorization": "Bearer " + live_token, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                data = json.load(response)
        except Exception as exc:
            raise OperatorError("Street Story authenticated capabilities readback failed") from exc
        rows = data.get("destinations") or []
        safe = [
            {
                key: row.get(key)
                for key in ("alias", "label", "provider", "status")
            }
            for row in rows
            if isinstance(row, dict)
        ]
        if not any(row.get("alias") == "lovekenig_tg" and row.get("provider") == "telegram" for row in safe):
            raise OperatorError("Street Story Vibe projection lacks lovekenig_tg")
        return safe

    def deploy_user_service(
        self,
        *,
        release_sha: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        sha = _safe_sha(release_sha)
        request = {"release_sha": sha}

        def action() -> Mapping[str, Any]:
            self._verify_approved_source(sha)
            release = self._release(sha)
            source = release / "source"
            requirements = source / "backend" / "requirements.txt"
            if not requirements.is_file():
                raise OperatorError("Street Story backend requirements are missing")
            live_token = self._ensure_live_token()
            self.app_state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.app_state_root, 0o700)
            providers_env = self._provider_env()

            py = shutil.which("python3.12")
            if not py:
                raise OperatorError("Python 3.12 is unavailable")
            venv = release / "venv"
            if not venv.is_dir():
                self._call([py, "-m", "venv", str(venv)], timeout=180)
                self._call(
                    [
                        str(venv / "bin/pip"),
                        "install",
                        "--disable-pip-version-check",
                        "-r",
                        str(requirements),
                    ],
                    timeout=900,
                )
                self._call([str(venv / "bin/python"), "-m", "compileall", "-q", str(source / "backend/street_story")], timeout=120)

            vibe_token_file, _principal_file = self._vibe_principal(sha)
            vibe_token = vibe_token_file.read_text(encoding="utf-8").strip()
            if len(vibe_token) < 20:
                raise OperatorError("Street Story Vibe token is invalid")
            data_dir = self.app_state_root / "data"
            data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(data_dir, 0o700)
            service_env = self.app_state_root / "service.env"
            self._write_private_env(
                service_env,
                {
                    "DATA_DIR": str(data_dir),
                    "STREET_STORY_DEVICE_TOKEN": live_token,
                    "VIBEPUBLISH_BASE_URL": VIBE_BASE_URL,
                    "VIBEPUBLISH_BEARER_TOKEN": vibe_token,
                    "STREET_STORY_DEPLOY_SHA": sha,
                    "STREET_STORY_OSM_USER_AGENT": "StreetStory/0.1 (+https://github.com/onedayonemasterpiece/street-story)",
                    "WORKER_POLL_SECONDS": "1",
                    "PROCESSING_DELAYED_AFTER_SECONDS": "1800",
                },
            )

            unit_dir = self.user_unit_root
            unit_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            unit = unit_dir / TARGET_SERVICE
            unit_text = "\n".join(
                (
                    "[Unit]",
                    "Description=Street Story backend (allowlisted DevCoveer release)",
                    "After=network-online.target vibepublish.service",
                    "Wants=network-online.target",
                    "Requires=vibepublish.service",
                    "",
                    "[Service]",
                    "Type=simple",
                    f"WorkingDirectory={source}",
                    f"EnvironmentFile={providers_env}",
                    f"EnvironmentFile={service_env}",
                    f"ExecStart={venv}/bin/uvicorn street_story.app:app --app-dir {source}/backend --host 127.0.0.1 --port {TARGET_PORT} --workers 1",
                    "Restart=on-failure",
                    "RestartSec=3",
                    "TimeoutStopSec=30",
                    "NoNewPrivileges=true",
                    "PrivateTmp=true",
                    "ProtectSystem=strict",
                    "ProtectHome=read-only",
                    f"ReadWritePaths={self.app_state_root}",
                    "UMask=0077",
                    "",
                    "[Install]",
                    "WantedBy=default.target",
                    "",
                )
            )
            fd, temp = tempfile.mkstemp(prefix=".street-story-unit-", dir=unit_dir)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(unit_text)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, unit)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temp)

            self.current_link.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.current_link.exists() or self.current_link.is_symlink():
                self.current_link.unlink()
            self.current_link.symlink_to(release)
            systemd_env = self._user_systemd_environment()
            self._call(["systemctl", "--user", "daemon-reload"], env=systemd_env, timeout=30)
            self._call(["systemctl", "--user", "enable", TARGET_SERVICE], env=systemd_env, timeout=30)
            self._call(["systemctl", "--user", "restart", TARGET_SERVICE], env=systemd_env, timeout=180)
            status = self._service_status()
            if status["active"] != "active":
                raise OperatorError("Street Story user service did not become active")
            local = self._wait_local_health(sha)
            destinations = self._local_capabilities(live_token)
            return {
                "release_sha": sha,
                "listener": f"127.0.0.1:{TARGET_PORT}",
                "service": status,
                "local_health": local,
                "destinations": destinations,
                "safe_alias_configured": False,
                "github_live_token_synced": True,
                "secrets_disclosed": False,
            }

        return self._apply("street_story_deploy_user_service", request, idempotency_key, action)

    def service(self, *, action: str, service: str | None = None) -> dict[str, Any]:
        if service not in (None, TARGET_SERVICE):
            raise OperatorError("service is not the allowlisted Street Story unit")
        if action == "status":
            return {"schema_version": 1, **self._service_status()}
        if action != "restart":
            raise OperatorError("unsupported Street Story service action")
        manifest = self.current_link / ".street-story-release.json"
        try:
            sha = json.loads(manifest.read_text(encoding="utf-8"))["release_sha"]
        except Exception as exc:
            raise OperatorError("current Street Story release manifest is unavailable") from exc
        self._call(
            ["systemctl", "--user", "restart", TARGET_SERVICE],
            env=self._user_systemd_environment(),
            timeout=180,
        )
        status = self._service_status()
        if status["active"] != "active":
            raise OperatorError("Street Story service restart failed")
        local = self._wait_local_health(sha)
        return {"schema_version": 1, "restarted": True, "local_health": local, **status}

    @staticmethod
    def _yc_rows(raw: str, wrapper: str) -> list[dict[str, Any]]:
        try:
            value = json.loads(raw)
        except ValueError as exc:
            raise OperatorError("Yandex Cloud JSON readback is invalid") from exc
        if isinstance(value, dict):
            value = value.get(wrapper, [])
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise OperatorError("Yandex Cloud list readback has an unexpected shape")
        return value

    @staticmethod
    def _append_case_hostname(path: Path, hostname: str) -> tuple[str, str]:
        original = path.read_text(encoding="utf-8")
        if hostname in original:
            return original, original
        lines = original.splitlines()
        candidates = [
            i for i, line in enumerate(lines)
            if "mcp-datahub.kenigevents.ru" in line and ")" in line
        ]
        if len(candidates) != 1:
            raise OperatorError("canonical Certbot hostname allowlist has unexpected content")
        i = candidates[0]
        before, after = lines[i].split(")", 1)
        lines[i] = before + "|" + hostname + ")" + after
        updated = "\n".join(lines) + ("\n" if original.endswith("\n") else "")
        path.write_text(updated, encoding="utf-8")
        os.chmod(path, 0o700)
        return original, updated

    def publish_edge_route(
        self,
        *,
        hostname: str,
        loopback_port: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if hostname != TARGET_HOSTNAME or loopback_port != TARGET_PORT:
            raise OperatorError("Street Story edge target is not allowlisted")
        request = {"hostname": hostname, "loopback_port": loopback_port}

        def action() -> Mapping[str, Any]:
            status = self._service_status()
            if status["active"] != "active":
                raise OperatorError("Street Story service must be active before edge publication")
            manifest = self.current_link / ".street-story-release.json"
            try:
                sha = _safe_sha(json.loads(manifest.read_text(encoding="utf-8"))["release_sha"])
            except Exception as exc:
                raise OperatorError("current Street Story release manifest is invalid") from exc
            self._wait_local_health(sha)

            renderer_source = self.vpn_root / "bot/vpn_manager.py"
            renderer = self.vpn_root / "scripts/render-configs.sh"
            env_file = self.vpn_root / ".env"
            if not all(path.is_file() for path in (renderer_source, renderer, env_file)):
                raise OperatorError("canonical VPN edge renderer is unavailable")
            source_text = renderer_source.read_text(encoding="utf-8")
            for marker in (
                "LOCAL_MCP_EDGE_STREET_STORY_ENABLED",
                "LOCAL_MCP_EDGE_STREET_STORY_HOST",
                "LOCAL_MCP_EDGE_STREET_STORY_PORT",
            ):
                if marker not in source_text:
                    raise OperatorError("installed VPN renderer lacks the Street Story typed route")

            zones = self._yc_rows(
                self._call(
                    [str(self.yc_binary), "dns", "zone", "list", "--format", "json", "--no-user-output"],
                    timeout=60,
                ).output,
                "dns_zones",
            )
            try:
                zone_id = str(next(item for item in zones if item.get("zone") == "kenigevents.ru.")["id"])
            except (KeyError, StopIteration, TypeError) as exc:
                raise OperatorError("canonical kenigevents.ru DNS zone is unavailable") from exc
            ip = self._call(["getent", "ahostsv4", "mcp-datahub.kenigevents.ru"], timeout=30).output.split()[0]
            if not re.fullmatch(r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}", ip):
                raise OperatorError("existing MCP public IPv4 readback is invalid")
            records = self._yc_rows(
                self._call(
                    [
                        str(self.yc_binary),
                        "dns",
                        "zone",
                        "list-records",
                        "--id",
                        zone_id,
                        "--format",
                        "json",
                        "--no-user-output",
                    ],
                    timeout=60,
                ).output,
                "record_sets",
            )
            wanted_name = TARGET_HOSTNAME + "."
            existing = next(
                (row for row in records if row.get("name") == wanted_name and row.get("type") == "A"),
                None,
            )
            if existing is not None and ip not in set(existing.get("data") or []):
                raise OperatorError("Street Story DNS record conflicts with the existing MCP edge IPv4")

            hook = self.certbot_root / "hooks/certbot-yc-auth.sh"
            cleanup = self.certbot_root / "hooks/certbot-yc-cleanup.sh"
            reload_hook = self.certbot_root / "hooks/reload-nginx.sh"
            certbot = self.certbot_root / "certbot-venv/bin/certbot"
            cert = self.certbot_root / "letsencrypt/live/my-data-hub-local-edge/fullchain.pem"
            if not all(path.is_file() for path in (hook, cleanup, reload_hook, certbot, cert)):
                raise OperatorError("canonical Certbot DNS-01 machinery is unavailable")
            auth_before, _ = self._append_case_hostname(hook, TARGET_HOSTNAME)
            cleanup_before, _ = self._append_case_hostname(cleanup, TARGET_HOSTNAME)
            try:
                san_before = self._call(
                    ["openssl", "x509", "-in", str(cert), "-noout", "-ext", "subjectAltName"],
                    timeout=30,
                ).output
                domains = sorted(set(re.findall(r"DNS:([a-z0-9.-]+)", san_before)) | {TARGET_HOSTNAME})
                if not domains or any(
                    domain.startswith("*.") or not domain.endswith(".kenigevents.ru")
                    for domain in domains
                ):
                    raise OperatorError("existing edge certificate contains an unexpected SAN")
                self._call(
                    [
                        str(certbot),
                        "certonly",
                        "--cert-name",
                        "my-data-hub-local-edge",
                        "--expand",
                        "--config-dir",
                        str(self.certbot_root / "letsencrypt"),
                        "--work-dir",
                        str(self.certbot_root / "letsencrypt-work"),
                        "--logs-dir",
                        str(self.certbot_root / "letsencrypt-logs"),
                        "--manual",
                        "--preferred-challenges",
                        "dns",
                        "--manual-auth-hook",
                        str(hook),
                        "--manual-cleanup-hook",
                        str(cleanup),
                        "--non-interactive",
                        "--agree-tos",
                        "--deploy-hook",
                        str(reload_hook),
                        *sum((["-d", domain] for domain in domains), []),
                    ],
                    timeout=600,
                )
            except BaseException:
                hook.write_text(auth_before, encoding="utf-8")
                cleanup.write_text(cleanup_before, encoding="utf-8")
                os.chmod(hook, 0o700)
                os.chmod(cleanup, 0o700)
                raise

            san_after = self._call(
                ["openssl", "x509", "-in", str(cert), "-noout", "-ext", "subjectAltName"],
                timeout=30,
            ).output
            if f"DNS:{TARGET_HOSTNAME}" not in san_after:
                raise OperatorError("certificate readback does not contain the Street Story hostname")

            original_env = env_file.read_text(encoding="utf-8")
            updates = {
                "LOCAL_MCP_EDGE_STREET_STORY_ENABLED": "1",
                "LOCAL_MCP_EDGE_STREET_STORY_HOST": TARGET_HOSTNAME,
                "LOCAL_MCP_EDGE_STREET_STORY_PORT": str(TARGET_PORT),
            }
            lines: list[str] = []
            seen: set[str] = set()
            for line in original_env.splitlines():
                key = line.split("=", 1)[0]
                if key in updates:
                    lines.append(f"{key}={updates[key]}")
                    seen.add(key)
                else:
                    lines.append(line)
            for key, value in updates.items():
                if key not in seen:
                    lines.append(f"{key}={value}")
            fd, temp = tempfile.mkstemp(prefix=".street-story-edge-", dir=self.vpn_root)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write("\n".join(lines) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, env_file)
                temp = ""
                self._call([str(renderer)], cwd=self.vpn_root, timeout=120)
                config = (self.vpn_root / "data/nginx/nginx.conf").read_text(encoding="utf-8")
                for preserved in (
                    "mcp-datahub.kenigevents.ru",
                    "identity.kenigevents.ru",
                    "ideas.kenigevents.ru",
                ):
                    if f"{preserved} local_mcp_tls;" not in config:
                        raise OperatorError("renderer would remove an existing SNI route")
                if f"{TARGET_HOSTNAME} local_mcp_tls;" not in config:
                    raise OperatorError("renderer did not produce the Street Story SNI route")
                if f"server_name {TARGET_HOSTNAME};" not in config or f"proxy_pass http://127.0.0.1:{TARGET_PORT};" not in config:
                    raise OperatorError("renderer did not produce the Street Story loopback route")
                self._call(["docker", "compose", "build", "nginx"], cwd=self.vpn_root, timeout=600)
                self._call(
                    ["docker", "compose", "up", "-d", "--no-deps", "--force-recreate", "nginx"],
                    cwd=self.vpn_root,
                    timeout=180,
                )
                self._call(["docker", "exec", "vpn-server-nginx-1", "nginx", "-t"], timeout=60)
                if existing is None:
                    self._call(
                        [
                            str(self.yc_binary),
                            "dns",
                            "zone",
                            "add-records",
                            "--id",
                            zone_id,
                            "--record",
                            f"{wanted_name} 300 A {ip}",
                            "--no-user-output",
                        ],
                        timeout=90,
                    )
            except BaseException:
                env_file.write_text(original_env, encoding="utf-8")
                with contextlib.suppress(Exception):
                    self._call([str(renderer)], cwd=self.vpn_root, timeout=120)
                    self._call(
                        ["docker", "compose", "up", "-d", "--no-deps", "--force-recreate", "nginx"],
                        cwd=self.vpn_root,
                        timeout=180,
                    )
                raise
            finally:
                if temp:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(temp)

            public = self._call(
                [
                    "curl",
                    "--fail",
                    "--silent",
                    "--show-error",
                    "--max-time",
                    "10",
                    f"https://{TARGET_HOSTNAME}/healthz",
                ],
                timeout=15,
            ).output
            try:
                payload = json.loads(public)
            except ValueError as exc:
                raise OperatorError("Street Story public health readback is invalid") from exc
            if payload.get("ok") is not True or payload.get("source_sha") != sha:
                raise OperatorError("Street Story public exact-SHA health gate failed")
            return {
                "hostname": TARGET_HOSTNAME,
                "loopback_port": TARGET_PORT,
                "dns_ipv4": ip,
                "certificate_san_verified": True,
                "renderer": "vpn-server",
                "public_health": {"ok": True, "source_sha": sha},
                "existing_sni_routes_preserved": True,
            }

        return self._apply("street_story_publish_edge_route", request, idempotency_key, action)

    def receipt(self, *, idempotency_key: str) -> dict[str, Any]:
        self._validate_key(idempotency_key)
        with self._locked(), self._db() as db:
            row = db.execute(
                "SELECT receipt_json FROM operations WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            raise OperatorError("Street Story deployment receipt was not found")
        receipt = json.loads(row[0])
        receipt["idempotent"] = True
        return receipt
