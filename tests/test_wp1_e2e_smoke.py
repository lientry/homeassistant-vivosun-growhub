"""WP-1 lifecycle smoke test using Home Assistant config entry APIs."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vivosun_growhub.const import CONF_EMAIL, CONF_PASSWORD, DOMAIN, PLATFORMS
from custom_components.vivosun_growhub.models import RuntimeData

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest import MonkeyPatch


class _StubCoordinator:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.started = False

    async def async_start(self) -> None:
        self.started = True

    async def async_shutdown(self) -> None:
        return None


async def test_wp1_setup_unload_entry_smoke(
    hass: HomeAssistant,
    enable_custom_integrations: bool,
    monkeypatch: MonkeyPatch,
) -> None:
    """A config entry can be set up and unloaded cleanly."""
    _ = enable_custom_integrations
    monkeypatch.setattr("custom_components.vivosun_growhub.VivosunCoordinator", _StubCoordinator)
    forward_mock = AsyncMock(return_value=True)
    unload_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward_mock)
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", unload_mock)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="smoke@example.com",
        data={CONF_EMAIL: "smoke@example.com", CONF_PASSWORD: "secret"},
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    runtime = hass.data[DOMAIN][entry.entry_id]
    assert isinstance(runtime, RuntimeData)
    assert isinstance(runtime.coordinator, _StubCoordinator)
    assert runtime.coordinator.started is True
    forward_mock.assert_awaited_once_with(entry, PLATFORMS)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    unload_mock.assert_awaited_once_with(entry, PLATFORMS)

    assert DOMAIN not in hass.data
