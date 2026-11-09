"""Tests for Vivosun options flow."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, patch

import pytest
from discovery_stubs import DiscoveryCoordinatorStub
from homeassistant.config_entries import HANDLERS
from homeassistant.const import UnitOfTemperature
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers.translation import async_get_translations
from pytest_homeassistant.common import MockConfigEntry

from custom_components.vivosun_growhub import async_migrate_entry
from custom_components.vivosun_growhub.config_flow import VivosunGrowhubConfigFlow, VivosunGrowhubOptionsFlow
from custom_components.vivosun_growhub.const import (
    CONF_CAMERA_IP,
    CONF_CAMERA_IPS,
    CONF_HAS_CAMERA,
    DOMAIN,
    OPTION_CFAN_OSCILLATION_CONTROLLERS,
    OPTION_REMEMBER_MANUAL_SETTINGS,
    OPTION_SPECTRUM_CONTROLLERS,
    OPTION_SUPPORT_CAPTURE_ENABLED,
)
from custom_components.vivosun_growhub.models import DeviceInfo, RuntimeData
from custom_components.vivosun_growhub.sensor import VivosunChannelSensorEntity, async_setup_entry

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


_DEV_ID = "dev-1"
_OSC_OPTION = "cfan_oscillation_controllers"


@pytest.mark.parametrize("key", [OPTION_SPECTRUM_CONTROLLERS, OPTION_CFAN_OSCILLATION_CONTROLLERS])
@pytest.mark.parametrize(
    ("saved", "expected"),
    [
        ([{}], []),
        ([[]], []),
        ([42], []),
        (["unknown-device"], []),
        ([_DEV_ID, {}], [_DEV_ID]),
        ("not-a-list", []),
    ],
)
async def test_malformed_saved_controller_selections_have_safe_form_defaults(
    hass: HomeAssistant, key: str, saved: object, expected: list[str],
) -> None:
    original_options = {
        key: saved,
        CONF_CAMERA_IPS: {"camera-1": "10.0.15.202"},
        OPTION_REMEMBER_MANUAL_SETTINGS: True,
        OPTION_SUPPORT_CAPTURE_ENABLED: True,
        "future_option": 42,
    }
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=original_options)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(
        entry.entry_id, cast("object", _StubCoordinator())
    )
    flow = VivosunGrowhubOptionsFlow(entry)
    flow.hass = hass
    form = await flow.async_step_init()
    assert form["type"] is FlowResultType.FORM
    setting = next(marker for marker in form["data_schema"].schema if marker.schema == key)
    assert setting.default() == expected
    assert entry.options == original_options


@pytest.mark.parametrize(
    ("key", "error"),
    [
        (OPTION_SPECTRUM_CONTROLLERS, "invalid_spectrum_controller"),
        (OPTION_CFAN_OSCILLATION_CONTROLLERS, "invalid_oscillation_controller"),
    ],
)
@pytest.mark.parametrize("invalid", [[{}], [[]], [42], [None], ["unknown-device"], [_DEV_ID, {}]])
async def test_malformed_submitted_controller_selections_return_form_errors(
    hass: HomeAssistant, key: str, error: str, invalid: list[object],
) -> None:
    original_options = {
        CONF_CAMERA_IPS: {"camera-1": "10.0.15.202"},
        OPTION_REMEMBER_MANUAL_SETTINGS: True,
        OPTION_SUPPORT_CAPTURE_ENABLED: True,
        "future_option": 42,
    }
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=original_options)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(
        entry.entry_id, cast("object", _StubCoordinator())
    )
    flow = VivosunGrowhubOptionsFlow(entry)
    flow.hass = hass
    result = await flow.async_step_init({
        key: invalid,
        OPTION_REMEMBER_MANUAL_SETTINGS: True,
        OPTION_SUPPORT_CAPTURE_ENABLED: True,
    })
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == error
    assert entry.options == original_options


async def test_global_manual_toggle_on_existing_options_screen_preserves_all_other_settings(
    hass: HomeAssistant, enable_custom_integrations: None,
) -> None:
    coordinator = _StubCoordinator()
    coordinator.camera_devices = [DeviceInfo("camera-1", "", "", "GrowCam", True, 1, "camera")]
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HAS_CAMERA: True}, options={
        OPTION_SUPPORT_CAPTURE_ENABLED: True,
        OPTION_SPECTRUM_CONTROLLERS: [_DEV_ID], CONF_CAMERA_IPS: {"camera-1": "10.0.15.202"},
        "unrelated_future_setting": 42,
    })
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(entry.entry_id, cast("object", coordinator))
    with patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)):
        form = await hass.config_entries.options.async_init(entry.entry_id)
        assert OPTION_REMEMBER_MANUAL_SETTINGS in {
            marker.schema for marker in form["data_schema"].schema if hasattr(marker, "schema")
        }
        next_form = await hass.config_entries.options.async_configure(form["flow_id"], user_input={
            OPTION_SUPPORT_CAPTURE_ENABLED: True,
            OPTION_SPECTRUM_CONTROLLERS: [_DEV_ID], OPTION_REMEMBER_MANUAL_SETTINGS: True,
        })
        await hass.config_entries.options.async_configure(
            next_form["flow_id"], user_input={CONF_CAMERA_IP: "10.0.15.202"},
        )
        await hass.async_block_till_done()
        assert entry.options[OPTION_REMEMBER_MANUAL_SETTINGS] is True
        assert entry.options["unrelated_future_setting"] == 42
        assert entry.options[OPTION_SPECTRUM_CONTROLLERS] == [_DEV_ID]
        assert entry.options[OPTION_SUPPORT_CAPTURE_ENABLED] is True
        assert entry.options[CONF_CAMERA_IPS] == {"camera-1": "10.0.15.202"}

        form = await hass.config_entries.options.async_init(entry.entry_id)
        camera = await hass.config_entries.options.async_configure(form["flow_id"], user_input={
            OPTION_SUPPORT_CAPTURE_ENABLED: True,
            OPTION_SPECTRUM_CONTROLLERS: [_DEV_ID], OPTION_REMEMBER_MANUAL_SETTINGS: False,
        })
        await hass.config_entries.options.async_configure(camera["flow_id"], user_input={CONF_CAMERA_IP: ""})
        await hass.async_block_till_done()
    assert OPTION_REMEMBER_MANUAL_SETTINGS not in entry.options
    assert CONF_CAMERA_IPS not in entry.options
    assert entry.options["unrelated_future_setting"] == 42


async def test_manual_options_reject_non_boolean_values(
    hass: HomeAssistant, enable_custom_integrations: None,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    coordinator = _StubCoordinator()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(entry.entry_id, cast("object", coordinator))
    # HA's BooleanSelector may coerce raw values before the flow receives them.
    flow = VivosunGrowhubOptionsFlow(entry)
    flow.hass = hass
    for invalid in (["dev-1:light"], "true", 1, {}):
        result = await flow.async_step_init({
            OPTION_REMEMBER_MANUAL_SETTINGS: invalid,
        })
        assert result["errors"]["base"] == "invalid_manual_setting"


def test_manual_option_uses_named_setting_and_description() -> None:
    base = Path(__file__).resolve().parents[1] / "custom_components/vivosun_growhub"
    for catalog in ("strings.json", "translations/en.json"):
        options = json.loads((base / catalog).read_text())["options"]["step"]["init"]
        assert options["data"][OPTION_REMEMBER_MANUAL_SETTINGS] == "Remember brightness and fan speed"
        assert options["data_description"][OPTION_REMEMBER_MANUAL_SETTINGS] == (
            "Reuse brightness and fan speed selected in Home Assistant "
            "when turning devices back on in manual mode without an active plan."
        )


async def test_ha_backend_loads_options_labels_from_en_translation(
    hass: HomeAssistant, enable_custom_integrations: None,
) -> None:
    """Test the actual core translation loading path used by options forms."""
    entry = MockConfigEntry(domain=DOMAIN, version=1, minor_version=2, data={})
    entry.add_to_hass(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert flow["step_id"] == "init"
    translated = await async_get_translations(hass, "en", "options", {DOMAIN})
    prefix = f"component.{DOMAIN}.options.step.init."
    assert translated[prefix + "data." + OPTION_SPECTRUM_CONTROLLERS] == "Controllers with spectrum presets"
    assert translated[prefix + "data." + _OSC_OPTION] == "Controllers with fan oscillation"
    assert translated[prefix + "data." + OPTION_REMEMBER_MANUAL_SETTINGS] == "Remember brightness and fan speed"
    assert translated[prefix + "data_description." + OPTION_REMEMBER_MANUAL_SETTINGS] == (
        "Reuse brightness and fan speed selected in Home Assistant "
        "when turning devices back on in manual mode without an active plan."
    )
    assert translated[prefix + "data_description." + OPTION_SPECTRUM_CONTROLLERS] == (
        "Select controllers whose connected lights offer spectrum presets in the Vivosun app."
    )
    assert translated[prefix + "data_description." + _OSC_OPTION] == (
        "Select controllers whose connected fans offer oscillation controls in the Vivosun app."
    )


@pytest.mark.parametrize(
    ("previous", "enabled"),
    [
        (["dev-1:light"], True),
        (["dev-1:light", "dev-2:cFan"], True),
        ([], False),
        (["dev-1:pump"], False),
        (["dev-1:light", 12], False),
        ("true", False),
        (1, False),
        ({}, False),
        (False, False),
    ],
)
async def test_one_time_legacy_options_migration(
    hass: HomeAssistant, enable_custom_integrations: None, previous: object, enabled: bool,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, version=1, minor_version=1, data={}, options={
            OPTION_REMEMBER_MANUAL_SETTINGS: previous, "unrelated_future_setting": 42,
            OPTION_SPECTRUM_CONTROLLERS: [_DEV_ID], _OSC_OPTION: ["dev-2"],
            CONF_CAMERA_IPS: {"camera-1": "10.0.15.202"},
        },
    )
    entry.add_to_hass(hass)
    assert await entry.async_migrate(hass)
    assert entry.version == VivosunGrowhubConfigFlow.VERSION == 1
    assert entry.minor_version == VivosunGrowhubConfigFlow.MINOR_VERSION == 2
    assert entry.options.get(OPTION_REMEMBER_MANUAL_SETTINGS, False) is enabled
    assert isinstance(entry.options.get(OPTION_REMEMBER_MANUAL_SETTINGS, False), bool)
    assert entry.options["unrelated_future_setting"] == 42
    assert entry.options[OPTION_SPECTRUM_CONTROLLERS] == [_DEV_ID]
    assert entry.options[_OSC_OPTION] == ["dev-2"]
    assert entry.options[CONF_CAMERA_IPS] == {"camera-1": "10.0.15.202"}
    assert await entry.async_migrate(hass)
    assert entry.minor_version == 2


async def test_new_minor_entry_loads_in_prior_same_major_without_migration_hook(
    hass: HomeAssistant, enable_custom_integrations: None,
) -> None:
    """Run core's downgrade check against a simulated released v1.1 handler."""
    entry = MockConfigEntry(
        domain=DOMAIN, version=1, minor_version=2, data={},
        options={OPTION_REMEMBER_MANUAL_SETTINGS: True},
    )
    entry.add_to_hass(hass)

    class ReleasedConfigFlow:
        VERSION = 1
        MINOR_VERSION = 1

    integration = SimpleNamespace(async_get_component=AsyncMock(return_value=SimpleNamespace()))
    with (
        patch.dict(HANDLERS, {DOMAIN: ReleasedConfigFlow}),
        patch("homeassistant.loader.async_get_integration", AsyncMock(return_value=integration)),
    ):
        assert await entry.async_migrate(hass)
    assert entry.version == 1
    assert entry.minor_version == 2
    assert entry.options[OPTION_REMEMBER_MANUAL_SETTINGS] is True
    integration.async_get_component.assert_awaited_once()


