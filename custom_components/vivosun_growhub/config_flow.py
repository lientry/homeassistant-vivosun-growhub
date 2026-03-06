"""Config flow for the Vivosun GrowHub integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import VivosunApiClient
from .const import CONF_EMAIL, CONF_PASSWORD, DEFAULT_TEMP_UNIT, DOMAIN
from .exceptions import VivosunAuthError, VivosunConnectionError, VivosunResponseError

OPTIONS_TEMP_UNIT = "temp_unit"

if TYPE_CHECKING:
    from homeassistant.data_entry_flow import FlowResult


class VivosunGrowhubConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):  # type: ignore[misc,call-arg]
    """Handle a config flow for Vivosun GrowHub."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, str] | None = None) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                unique_id = await self._async_validate_input(user_input)
            except VivosunAuthError:
                errors["base"] = "invalid_auth"
            except VivosunConnectionError:
                errors["base"] = "cannot_connect"
            except (VivosunResponseError, Exception):
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input[CONF_EMAIL],
                    data={
                        CONF_EMAIL: user_input[CONF_EMAIL],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_EMAIL): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def _async_validate_input(self, user_input: dict[str, str]) -> str:
        """Validate credentials and return account user id."""
        api = VivosunApiClient(async_get_clientsession(self.hass))
        tokens = await api.login(user_input[CONF_EMAIL], user_input[CONF_PASSWORD])
        return tokens.user_id

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
        if user_input is not None:
            if user_input != entry.options:
                self.hass.async_create_task(self.hass.config_entries.async_reload(entry.entry_id))
            return self.async_create_entry(title="", data=user_input)

        schema = vol.Schema(
            {
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
        )
        return self.async_show_form(step_id="init", data_schema=schema)
