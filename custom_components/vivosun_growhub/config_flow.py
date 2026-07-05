"""Config flow for the Vivosun GrowHub integration."""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import VivosunApiClient
from .camera_config import camera_ips_from_options
from .const import (
    CONF_CAMERA_IP,
    CONF_CAMERA_IPS,
    CONF_EMAIL,
    CONF_HAS_CAMERA,
    CONF_PASSWORD,
    DEFAULT_TEMP_UNIT,
    DOMAIN,
    OPTION_SUPPORT_CAPTURE_ENABLED,
)
from .exceptions import VivosunAuthError, VivosunConnectionError, VivosunResponseError
from .models import DeviceInfo

OPTIONS_TEMP_UNIT = "temp_unit"

if TYPE_CHECKING:
    from collections.abc import Mapping


class VivosunGrowhubConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):  # type: ignore[misc,call-arg]
    """Handle a config flow for Vivosun GrowHub."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize config flow state."""
        self._pending_user_input: dict[str, str] | None = None
        self._camera_devices: list[DeviceInfo] = []
        self._camera_index = 0
        self._camera_ips: dict[str, str] = {}

    async def async_step_user(self, user_input: dict[str, str] | None = None) -> config_entries.ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                unique_id, camera_devices = await self._async_validate_input(user_input)
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
                self._camera_devices = camera_devices
                if camera_devices:
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

    async def async_step_camera(self, user_input: dict[str, str] | None = None) -> config_entries.ConfigFlowResult:
        """Optionally collect LAN IPs for discovered GrowCam devices."""
        pending = self._pending_user_input
        if pending is None or not self._camera_devices:
            return await self.async_step_user()

        camera = self._camera_devices[self._camera_index]
        errors: dict[str, str] = {}
        if user_input is not None:
            camera_ip = user_input.get(CONF_CAMERA_IP, "").strip()
            if camera_ip:
                try:
                    ipaddress.ip_address(camera_ip)
                except ValueError:
                    errors[CONF_CAMERA_IP] = "invalid_ip"
            if not errors:
                if camera_ip:
                    self._camera_ips[camera.device_id] = camera_ip
                self._camera_index += 1
                if self._camera_index < len(self._camera_devices):
                    return await self.async_step_camera()
                return self._create_camera_entry(pending)

        schema = vol.Schema({vol.Optional(CONF_CAMERA_IP, default=""): str})
        return self.async_show_form(
            step_id="camera",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "camera_name": camera.name,
                "camera_number": str(self._camera_index + 1),
                "camera_count": str(len(self._camera_devices)),
            },
        )

    def _create_camera_entry(self, pending: dict[str, str]) -> config_entries.ConfigFlowResult:
        """Create a config entry after all discovered cameras were presented."""
        options: dict[str, object] = {}
        if self._camera_ips:
            options[CONF_CAMERA_IPS] = dict(self._camera_ips)
        return self.async_create_entry(
            title=pending[CONF_EMAIL],
            data={
                CONF_EMAIL: pending[CONF_EMAIL],
                CONF_PASSWORD: pending[CONF_PASSWORD],
                CONF_HAS_CAMERA: True,
            },
            options=options,
        )

    async def _async_validate_input(self, user_input: dict[str, str]) -> tuple[str, list[DeviceInfo]]:
        """Validate credentials and return account user id plus discovered cameras."""
        api = VivosunApiClient(async_get_clientsession(self.hass))
        tokens = await api.login(user_input[CONF_EMAIL], user_input[CONF_PASSWORD])
        devices = await api.get_devices(tokens)
        return tokens.user_id, [device for device in devices if device.device_type == "camera"]

    @staticmethod
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return VivosunGrowhubOptionsFlow(config_entry)