async def test_missing_old_remember_option_migrates_to_disabled_without_touching_other_options(
    hass: HomeAssistant, enable_custom_integrations: None,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, version=1, minor_version=1, data={}, options={
            "future": {"feature": True}, OPTION_SUPPORT_CAPTURE_ENABLED: True,
        },
    )
    entry.add_to_hass(hass)
    assert await entry.async_migrate(hass)
    assert (entry.version, entry.minor_version) == (1, 2)
    assert OPTION_REMEMBER_MANUAL_SETTINGS not in entry.options
    assert entry.options == {"future": {"feature": True}, OPTION_SUPPORT_CAPTURE_ENABLED: True}
    assert await entry.async_migrate(hass)


async def test_migration_preserves_future_minor_and_rejects_future_major(
    hass: HomeAssistant, enable_custom_integrations: None,
) -> None:
    future_minor = MockConfigEntry(
        domain=DOMAIN, version=1, minor_version=3, data={},
        options={OPTION_REMEMBER_MANUAL_SETTINGS: ["dev-1:cFan"], "future": "untouched"},
    )
    future_minor.add_to_hass(hass)
    assert await future_minor.async_migrate(hass)
    assert future_minor.version == 1
    assert future_minor.minor_version == 3
    assert future_minor.options[OPTION_REMEMBER_MANUAL_SETTINGS] == ["dev-1:cFan"]
    assert future_minor.options["future"] == "untouched"
    assert await async_migrate_entry(hass, future_minor)

    future_major = MockConfigEntry(domain=DOMAIN, version=2, minor_version=1, data={})
    future_major.add_to_hass(hass)
    assert not await future_major.async_migrate(hass)
    assert not await async_migrate_entry(hass, future_major)


