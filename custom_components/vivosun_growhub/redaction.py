"""Shared helpers for redacting sensitive integration data."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

REDACTED = "***"

_SECRET_KEY_MARKERS = (
    "password",
    "token",
    "secret",
    "authorization",
    "auth_header",
    "credential",
    "accesskey",
    "secretkey",
    "sessiontoken",
    "signedurl",
    "signature",
)
_PARTIAL_IDENTIFIER_MARKERS = (
    "email",
    "userid",
    "identityid",
    "deviceid",
    "clientid",
    "topicprefix",
)
_DEBUG_PRESERVED_LENGTH = 3
_HASH_LENGTH = 8


def redact_value_for_debug(value: object) -> object:
    """Return a safe value for debug logs and diagnostics output."""
    if isinstance(value, str):
        return redact_identifier(value)
    return REDACTED


def redact_identifier(value: str) -> str:
    """Return stable partially redacted value for troubleshooting contexts."""
    if not value:
        return REDACTED

    if "@" in value:
        return _redact_email(value)

    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:_HASH_LENGTH]
    prefix = value[:_DEBUG_PRESERVED_LENGTH]
    return f"{prefix}...{digest}"


def sanitize_mapping_for_debug(payload: Mapping[str, object]) -> dict[str, object]:
    """Return recursively sanitized mapping for safe debug logging."""
    return {key: _sanitize_value(key, value) for key, value in payload.items()}


def _sanitize_value(key: str, value: object) -> object:
    key_lower = key.lower()
    if _is_secret_key(key_lower):
        return REDACTED

    if isinstance(value, Mapping):
        nested = {nested_key: _sanitize_value(nested_key, nested_value) for nested_key, nested_value in value.items()}
        return nested

    if isinstance(value, list):
        return [_sanitize_value(key, item) for item in value]

    if _is_identifier_key(key_lower):
        return redact_value_for_debug(value)

    return value


def _redact_email(email: str) -> str:
    local, _, domain = email.partition("@")
    local_prefix = local[:1] if local else ""
    digest = hashlib.sha256(email.encode("utf-8")).hexdigest()[:_HASH_LENGTH]
    return f"{local_prefix}***@{domain}#{digest}"


def _is_identifier_key(key_lower: str) -> bool:
    normalized = key_lower.replace("_", "")
    return any(marker in normalized for marker in _PARTIAL_IDENTIFIER_MARKERS)


def _is_secret_key(key_lower: str) -> bool:
    return any(marker in key_lower for marker in _SECRET_KEY_MARKERS)
