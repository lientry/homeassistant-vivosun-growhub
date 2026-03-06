"""Tests for integration setup/unload entry branches."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import ConfigEntryNotReady
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vivosun_growhub import async_setup_entry, async_unload_entry
from custom_components.vivosun_growhub.const import CONF_EMAIL, CONF_PASSWORD, DOMAIN, PLATFORMS
from custom_components.vivosun_growhub.exceptions import VivosunResponseError
from custom_components.vivosun_growhub.models import RuntimeData

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest import MonkeyPatch


class _CoordinatorStub:
    instances: ClassVar[list[_CoordinatorStub]] = []
    start_exception: ClassVar[Exception | None] = None

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.started = False
        self.stopped = False
        _CoordinatorStub.instances.append(self)

    async def async_start(self) -> None:
        if _CoordinatorStub.start_exception is not None:
            raise _CoordinatorStub.start_exception
        self.started = True

    async def async_shutdown(self) -> None:
        self.stopped = True


def _reset_stub() -> None:
    _CoordinatorStub.instances.clear()
    _CoordinatorStub.start_exception = None


async def test_setup_entry_rejects_missing_credentials(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="bad", data={CONF_EMAIL: "user@example.com"})

    with pytest.raises(ConfigEntryNotReady, match="Missing credentials"):
        await async_setup_entry(hass, entry)


async def test_setup_entry_wraps_growhub_errors_and_shuts_down(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    _reset_stub()
    _CoordinatorStub.start_exception = VivosunResponseError("bootstrap failed")
    monkeypatch.setattr("custom_components.vivosun_growhub.VivosunCoordinator", _CoordinatorStub)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="test",
        data={CONF_EMAIL: "user@example.com", CONF_PASSWORD: "secret"},
    )

    with pytest.raises(ConfigEntryNotReady, match="bootstrap failed"):
        await async_setup_entry(hass, entry)

    assert len(_CoordinatorStub.instances) == 1
    assert _CoordinatorStub.instances[0].stopped is True


async def test_setup_entry_re_raises_unexpected_exception_and_shuts_down(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    _reset_stub()
    _CoordinatorStub.start_exception = RuntimeError("boom")
    monkeypatch.setattr("custom_components.vivosun_growhub.VivosunCoordinator", _CoordinatorStub)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="test",
        data={CONF_EMAIL: "user@example.com", CONF_PASSWORD: "secret"},
    )

    with pytest.raises(RuntimeError, match="boom"):
        await async_setup_entry(hass, entry)

    assert len(_CoordinatorStub.instances) == 1
    assert _CoordinatorStub.instances[0].stopped is True


async def test_unload_entry_returns_false_without_mutating_runtime(
    hass: HomeAssistant,
) -> None:
    coordinator = _CoordinatorStub()
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="test",
        data={CONF_EMAIL: "user@example.com", CONF_PASSWORD: "secret"},
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(
        entry_id=entry.entry_id,
        coordinator=coordinator,
    )
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=False)

    result = await async_unload_entry(hass, entry)

    assert result is False
    assert entry.entry_id in hass.data[DOMAIN]
    assert coordinator.stopped is False


async def test_setup_entry_success_stores_runtime_and_forwards_platforms(
    hass: HomeAssistant,
    monkeypatch: MonkeyPatch,
) -> None:
    _reset_stub()
    monkeypatch.setattr("custom_components.vivosun_growhub.VivosunCoordinator", _CoordinatorStub)
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="test",
        data={CONF_EMAIL: "user@example.com", CONF_PASSWORD: "secret"},
    )

    assert await async_setup_entry(hass, entry) is True
    runtime = hass.data[DOMAIN][entry.entry_id]
    assert isinstance(runtime, RuntimeData)
    assert isinstance(runtime.coordinator, _CoordinatorStub)
    hass.config_entries.async_forward_entry_setups.assert_awaited_once_with(entry, PLATFORMS)
