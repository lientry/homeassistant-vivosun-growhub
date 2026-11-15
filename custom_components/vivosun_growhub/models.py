"""Data models for the Vivosun GrowHub integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .devices import (
    DEVICES_BY_GROUP_ID,
    DEVICE_TYPE_CONTROLLER,
    DEVICE_TYPE_HUMIDIFIER,
    DEVICE_TYPE_DEHUMIDIFIER,
    DEVICE_TYPE_AIR_CONDITIONER,
    DEVICE_TYPE_HEATER,
    DEVICE_TYPE_CAMERA,
    DEVICE_TYPE_VSEED,
    DEVICE_TYPE_CURING_BOX,
    DEVICE_TYPE_VSPLUGA10,
    DEVICE_TYPE_VSPLUGA22,
    MODEL_A10,
    MODEL_A22,
    MODEL_C08,
    MODEL_S40,
)
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
    parent_device_id: str
    device_group_id: int
    client_id: str
    topic_prefix: str
    name: str
    online: bool
    scene_id: int
    device_type: str = "unknown"
    camera_username: str | None = None
    camera_password: str | None = None
    supports_point_log: bool = True

def synthetic_client_id(
    model: str,
    parent_id: str,
    device_id: str,
    ) -> str:
    return f"vivosun-{model}-{parent_id}-{device_id}"

def infer_device_type(name: str, client_id: str = "", device_group_id: int | None = None) -> str:
    """Infer device type from stable model tokens and friendly-name hints."""
    if device_group_id in DEVICES_BY_GROUP_ID:
        return DEVICES_BY_GROUP_ID[device_group_id]
    combined = f"{name} {client_id}".lower()
    model_token = client_model_token(client_id)
    if model_token == MODEL_A10:
        return DEVICE_TYPE_VSPLUGA10
    if model_token == MODEL_A22:
        return DEVICE_TYPE_VSPLUGA22
    if model_token == MODEL_S40:
        return DEVICE_TYPE_VSEED
    if model_token == MODEL_C08:
        return DEVICE_TYPE_AIR_CONDITIONER
    if model_token.startswith("VSCTL") or "growhub" in combined or DEVICE_TYPE_CONTROLLER in combined:
        return DEVICE_TYPE_CONTROLLER
    if model_token.startswith("VSDRY") or "aerodrain" in combined or DEVICE_TYPE_DEHUMIDIFIER in combined:
        return DEVICE_TYPE_DEHUMIDIFIER
    if model_token.startswith(("VSHMD", "VSHUM")) or "aerostream" in combined or DEVICE_TYPE_HUMIDIFIER in combined:
        return DEVICE_TYPE_HUMIDIFIER
    if model_token.startswith("VSHT") or "aeroflux" in combined or DEVICE_TYPE_HEATER in combined:
        return DEVICE_TYPE_HEATER
    if "growcam" in combined or DEVICE_TYPE_CAMERA in combined:
        return DEVICE_TYPE_CAMERA
    if model_token.startswith("VSCB") or "vcure" in combined:
        return DEVICE_TYPE_CURING_BOX
    return "unknown"

@dataclass(slots=True, frozen=True)
class CaptureDevice:
    """Account-discovered routing for read-only capture, independent of entities."""

    client_id: str
    device_id: str
    topic_prefix: str
    name: str
    device_type: str
    missing_fields: tuple[str, ...] = ()


def client_model_token(client_id: str) -> str:
    parts = client_id.split("-")
    if len(parts) >= 2:
        return parts[1].upper()
    return client_id.upper()

def parent_device_id_token(client_id: str) -> str:
    parts = client_id.split("-")
    if len(parts) >= 3:
        return parts[2].upper()
    return ""

@dataclass(slots=True, frozen=True)
class PlanStageInfo:
    """Plan stage details fetched from iot/plan/stageInfo."""

    stage_name: str
    icon: str
    content: dict[str, object]


@dataclass(slots=True, frozen=True)
class AwsIdentity:
    """AWS IoT identity payload parsed from awsIdentity endpoint."""

    aws_host: str
    aws_region: str
    aws_identity_id: str
    aws_open_id_token: str
    aws_port: int
