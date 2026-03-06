"""Tests for Vivosun config flow credential validation."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vivosun_growhub.const import DOMAIN
from custom_components.vivosun_growhub.exceptions import (
    VivosunAuthError,
    VivosunConnectionError,
    VivosunResponseError,
)
from custom_components.vivosun_growhub.models import AuthTokens


def _tokens(*, user_id: str = "user-1") -> AuthTokens:
    return AuthTokens(
        access_token="access-token",
        login_token="login-token",
        refresh_token="refresh-token",
        user_id=user_id,
    )


async def test_config_flow_user_success_creates_entry(hass: object, enable_custom_integrations: None) -> None:
    with patch("custom_components.vivosun_growhub.config_flow.VivosunApiClient.login", new_callable=AsyncMock) as login:
        login.return_value = _tokens(user_id="account-123")

        init_result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        assert init_result["type"] is FlowResultType.FORM
        assert init_result["step_id"] == "user"

        result = await hass.config_entries.flow.async_configure(
            init_result["flow_id"],
            user_input={"email": "user@example.com", "password": "secret"},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "user@example.com"
    assert result["data"] == {"email": "user@example.com", "password": "secret"}


async def test_config_flow_user_invalid_auth_maps_error(hass: object, enable_custom_integrations: None) -> None:
    with patch("custom_components.vivosun_growhub.config_flow.VivosunApiClient.login", new_callable=AsyncMock) as login:
        login.side_effect = VivosunAuthError("bad credentials")

        init_result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(
            init_result["flow_id"],
            user_input={"email": "user@example.com", "password": "bad"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_config_flow_user_cannot_connect_maps_error(hass: object, enable_custom_integrations: None) -> None:
    with patch("custom_components.vivosun_growhub.config_flow.VivosunApiClient.login", new_callable=AsyncMock) as login:
        login.side_effect = VivosunConnectionError("offline")

        init_result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(
            init_result["flow_id"],
            user_input={"email": "user@example.com", "password": "secret"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_config_flow_user_response_error_maps_unknown(hass: object, enable_custom_integrations: None) -> None:
    with patch("custom_components.vivosun_growhub.config_flow.VivosunApiClient.login", new_callable=AsyncMock) as login:
        login.side_effect = VivosunResponseError("unexpected payload")

        init_result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(
            init_result["flow_id"],
            user_input={"email": "user@example.com", "password": "secret"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "unknown"}


async def test_config_flow_user_unexpected_error_maps_unknown(hass: object, enable_custom_integrations: None) -> None:
    with patch("custom_components.vivosun_growhub.config_flow.VivosunApiClient.login", new_callable=AsyncMock) as login:
        login.side_effect = RuntimeError("boom")

        init_result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(
            init_result["flow_id"],
            user_input={"email": "user@example.com", "password": "secret"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "unknown"}


async def test_config_flow_duplicate_user_id_aborts(hass: object, enable_custom_integrations: None) -> None:
    existing = MockConfigEntry(
        domain=DOMAIN,
        unique_id="account-123",
        data={"email": "existing@example.com", "password": "secret"},
    )
    existing.add_to_hass(hass)

    with patch("custom_components.vivosun_growhub.config_flow.VivosunApiClient.login", new_callable=AsyncMock) as login:
        login.return_value = _tokens(user_id="account-123")
        init_result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(
            init_result["flow_id"],
            user_input={"email": "user@example.com", "password": "secret"},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
