"""Tests for Vivosun options flow."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, patch

from homeassistant.const import UnitOfTemperature
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vivosun_growhub.config_flow import VivosunGrowhubOptionsFlow
from custom_components.vivosun_growhub.const import DOMAIN
from custom_components.vivosun_growhub.models import DeviceInfo, RuntimeData
from custom_components.vivosun_growhub.sensor import VivosunChannelSensorEntity, async_setup_entry

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


class _StubCoordinator:
    def __init__(self) -> None:
        self.data: dict[str, object] = {"sensors": {"inTemp": 2500}}
        self.device = DeviceInfo(
            device_id="dev-1",
            client_id="vivosun-VSCTLE42A-acc-dev-1",
            topic_prefix="prefix",
            name="GrowHub",
            online=True,
            scene_id=66078,
        )
        self.is_mqtt_connected = True


async def test_options_flow_updates_temp_unit_and_sensor_unit(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    coordinator = _StubCoordinator()
    entry = MockConfigEntry(domain=DOMAIN, title="t", data={}, options={"temp_unit": "celsius"})
    entry.add_to_hass(hass)
    runtime = RuntimeData(entry_id=entry.entry_id, coordinator=cast("object", coordinator))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    added: list[VivosunChannelSensorEntity] = []

    def _add(entities: list[VivosunChannelSensorEntity]) -> None:
        added.extend(entities)

    await async_setup_entry(hass, entry, _add)
    in_temp = next(entity for entity in added if entity.unique_id.endswith("_inTemp"))
    assert in_temp.native_unit_of_measurement == UnitOfTemperature.CELSIUS
    assert in_temp.native_value == 25.0

    with patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)) as reload_mock:
        init_result = await hass.config_entries.options.async_init(entry.entry_id)
        assert init_result["type"] is FlowResultType.FORM
        assert init_result["step_id"] == "init"

        finish_result = await hass.config_entries.options.async_configure(
            init_result["flow_id"],
            user_input={"temp_unit": "fahrenheit"},
        )
        assert finish_result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()

    assert entry.options == {"temp_unit": "fahrenheit"}
    reload_mock.assert_awaited_once_with(entry.entry_id)
    assert in_temp.native_unit_of_measurement == UnitOfTemperature.FAHRENHEIT
    assert in_temp.native_value == 77.0


async def test_options_flow_same_values_do_not_trigger_reload(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    coordinator = _StubCoordinator()
    entry = MockConfigEntry(domain=DOMAIN, title="t", data={}, options={"temp_unit": "celsius"})
    entry.add_to_hass(hass)
    runtime = RuntimeData(entry_id=entry.entry_id, coordinator=cast("object", coordinator))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    with patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)) as reload_mock:
        init_result = await hass.config_entries.options.async_init(entry.entry_id)
        finish_result = await hass.config_entries.options.async_configure(
            init_result["flow_id"],
            user_input={"temp_unit": "celsius"},
        )
        assert finish_result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()

    reload_mock.assert_not_awaited()


async def test_options_flow_entry_accessor_falls_back_to_base_config_entry() -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="t", data={})
    flow = VivosunGrowhubOptionsFlow()
    flow.config_entry = entry  # type: ignore[attr-defined]

    assert flow._entry() is entry
