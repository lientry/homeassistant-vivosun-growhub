"""Native fan registry and attribute labels keep protocol identifiers intact."""

from __future__ import annotations

import json
import logging
import re
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from homeassistant.helpers import entity_registry
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.helpers.translation import (
    async_get_translations,
    async_translate_state,
    async_translate_state_attr,
)
from homeassistant.setup import async_setup_component

from custom_components.vivosun_growhub.const import DOMAIN
from custom_components.vivosun_growhub.fan import VivosunCirculationFanEntity
from custom_components.vivosun_growhub.select import VivosunLightModeSelect, VivosunSpectrumSelect
from tests.test_light_pending import _receive, _setup

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


@pytest.mark.parametrize("catalog", ["strings.json", "translations/en.json"])
def test_entity_state_translation_keys_follow_hassfest_schema(catalog: str) -> None:
    resources = json.loads((Path(__file__).parents[1] / "custom_components" / DOMAIN / catalog).read_text())

    def check(value: object) -> None:
        if not isinstance(value, dict):
            return
        states = value.get("state")
        if isinstance(states, dict):
            assert all(re.fullmatch(r"[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?", key) for key in states)
        for child in value.values():
            check(child)

    check(resources["entity"])


async def test_native_fan_preset_label_preserves_entity_and_command(
    hass: HomeAssistant, enable_custom_integrations: None,
) -> None:
    """Register the real fan so frontend localization can find its preset label."""
    coordinator, mqtt, _ = _setup(hass)
    assert await async_setup_component(hass, "fan", {})
    platform = EntityPlatform(
        hass=hass, logger=logging.getLogger(__name__), domain="fan", platform_name=DOMAIN,
        platform=None, scan_interval=timedelta(seconds=30), entity_namespace=None,
    )
    fan = VivosunCirculationFanEntity(coordinator, "light-0")
    await platform.async_add_entities([fan])
    await hass.async_block_till_done()
    try:
        assert fan.entity_id is not None
        registered = entity_registry.async_get(hass).async_get(fan.entity_id)
        assert registered is not None
        assert registered.translation_key == "circulation_fan"
        assert registered.unique_id == "vivosun_growhub_light-0_cfan"
        assert fan.name == "Circulation Fan"
        translated = await async_get_translations(hass, "en", "entity", {DOMAIN})
        hass.config.components.add(DOMAIN)
        prefix = f"component.{DOMAIN}.entity.fan.{registered.translation_key}."
        assert translated[prefix + "state_attributes.preset_mode.state.natural_wind"] == "natural wind"
        assert async_translate_state_attr(
            hass, "natural_wind", "fan", DOMAIN, registered.translation_key, None, "preset_mode"
        ) == "natural wind"
        for state, expected in (("true", "yes"), ("false", "no")):
            assert async_translate_state_attr(
                hass, state, "fan", DOMAIN, registered.translation_key, None, "oscillating"
            ) == expected

        await hass.services.async_call(
            "fan", "set_preset_mode", {"entity_id": fan.entity_id, "preset_mode": "natural_wind"},
            blocking=True,
        )
        assert mqtt.published[-1][1]["state"] == {"desired": {"cFan": {"mode": 0, "manu": {"lv": 200}}}}
        await _receive(coordinator, "thing-0", "update/accepted", {
            "version": 1, "state": {"reported": {"cFan": {"lv": 200}}},
        })
        state = hass.states.get(fan.entity_id)
        assert state is not None
        assert state.attributes["preset_mode"] == "natural_wind"
        assert state.attributes["preset_modes"] == ["natural_wind"]
    finally:
        await coordinator._disconnect_mqtt_client()
        await platform.async_reset()


async def test_native_select_controls_preserve_service_options(
    hass: HomeAssistant, enable_custom_integrations: None,
) -> None:
    coordinator, _, _ = _setup(hass)
    mode = VivosunLightModeSelect(coordinator, "light-0")
    spectrum = VivosunSpectrumSelect(coordinator, "light-0")
    assert mode.entity_category is None
    assert mode.name == "Grow Light Mode" and spectrum.name == "Spectrum"
    assert mode.options == ["Manual", "Cycle"]
    assert spectrum.options == ["Seedling", "Vegetative", "Bloom"]
    assert mode.unique_id == "vivosun_growhub_light-0_light_mode"
    assert spectrum.unique_id == "vivosun_growhub_light-0_spectrum"
    await async_get_translations(hass, "en", "entity", {DOMAIN})
    hass.config.components.add(DOMAIN)
    for entity, expected in (
        (mode, ("Manual", "Cycle")),
        (spectrum, ("Seedling", "Vegetative", "Bloom")),
    ):
        assert [
            async_translate_state(hass, raw, "select", DOMAIN, entity.translation_key, None)
            for raw in entity.options
        ] == list(expected)
