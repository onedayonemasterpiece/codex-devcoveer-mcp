"""Bounded Projects Hub public edge publication authority for DevCoveer.

This operator exposes one capability only: publish the already healthy, publicly
authenticated Projects Hub release on the existing kenigevents.ru local HTTPS edge.
It cannot deploy source, change application configuration, or publish another host.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Callable, Mapping

try:
    from .street_story_operator import (
        CommandResult,
        OperatorError,
        StreetStoryOperator,
    )
except ImportError:  # canonical flat runtime
    from street_story_operator import (
        CommandResult,
        OperatorError,
        StreetStoryOperator,
    )

TARGET_HOSTNAME = "projects-hub.kenigevents.ru"
TARGET_PORT = 8196
TARGET_SERVICE = "projects-hub.service"
PROJECT_ROOT = Path("/home/dev/projects/projects-hub")
STATE_ROOT = Path("/home/dev/.local/state/openai-codex-mcp/projects-hub-edge-operator")
RELEASES_ROOT = Path("/home/dev/.local/share/projects-hub/releases")
CURRENT_LINK = Path("/home/dev/.local/share/projects-hub/current")
APP_STATE_ROOT = Path("/home/dev/.local/state/projects-hub")
VPN_ROOT = Path("/home/dev/projects/vpn-server")
YC_BINARY = Path("/home/dev/yandex-cloud/bin/yc")
CERTBOT_ROOT = Path("/home/dev/.local/state/my-data-hub-local-edge")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class ProjectsHubEdgeOperator(StreetStoryOperator):
    """Allowlisted Projects Hub edge publisher with durable idempotent receipts."""

    def __init__(
        self,
        *,
        state_root: Path = STATE_ROOT,
        current_link: Path = CURRENT_LINK,
        releases_root: Path = RELEASES_ROOT,
        vpn_root: Path = VPN_ROOT,
        certbot_root: Path = CERTBOT_ROOT,
        yc_binary: Path = YC_BINARY,
        runner: Callable[
            [list[str], Path | None, Mapping[str, str] | None, int],
            CommandResult,
        ]
        | None = None,
    ) -> None:
        super().__init__(
            state_root=state_root,
            project_root=PROJECT_ROOT,
            releases_root=releases_root,
            current_link=current_link,
            app_state_root=APP_STATE_ROOT,
            vpn_root=vpn_root,
            yc_binary=yc_binary,
            certbot_root=certbot_root,
            runner=runner,
        )

    def _current_release_sha(self) -> str:
        if not self.current_link.is_symlink():
            raise OperatorError(
                "Projects Hub current release is not an exact release symlink"
            )
        try:
            target = self.current_link.resolve(strict=True)
            releases = self.releases_root.resolve(strict=True)
        except OSError as exc:
            raise OperatorError("Projects Hub current release is unavailable") from exc
        if target.parent != releases or SHA_RE.fullmatch(target.name) is None:
            raise OperatorError(
                "Projects Hub current release is not an exact SHA release"
            )
        return target.name

    def _projects_hub_service_status(self) -> dict[str, str]:
        result = self._call(
            [
                "systemctl",
                "--user",
                "show",
                TARGET_SERVICE,
                "--property=ActiveState,SubState,MainPID,ExecMainStatus,NRestarts",
            ],
            env=self._user_systemd_environment(),
            timeout=30,
        )
        fields = dict(
            line.split("=", 1)
            for line in result.output.splitlines()
            if "=" in line
        )
        return {
            "service": TARGET_SERVICE,
            "active": fields.get("ActiveState", "unknown"),
            "substate": fields.get("SubState", "unknown"),
            "exec_status": fields.get("ExecMainStatus", "unknown"),
            "restarts": fields.get("NRestarts", "unknown"),
        }

    def _local_health(self, sha: str) -> dict[str, Any]:
        raw = self._call(
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--max-time",
                "10",
                f"http://127.0.0.1:{TARGET_PORT}/healthz",
            ],
            timeout=15,
        ).output
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise OperatorError(
                "Projects Hub local health readback is invalid"
            ) from exc
        if (
            payload.get("ok") is not True
            or payload.get("release_sha") != sha
            or payload.get("auth_mode")
            not in {"public_yandex", "public_yandex+loopback_dev"}
        ):
            raise OperatorError(
                "Projects Hub local public-auth exact-SHA health gate failed"
            )
        return {
            "ok": True,
            "release_sha": sha,
            "auth_mode": payload["auth_mode"],
        }

    @staticmethod
    def _updated_vpn_env(original: str) -> str:
        updates = {
            "LOCAL_MCP_EDGE_PROJECTS_HUB_ENABLED": "1",
            "LOCAL_MCP_EDGE_PROJECTS_HUB_HOST": TARGET_HOSTNAME,
            "LOCAL_MCP_EDGE_PROJECTS_HUB_PORT": str(TARGET_PORT),
        }
        lines: list[str] = []
        seen: set[str] = set()
        for line in original.splitlines():
            key = line.split("=", 1)[0].strip()
            if key in updates:
                lines.append(f"{key}={updates[key]}")
                seen.add(key)
            else:
                lines.append(line)
        for key, value in updates.items():
            if key not in seen:
                lines.append(f"{key}={value}")
        return "\n".join(lines) + "\n"

    def _dns_target(self) -> tuple[str, str, dict[str, Any] | None]:
        zones = self._yc_rows(
            self._call(
                [
                    str(self.yc_binary),
                    "dns",
                    "zone",
                    "list",
                    "--format",
                    "json",
                    "--no-user-output",
                ],
                timeout=60,
            ).output,
            "dns_zones",
        )
        try:
            zone_id = str(
                next(
                    item
                    for item in zones
                    if item.get("zone") == "kenigevents.ru."
                )["id"]
            )
        except (KeyError, StopIteration, TypeError) as exc:
            raise OperatorError(
                "canonical kenigevents.ru DNS zone is unavailable"
            ) from exc

        ip = self._call(
            ["getent", "ahostsv4", "mcp-datahub.kenigevents.ru"],
            timeout=30,
        ).output.split()[0]
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
            (
                row
                for row in records
                if row.get("name") == wanted_name and row.get("type") == "A"
            ),
            None,
        )
        if existing is not None and ip not in set(existing.get("data") or []):
            raise OperatorError(
                "Projects Hub DNS record conflicts with the existing MCP edge IPv4"
            )
        return zone_id, ip, existing

    def _ensure_certificate(self) -> None:
        hook = self.certbot_root / "hooks/certbot-yc-auth.sh"
        cleanup = self.certbot_root / "hooks/certbot-yc-cleanup.sh"
        reload_hook = self.certbot_root / "hooks/reload-nginx.sh"
        certbot = self.certbot_root / "certbot-venv/bin/certbot"
        cert = (
            self.certbot_root
            / "letsencrypt/live/my-data-hub-local-edge/fullchain.pem"
        )
        if not all(
            path.is_file()
            for path in (hook, cleanup, reload_hook, certbot, cert)
        ):
            raise OperatorError(
                "canonical Certbot DNS-01 machinery is unavailable"
            )

        san_before = self._call(
            [
                "openssl",
                "x509",
                "-in",
                str(cert),
                "-noout",
                "-ext",
                "subjectAltName",
            ],
            timeout=30,
        ).output
        if f"DNS:{TARGET_HOSTNAME}" in san_before:
            return

        auth_before, _ = self._append_case_hostname(hook, TARGET_HOSTNAME)
        cleanup_before, _ = self._append_case_hostname(
            cleanup, TARGET_HOSTNAME
        )
        try:
            domains = sorted(
                set(re.findall(r"DNS:([a-z0-9.-]+)", san_before))
                | {TARGET_HOSTNAME}
            )
            if not domains or any(
                domain.startswith("*.")
                or not domain.endswith(".kenigevents.ru")
                for domain in domains
            ):
                raise OperatorError(
                    "existing edge certificate contains an unexpected SAN"
                )
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
            [
                "openssl",
                "x509",
                "-in",
                str(cert),
                "-noout",
                "-ext",
                "subjectAltName",
            ],
            timeout=30,
        ).output
        if f"DNS:{TARGET_HOSTNAME}" not in san_after:
            raise OperatorError(
                "certificate readback does not contain the Projects Hub hostname"
            )

    def _activate_renderer(
        self,
        *,
        zone_id: str,
        ip: str,
        existing_dns: dict[str, Any] | None,
    ) -> None:
        renderer_source = self.vpn_root / "bot/vpn_manager.py"
        renderer = self.vpn_root / "scripts/render-configs.sh"
        env_file = self.vpn_root / ".env"
        if not all(
            path.is_file() for path in (renderer_source, renderer, env_file)
        ):
            raise OperatorError("canonical VPN edge renderer is unavailable")

        source_text = renderer_source.read_text(encoding="utf-8")
        for marker in (
            "LOCAL_MCP_EDGE_PROJECTS_HUB_ENABLED",
            "LOCAL_MCP_EDGE_PROJECTS_HUB_HOST",
            "LOCAL_MCP_EDGE_PROJECTS_HUB_PORT",
        ):
            if marker not in source_text:
                raise OperatorError(
                    "installed VPN renderer lacks the Projects Hub typed route"
                )

        original_env = env_file.read_text(encoding="utf-8")
        if "LOCAL_MCP_EDGE_ENABLED=1" not in original_env:
            raise OperatorError("canonical local MCP edge is not enabled")

        rendered_env = self._updated_vpn_env(original_env)
        fd, temp = tempfile.mkstemp(
            prefix=".projects-hub-edge-",
            dir=self.vpn_root,
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(rendered_env)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, env_file)
            temp = ""
            self._call([str(renderer)], cwd=self.vpn_root, timeout=120)

            config = (
                self.vpn_root / "data/nginx/nginx.conf"
            ).read_text(encoding="utf-8")
            for preserved in (
                "mcp-datahub.kenigevents.ru",
                "identity.kenigevents.ru",
                "ideas.kenigevents.ru",
                "street-story.kenigevents.ru",
            ):
                if f"{preserved} local_mcp_tls;" not in config:
                    raise OperatorError(
                        "renderer would remove an existing SNI route"
                    )
            if f"{TARGET_HOSTNAME} local_mcp_tls;" not in config:
                raise OperatorError(
                    "renderer did not produce the Projects Hub SNI route"
                )
            if (
                f"server_name {TARGET_HOSTNAME};" not in config
                or f"proxy_pass http://127.0.0.1:{TARGET_PORT};"
                not in config
            ):
                raise OperatorError(
                    "renderer did not produce the Projects Hub loopback route"
                )

            block_start = config.index(f"server_name {TARGET_HOSTNAME};")
            block = config[block_start : block_start + 2200]
            for required_header in (
                "proxy_set_header X-Forwarded-Host $host;",
                "proxy_set_header X-Forwarded-Proto https;",
                "proxy_set_header X-Forwarded-Port 443;",
                'proxy_set_header X-Forwarded-For "";',
            ):
                if required_header not in block:
                    raise OperatorError(
                        "Projects Hub edge forwarding contract is incomplete"
                    )

            self._call(
                ["docker", "compose", "build", "nginx"],
                cwd=self.vpn_root,
                timeout=600,
            )
            self._call(
                [
                    "docker",
                    "compose",
                    "up",
                    "-d",
                    "--no-deps",
                    "--force-recreate",
                    "nginx",
                ],
                cwd=self.vpn_root,
                timeout=180,
            )
            self._call(
                ["docker", "exec", "vpn-server-nginx-1", "nginx", "-t"],
                timeout=60,
            )

            if existing_dns is None:
                self._call(
                    [
                        str(self.yc_binary),
                        "dns",
                        "zone",
                        "add-records",
                        "--id",
                        zone_id,
                        "--record",
                        f"{TARGET_HOSTNAME}. 300 A {ip}",
                        "--no-user-output",
                    ],
                    timeout=90,
                )
        except BaseException:
            env_file.write_text(original_env, encoding="utf-8")
            with contextlib.suppress(Exception):
                self._call([str(renderer)], cwd=self.vpn_root, timeout=120)
                self._call(
                    [
                        "docker",
                        "compose",
                        "up",
                        "-d",
                        "--no-deps",
                        "--force-recreate",
                        "nginx",
                    ],
                    cwd=self.vpn_root,
                    timeout=180,
                )
            raise
        finally:
            if temp:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temp)

    def _public_readback(
        self,
        sha: str,
        timeout: int = 120,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last = "unreachable"
        while time.monotonic() < deadline:
            try:
                health_raw = self._call(
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
                health = json.loads(health_raw)
                config_raw = self._call(
                    [
                        "curl",
                        "--fail",
                        "--silent",
                        "--show-error",
                        "--max-time",
                        "10",
                        f"https://{TARGET_HOSTNAME}/api/auth/config",
                    ],
                    timeout=15,
                ).output
                config = json.loads(config_raw)
                if (
                    health.get("ok") is True
                    and health.get("release_sha") == sha
                    and health.get("auth_mode")
                    in {"public_yandex", "public_yandex+loopback_dev"}
                    and config.get("mode") == "yandex_pkce"
                    and config.get("provider") == "custom:yandex"
                    and config.get("redirect_url")
                    == f"https://{TARGET_HOSTNAME}/"
                ):
                    return {
                        "health": {
                            "ok": True,
                            "release_sha": sha,
                            "auth_mode": health["auth_mode"],
                        },
                        "auth": {
                            "mode": "yandex_pkce",
                            "provider": "custom:yandex",
                            "redirect_origin": TARGET_HOSTNAME,
                        },
                    }
                last = "public readback mismatch"
            except (OperatorError, ValueError, TypeError) as exc:
                last = str(exc)
            time.sleep(1)
        raise OperatorError(
            "Projects Hub public HTTPS/auth readback failed: " + last[:180]
        )

    def publish_edge_route(
        self,
        *,
        hostname: str,
        loopback_port: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if hostname != TARGET_HOSTNAME or loopback_port != TARGET_PORT:
            raise OperatorError("Projects Hub edge target is not allowlisted")
        request = {
            "hostname": hostname,
            "loopback_port": loopback_port,
        }

        def action() -> Mapping[str, Any]:
            status = self._projects_hub_service_status()
            if (
                status["active"] != "active"
                or status["substate"] != "running"
            ):
                raise OperatorError(
                    "Projects Hub service must be active before edge publication"
                )
            sha = self._current_release_sha()
            local = self._local_health(sha)
            zone_id, ip, existing_dns = self._dns_target()
            self._ensure_certificate()
            self._activate_renderer(
                zone_id=zone_id,
                ip=ip,
                existing_dns=existing_dns,
            )
            public = self._public_readback(sha)
            return {
                "hostname": TARGET_HOSTNAME,
                "loopback_port": TARGET_PORT,
                "release_sha": sha,
                "dns_ipv4": ip,
                "certificate_san_verified": True,
                "renderer": "vpn-server",
                "local_health": local,
                "public_health": public["health"],
                "public_auth": public["auth"],
                "existing_sni_routes_preserved": True,
            }

        return self._apply(
            "projects_hub_publish_edge_route",
            request,
            idempotency_key,
            action,
        )
