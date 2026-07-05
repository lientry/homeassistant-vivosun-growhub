"""Switch entities for Vivosun device controls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import VivosunCoordinator
from .entity_helpers import build_device_info, shadow_slice

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceInfo
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .models import RuntimeData


@dataclass(frozen=True, kw_only=True)
class VivosunSwitchDescription:
    """Description for a Vivosun switch entity."""

    key: str
    name: str
    icon: str


_CURING_BOX_SWITCHES: tuple[VivosunSwitchDescription, ...] = (
    VivosunSwitchDescription(key="ctlGlass", name="Privacy Glass", icon="mdi:blinds"),
    VivosunSwitchDescription(key="ctlLight", name="Interior Light", icon="mdi:lightbulb-on"),
    VivosunSwitchDescription(key="ctlLock", name="Door Lock", icon="mdi:lock"),
)


def _runtime(hass: HomeAssistant, entry: ConfigEntry) -> RuntimeData:
    return cast("RuntimeData", hass.data[DOMAIN][entry.entry_id])


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Vivosun switch entities from a config entry."""
    coordinator = _runtime(hass, entry).coordinator
    if coordinator is None:
        return

    entities: list[SwitchEntity] = []
    for device in coordinator.devices:
        if device.device_type != "curing_box":
            continue
        for description in _CURING_BOX_SWITCHES:
            entities.append(VivosunControlSwitch(coordinator, device.device_id, description))
    async_add_entities(entities)


class VivosunControlSwitch(CoordinatorEntity[VivosunCoordinator], SwitchEntity):  # type: ignore[misc]
    """Switch for a top-level VIVOSUN desired control key."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: VivosunCoordinator,
        device_id: str,
        description: VivosunSwitchDescription,
    ) -> None:
        """Initialize the control switch."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._description = description
        self._attr_name = description.name
        self._attr_icon = description.icon
        self._attr_unique_id = f"vivosun_growhub_{device_id}_{description.key}"

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(self.coordinator, self._device_id)

    @property
    def is_on(self) -> bool | None:
        reported = shadow_slice(self.coordinator, self._device_id, "reported_supported")
        value = reported.get(self._description.key)
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return bool(value)
        if isinstance(value, str) and value in {"0", "1"}:
            return value == "1"
        return None

    async def _publish(self, on: bool) -> None:
        await self.coordinator.async_publish_shadow_update(
            {"state": {"desired": {self._description.key: int(on)}}},
            device_id=self._device_id,
            qos=1,
        )

    async def async_turn_on(self, **kwargs: object) -> None:
        """Turn the control on."""
        await self._publish(True)

    async def async_turn_off(self, **kwargs: object) -> None:
        """Turn the control off."""
        await self._publish(False)
