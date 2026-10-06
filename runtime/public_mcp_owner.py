"""Private owner key used only to consent to an external MCP client."""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from pathlib import Path


class DomainError(ValueError):
    def __init__(self, code, **_):
        super().__init__(code)


@dataclass(frozen=True)
class Actor:
    principal_id: str
    scopes: frozenset[str]
    owner: bool = True


class OwnerStore:
    def __init__(self, key_path):
        self.path = Path(key_path)
        self._key()

    def _key(self):
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError('Owner key must be a private regular file')
        if self.path.stat().st_mode & 0o077:
            raise ValueError('Owner key must have mode 0600')
        key = self.path.read_text().strip()
        if not key.startswith('dvc_owner_') or len(key) < 50:
            raise ValueError('Invalid owner key')
        return key

    def _actor(self):
        generation = hashlib.sha256(self._key().encode()).hexdigest()
        return Actor('owner:' + generation, frozenset({'devcoveer'}))

    def authenticate(self, key):
        if not isinstance(key, str) or not hmac.compare_digest(key.encode(), self._key().encode()):
            raise DomainError('unauthorized')
        return self._actor()

    def current(self, actor):
        current = self._actor()
        if actor != current:
            raise DomainError('access_revoked')
        return current
