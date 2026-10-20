"""Optional, persisted Home Assistant manual brightness and fan-speed choices."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Literal, TypeAlias

from homeassistant.helpers.storage import Store

from .const import DOMAIN, LIGHT_MIN_BRIGHTNESS

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)
MANUAL_CHANNELS = ("light", "cFan", "dFan")
ManualSelection: TypeAlias = int | Literal["natural_wind"]


def _valid_level(channel: str, level: object) -> bool:
    if type(level) is not int:
        return False
    if channel == "light":
        return LIGHT_MIN_BRIGHTNESS <= level <= 100
    return channel in ("cFan", "dFan") and 10 <= level <= 100 and level % 10 == 0


def _valid_selection(channel: str, selection: object) -> bool:
    """Check a normalized setting independent of controller raw values."""
    return _valid_level(channel, selection) or (channel == "cFan" and selection == "natural_wind")


def _encode_selection(selection: ManualSelection) -> dict[str, object]:
    if selection == "natural_wind":
        return {"kind": "preset", "value": "natural_wind"}
    return {"kind": "level", "value": selection}


def _decode_selection(channel: str, value: object) -> ManualSelection | None:
    if not isinstance(value, dict) or set(value) != {"kind", "value"}:
        return None
    raw_level = value["value"]
    if value["kind"] == "level" and type(raw_level) is int and _valid_level(channel, raw_level):
        return raw_level
    if value["kind"] == "preset" and channel == "cFan" and raw_level == "natural_wind":
        return "natural_wind"
    return None


class _ManualSettingsStore(Store[dict[str, object]]):
    """Migrate numeric-only storage into a validated tagged selection schema."""

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: object,
    ) -> dict[str, object]:
        _ = old_minor_version
        if old_major_version != 1:
            raise ValueError("Unsupported remembered manual setting storage version")
        values = old_data.get("levels") if isinstance(old_data, dict) else None
        settings: dict[str, dict[str, dict[str, object]]] = {}
        if isinstance(values, dict):
            for device_id, channels in values.items():
                if not isinstance(device_id, str) or not device_id or not isinstance(channels, dict):
                    continue
                valid = {
                    channel: _encode_selection(level)
                    for channel, level in channels.items()
                    if isinstance(channel, str) and _valid_level(channel, level)
                }
                if valid:
                    settings[device_id] = valid
        return {"settings": settings}


class ManualPreferences:
    """Keep deliberate HA selections separate from reported physical device state."""

    def __init__(self, hass: HomeAssistant, entry_id: str, enabled: bool) -> None:
        self._store = _ManualSettingsStore(hass, 2, f"{DOMAIN}_manual_settings_{entry_id}")
        self._enabled = enabled is True
        self._settings: dict[str, dict[str, ManualSelection]] = {}
        self._save_lock = asyncio.Lock()

    def enabled(self, device_id: str, channel: str) -> bool:
        """One explicit opt-in applies to all supported controller channels."""
        return self._enabled and bool(device_id) and channel in MANUAL_CHANNELS

    def selection(self, device_id: str, channel: str) -> ManualSelection | None:
        """Return a stored choice without triggering a device command."""
        return self._settings.get(device_id, {}).get(channel)

    async def async_load(self) -> None:
        """Load and validate stored preferences without replaying commands."""
        try:
            saved = await self._store.async_load()
        except Exception:
            _LOGGER.warning("Cannot load remembered manual settings", exc_info=True)
            return
        values = saved.get("settings") if isinstance(saved, dict) else None
        if not isinstance(values, dict):
            return
        for device_id, channels in values.items():
            if not isinstance(device_id, str) or not device_id or not isinstance(channels, dict):
                continue
            valid: dict[str, ManualSelection] = {}
            for channel, encoded in channels.items():
                if isinstance(channel, str) and (choice := _decode_selection(channel, encoded)) is not None:
                    valid[channel] = choice
            if valid:
                self._settings[device_id] = valid

    async def async_remember(self, device_id: str, channel: str, selection: ManualSelection) -> None:
        """Persist cloud-accepted HA intent, without affecting successful device commands on IO failure."""
        if not device_id or not _valid_selection(channel, selection):
            return
        async with self._save_lock:
            self._settings.setdefault(device_id, {})[channel] = selection
            try:
                await self._store.async_save({"settings": {
                    device: {key: _encode_selection(value) for key, value in settings.items()}
                    for device, settings in self._settings.items()
                }})
            except Exception:
                _LOGGER.warning("Cannot persist remembered manual settings", exc_info=True)
