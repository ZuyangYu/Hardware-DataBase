"""Deployment-injected identity and client dependencies for Sandbox services.

The HDB repository deliberately does not convert ``SANDBOX_SERVICE_AUTH_REF``
into a token.  A deployment can inject a provider (workload identity, sidecar,
or owner-only secret mount) through :func:`configure_service_client_factory`.
The small file provider is only a convenience for a mounted short-lived token;
it reloads on every request and never exposes the reference in a header.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable

import src.settings as settings

from .client import SandboxServiceClient


ClientFactory = Callable[[], SandboxServiceClient | None]

_FACTORY: ClientFactory | None = None
_CLIENT: SandboxServiceClient | None = None
_LOCK = threading.Lock()


class FileTokenProvider:
    """Read a bounded bearer token from an operator-owned secret mount."""

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(str(path))

    def __call__(self, _service: str) -> str:
        try:
            stat = os.stat(self.path)
            if stat.st_mode & 0o077:
                raise RuntimeError("sandbox service token file is too broadly accessible")
            with open(self.path, "r", encoding="utf-8") as handle:
                token = handle.read().strip()
        except (OSError, UnicodeError) as exc:
            raise RuntimeError("sandbox service identity is unavailable") from exc
        if not token or len(token) > 8192 or any(ord(char) < 32 for char in token):
            raise RuntimeError("sandbox service identity is invalid")
        return token


def configure_service_client_factory(factory: ClientFactory | None) -> None:
    """Inject or clear the deployment's authenticated client factory."""

    global _FACTORY, _CLIENT
    with _LOCK:
        old, _FACTORY = _CLIENT, factory
        _CLIENT = None
    if old is not None:
        try:
            old.close()
        except Exception:
            pass


def _default_factory() -> SandboxServiceClient | None:
    runner_url = str(getattr(settings, "SANDBOX_RUNNER_URL", "") or "").strip()
    gateway_url = str(getattr(settings, "SANDBOX_GATEWAY_URL", "") or "").strip()
    token_file = str(getattr(settings, "SANDBOX_SERVICE_TOKEN_FILE", "") or "").strip()
    if not runner_url and not gateway_url:
        return None
    # No implicit environment-token or auth-ref interpretation.  A deployment
    # must explicitly mount a token file or inject a factory.
    if not token_file:
        return None
    provider = FileTokenProvider(token_file)
    return SandboxServiceClient(
        runner_url=runner_url or None,
        gateway_url=gateway_url or None,
        auth_provider=provider,
        require_authentication=True,
    )


def get_service_client() -> SandboxServiceClient | None:
    global _CLIENT
    with _LOCK:
        if _CLIENT is not None:
            return _CLIENT
        factory = _FACTORY
        if factory is None:
            factory = _default_factory
        try:
            _CLIENT = factory()
        except Exception:
            # Dependency resolution is fail-closed; routes return 503 and no
            # unauthenticated request is attempted.
            _CLIENT = None
        return _CLIENT


def close_service_client() -> None:
    global _CLIENT
    with _LOCK:
        client, _CLIENT = _CLIENT, None
    if client is not None:
        try:
            client.close()
        except Exception:
            pass


__all__ = [
    "FileTokenProvider",
    "ClientFactory",
    "close_service_client",
    "configure_service_client_factory",
    "get_service_client",
]
