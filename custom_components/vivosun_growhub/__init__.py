"""The Vivosun GrowHub integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_EMAIL, CONF_PASSWORD, DOMAIN, PLATFORMS
from .coordinator import VivosunCoordinator
from .exceptions import VivosunGrowhubError
from .models import RuntimeData

if TYPE_CHECKING:
    from collections.abc import MutableMapping

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.typing import ConfigType


def _domain_data(hass: HomeAssistant) -> MutableMapping[str, RuntimeData]:
    """Return typed domain runtime storage."""
    return cast("MutableMapping[str, RuntimeData]", hass.data.setdefault(DOMAIN, {}))


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Vivosun GrowHub integration from yaml (unused)."""
    _ = config
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Vivosun GrowHub from a config entry."""
    email_value = entry.data.get(CONF_EMAIL)
    password_value = entry.data.get(CONF_PASSWORD)
    if not isinstance(email_value, str) or not isinstance(password_value, str):
        raise ConfigEntryNotReady("Missing credentials in config entry")

    coordinator = VivosunCoordinator(
        hass,
        async_get_clientsession(hass),
        email=email_value,
        password=password_value,
    )

    try:
        await coordinator.async_start()
    except VivosunGrowhubError as err:
        await coordinator.async_shutdown()
        raise ConfigEntryNotReady(str(err)) from err
    except Exception:
        await coordinator.async_shutdown()
        raise

    _domain_data(hass)[entry.entry_id] = RuntimeData(
        entry_id=entry.entry_id,
        coordinator=coordinator,
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Vivosun GrowHub config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    domain_data = _domain_data(hass)
    runtime = domain_data.pop(entry.entry_id, None)
    if runtime is not None and runtime.coordinator is not None:
        await runtime.coordinator.async_shutdown()
    if not domain_data:
        hass.data.pop(DOMAIN, None)
    return True
