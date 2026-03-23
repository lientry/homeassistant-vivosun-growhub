"""Config flow for the Vivosun GrowHub integration."""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import VivosunApiClient
from .const import CONF_CAMERA_IP, CONF_EMAIL, CONF_HAS_CAMERA, CONF_PASSWORD, DEFAULT_TEMP_UNIT, DOMAIN
from .exceptions import VivosunAuthError, VivosunConnectionError, VivosunResponseError

OPTIONS_TEMP_UNIT = "temp_unit"

if TYPE_CHECKING:
    from homeassistant.data_entry_flow import FlowResult


class VivosunGrowhubConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):  # type: ignore[misc,call-arg]
    """Handle a config flow for Vivosun GrowHub."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize config flow state."""
        self._pending_user_input: dict[str, str] | None = None

    async def async_step_user(self, user_input: dict[str, str] | None = None) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                unique_id, has_camera = await self._async_validate_input(user_input)
            except VivosunAuthError:
                errors["base"] = "invalid_auth"
            except VivosunConnectionError:
                errors["base"] = "cannot_connect"
            except (VivosunResponseError, Exception):
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                self._pending_user_input = user_input
                if has_camera:
                    return await self.async_step_camera()
                return self.async_create_entry(
                    title=user_input[CONF_EMAIL],
                    data={
                        CONF_EMAIL: user_input[CONF_EMAIL],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_HAS_CAMERA: False,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_EMAIL): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_camera(self, user_input: dict[str, str] | None = None) -> FlowResult:
        """Optionally collect the LAN IP for a discovered GrowCam device."""
        pending = self._pending_user_input
        if pending is None:
            return await self.async_step_user()

        errors: dict[str, str] = {}
        if user_input is not None:
            camera_ip = user_input.get(CONF_CAMERA_IP, "").strip()
            if camera_ip:
                try:
                    ipaddress.ip_address(camera_ip)
                except ValueError:
                    errors[CONF_CAMERA_IP] = "invalid_ip"
            if not errors:
                options = {CONF_CAMERA_IP: camera_ip} if camera_ip else {}
                return self.async_create_entry(
                    title=pending[CONF_EMAIL],
                    data={
                        CONF_EMAIL: pending[CONF_EMAIL],
                        CONF_PASSWORD: pending[CONF_PASSWORD],
                        CONF_HAS_CAMERA: True,
                    },
                    options=options,
                )

        schema = vol.Schema({vol.Optional(CONF_CAMERA_IP, default=""): str})
        return self.async_show_form(step_id="camera", data_schema=schema, errors=errors)

    async def _async_validate_input(self, user_input: dict[str, str]) -> tuple[str, bool]:
        """Validate credentials and return account user id plus camera presence."""
        api = VivosunApiClient(async_get_clientsession(self.hass))
        tokens = await api.login(user_input[CONF_EMAIL], user_input[CONF_PASSWORD])
        devices = await api.get_devices(tokens)
        return tokens.user_id, any(device.device_type == "camera" for device in devices)

    @staticmethod
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return VivosunGrowhubOptionsFlow(config_entry)


class VivosunGrowhubOptionsFlow(config_entries.OptionsFlow):  # type: ignore[misc]
    """Handle options for Vivosun GrowHub."""

    def __init__(self, config_entry: config_entries.ConfigEntry | None = None) -> None:
        """Initialize options flow across Home Assistant versions."""
        self._config_entry = config_entry

    def _entry(self) -> config_entries.ConfigEntry:
        """Return the config entry attached to this options flow."""
        if self._config_entry is not None:
            return self._config_entry
        return self.config_entry

    async def async_step_init(self, user_input: dict[str, str] | None = None) -> FlowResult:
        """Manage options."""
        entry = self._entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            normalized_input = self._normalize_options(user_input)
            camera_ip = normalized_input.get(CONF_CAMERA_IP, "")
            if camera_ip:
                try:
                    ipaddress.ip_address(camera_ip)
                except ValueError:
                    errors[CONF_CAMERA_IP] = "invalid_ip"
            if not errors:
                if normalized_input != entry.options:
                    self.hass.async_create_task(self.hass.config_entries.async_reload(entry.entry_id))
                return self.async_create_entry(title="", data=normalized_input)

        schema_fields: dict[vol.Marker, object] = {
            vol.Required(
                OPTIONS_TEMP_UNIT,
                default=entry.options.get(OPTIONS_TEMP_UNIT, DEFAULT_TEMP_UNIT),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=["celsius", "fahrenheit"],
                    translation_key=OPTIONS_TEMP_UNIT,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
        }
        if self._should_show_camera_ip(entry):
            schema_fields[
                vol.Optional(
                    CONF_CAMERA_IP,
                    default=entry.options.get(CONF_CAMERA_IP, ""),
                )
            ] = str

        schema = vol.Schema(schema_fields)
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)

    def _should_show_camera_ip(self, entry: config_entries.ConfigEntry) -> bool:
        """Return whether camera IP should be exposed in options."""
        if entry.data.get(CONF_HAS_CAMERA) or entry.options.get(CONF_CAMERA_IP):
            return True
        runtime = self.hass.data.get(DOMAIN, {}).get(entry.entry_id)
        coordinator = getattr(runtime, "coordinator", None)
        camera_devices = getattr(coordinator, "camera_devices", None)
        return bool(camera_devices)

    def _normalize_options(self, user_input: dict[str, str]) -> dict[str, str]:
        """Strip empty optional values so options remain stable across no-op submits."""
        normalized = dict(user_input)
        camera_ip = normalized.get(CONF_CAMERA_IP)
        if camera_ip is None:
            return normalized

        stripped_camera_ip = camera_ip.strip()
        if stripped_camera_ip:
            normalized[CONF_CAMERA_IP] = stripped_camera_ip
        else:
            normalized.pop(CONF_CAMERA_IP, None)
        return normalized