class VivosunGrowhubOptionsFlow(config_entries.OptionsFlow):  # type: ignore[misc]
    """Handle options for Vivosun GrowHub."""

    def __init__(self, config_entry: config_entries.ConfigEntry | None = None) -> None:
        """Initialize options flow across Home Assistant versions."""
        self._config_entry = config_entry
        self._pending_options: dict[str, object] | None = None
        self._camera_devices: list[DeviceInfo] = []
        self._camera_index = 0
        self._camera_ips: dict[str, str] = {}

    def _entry(self) -> config_entries.ConfigEntry:
        """Return the config entry attached to this options flow."""
        if self._config_entry is not None:
            return self._config_entry
        return self.config_entry

    async def async_step_init(self, user_input: dict[str, object] | None = None) -> config_entries.ConfigFlowResult:
        """Manage options."""
        entry = self._entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            normalized_input = self._normalize_options(user_input)
            if not errors:
                self._camera_devices = self._get_camera_devices(entry)
                if self._camera_devices:
                    self._pending_options = normalized_input
                    self._camera_ips = camera_ips_from_options(entry.options, self._camera_devices)
                    return await self.async_step_camera()
                self._preserve_camera_options(normalized_input, entry.options)
                return self._finish_options(normalized_input)

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
            ),
            vol.Optional(
                OPTION_SUPPORT_CAPTURE_ENABLED,
                default=bool(entry.options.get(OPTION_SUPPORT_CAPTURE_ENABLED, False)),
            ): selector.BooleanSelector(),
        }
        schema = vol.Schema(schema_fields)
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)

    async def async_step_camera(self, user_input: dict[str, object] | None = None) -> config_entries.ConfigFlowResult:
        """Configure each discovered GrowCam by stable device ID."""
        if self._pending_options is None or not self._camera_devices:
            return await self.async_step_init()

        camera = self._camera_devices[self._camera_index]
        errors: dict[str, str] = {}
        if user_input is not None:
            camera_ip_value = user_input.get(CONF_CAMERA_IP, "")
            camera_ip = camera_ip_value.strip() if isinstance(camera_ip_value, str) else ""
            if camera_ip:
                try:
                    ipaddress.ip_address(camera_ip)
                except ValueError:
                    errors[CONF_CAMERA_IP] = "invalid_ip"
            if not errors:
                if camera_ip:
                    self._camera_ips[camera.device_id] = camera_ip
                else:
                    self._camera_ips.pop(camera.device_id, None)
                self._camera_index += 1
                if self._camera_index < len(self._camera_devices):
                    return await self.async_step_camera()
                options = dict(self._pending_options)
                if self._camera_ips:
                    options[CONF_CAMERA_IPS] = dict(self._camera_ips)
                return self._finish_options(options)

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_CAMERA_IP,
                    default=self._camera_ips.get(camera.device_id, ""),
                ): str
            }
        )
        return self.async_show_form(
            step_id="camera",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "camera_name": camera.name,
                "camera_number": str(self._camera_index + 1),
                "camera_count": str(len(self._camera_devices)),
            },
        )

    def _get_camera_devices(self, entry: config_entries.ConfigEntry) -> list[DeviceInfo]:
        """Return currently discovered camera devices."""
        runtime = self.hass.data.get(DOMAIN, {}).get(entry.entry_id)
        coordinator = getattr(runtime, "coordinator", None)
        camera_devices = getattr(coordinator, "camera_devices", None)
        if not isinstance(camera_devices, list):
            return []
        return [device for device in camera_devices if isinstance(device, DeviceInfo)]

    def _finish_options(self, options: dict[str, object]) -> config_entries.ConfigFlowResult:
        """Save normalized options and reload when they changed."""
        entry = self._entry()
        options.pop(CONF_CAMERA_IP, None)
        if options != entry.options:
            self.hass.async_create_task(self.hass.config_entries.async_reload(entry.entry_id))
        return self.async_create_entry(title="", data=options)

    @staticmethod
    def _preserve_camera_options(
        normalized: dict[str, object],
        existing: Mapping[str, object],
    ) -> None:
        """Preserve camera settings when no cameras are currently discoverable."""
        for key in (CONF_CAMERA_IP, CONF_CAMERA_IPS):
            if key in existing:
                normalized[key] = existing[key]

    def _normalize_options(self, user_input: dict[str, object]) -> dict[str, object]:
        """Strip empty optional values so options remain stable across no-op submits."""
        normalized = dict(user_input)
        support_capture_enabled = normalized.get(OPTION_SUPPORT_CAPTURE_ENABLED)
        if support_capture_enabled is True:
            normalized[OPTION_SUPPORT_CAPTURE_ENABLED] = True
        else:
            normalized.pop(OPTION_SUPPORT_CAPTURE_ENABLED, None)

        normalized.pop(CONF_CAMERA_IP, None)
        normalized.pop(CONF_CAMERA_IPS, None)
        return normalized
