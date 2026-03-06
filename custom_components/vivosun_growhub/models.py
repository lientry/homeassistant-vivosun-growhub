"""Data models for the Vivosun GrowHub integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .coordinator import VivosunCoordinator


@dataclass(slots=True)
class RuntimeData:
    """Runtime data container for a config entry."""

    entry_id: str
    coordinator: VivosunCoordinator | None = None
    devices: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class DeviceIdentifiers:
    """Baseline identifiers for GrowHub devices."""

    device_id: str
    client_id: str
    topic_prefix: str


@dataclass(slots=True, frozen=True)
class AuthTokens:
    """Authentication tokens returned by the Vivosun login endpoint."""

    access_token: str
    login_token: str
    refresh_token: str
    user_id: str


@dataclass(slots=True, frozen=True)
class DeviceInfo:
    """Device entry parsed from getTotalList endpoint."""

    device_id: str
    client_id: str
    topic_prefix: str
    name: str
    online: bool
    scene_id: int


@dataclass(slots=True, frozen=True)
class AwsIdentity:
    """AWS IoT identity payload parsed from awsIdentity endpoint."""

    aws_host: str
    aws_region: str
    aws_identity_id: str
    aws_open_id_token: str
    aws_port: int
