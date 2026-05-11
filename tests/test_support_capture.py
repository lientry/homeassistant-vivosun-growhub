"""Tests for support capture utilities and services."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vivosun_growhub import async_setup, async_setup_entry, async_unload_entry
from custom_components.vivosun_growhub.const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    DOMAIN,
    OPTION_SUPPORT_CAPTURE_ENABLED,
    PLATFORMS,
    SERVICE_START_SUPPORT_CAPTURE,
    SERVICE_STOP_SUPPORT_CAPTURE,
    SUPPORT_CAPTURE_DEFAULT_MAX_EVENTS,
)
from custom_components.vivosun_growhub.support_capture import SupportCaptureManager

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest import MonkeyPatch


class _CoordinatorStub:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.started = False
        self.stopped = False
        self.started_capture_max_events: list[int] = []
        self.stop_capture_calls = 0

    async def async_start(self) -> None:
        self.started = True

    async def async_shutdown(self) -> None:
        self.stopped = True

    async def async_start_support_capture(self, *, max_events: int) -> None:
        self.started_capture_max_events.append(max_events)

    async def async_stop_support_capture(self) -> None:
        self.stop_capture_calls += 1


def test_support_capture_manager_records_bounded_events() -> None:
    manager = SupportCaptureManager()
    manager.start(
        max_events=2,
        devices=[{"device_id": "dev-1"}],
        subscription_topics=["$aws/things/dev-1/shadow/get/rejected"],
    )
    manager.record("one", data={"value": 1})
    manager.record("two", data={"value": 2})
    manager.record("three", data={"value": 3})
    manager.stop()

    snapshot = manager.snapshot()

    assert snapshot["active"] is False
    assert snapshot["max_events"] == 2
    assert snapshot["dropped_events"] == 1
    assert snapshot["subscription_results"] == []
    assert snapshot["model_metadata_results"] == []
    events = cast("list[dict[str, object]]", snapshot["events"])
    assert [event["kind"] for event in events] == ["two", "three"]


async def test_support_capture_services_start_and_stop_single_entry(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr("custom_components.vivosun_growhub.VivosunCoordinator", _CoordinatorStub)
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

    runtime = hass.data[DOMAIN][entry.entry_id]
    coordinator = cast("_CoordinatorStub", runtime.coordinator)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_START_SUPPORT_CAPTURE,
        {"max_events": 123},
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN,
        SERVICE_STOP_SUPPORT_CAPTURE,
        {},
        blocking=True,
    )

    assert coordinator.started_capture_max_events == [123]
    assert coordinator.stop_capture_calls == 1
    assert forward_mock.await_args.args == (entry, PLATFORMS)

    assert await async_unload_entry(hass, entry)
    assert hass.services.has_service(DOMAIN, SERVICE_START_SUPPORT_CAPTURE) is False
    assert hass.services.has_service(DOMAIN, SERVICE_STOP_SUPPORT_CAPTURE) is False


async def test_setup_entry_registers_support_capture_services_without_async_setup(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr("custom_components.vivosun_growhub.VivosunCoordinator", _CoordinatorStub)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True))

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="test@example.com",
        data={CONF_EMAIL: "test@example.com", CONF_PASSWORD: "secret"},
    )

    assert await async_setup_entry(hass, entry)
    assert hass.services.has_service(DOMAIN, SERVICE_START_SUPPORT_CAPTURE) is True
    assert hass.services.has_service(DOMAIN, SERVICE_STOP_SUPPORT_CAPTURE) is True


async def test_setup_entry_starts_support_capture_when_option_enabled(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr("custom_components.vivosun_growhub.VivosunCoordinator", _CoordinatorStub)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True))

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="test@example.com",
        data={CONF_EMAIL: "test@example.com", CONF_PASSWORD: "secret"},
        options={OPTION_SUPPORT_CAPTURE_ENABLED: True},
    )

    assert await async_setup_entry(hass, entry)

    runtime = hass.data[DOMAIN][entry.entry_id]
    coordinator = cast("_CoordinatorStub", runtime.coordinator)
    assert coordinator.started_capture_max_events == [SUPPORT_CAPTURE_DEFAULT_MAX_EVENTS]