async def test_options_memory_toggle_is_single_boolean_even_without_controller(
    hass: HomeAssistant, enable_custom_integrations: None,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, version=1, minor_version=2, data={})
    entry.add_to_hass(hass)
    form = await hass.config_entries.options.async_init(entry.entry_id)
    markers = form["data_schema"].schema
    options = {marker.schema: selector for marker, selector in markers.items()}
    from homeassistant.helpers.selector import BooleanSelector

    assert isinstance(options[OPTION_REMEMBER_MANUAL_SETTINGS], BooleanSelector)
    assert OPTION_SPECTRUM_CONTROLLERS not in options
    assert _OSC_OPTION not in options


async def test_global_memory_toggle_preserves_form_default_when_field_omitted(
    hass: HomeAssistant, enable_custom_integrations: None,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, version=1, minor_version=2, data={}, options={
        OPTION_REMEMBER_MANUAL_SETTINGS: True, "future": "preserve",
    })
    entry.add_to_hass(hass)
    with patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)):
        flow = await hass.config_entries.options.async_init(entry.entry_id)
        await hass.config_entries.options.async_configure(flow["flow_id"], user_input={})
        await hass.async_block_till_done()
    # Core applies the form field's default=True before calling the flow.
    assert entry.options[OPTION_REMEMBER_MANUAL_SETTINGS] is True
    assert entry.options["future"] == "preserve"


