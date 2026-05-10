"""The Vivosun GrowHub integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import voluptuous as vol
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    DOMAIN,
    OPTION_SUPPORT_CAPTURE_ENABLED,
    PLATFORMS,
    SERVICE_START_SUPPORT_CAPTURE,
    SERVICE_STOP_SUPPORT_CAPTURE,
    SUPPORT_CAPTURE_DEFAULT_MAX_EVENTS,
)
from .coordinator import VivosunCoordinator
from .exceptions import VivosunGrowhubError
from .models import RuntimeData

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

if TYPE_CHECKING:
    from collections.abc import MutableMapping

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant, ServiceCall
    from homeassistant.helpers.typing import ConfigType


def _domain_data(hass: HomeAssistant) -> MutableMapping[str, RuntimeData]:
    """Return typed domain runtime storage."""
    return cast("MutableMapping[str, RuntimeData]", hass.data.setdefault(DOMAIN, {}))


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Vivosun GrowHub integration from yaml (unused)."""
    _ = config
    hass.data.setdefault(DOMAIN, {})
    _register_services(hass)
    return True


def _register_services(hass: HomeAssistant) -> None:
    """Register integration-wide support capture services once."""
    async def _handle_start(call: ServiceCall) -> None:
        await _async_handle_start_support_capture(hass, call)

    async def _handle_stop(call: ServiceCall) -> None:
        await _async_handle_stop_support_capture(hass, call)

    if not hass.services.has_service(DOMAIN, SERVICE_START_SUPPORT_CAPTURE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_START_SUPPORT_CAPTURE,
            _handle_start,
            schema=vol.Schema(
                {
                    vol.Optional("entry_id"): str,
                    vol.Optional("max_events", default=SUPPORT_CAPTURE_DEFAULT_MAX_EVENTS): vol.All(
                        vol.Coerce(int), vol.Range(min=10, max=5000)
                    ),
                }
            ),
        )
    if not hass.services.has_service(DOMAIN, SERVICE_STOP_SUPPORT_CAPTURE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_STOP_SUPPORT_CAPTURE,
            _handle_stop,
            schema=vol.Schema({vol.Optional("entry_id"): str}),
        )


def _resolve_runtime_for_service(hass: HomeAssistant, entry_id: str | None) -> RuntimeData:
    """Resolve a target config entry for support capture services."""
    domain_data = _domain_data(hass)
    if entry_id is not None:
        runtime = domain_data.get(entry_id)
        if runtime is None or runtime.coordinator is None:
            raise ServiceValidationError(f"Unknown or unloaded Vivosun entry_id: {entry_id}")
        return runtime

    runtimes = [runtime for runtime in domain_data.values() if runtime.coordinator is not None]
    if len(runtimes) != 1:
        raise ServiceValidationError("entry_id is required when multiple Vivosun entries are configured")
    return runtimes[0]


async def _async_handle_start_support_capture(hass: HomeAssistant, call: ServiceCall) -> None:
    """Start support capture for a config entry."""
    runtime = _resolve_runtime_for_service(hass, cast("str | None", call.data.get("entry_id")))
    coordinator = runtime.coordinator
    if coordinator is None:
        raise ServiceValidationError("Vivosun coordinator is not loaded")
    await coordinator.async_start_support_capture(
        max_events=cast("int", call.data.get("max_events", SUPPORT_CAPTURE_DEFAULT_MAX_EVENTS))
    )


async def _async_handle_stop_support_capture(hass: HomeAssistant, call: ServiceCall) -> None:
    """Stop support capture for a config entry."""
    runtime = _resolve_runtime_for_service(hass, cast("str | None", call.data.get("entry_id")))
    coordinator = runtime.coordinator
    if coordinator is None:
        raise ServiceValidationError("Vivosun coordinator is not loaded")
    await coordinator.async_stop_support_capture()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Vivosun GrowHub from a config entry."""
    _register_services(hass)
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
        if entry.options.get(OPTION_SUPPORT_CAPTURE_ENABLED) is True:
            await coordinator.async_start_support_capture(max_events=SUPPORT_CAPTURE_DEFAULT_MAX_EVENTS)
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
        hass.services.async_remove(DOMAIN, SERVICE_START_SUPPORT_CAPTURE)
        hass.services.async_remove(DOMAIN, SERVICE_STOP_SUPPORT_CAPTURE)
        hass.data.pop(DOMAIN, None)
    return True
