"""WP-1 skeleton tests."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from homeassistant.const import Platform
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vivosun_growhub import async_setup, async_setup_entry, async_unload_entry
from custom_components.vivosun_growhub.const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    DESIRED_LEVEL_PATH_NOTE,
    DOMAIN,
    MODE_AUTO,
    MODE_CYCLE,
    MODE_MANUAL,
    MODE_PLAN,
    PLATFORMS,
    SENSOR_CHANNEL_KEYS,
    SENSOR_KEY_CORE_TEMP,
    SENSOR_KEY_INSIDE_HUMI,
    SENSOR_KEY_INSIDE_TEMP,
    SENSOR_KEY_INSIDE_VPD,
    SENSOR_KEY_OUTSIDE_HUMI,
    SENSOR_KEY_OUTSIDE_TEMP,
    SENSOR_KEY_OUTSIDE_VPD,
    SENSOR_KEY_PROBE_HUMI,
    SENSOR_KEY_PROBE_TEMP,
    SENSOR_KEY_PROBE_VPD,
    SENSOR_KEY_RSSI,
    SENSOR_KEY_WATER_LEVEL,
    SENSOR_UNAVAILABLE_SENTINEL,
    SHADOW_NAME,
    TOPIC_CHANNEL_APP,
    TOPIC_SHADOW_BASE,
    TOPIC_SHADOW_GET,
    TOPIC_SHADOW_GET_ACCEPTED,
    TOPIC_SHADOW_UPDATE,
    TOPIC_SHADOW_UPDATE_ACCEPTED,
    TOPIC_SHADOW_UPDATE_DELTA,
    TOPIC_SHADOW_UPDATE_DOCUMENTS,
)
from custom_components.vivosun_growhub.models import RuntimeData

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest import MonkeyPatch


class _StubCoordinator:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.started = False
        self.stopped = False

    async def async_start(self) -> None:
        self.started = True

    async def async_shutdown(self) -> None:
        self.stopped = True


async def test_constants_match_wp1_spec() -> None:
    """Validate WP-1 constants from issue spec."""
    assert SHADOW_NAME == "GrowHub"
    assert TOPIC_SHADOW_BASE == "$aws/things/{thing}/shadow"
    assert TOPIC_SHADOW_GET == "$aws/things/{thing}/shadow/get"
    assert TOPIC_SHADOW_GET_ACCEPTED == "$aws/things/{thing}/shadow/get/accepted"
    assert TOPIC_SHADOW_UPDATE == "$aws/things/{thing}/shadow/update"
    assert TOPIC_SHADOW_UPDATE_ACCEPTED == "$aws/things/{thing}/shadow/update/accepted"
    assert TOPIC_SHADOW_UPDATE_DOCUMENTS == "$aws/things/{thing}/shadow/update/documents"
    assert TOPIC_SHADOW_UPDATE_DELTA == "$aws/things/{thing}/shadow/update/delta"
    assert TOPIC_CHANNEL_APP == "{topic_prefix}/channel/app"

    assert SENSOR_CHANNEL_KEYS == (
        SENSOR_KEY_INSIDE_TEMP,
        SENSOR_KEY_INSIDE_HUMI,
        SENSOR_KEY_INSIDE_VPD,
        SENSOR_KEY_OUTSIDE_TEMP,
        SENSOR_KEY_OUTSIDE_HUMI,
        SENSOR_KEY_OUTSIDE_VPD,
        SENSOR_KEY_CORE_TEMP,
        SENSOR_KEY_RSSI,
        SENSOR_KEY_PROBE_TEMP,
        SENSOR_KEY_PROBE_HUMI,
        SENSOR_KEY_PROBE_VPD,
        SENSOR_KEY_WATER_LEVEL,
    )
    assert len(SENSOR_CHANNEL_KEYS) == 12
    assert SENSOR_UNAVAILABLE_SENTINEL == -6666

    assert MODE_MANUAL == 0
    assert MODE_AUTO == 1
    assert MODE_CYCLE == 1
    assert MODE_PLAN == 2

    assert DESIRED_LEVEL_PATH_NOTE == "desired.<key>.manu.lv"
    assert PLATFORMS == [
        Platform.LIGHT, Platform.FAN, Platform.SENSOR,
        Platform.BINARY_SENSOR, Platform.HUMIDIFIER, Platform.CLIMATE,
    ]


async def test_async_setup_entry_and_unload_lifecycle(hass: HomeAssistant, monkeypatch: MonkeyPatch) -> None:
    """Setup should create runtime data and unload should cleanly remove it."""
    monkeypatch.setattr("custom_components.vivosun_growhub.VivosunCoordinator", _StubCoordinator)
    forward_mock = AsyncMock(return_value=True)
    unload_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward_mock)
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", unload_mock)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="test@example.com",
        data={CONF_EMAIL: "test@example.com", CONF_PASSWORD: "secret"},
    )

    assert await async_setup(hass, {})
    assert await async_setup_entry(hass, entry)

    domain_data = hass.data[DOMAIN]
    runtime = domain_data[entry.entry_id]
    assert isinstance(runtime, RuntimeData)
    assert runtime.entry_id == entry.entry_id
    assert isinstance(runtime.coordinator, _StubCoordinator)
    assert runtime.coordinator.started is True
    assert runtime.devices == {}
    forward_mock.assert_awaited_once_with(entry, PLATFORMS)

    assert await async_unload_entry(hass, entry)
    unload_mock.assert_awaited_once_with(entry, PLATFORMS)
    assert DOMAIN not in hass.data