async def test_options_flow_clear_last_camera_ip_preserves_spectrum_and_other_options(
    hass: HomeAssistant, enable_custom_integrations: None,
) -> None:
    coordinator = _StubCoordinator()
    coordinator.camera_devices = [
        DeviceInfo("camera-1", "", "", "GrowCam", True, 1001, "camera")
    ]
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HAS_CAMERA: True}, options={
        OPTION_SUPPORT_CAPTURE_ENABLED: True,
        OPTION_SPECTRUM_CONTROLLERS: [_DEV_ID],
        CONF_CAMERA_IPS: {"camera-1": "10.0.15.202"},
        "unknown_future_option": "keep",
    })
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(entry.entry_id, cast("object", coordinator))

    with patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)) as reload_mock:
        form = await hass.config_entries.options.async_init(entry.entry_id)
        cameras = await hass.config_entries.options.async_configure(form["flow_id"], user_input={
            OPTION_SUPPORT_CAPTURE_ENABLED: True,
            OPTION_SPECTRUM_CONTROLLERS: [_DEV_ID],
        })
        assert cameras["step_id"] == "camera"
        done = await hass.config_entries.options.async_configure(
            cameras["flow_id"], user_input={CONF_CAMERA_IP: ""}
        )
        assert done["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()

    assert entry.options == {
        OPTION_SUPPORT_CAPTURE_ENABLED: True,
        OPTION_SPECTRUM_CONTROLLERS: [_DEV_ID],
        "unknown_future_option": "keep",
    }
    reload_mock.assert_awaited_once_with(entry.entry_id)


async def test_options_flow_clears_one_camera_but_keeps_other_camera_ip(
    hass: HomeAssistant, enable_custom_integrations: None,
) -> None:
    coordinator = _StubCoordinator()
    coordinator.camera_devices = [
        DeviceInfo(f"camera-{i}", "", "", f"GrowCam {i}", True, 1000 + i, "camera")
        for i in (1, 2)
    ]
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HAS_CAMERA: True}, options={
        CONF_CAMERA_IPS: {"camera-1": "10.0.15.202", "camera-2": "10.0.15.203"}
    })
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(entry.entry_id, cast("object", coordinator))
    with patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)):
        form = await hass.config_entries.options.async_init(entry.entry_id)
        first = await hass.config_entries.options.async_configure(
            form["flow_id"], user_input={}
        )
        second = await hass.config_entries.options.async_configure(
            first["flow_id"], user_input={CONF_CAMERA_IP: ""}
        )
        await hass.config_entries.options.async_configure(
            second["flow_id"], user_input={CONF_CAMERA_IP: "10.0.15.203"}
        )
        await hass.async_block_till_done()
    assert entry.options[CONF_CAMERA_IPS] == {"camera-2": "10.0.15.203"}


