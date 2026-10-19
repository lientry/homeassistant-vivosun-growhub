"""Shared helpers for redacting sensitive integration data."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping

REDACTED = "***"

_SECRET_KEY_MARKERS = (
    "password",
    "devpass",
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
    "apikey",
    "privatekey",
    "cookie",
    "headers",
)
_PARTIAL_IDENTIFIER_MARKERS = (
    "email",
    "devuser",
    "userid",
    "identityid",
    "deviceid",
    "clientid",
    "topicprefix",
)
_DEBUG_PRESERVED_LENGTH = 3
_HASH_LENGTH = 8
_CAPTURE_PROGRAM_ID_FIELDS = frozenset({"userplanid", "userplanstageid"})
_HARDWARE_ID_FIELDS = frozenset({"hwid", "hardwareid"})
_PRIVATE_CAPTURE_FIELDS = frozenset({
    "account", "accountid", "owner", "ownerid", "tenant", "tenantid", "user", "userid", "username", "email",
    "identity", "identityid", "devuser",
    "mac", "macaddress", "ssid", "bssid", "serial", "serialnumber", "sn", "ip", "ips", "ipaddress", "ipaddr",
    "location", "gps", "coordinates", "latitude", "longitude", "lat", "lon", "lng", "address", "host", "url",
})


def redact_value_for_debug(value: object) -> object:
    """Return a safe value for debug logs and diagnostics output."""
    if isinstance(value, str):
        if value == REDACTED:
            return REDACTED
        return redact_identifier(value)
    return REDACTED


def redact_identifier(value: str) -> str:
    """Return stable partially redacted value for troubleshooting contexts."""
    if not value:
        return REDACTED
    if re.fullmatch(r".{1,3}\.\.\.[0-9a-f]{8}", value):
        return value

    if "@" in value:
        return _redact_email(value)

    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:_HASH_LENGTH]
    prefix = value[:_DEBUG_PRESERVED_LENGTH]
    return f"{prefix}...{digest}"


def redact_topic(topic: str) -> str:
    """Keep MQTT operation suffixes while removing account and device routes."""
    if topic.startswith("$aws/things/"):
        parts = topic.split("/")
        if len(parts) >= 3:
            parts[2] = redact_identifier(parts[2])
        return "/".join(parts)
    prefix, separator, suffix = topic.partition("/channel/")
    if separator:
        return f"{redact_identifier(prefix)}{separator}{suffix}"
    if topic.endswith("/#"):
        return f"{redact_identifier(topic[:-2])}/#"
    return redact_identifier(topic)


def sanitize_mapping_for_debug(payload: Mapping[str, object]) -> dict[str, object]:
    """Return recursively sanitized mapping for safe debug logging."""
    return {key: _sanitize_value(key, value) for key, value in payload.items()}


def sanitize_mapping_for_capture(payload: Mapping[str, object]) -> dict[str, object]:
    """Preserve opted-in device evidence while masking credentials, accounts, networks and location."""
    return {key: _sanitize_value(key, value, capture=True) for key, value in payload.items()}


def is_sensitive_capture_key(key: str) -> bool:
    """Recognize private fields without treating device, scene or program identifiers as secrets."""
    for part in key.split("."):
        normalized = part.lower().replace("_", "").replace("-", "")
        if normalized in _HARDWARE_ID_FIELDS:
            return True
        if normalized in _CAPTURE_PROGRAM_ID_FIELDS:
            continue
        words = re.split(r"[_-]|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", part)
        if (
            normalized in _PRIVATE_CAPTURE_FIELDS
            or _is_secret_key(normalized)
            or any(marker in normalized for marker in ("email", "devuser", "userid", "identityid"))
            or any(word.lower() in _PRIVATE_CAPTURE_FIELDS for word in words)
        ):
            return True
    return False


def _sanitize_value(key: str, value: object, *, capture: bool = False) -> object:
    if any(part.lower().replace("_", "").replace("-", "") in _HARDWARE_ID_FIELDS for part in key.split(".")):
        return REDACTED
    if capture and is_sensitive_capture_key(key):
        return REDACTED
    key_lower = key.lower()
    if _is_secret_key(key_lower) or key_lower.replace("_", "") in {
        "mac",
        "macaddress",
        "ssid",
        "bssid",
        "location",
        "serial",
        "serialnumber",
        "ip",
        "ipaddress",
    }:
        return REDACTED

    if isinstance(value, Mapping):
        nested = {
            nested_key: _sanitize_value(nested_key, nested_value, capture=capture)
            for nested_key, nested_value in value.items()
        }
        return nested

    if isinstance(value, list):
        return [_sanitize_value(key, item, capture=capture) for item in value]

    if key_lower in {"topic", "subscription_topics"} and isinstance(value, str):
        return redact_topic(value)

    if _is_identifier_key(key_lower) and not (capture and "deviceid" in key_lower.replace("_", "").replace("-", "")):
        return redact_value_for_debug(value)

    return value


def _redact_email(email: str) -> str:
    local, _, domain = email.partition("@")
    local_prefix = local[:1] if local else ""
    digest = hashlib.sha256(email.encode("utf-8")).hexdigest()[:_HASH_LENGTH]
    return f"{local_prefix}***@{domain}#{digest}"


def _is_identifier_key(key_lower: str) -> bool:
    normalized = key_lower.replace("_", "").replace("-", "")
    return any(marker in normalized for marker in _PARTIAL_IDENTIFIER_MARKERS)


def _is_secret_key(key_lower: str) -> bool:
    normalized = key_lower.replace("_", "").replace("-", "")
    if normalized == "modeltoken":
        return False
    return any(marker.replace("_", "") in normalized for marker in _SECRET_KEY_MARKERS)
