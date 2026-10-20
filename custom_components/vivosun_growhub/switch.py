"""Switch entities for Vivosun device controls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from homeassistant.components.switch import SwitchEntity
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import VivosunCoordinator
from .entity_helpers import build_device_info, is_entity_available, shadow_slice
from .models import client_model_token
from .ports import PORTS_BY_MODEL, PortReport, build_manual_port_payload, reported_port
from .vseed import STAGES, command, reported_seed

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
        model = client_model_token(device.client_id)
        if model in PORTS_BY_MODEL:
            entities.extend(
                VivosunPortSwitch(coordinator, device.device_id, port, model) for port in PORTS_BY_MODEL[model]
            )
            if model == "VSPLUGA22":
                continue
        if device.device_type == "vseed":
            entities.append(VivosunSeedIrrigationSwitch(coordinator, device.device_id))
            for phase in (1, 2, 3):
                for key in ("lgtEn", "pumpEn"):
                    entities.append(VivosunSeedStageSwitch(coordinator, device.device_id, phase, key))
        if device.device_type != "curing_box":
            continue
        for description in _CURING_BOX_SWITCHES:
            entities.append(VivosunControlSwitch(coordinator, device.device_id, description))
    async_add_entities(entities)


class VivosunPortSwitch(CoordinatorEntity[VivosunCoordinator], SwitchEntity):  # type: ignore[misc]
    """Physical A22 or VGrow output with reported status and guarded manual writes."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:power-socket"

    def __init__(self, coordinator: VivosunCoordinator, device_id: str, port: str, model: str) -> None:
        super().__init__(coordinator)
        if port not in PORTS_BY_MODEL.get(model, ()):
            raise ValueError("Unsupported model/physical port")
        self._device_id = device_id
        self._port = port
        self._model_name = "GrowHub A22" if model == "VSPLUGA22" else "VGrow"
        self._attr_name = port.upper() if port.startswith("usb") else f"Outlet {port[-1]}"
        self._attr_unique_id = f"vivosun_growhub_{device_id}_{port}"

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(self.coordinator, self._device_id)

    def _report(self) -> PortReport:
        return reported_port(shadow_slice(self.coordinator, self._device_id, "ports").get(self._port))

    @property
    def available(self) -> bool:
        return is_entity_available(self.coordinator, self._device_id) and self._report().is_on is not None

    @property
    def is_on(self) -> bool | None:
        """Return observed output even when a plan or automatic mode controls it."""
        return self._report().is_on

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        report = self._report()
        return {
            "accessory_code": report.accessory_code,
            "mode_code": report.mode,
            "in_plan": report.in_plan,
        }

    async def _publish(self, on: bool) -> None:
        if not is_entity_available(self.coordinator, self._device_id):
            raise HomeAssistantError(f"{self._model_name} {self._attr_name} is unavailable")
        report = self._report()
        if report.is_on is None:
            raise HomeAssistantError(f"{self._model_name} {self._attr_name} has no reported output level")
        if not report.can_write:
            raise ServiceValidationError(
                f"Cannot control {self._attr_name}: manual mode and no active plan must both be reported "
                "(mode=0, inPlan=0)"
            )
        await self.coordinator.async_publish_shadow_update(
            build_manual_port_payload(self._port, on), device_id=self._device_id, qos=1
        )

    async def async_turn_on(self, **kwargs: object) -> None:
        await self._publish(True)

    async def async_turn_off(self, **kwargs: object) -> None:
        await self._publish(False)


class VivosunSeedIrrigationSwitch(CoordinatorEntity[VivosunCoordinator], SwitchEntity):  # type: ignore[misc]
    """Manual Quick Irrigation (10 minutes), with reported-only status."""

    _attr_has_entity_name = True
    _attr_name = "Quick Irrigation"
    _attr_icon = "mdi:watering-can"

    def __init__(self, coordinator: VivosunCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"vivosun_growhub_{device_id}_seed_quick_irrigation"

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(self.coordinator, self._device_id)

    @property
    def available(self) -> bool:
        return is_entity_available(self.coordinator, self._device_id)

    @property
    def is_on(self) -> bool | None:
        quick = reported_seed(self.coordinator, self._device_id).get("quickAct")
        value = quick.get("waterTime") if isinstance(quick, dict) else None
        return value > 0 if type(value) is int else None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {"water_warning_code": reported_seed(self.coordinator, self._device_id).get("lackWater")}

    async def _publish(self, seconds: int) -> None:
        await self.coordinator.async_publish_shadow_update(
            command(("quickAct", "waterTime"), seconds), device_id=self._device_id, qos=1
        )

    async def async_turn_on(self, **kwargs: object) -> None:
        """Request the app's 600-second manual cycle; hardware may refuse."""
        await self._publish(600)

    async def async_turn_off(self, **kwargs: object) -> None:
        """Request app-equivalent stop without retries."""
        await self._publish(0)


class VivosunSeedStageSwitch(CoordinatorEntity[VivosunCoordinator], SwitchEntity):  # type: ignore[misc]
    """Edit a stored stage recipe's light or pump enable flag."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: VivosunCoordinator, device_id: str, phase: int, key: str) -> None:
        super().__init__(coordinator)
        if phase not in (1, 2, 3) or key not in ("lgtEn", "pumpEn"):
            raise ValueError("Unsupported VSeed enable setting")
        self._device_id = device_id
        self._phase = phase
        self._key = key
        function = "Light Enabled" if key == "lgtEn" else "Irrigation Enabled"
        self._attr_name = f"{STAGES[phase]} {function}"
        self._attr_unique_id = f"vivosun_growhub_{device_id}_seed_stage{phase}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(self.coordinator, self._device_id)

    @property
    def available(self) -> bool:
        return is_entity_available(self.coordinator, self._device_id)

    @property
    def is_on(self) -> bool | None:
        stage = reported_seed(self.coordinator, self._device_id).get(f"stage{self._phase}")
        value = stage.get(self._key) if isinstance(stage, dict) else None
        return value == 1 if type(value) is int and value in (0, 1) else None

    async def _publish(self, enabled: bool) -> None:
        await self.coordinator.async_publish_shadow_update(
            command((f"stage{self._phase}", self._key), int(enabled)),
            device_id=self._device_id,
            qos=1,
        )

    async def async_turn_on(self, **kwargs: object) -> None:
        await self._publish(True)

    async def async_turn_off(self, **kwargs: object) -> None:
        await self._publish(False)


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