async def test_options_flow_preserves_saved_camera_ips_when_no_cameras_discovered(
    hass: HomeAssistant, enable_custom_integrations: None,
) -> None:
    coordinator = _StubCoordinator()
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={
        CONF_CAMERA_IPS: {"missing-camera": "10.0.15.202"}, "unknown_future_option": 42
    })
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(entry.entry_id, cast("object", coordinator))
    with patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)):
        form = await hass.config_entries.options.async_init(entry.entry_id)
        await hass.config_entries.options.async_configure(
            form["flow_id"], user_input={OPTION_SPECTRUM_CONTROLLERS: [_DEV_ID]}
        )
        await hass.async_block_till_done()
    assert entry.options[CONF_CAMERA_IPS] == {"missing-camera": "10.0.15.202"}
    assert entry.options["unknown_future_option"] == 42
    assert entry.options[OPTION_SPECTRUM_CONTROLLERS] == [_DEV_ID]


def test_spectrum_option_identifies_controllers_with_app_presets() -> None:
    base = Path(__file__).resolve().parents[1] / "custom_components/vivosun_growhub"
    strings = json.loads((base / "strings.json").read_text())
    translated = json.loads((base / "translations/en.json").read_text())
    for catalog in (strings, translated):
        initial = catalog["options"]["step"]["init"]
        assert initial["data"][OPTION_SPECTRUM_CONTROLLERS] == "Controllers with spectrum presets"
        assert initial["data_description"][OPTION_SPECTRUM_CONTROLLERS] == (
            "Select controllers whose connected lights offer spectrum presets in the Vivosun app."
        )
    assert strings["options"] == translated["options"]


def test_cfan_oscillation_option_explains_app_capability_in_both_translations() -> None:
    base = Path(__file__).resolve().parents[1] / "custom_components/vivosun_growhub"
    for catalog in ("strings.json", "translations/en.json"):
        options = json.loads((base / catalog).read_text())["options"]["step"]["init"]
        assert options["data"][_OSC_OPTION] == "Controllers with fan oscillation"
        assert options["data_description"][_OSC_OPTION] == (
            "Select controllers whose connected fans offer oscillation controls in the Vivosun app."
        )


