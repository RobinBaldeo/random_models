"""Async Redis checkpoint backend keyed by thread_id, with fakeredis fallback for tests."""

from __future__ import annotations

import base64
import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

# ---------------------------------------------------------------------------
# NEW: Use LangGraph's native JsonPlusSerializer instead of dumpd/load
# ---------------------------------------------------------------------------
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from ..models import Checkpoint, RunStatus

# ---------------------------------------------------------------------------
# LangGraph Send import (optional – graceful fallback)
# ---------------------------------------------------------------------------
try:
    from langgraph.types import Send as _Send  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    _Send = None  # type: ignore[assignment, misc]

# ---------------------------------------------------------------------------
# Module-level serde instance (shared across all backends)
# ---------------------------------------------------------------------------
_serde = JsonPlusSerializer()

# ---------------------------------------------------------------------------
# URL scheme constants
# ---------------------------------------------------------------------------
_SCHEME_REDIS = "redis://"
_SCHEME_REDISS = "rediss://"
_SCHEME_UNIX = "unix://"
_SCHEME_FAKEREDIS = "fakeredis://"
_VALID_SCHEMES = (_SCHEME_REDIS, _SCHEME_REDISS, _SCHEME_UNIX, _SCHEME_FAKEREDIS)

try:  # pragma: no cover - optional dependency
    import redis.asyncio as aioredis  # type: ignore
except Exception:  # pragma: no cover
    aioredis = None

try:  # pragma: no cover - optional dependency
    import fakeredis  # type: ignore
    import fakeredis.aioredis as fake_aioredis  # type: ignore
except Exception:  # pragma: no cover
    fakeredis = None
    fake_aioredis = None


@dataclass
class RedisTLSConfig:
    """TLS/SSL configuration for Redis connection (mTLS support)."""

    ssl_required: bool | None = None
    cert_base64: str | None = None
    cert_password: str | None = None


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse an ISO-8601 datetime string, normalising trailing 'Z'."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:  # pragma: no cover - defensive
        return None


def _resolve_env_template(value: str | None) -> str | None:
    """Resolve ``${ENV_VAR}`` placeholders to environment values.

    Returns None for non-string values (e.g., dicts from misconfigured properties).
    """
    if value is None or not isinstance(value, str):
        return None
    value = value.strip()
    if value.startswith("${") and value.endswith("}"):
        return os.getenv(value[2:-1])
    return value


def _coerce_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized if normalized else None


def _write_pem_to_tempfile(pem_data: bytes, suffix: str = ".pem") -> str:
    """Write PEM data to a temporary file and return the path."""
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        f.write(pem_data)
    return path


# ---------------------------------------------------------------------------
# Serialisation helpers using JsonPlusSerializer
# ---------------------------------------------------------------------------

def _serialize_state(state: Any) -> dict[str, Any]:
    """Serialize checkpoint state via JsonPlusSerializer.

    Stores the type tag and base64-encoded payload bytes so the outer
    envelope can still be persisted as JSON in Redis.
    """
    type_name, payload_bytes = _serde.dumps_typed(state)
    return {
        "type": type_name,
        "payload_b64": base64.b64encode(payload_bytes).decode("ascii"),
    }


def _deserialize_state(data: Any) -> Any:
    """Deserialize checkpoint state from JsonPlusSerializer format.

    Also handles legacy format (produced by the old _deep_serialize path)
    by falling back to returning the raw data as-is.
    """
    if isinstance(data, dict) and "type" in data and "payload_b64" in data:
        type_name = data["type"]
        payload_bytes = base64.b64decode(data["payload_b64"])
        return _serde.loads_typed((type_name, payload_bytes))
    # Legacy fallback: return raw dict (old checkpoints before migration)
    return data


# ========================== RedisCheckpointBackend ==========================


