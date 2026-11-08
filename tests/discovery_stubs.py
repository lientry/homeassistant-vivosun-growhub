"""Typed coordinator discovery behavior for single-platform setup tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from custom_components.vivosun_growhub.gardens import GardenInventory
    from custom_components.vivosun_growhub.models import DeviceInfo


class DiscoveryCoordinatorStub:
    """Call a platform with current devices, as the runtime coordinator does."""

    def async_add_discovery_listener(
        self, callback: Callable[[list[DeviceInfo]], None], *, cameras: bool = False,
    ) -> Callable[[], None]:
        callback(self.camera_devices if cameras else self.devices)
        return lambda: None

    def async_add_garden_listener(
        self, callback: Callable[[GardenInventory], None],
    ) -> Callable[[], None]:
        return lambda: None