async def test_oscillation_option_per_controller_default_off_and_removable_with_camera(
    hass: HomeAssistant, enable_custom_integrations: None,
) -> None:
    coordinator = _StubCoordinator()
    coordinator.camera_devices = [DeviceInfo("camera-1", "", "", "GrowCam", True, 1, "camera")]
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HAS_CAMERA: True}, options={
        "unrelated_future_setting": 42,
        OPTION_SUPPORT_CAPTURE_ENABLED: True, OPTION_SPECTRUM_CONTROLLERS: [_DEV_ID],
        OPTION_REMEMBER_MANUAL_SETTINGS: True,
        CONF_CAMERA_IPS: {"camera-1": "10.0.15.202"},
    })
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(entry.entry_id, cast("object", coordinator))
    with patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)):
        form = await hass.config_entries.options.async_init(entry.entry_id)
        assert _OSC_OPTION in {marker.schema for marker in form["data_schema"].schema}
        assert _OSC_OPTION not in entry.options
        with pytest.raises(InvalidData):
            await hass.config_entries.options.async_configure(form["flow_id"], user_input={
                _OSC_OPTION: ["camera-1"],
            })
        camera = await hass.config_entries.options.async_configure(form["flow_id"], user_input={
            _OSC_OPTION: [_DEV_ID],
            OPTION_SUPPORT_CAPTURE_ENABLED: True, OPTION_SPECTRUM_CONTROLLERS: [_DEV_ID],
            OPTION_REMEMBER_MANUAL_SETTINGS: True,
        })
        assert camera["type"] is FlowResultType.FORM
        await hass.config_entries.options.async_configure(
            camera["flow_id"], user_input={CONF_CAMERA_IP: "10.0.15.202"},
        )
        await hass.async_block_till_done()
        assert entry.options[_OSC_OPTION] == [_DEV_ID]
        assert entry.options["unrelated_future_setting"] == 42
        assert entry.options[CONF_CAMERA_IPS] == {"camera-1": "10.0.15.202"}

        form = await hass.config_entries.options.async_init(entry.entry_id)
        camera = await hass.config_entries.options.async_configure(form["flow_id"], user_input={
            _OSC_OPTION: [],
            OPTION_SUPPORT_CAPTURE_ENABLED: True, OPTION_SPECTRUM_CONTROLLERS: [_DEV_ID],
            OPTION_REMEMBER_MANUAL_SETTINGS: True,
        })
        await hass.config_entries.options.async_configure(
            camera["flow_id"], user_input={CONF_CAMERA_IP: "10.0.15.202"},
        )
        await hass.async_block_till_done()
    assert _OSC_OPTION not in entry.options
    assert entry.options[OPTION_SPECTRUM_CONTROLLERS] == [_DEV_ID]
    assert entry.options[OPTION_REMEMBER_MANUAL_SETTINGS] is True
    assert entry.options["unrelated_future_setting"] == 42


async def test_spectrum_opt_in_is_per_controller_and_removable(
    hass: HomeAssistant, enable_custom_integrations: None,
) -> None:
    coordinator = _StubCoordinator()
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={"unrelated_setting": "keep"})
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(entry.entry_id, cast("object", coordinator))
    with patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)):
        flow = await hass.config_entries.options.async_init(entry.entry_id)
        assert flow["type"] is FlowResultType.FORM
        with pytest.raises(InvalidData):
            await hass.config_entries.options.async_configure(
                flow["flow_id"],
                user_input={OPTION_SPECTRUM_CONTROLLERS: ["unknown-device"]},
            )
        saved = await hass.config_entries.options.async_configure(
            flow["flow_id"], user_input={OPTION_SPECTRUM_CONTROLLERS: [_DEV_ID]}
        )
        assert saved["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
        assert entry.options[OPTION_SPECTRUM_CONTROLLERS] == [_DEV_ID]
        assert entry.options["unrelated_setting"] == "keep"
        flow = await hass.config_entries.options.async_init(entry.entry_id)
        await hass.config_entries.options.async_configure(
            flow["flow_id"], user_input={OPTION_SPECTRUM_CONTROLLERS: []}
        )
        await hass.async_block_till_done()
    assert OPTION_SPECTRUM_CONTROLLERS not in entry.options
    assert entry.options["unrelated_setting"] == "keep"


class _StubCoordinator(DiscoveryCoordinatorStub):
    def __init__(self) -> None:
        self._device = DeviceInfo(
            device_id=_DEV_ID,
            client_id="vivosun-VSCTLE42A-acc-dev-1",
            topic_prefix="prefix",
            name="GrowHub",
            online=True,
            scene_id=66078,
            device_type="controller",
        )
        self.data: dict[str, object] = {"sensors": {_DEV_ID: {"inTemp": 2500}}}
        self.is_mqtt_connected = True

    @property
    def device(self) -> DeviceInfo:
        return self._device

    @property
    def devices(self) -> list[DeviceInfo]:
        return [self._device]

    def get_device(self, device_id: str) -> DeviceInfo | None:
        if device_id == self._device.device_id:
            return self._device
        return None


async def test_options_flow_no_longer_exposes_stale_temp_unit(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    coordinator = _StubCoordinator()
    entry = MockConfigEntry(domain=DOMAIN, title="t", data={}, options={"temp_unit": "fahrenheit"})
    entry.add_to_hass(hass)
    runtime = RuntimeData(entry_id=entry.entry_id, coordinator=cast("object", coordinator))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    added: list[VivosunChannelSensorEntity] = []

    def _add(entities: list[VivosunChannelSensorEntity]) -> None:
        added.extend(entities)

    await async_setup_entry(hass, entry, _add)
    in_temp = next(entity for entity in added if entity.unique_id.endswith("_inTemp"))
    assert in_temp.native_unit_of_measurement == UnitOfTemperature.CELSIUS
    assert in_temp.native_value == 25.0

    with patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)) as reload_mock:
        init_result = await hass.config_entries.options.async_init(entry.entry_id)
        assert init_result["type"] is FlowResultType.FORM
        assert init_result["step_id"] == "init"
        assert "temp_unit" not in {marker.schema for marker in init_result["data_schema"].schema}

        finish_result = await hass.config_entries.options.async_configure(
            init_result["flow_id"],
            user_input={},
        )
        assert finish_result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()

    assert entry.options == {"temp_unit": "fahrenheit"}
    reload_mock.assert_not_awaited()
    assert in_temp.native_unit_of_measurement == UnitOfTemperature.CELSIUS
    assert in_temp.native_value == 25.0


