"""Tests for shared entity helper utilities."""

from __future__ import annotations

from typing import TYPE_CHECKING

from custom_components.vivosun_growhub.entity_helpers import build_device_info, is_entity_available
from custom_components.vivosun_growhub.models import DeviceInfo

if TYPE_CHECKING:
    from homeassistant.helpers.device_registry import DeviceInfo as HADeviceInfo


class _CoordinatorStub:
    def __init__(self, *, client_id: str = "vivosun-VSX100-account-device-1") -> None:
        self.device = DeviceInfo(
            device_id="device-1",
            client_id=client_id,
            topic_prefix="prefix",
            name="GrowHub",
            online=True,
            scene_id=66078,
        )
        self.is_mqtt_connected = True
        self.data: object = {"shadows": {"device-1": {"connection": {"connected": True}}}}

    def get_device(self, device_id: str) -> DeviceInfo | None:
        if device_id == self.device.device_id:
            return self.device
        return None


def test_build_device_info_uses_domain_identifier_and_model_token() -> None:
    coordinator = _CoordinatorStub(client_id="vivosun-VSCTLE42A-user-device")
    info: HADeviceInfo = build_device_info(coordinator, "device-1")

    assert ("vivosun_growhub", "device-1") in info["identifiers"]
    assert info["name"] == "GrowHub"
    assert info["manufacturer"] == "VIVOSUN"
    assert info["model"] == "VSCTLE42A"


def test_build_device_info_falls_back_to_full_client_id_model() -> None:
    coordinator = _CoordinatorStub(client_id="singletoken")
    info: HADeviceInfo = build_device_info(coordinator, "device-1")

    assert info["model"] == "singletoken"


def test_is_entity_available_false_when_mqtt_disconnected() -> None:
    coordinator = _CoordinatorStub()
    coordinator.is_mqtt_connected = False

    assert is_entity_available(coordinator, "device-1") is False


def test_is_entity_available_true_when_runtime_data_shapes_are_missing() -> None:
    coordinator = _CoordinatorStub()

    coordinator.data = "not-a-mapping"
    assert is_entity_available(coordinator, "device-1") is True

    coordinator.data = {"shadows": "not-a-mapping"}
    assert is_entity_available(coordinator, "device-1") is True

    coordinator.data = {"shadows": {"device-1": {"connection": "not-a-mapping"}}}
    assert is_entity_available(coordinator, "device-1") is True

    coordinator.data = {"shadows": {"device-1": {"connection": {}}}}
    assert is_entity_available(coordinator, "device-1") is True


def test_is_entity_available_reflects_shadow_connected_flag() -> None:
    coordinator = _CoordinatorStub()

    coordinator.data = {"shadows": {"device-1": {"connection": {"connected": 0}}}}
    assert is_entity_available(coordinator, "device-1") is False

    coordinator.data = {"shadows": {"device-1": {"connection": {"connected": 1}}}}
    assert is_entity_available(coordinator, "device-1") is True