class RedisCheckpointBackend:
    """Async Redis backend for checkpoint persistence, keyed by ``thread_id``.

    All public I/O methods (``save``, ``load``, ``delete``) are **async** to
    avoid blocking the event loop during network round-trips.

    Configuration is loaded from APP_CONFIG with fallbacks to constructor
    arguments and environment variables.
    """

    def __init__(
        self,
        *,
        url: str | None = None,
        ttl_seconds: int | None = None,
        namespace: str = "orch",
        emulate: bool | None = None,
        tls_config: RedisTLSConfig | None = None,
    ) -> None:
        self._ttl_seconds = ttl_seconds if isinstance(ttl_seconds, int) and ttl_seconds > 0 else 86400
        self._namespace = namespace.strip(":") if namespace else "orch"
        self._ssl_cert_path: str | None = None
        self._ssl_key_path: str | None = None

        config = self._load_app_config()
        tls = tls_config or RedisTLSConfig()

        # Resolve final values: explicit args > APP_CONFIG > defaults
        final_url = _coerce_string(url) or _coerce_string(config["url"]) or "redis://localhost:6379/0"
        final_ssl = tls.ssl_required if tls.ssl_required is not None else self._coerce_bool(config["ssl"])
        final_emulate = self._coerce_bool(emulate) if emulate is not None else self._coerce_bool(config["emulate"])
        redis_auth_secret = _coerce_string(config["auth_secret"])
        final_cert_base64 = _coerce_string(tls.cert_base64) if tls.cert_base64 is not None else _coerce_string(config["cert_base64"])
        final_cert_password = _coerce_string(tls.cert_password) if tls.cert_password is not None else _coerce_string(config["cert_password"])

        normalized_url = self._normalize_url(final_url, ssl_required=final_ssl)
        emulate_flag = self._should_emulate(final_emulate, normalized_url)

        try:
            self._client = self._build_client(
                normalized_url,
                emulate_flag,
                auth_secret=redis_auth_secret,
                tls_config=RedisTLSConfig(
                    ssl_required=final_ssl,
                    cert_base64=final_cert_base64,
                    cert_password=final_cert_password,
                ),
            )
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(f"Failed to initialise Redis client: {exc}") from exc

    # -- configuration helpers -----------------------------------------------

    @staticmethod
    def _load_app_config() -> dict[str, Any]:
        """Load Redis configuration from APP_CONFIG."""
        config: dict[str, Any] = {
            "url": None,
            "auth_secret": None,
            "ssl": None,
            "emulate": None,
            "cert_base64": None,
            "cert_password": None,
        }
        try:
            from config.settings import APP_CONFIG

            config["url"] = _coerce_string(APP_CONFIG.get_property("app.redis.redis_url"))
            config["auth_secret"] = _coerce_string(APP_CONFIG.get_property("app.redis.redis_password"))
            config["ssl"] = APP_CONFIG.get_property("app.redis.ssl")
            config["emulate"] = APP_CONFIG.get_property("app.redis.emulate")
            config["cert_base64"] = _coerce_string(APP_CONFIG.get_property("app.certificate.Base64Data"))
            config["cert_password"] = _coerce_string(APP_CONFIG.get_property("app.certificate.cert_password"))
        except Exception:  # pragma: no cover - graceful fallback
            pass
        return config

    @staticmethod
    def _coerce_bool(value: Any) -> bool | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                return True
            if lowered in {"0", "false", "no", "off"}:
                return False
        return None

    @staticmethod
    def _normalize_url(url: str, *, ssl_required: bool | None = None) -> str:
        """Normalise Redis URL while respecting SSL/TLS configuration."""
        if url.startswith(_VALID_SCHEMES):
            if url.startswith(_SCHEME_REDIS):
                if ssl_required is False:
                    return url
                return _SCHEME_REDISS + url[len(_SCHEME_REDIS):]
            if url.startswith(_SCHEME_REDISS) and ssl_required is False:
                return _SCHEME_REDIS + url[len(_SCHEME_REDISS):]
            return url
        scheme = _SCHEME_REDISS if ssl_required is not False else _SCHEME_REDIS
        return f"{scheme}{url}/0"

    @staticmethod
    def _truthy(value: str | None) -> bool:
        return bool(value) and value.lower() in {"1", "true", "yes"}

    def _should_emulate(self, emulate: bool | None, url: str) -> bool:
        """Determine whether to use fakeredis emulation.

        Priority: ``fakeredis://`` URL > explicit arg > ``REDIS_EMULATOR`` env var.
        """
        if url.startswith(_SCHEME_FAKEREDIS):
            return True
        if emulate is not None:
            return bool(emulate)
        return self._truthy(os.getenv("REDIS_EMULATOR"))

    def _build_client(
        self,
        url: str,
        emulate: bool,
        *,
        auth_secret: str | None = None,
        tls_config: RedisTLSConfig | None = None,
    ) -> Any:
        """Build an async redis / fakeredis client with optional mTLS."""
        if emulate:
            if fake_aioredis is None:  # pragma: no cover
                raise RuntimeError("fakeredis is required for REDIS_EMULATOR mode")
            return fake_aioredis.FakeRedis(decode_responses=True)

        if aioredis is None:  # pragma: no cover
            raise RuntimeError("redis (redis-py) is required for Redis backend")

        tls = tls_config or RedisTLSConfig()
        if tls.cert_base64 and tls.cert_password:
            return self._build_mtls_client(url, tls, auth_secret=auth_secret)

        kwargs: dict[str, Any] = {"decode_responses": True}
        if auth_secret:
            kwargs["password"] = auth_secret
        return aioredis.from_url(url, **kwargs)

    def _build_mtls_client(self, url: str, tls: RedisTLSConfig, *, auth_secret: str | None = None) -> Any:
        """Build an async Redis client with mTLS client certificates (PKCS#12)."""
        try:
            from cryptography.hazmat.primitives.serialization import (
                Encoding,
                NoEncryption,
                PrivateFormat,
                pkcs12,
            )

            pfx_data = base64.b64decode(tls.cert_base64)
            private_key, certificate, _ = pkcs12.load_key_and_certificates(
                pfx_data, tls.cert_password.encode("utf-8")
            )

            self._ssl_cert_path = _write_pem_to_tempfile(
                certificate.public_bytes(Encoding.PEM),
            )
            self._ssl_key_path = _write_pem_to_tempfile(
                private_key.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption()),
            )

            kwargs: dict[str, Any] = {
                "decode_responses": True,
                "ssl_certfile": self._ssl_cert_path,
                "ssl_keyfile": self._ssl_key_path,
                "ssl_cert_reqs": "none",
            }
            if auth_secret:
                kwargs["password"] = auth_secret

            return aioredis.from_url(
                url,
                **kwargs,
            )
        except Exception as exc:
            raise RuntimeError(f"Failed to load client certificate: {exc}") from exc

    # -- key helpers ----------------------------------------------------------

    @staticmethod
    def _normalize_thread_id(thread_id: Any) -> str:
        if isinstance(thread_id, str):
            normalized = thread_id.strip()
            if normalized:
                return normalized
            raise ValueError("thread_id must be a non-empty string")
        if thread_id is None:
            raise ValueError("thread_id is required")
        raise ValueError("thread_id must be a string")

    def _key(self, thread_id: Any) -> str:
        """Build the Redis key for a given ``thread_id``."""
        normalized_thread_id = self._normalize_thread_id(thread_id)
        return f"{self._namespace}:threads:{normalized_thread_id}"

    # -- serialisation -------------------------------------------------------

    @staticmethod
    def _serialize(checkpoint: Checkpoint) -> dict[str, Any]:
        return {
            "run_id": checkpoint.run_id,
            "thread_id": checkpoint.thread_id,
            "state": _serialize_state(checkpoint.state),
            "status": checkpoint.status.value,
            "metadata": _serialize_state(checkpoint.metadata) if checkpoint.metadata else {},
            "created_at": checkpoint.created_at.isoformat(),
            "expires_at": checkpoint.expires_at.isoformat() if checkpoint.expires_at else None,
            "token": checkpoint.token,
        }

    @staticmethod
    def _deserialize(payload: Mapping[str, Any]) -> Checkpoint:
        status = RunStatus.from_raw(str(payload.get("status") or ""), RunStatus.RUNNING)
        created_at = _parse_datetime(str(payload.get("created_at") or "")) or datetime.now(timezone.utc)
        expires_at = _parse_datetime(payload.get("expires_at"))
        raw_metadata = payload.get("metadata") or {}
        metadata = _deserialize_state(raw_metadata) if raw_metadata else {}
        if not isinstance(metadata, dict):
            metadata = {}
        state = _deserialize_state(payload.get("state")) if payload.get("state") else {}
        if not isinstance(state, dict):
            state = {}

        return Checkpoint(
            run_id=str(payload.get("run_id") or ""),
            state=state,  # type: ignore[arg-type]
            status=status,
            thread_id=payload.get("thread_id"),
            metadata=metadata,
            created_at=created_at,
            expires_at=expires_at,
            token=payload.get("token"),
        )

    # -- public async API ----------------------------------------------------

    async def save(self, checkpoint: Checkpoint) -> None:
        """Persist a checkpoint snapshot keyed by ``thread_id``."""
        thread_id = self._normalize_thread_id(checkpoint.thread_id)
        key = self._key(thread_id)
        payload = json.dumps(
            self._serialize(checkpoint), separators=(",", ":"), ensure_ascii=False,
        )
        if self._ttl_seconds:
            await self._client.set(key, payload, ex=self._ttl_seconds)
        else:
            await self._client.set(key, payload)

    async def load(self, thread_id: str) -> Checkpoint | None:
        """Load a checkpoint for the given ``thread_id``, or ``None`` if absent."""
        data = await self._client.get(self._key(thread_id))
        if not data:
            return None
        if isinstance(data, bytes):  # pragma: no cover
            data = data.decode("utf-8")
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:  # pragma: no cover
            return None
        if not isinstance(payload, Mapping):
            return None
        return self._deserialize(payload)

    async def delete(self, thread_id: str) -> None:
        """Remove the stored checkpoint for ``thread_id``."""
        await self._client.delete(self._key(thread_id))


__all__ = ["RedisCheckpointBackend", "RedisTLSConfig"]