async def test_options_flow_enables_support_capture_and_triggers_reload(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    coordinator = _StubCoordinator()
    entry = MockConfigEntry(domain=DOMAIN, title="t", data={}, options={})
    entry.add_to_hass(hass)
    runtime = RuntimeData(entry_id=entry.entry_id, coordinator=cast("object", coordinator))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    with patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)) as reload_mock:
        init_result = await hass.config_entries.options.async_init(entry.entry_id)
        finish_result = await hass.config_entries.options.async_configure(
            init_result["flow_id"],
            user_input={OPTION_SUPPORT_CAPTURE_ENABLED: True},
        )
        assert finish_result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()

    assert entry.options == {OPTION_SUPPORT_CAPTURE_ENABLED: True}
    reload_mock.assert_awaited_once_with(entry.entry_id)


async def test_options_flow_same_values_do_not_trigger_reload(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    coordinator = _StubCoordinator()
    entry = MockConfigEntry(domain=DOMAIN, title="t", data={}, options={})
    entry.add_to_hass(hass)
    runtime = RuntimeData(entry_id=entry.entry_id, coordinator=cast("object", coordinator))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    with patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)) as reload_mock:
        init_result = await hass.config_entries.options.async_init(entry.entry_id)
        finish_result = await hass.config_entries.options.async_configure(
            init_result["flow_id"],
            user_input={},
        )
        assert finish_result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()

    reload_mock.assert_not_awaited()


async def test_options_flow_entry_accessor_falls_back_to_base_config_entry() -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="t", data={})
    flow = VivosunGrowhubOptionsFlow(config_entry=entry)

    assert flow._entry() is entry


async def test_options_flow_updates_camera_ip_when_camera_supported(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    coordinator = _StubCoordinator()
    coordinator.camera_devices = [
        DeviceInfo(
            device_id="camera-1",
            client_id="",
            topic_prefix="",
            name="GrowCam C4",
            online=True,
            scene_id=1001,
            device_type="camera",
        )
    ]
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="t",
        data={CONF_HAS_CAMERA: True},
        options={},
    )
    entry.add_to_hass(hass)
    runtime = RuntimeData(entry_id=entry.entry_id, coordinator=cast("object", coordinator))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    with patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)) as reload_mock:
        init_result = await hass.config_entries.options.async_init(entry.entry_id)
        finish_result = await hass.config_entries.options.async_configure(
            init_result["flow_id"],
            user_input={},
        )
        assert finish_result["type"] is FlowResultType.FORM
        finish_result = await hass.config_entries.options.async_configure(
            finish_result["flow_id"],
            user_input={CONF_CAMERA_IP: "10.0.15.202"},
        )
        assert finish_result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()

    assert entry.options == {
        CONF_CAMERA_IPS: {"camera-1": "10.0.15.202"},
    }
    reload_mock.assert_awaited_once_with(entry.entry_id)


async def test_options_flow_rejects_invalid_camera_ip(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    coordinator = _StubCoordinator()
    coordinator.camera_devices = [
        DeviceInfo(
            device_id="camera-1",
            client_id="",
            topic_prefix="",
            name="GrowCam C4",
            online=True,
            scene_id=1001,
            device_type="camera",
        )
    ]
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="t",
        data={CONF_HAS_CAMERA: True},
        options={},
    )
    entry.add_to_hass(hass)
    runtime = RuntimeData(entry_id=entry.entry_id, coordinator=cast("object", coordinator))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    with patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)) as reload_mock:
        init_result = await hass.config_entries.options.async_init(entry.entry_id)
        finish_result = await hass.config_entries.options.async_configure(
            init_result["flow_id"],
            user_input={},
        )
        finish_result = await hass.config_entries.options.async_configure(
            finish_result["flow_id"],
            user_input={CONF_CAMERA_IP: "not-an-ip"},
        )
        assert finish_result["type"] is FlowResultType.FORM
        assert finish_result["step_id"] == "camera"
        assert finish_result["errors"] == {CONF_CAMERA_IP: "invalid_ip"}
        await hass.async_block_till_done()

    assert entry.options == {}
    reload_mock.assert_not_awaited()


async def test_options_flow_blank_camera_ip_is_treated_as_noop(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    coordinator = _StubCoordinator()
    coordinator.camera_devices = [
        DeviceInfo(
            device_id="camera-1",
            client_id="",
            topic_prefix="",
            name="GrowCam C4",
            online=True,
            scene_id=1001,
            device_type="camera",
        )
    ]
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="t",
        data={CONF_HAS_CAMERA: True},
        options={},
    )
    entry.add_to_hass(hass)
    runtime = RuntimeData(entry_id=entry.entry_id, coordinator=cast("object", coordinator))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    with patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)) as reload_mock:
        init_result = await hass.config_entries.options.async_init(entry.entry_id)
        finish_result = await hass.config_entries.options.async_configure(
            init_result["flow_id"],
            user_input={},
        )
        finish_result = await hass.config_entries.options.async_configure(
            finish_result["flow_id"],
            user_input={CONF_CAMERA_IP: "  "},
        )
        assert finish_result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()

    assert entry.options == {}
    reload_mock.assert_not_awaited()


async def test_options_flow_converts_legacy_camera_ip_and_configures_second_camera(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    coordinator = _StubCoordinator()
    coordinator.camera_devices = [
        DeviceInfo(
            device_id=f"camera-{index}",
            client_id="",
            topic_prefix="",
            name=f"GrowCam Tent {index}",
            online=True,
            scene_id=1000 + index,
            device_type="camera",
        )
        for index in (1, 2)
    ]
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="t",
        data={CONF_HAS_CAMERA: True},
        options={CONF_CAMERA_IP: "10.0.15.202"},
    )
    entry.add_to_hass(hass)
    runtime = RuntimeData(entry_id=entry.entry_id, coordinator=cast("object", coordinator))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    with patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)) as reload_mock:
        init_result = await hass.config_entries.options.async_init(entry.entry_id)
        first_camera = await hass.config_entries.options.async_configure(
            init_result["flow_id"],
            user_input={},
        )
        second_camera = await hass.config_entries.options.async_configure(
            first_camera["flow_id"],
            user_input={CONF_CAMERA_IP: "10.0.15.202"},
        )
        result = await hass.config_entries.options.async_configure(
            second_camera["flow_id"],
            user_input={CONF_CAMERA_IP: "10.0.15.203"},
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()

    assert entry.options == {
        CONF_CAMERA_IPS: {
            "camera-1": "10.0.15.202",
            "camera-2": "10.0.15.203",
        },
    }
    reload_mock.assert_awaited_once_with(entry.entry_id)


async def test_options_flow_false_support_capture_is_treated_as_noop(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    coordinator = _StubCoordinator()
    entry = MockConfigEntry(domain=DOMAIN, title="t", data={}, options={})
    entry.add_to_hass(hass)
    runtime = RuntimeData(entry_id=entry.entry_id, coordinator=cast("object", coordinator))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    with patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)) as reload_mock:
        init_result = await hass.config_entries.options.async_init(entry.entry_id)
        finish_result = await hass.config_entries.options.async_configure(
            init_result["flow_id"],
            user_input={OPTION_SUPPORT_CAPTURE_ENABLED: False},
        )
        assert finish_result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()

    assert entry.options == {}
    reload_mock.assert_not_awaited()
