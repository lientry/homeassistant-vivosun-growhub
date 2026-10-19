"""Real MQTT ingress and light projection for locally requested levels."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, cast

import pytest

from custom_components.vivosun_growhub.coordinator import VivosunCoordinator
from custom_components.vivosun_growhub.light import VivosunLightEntity
from custom_components.vivosun_growhub.models import DeviceInfo

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant


class _LocalMqtt:
    def __init__(self) -> None:
        self.is_connected = True
        self.published: list[tuple[str, dict[str, object]]] = []
        self.publish_error: Exception | None = None

    async def publish(self, topic: str, payload: bytes, qos: int = 0) -> None:
        assert qos == 0
        if self.publish_error is not None:
            raise self.publish_error
        self.published.append((topic, json.loads(payload)))

    async def disconnect(self) -> None:
        self.is_connected = False


def _setup(hass: HomeAssistant, count: int = 1) -> tuple[VivosunCoordinator, _LocalMqtt, list[VivosunLightEntity]]:
    coordinator = VivosunCoordinator(hass, object(), email="offline@example.com", password="unused")
    coordinator._devices = [
        DeviceInfo(
            device_id=f"light-{i}",
            client_id=f"thing-{i}",
            topic_prefix=f"topic/{i}",
            name=f"GrowHub {i}",
            online=True,
            scene_id=66078,
            device_type="controller",
        )
        for i in range(count)
    ]
    coordinator._build_topic_maps()
    mqtt = _LocalMqtt()
    coordinator._mqtt_client = cast("object", mqtt)
    coordinator.data = coordinator._build_state_snapshot()
    return coordinator, mqtt, [VivosunLightEntity(coordinator, f"light-{i}") for i in range(count)]


async def _receive(
    coordinator: VivosunCoordinator, thing: str, suffix: str, document: dict[str, object]
) -> None:
    await coordinator._handle_mqtt_publish(
        f"$aws/things/{thing}/shadow/{suffix}", json.dumps(document).encode(), 1
    )


async def _reported(
    coordinator: VivosunCoordinator,
    thing: str,
    level: int,
    version: int,
    *,
    previous_level: int | None = None,
    desired_level: int | None = None,
) -> None:
    reported = {"light": {"lv": level}}
    if previous_level is None:
        await _receive(coordinator, thing, "get/accepted", {"state": {"reported": reported}, "version": version})
    else:
        current_state: dict[str, object] = {"reported": reported}
        if desired_level is not None:
            current_state["desired"] = {"light": {"manu": {"lv": desired_level}}}
        await _receive(
            coordinator,
            thing,
            "update/documents",
            {
                "previous": {"state": {"reported": {"light": {"lv": previous_level}}}, "version": version - 1},
                "current": {"state": current_state, "version": version},
            },
        )


async def _desired_ack(
    coordinator: VivosunCoordinator, thing: str, level: int, version: int, token: str
) -> None:
    await _receive(
        coordinator,
        thing,
        "update/accepted",
        {"state": {"desired": {"light": {"manu": {"lv": level}}}}, "version": version, "clientToken": token},
    )


def _field_metadata(timestamp: int | None) -> dict[str, object]:
    """Minimal AWS metadata shape, with one timestamp for each physical level leaf."""
    if timestamp is None:
        return {}
    return {"reported": {"light": {"lv": {"timestamp": timestamp}, "manu": {"lv": {"timestamp": timestamp}}}}}


async def _reported_with_metadata(
    coordinator: VivosunCoordinator,
    *,
    level: int,
    version: int,
    timestamp: int | None,
    suffix: str = "update/accepted",
    previous_level: int | None = None,
    previous_timestamp: int | None = None,
    thing: str = "thing-0",
) -> None:
    """Sanitized E42A-style reported update with field-level cloud metadata."""
    reported = {"light": {"lv": level, "manu": {"lv": level}}}
    current: dict[str, object] = {
        "state": {"reported": reported},
        "metadata": _field_metadata(timestamp),
        "version": version,
    }
    if suffix != "update/documents":
        await _receive(coordinator, thing, suffix, current)
        return
    assert previous_level is not None
    previous: dict[str, object] = {
        "state": {"reported": {"light": {"lv": previous_level, "manu": {"lv": previous_level}}}},
        "metadata": _field_metadata(previous_timestamp),
        "version": version - 1,
    }
    await _receive(coordinator, thing, suffix, {"previous": previous, "current": current})


async def _desired_ack_with_metadata(
    coordinator: VivosunCoordinator, *, level: int, version: int, timestamp: int, token: str
) -> None:
    await _receive(
        coordinator,
        "thing-0",
        "update/accepted",
        {
            "state": {"desired": {"light": {"mode": 0, "manu": {"lv": level}}}},
            "metadata": {"desired": {"light": {"manu": {"lv": {"timestamp": timestamp}}}}},
            "version": version,
            "clientToken": token,
        },
    )


def _levels(coordinator: VivosunCoordinator, light: VivosunLightEntity) -> tuple[list[int | None], Callable[[], None]]:
    observed: list[int | None] = []

    def _changed() -> None:
        brightness = light.brightness
        level = round(brightness * 100 / 255) if brightness is not None else None
        if not observed or observed[-1] != level:
            observed.append(level)

    remove = coordinator.async_add_listener(_changed)
    return observed, remove


async def test_captured_brightness_same_version_old_report_does_not_bounce(hass: HomeAssistant) -> None:
    coordinator, mqtt, (light,) = _setup(hass)
    await _reported(coordinator, "thing-0", 25, 65392)
    changes, remove = _levels(coordinator, light)
    try:
        await light.async_turn_on(brightness=148)  # 58% requested locally
        assert round(light.brightness * 100 / 255) == 58
        token = cast("str", mqtt.published[-1][1]["clientToken"])
        await _desired_ack(coordinator, "thing-0", 58, 65393, token)
        await _reported(coordinator, "thing-0", 25, 65393, previous_level=25, desired_level=58)
        assert round(light.brightness * 100 / 255) == 58
        await _reported(coordinator, "thing-0", 58, 65394, previous_level=25)
        assert changes == [58]
        assert len(mqtt.published) == 1
    finally:
        remove()
        await coordinator.async_shutdown()


async def test_captured_power_on_same_version_old_off_report_does_not_bounce(hass: HomeAssistant) -> None:
    coordinator, mqtt, (light,) = _setup(hass)
    await _reported(coordinator, "thing-0", 0, 65402)
    changes, remove = _levels(coordinator, light)
    try:
        await light.async_turn_on()
        assert light.is_on and round(light.brightness * 100 / 255) == 25
        token = cast("str", mqtt.published[-1][1]["clientToken"])
        await _desired_ack(coordinator, "thing-0", 25, 65403, token)
        await _reported(coordinator, "thing-0", 0, 65403, previous_level=0, desired_level=25)
        assert light.is_on
        await _reported(coordinator, "thing-0", 25, 65404, previous_level=0)
        assert changes == [25]
        assert mqtt.published[0][1]["state"] == {"desired": {"light": {"mode": 0, "manu": {"lv": 25}}}}
    finally:
        remove()
        await coordinator.async_shutdown()


async def test_actual_capture_shaped_update_accepted_field_metadata_confirms_before_documents(
    hass: HomeAssistant,
) -> None:
    coordinator, mqtt, (light,) = _setup(hass)
    await _reported_with_metadata(coordinator, level=25, version=65392, timestamp=1792364882, suffix="get/accepted")
    changes, remove = _levels(coordinator, light)
    try:
        await light.async_turn_on(brightness=148)
        token = cast("str", mqtt.published[-1][1]["clientToken"])
        await _desired_ack_with_metadata(coordinator, level=58, version=65393, timestamp=1792364888, token=token)
        await _reported_with_metadata(
            coordinator, level=25, version=65393, timestamp=1792364882,
            suffix="update/documents", previous_level=25, previous_timestamp=1792364882,
        )
        assert ("light-0", "light", "level") in coordinator._pending_fields
        assert changes == [58]

        # Actual E42A event 31 has newer reported-field metadata and is enough,
        # even when no following documents message arrives.
        await _reported_with_metadata(coordinator, level=58, version=65394, timestamp=1792364889)
        assert coordinator._pending_fields == {}
        assert coordinator._shadow_states["light-0"]["light"]["level"] == 58
        assert changes == [58]
        assert len(mqtt.published) == 1
    finally:
        remove()
        await coordinator.async_shutdown()


async def test_capture_shaped_off_to_on_confirms_from_reported_update_accepted(hass: HomeAssistant) -> None:
    coordinator, mqtt, (light,) = _setup(hass)
    await _reported_with_metadata(coordinator, level=0, version=65396, timestamp=1792364908, suffix="get/accepted")
    try:
        await light.async_turn_on()
        token = cast("str", mqtt.published[-1][1]["clientToken"])
        await _desired_ack_with_metadata(coordinator, level=25, version=65397, timestamp=1792364911, token=token)
        await _reported_with_metadata(
            coordinator, level=0, version=65397, timestamp=1792364908,
            suffix="update/documents", previous_level=0, previous_timestamp=1792364908,
        )
        assert ("light-0", "light", "level") in coordinator._pending_fields
        await _reported_with_metadata(coordinator, level=25, version=65398, timestamp=1792364912)
        assert coordinator._pending_fields == {}
        assert light.is_on and round(light.brightness * 100 / 255) == 25
        assert len(mqtt.published) == 1
    finally:
        await coordinator.async_shutdown()


async def test_get_snapshot_with_genuinely_new_reported_field_confirms(hass: HomeAssistant) -> None:
    coordinator, mqtt, (light,) = _setup(hass)
    await _reported_with_metadata(coordinator, level=25, version=100, timestamp=1000, suffix="get/accepted")
    try:
        await light.async_turn_on(brightness=148)
        token = cast("str", mqtt.published[-1][1]["clientToken"])
        await _desired_ack_with_metadata(coordinator, level=58, version=101, timestamp=1010, token=token)
        await _reported_with_metadata(coordinator, level=58, version=104, timestamp=1011, suffix="get/accepted")
        assert coordinator._pending_fields == {}
        assert round(light.brightness * 100 / 255) == 58
        assert len(mqtt.published) == 1
    finally:
        await coordinator.async_shutdown()


async def test_get_with_new_field_timestamp_but_pre_ack_shadow_version_cannot_confirm(
    hass: HomeAssistant,
) -> None:
    coordinator, mqtt, (light,) = _setup(hass)
    await _reported_with_metadata(coordinator, level=25, version=100, timestamp=1000, suffix="get/accepted")
    try:
        await light.async_turn_on(brightness=148)
        token = cast("str", mqtt.published[-1][1]["clientToken"])
        await _desired_ack_with_metadata(coordinator, level=58, version=105, timestamp=1010, token=token)
        # A delayed GET response cannot postdate the command if its cloud
        # version is older than the accepted desired version.
        await _reported_with_metadata(coordinator, level=58, version=104, timestamp=1011, suffix="get/accepted")
        assert ("light-0", "light", "level") in coordinator._pending_fields
        assert coordinator._shadow_states["light-0"]["light"]["level"] == 25
        await _reported_with_metadata(coordinator, level=58, version=106, timestamp=1012)
        assert coordinator._pending_fields == {}
    finally:
        await coordinator.async_shutdown()


async def test_higher_shadow_version_with_stale_matching_field_does_not_confirm_or_replace_cache(
    hass: HomeAssistant,
) -> None:
    coordinator, mqtt, (light,) = _setup(hass)
    await _reported_with_metadata(coordinator, level=25, version=100, timestamp=1000, suffix="get/accepted")
    try:
        await light.async_turn_on(brightness=148)
        token = cast("str", mqtt.published[-1][1]["clientToken"])
        await _desired_ack_with_metadata(coordinator, level=58, version=101, timestamp=1010, token=token)
        await _reported_with_metadata(coordinator, level=58, version=105, timestamp=999, suffix="get/accepted")
        assert ("light-0", "light", "level") in coordinator._pending_fields
        assert coordinator._shadow_states["light-0"]["light"]["level"] == 25
        key = ("light-0", "light", "level")
        coordinator._expire_pending_field(key, coordinator._pending_fields[key].generation)
        assert round(light.brightness * 100 / 255) == 25
    finally:
        await coordinator.async_shutdown()


async def test_fresh_external_report_to_baseline_wins_even_if_value_matches_old_level(hass: HomeAssistant) -> None:
    coordinator, mqtt, (light,) = _setup(hass)
    await _reported_with_metadata(coordinator, level=25, version=100, timestamp=1000, suffix="get/accepted")
    changes, remove = _levels(coordinator, light)
    try:
        await light.async_turn_on(brightness=148)
        token = cast("str", mqtt.published[-1][1]["clientToken"])
        await _desired_ack_with_metadata(coordinator, level=58, version=101, timestamp=1010, token=token)
        await _reported_with_metadata(coordinator, level=25, version=102, timestamp=1011)
        assert coordinator._pending_fields == {}
        assert changes == [58, 25]
    finally:
        remove()
        await coordinator.async_shutdown()


async def test_same_value_document_with_new_reported_field_timestamp_cancels_pending(hass: HomeAssistant) -> None:
    coordinator, mqtt, (light,) = _setup(hass)
    await _reported_with_metadata(coordinator, level=25, version=100, timestamp=1000, suffix="get/accepted")
    try:
        await light.async_turn_on(brightness=148)
        token = cast("str", mqtt.published[-1][1]["clientToken"])
        await _desired_ack_with_metadata(coordinator, level=58, version=101, timestamp=1010, token=token)
        # The physical field was re-reported as 25, without numeric change.
        await _reported_with_metadata(
            coordinator, level=25, version=102, timestamp=1011,
            suffix="update/documents", previous_level=25, previous_timestamp=1000,
        )
        assert coordinator._pending_fields == {}
        assert round(light.brightness * 100 / 255) == 25
    finally:
        await coordinator.async_shutdown()


async def test_only_metadata_for_selected_light_level_field_counts_as_fresh(hass: HomeAssistant) -> None:
    coordinator, mqtt, (light,) = _setup(hass)
    await _reported_with_metadata(coordinator, level=25, version=100, timestamp=1000, suffix="get/accepted")
    try:
        await light.async_turn_on(brightness=148)
        token = cast("str", mqtt.published[-1][1]["clientToken"])
        await _desired_ack_with_metadata(coordinator, level=58, version=101, timestamp=1010, token=token)
        # light.lv is what normalization selects. New manu.lv metadata is NOT
        # evidence that the top-level physical lv changed.
        await _receive(coordinator, "thing-0", "get/accepted", {
            "state": {"reported": {"light": {"lv": 58, "manu": {"lv": 58}}}},
            "metadata": {"reported": {"light": {
                "lv": {"timestamp": 1000}, "manu": {"lv": {"timestamp": 1011}},
            }}}, "version": 102,
        })
        assert ("light-0", "light", "level") in coordinator._pending_fields
        assert coordinator._shadow_states["light-0"]["light"]["level"] == 25

        # With an invalid top-level lv, normalization falls back to manu.lv;
        # its timestamp now belongs to the selected brightness field.
        await _receive(coordinator, "thing-0", "get/accepted", {
            "state": {"reported": {"light": {"lv": None, "manu": {"lv": 58}}}},
            "metadata": {"reported": {"light": {
                "lv": {"timestamp": 1000}, "manu": {"lv": {"timestamp": 1011}},
            }}}, "version": 103,
        })
        assert coordinator._pending_fields == {}
        assert round(light.brightness * 100 / 255) == 58
    finally:
        await coordinator.async_shutdown()


async def test_fresh_superseded_target_still_has_command_attribution_ambiguity(hass: HomeAssistant) -> None:
    coordinator, mqtt, (light,) = _setup(hass)
    await _reported_with_metadata(coordinator, level=25, version=100, timestamp=1000, suffix="get/accepted")
    try:
        await light.async_turn_on(brightness=148)  # Earlier local command 58
        await light.async_turn_on(brightness=179)  # Latest local command 70
        token = cast("str", mqtt.published[-1][1]["clientToken"])
        await _desired_ack_with_metadata(coordinator, level=70, version=102, timestamp=1010, token=token)
        await _reported_with_metadata(coordinator, level=58, version=103, timestamp=1011)
        assert ("light-0", "light", "level") in coordinator._pending_fields
        assert round(light.brightness * 100 / 255) == 70
        assert coordinator._shadow_states["light-0"]["light"]["level"] == 58
        await _reported_with_metadata(coordinator, level=70, version=104, timestamp=1012)
        assert coordinator._pending_fields == {}
        assert len(mqtt.published) == 2
    finally:
        await coordinator.async_shutdown()


@pytest.mark.parametrize("outcome", ["unconfirmed", "rejected"])
async def test_latest_rapid_request_failure_returns_to_actual_report_and_warns(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, outcome: str,
) -> None:
    coordinator, mqtt, (light,) = _setup(hass)
    await _reported_with_metadata(coordinator, level=25, version=100, timestamp=1000, suffix="get/accepted")
    changes, remove = _levels(coordinator, light)
    try:
        await light.async_turn_on(brightness=148)  # 58%
        await light.async_turn_on(brightness=179)  # 70%, the latest intent
        token = cast("str", mqtt.published[-1][1]["clientToken"])
        if outcome == "unconfirmed":
            await _desired_ack_with_metadata(coordinator, level=70, version=102, timestamp=1010, token=token)
        await _reported_with_metadata(coordinator, level=58, version=103, timestamp=1011)
        assert round(light.brightness * 100 / 255) == 70
        assert coordinator._shadow_states["light-0"]["light"]["level"] == 58

        if outcome == "unconfirmed":
            pending = coordinator._pending_fields[("light-0", "light", "level")]
            coordinator._expire_pending_field(("light-0", "light", "level"), pending.generation)
            assert "Light command remains unconfirmed" in caplog.text
        else:
            await _receive(coordinator, "thing-0", "update/rejected", {"clientToken": token, "code": 403})
            assert "Cloud rejected light command" in caplog.text
        assert coordinator._pending_fields == {}
        assert round(light.brightness * 100 / 255) == 58
        assert changes == [58, 70, 58]
        assert [payload["state"]["desired"]["light"]["manu"]["lv"] for _, payload in mqtt.published] == [58, 70]

        await _reported_with_metadata(coordinator, level=70, version=104, timestamp=1012)
        assert changes == [58, 70, 58, 70]
        assert len(mqtt.published) == 2  # A late report never causes automatic retries.
    finally:
        remove()
        await coordinator.async_shutdown()


async def test_same_second_without_cloud_ack_keeps_pending_until_more_evidence(hass: HomeAssistant) -> None:
    coordinator, _, (light,) = _setup(hass)
    await _reported_with_metadata(coordinator, level=25, version=100, timestamp=1010, suffix="get/accepted")
    try:
        await light.async_turn_on(brightness=148)
        await _reported_with_metadata(coordinator, level=58, version=101, timestamp=1010)
        assert ("light-0", "light", "level") in coordinator._pending_fields
        assert round(light.brightness * 100 / 255) == 58
    finally:
        await coordinator.async_shutdown()


async def test_missing_metadata_and_documents_without_previous_do_not_certify(hass: HomeAssistant) -> None:
    coordinator, mqtt, (light,) = _setup(hass)
    await _reported(coordinator, "thing-0", 25, 100)
    try:
        await light.async_turn_on(brightness=148)
        token = cast("str", mqtt.published[-1][1]["clientToken"])
        await _desired_ack(coordinator, "thing-0", 58, 101, token)
        await _receive(coordinator, "thing-0", "update/documents", {
            "current": {"state": {"reported": {"light": {"lv": 58}}}, "version": 102},
        })
        assert ("light-0", "light", "level") in coordinator._pending_fields
    finally:
        await coordinator.async_shutdown()


async def test_same_second_get_is_ambiguous_without_field_transition(hass: HomeAssistant) -> None:
    coordinator, mqtt, (light,) = _setup(hass)
    await _reported_with_metadata(coordinator, level=25, version=100, timestamp=1010, suffix="get/accepted")
    try:
        await light.async_turn_on(brightness=148)
        token = cast("str", mqtt.published[-1][1]["clientToken"])
        await _desired_ack_with_metadata(coordinator, level=58, version=101, timestamp=1010, token=token)
        await _reported_with_metadata(coordinator, level=58, version=105, timestamp=1010, suffix="get/accepted")
        assert ("light-0", "light", "level") in coordinator._pending_fields
        assert round(light.brightness * 100 / 255) == 58
    finally:
        await coordinator.async_shutdown()


async def test_same_second_reported_update_after_cloud_ack_version_confirms(hass: HomeAssistant) -> None:
    coordinator, mqtt, (light,) = _setup(hass)
    await _reported_with_metadata(coordinator, level=25, version=100, timestamp=1010, suffix="get/accepted")
    try:
        await light.async_turn_on(brightness=148)
        token = cast("str", mqtt.published[-1][1]["clientToken"])
        await _desired_ack_with_metadata(coordinator, level=58, version=101, timestamp=1010, token=token)
        await _reported_with_metadata(coordinator, level=58, version=102, timestamp=1010)
        assert coordinator._pending_fields == {}
        assert round(light.brightness * 100 / 255) == 58
    finally:
        await coordinator.async_shutdown()


async def test_repeated_already_confirmed_brightness_sends_both_commands_without_false_timeout(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("custom_components.vivosun_growhub.coordinator._FIELD_CONFIRMATION_TIMEOUT", 0.01)
    coordinator, mqtt, (light,) = _setup(hass)
    await _reported_with_metadata(coordinator, level=25, version=100, timestamp=1000, suffix="get/accepted")
    changes, remove = _levels(coordinator, light)
    try:
        await light.async_turn_on(brightness=64)
        await light.async_turn_on(brightness=64)
        await asyncio.sleep(0.025)
        assert coordinator._pending_fields == {}
        assert "remains unconfirmed" not in caplog.text
        assert changes == []
        assert len(mqtt.published) == 2
        assert mqtt.published[0][1]["clientToken"] != mqtt.published[1][1]["clientToken"]
        assert all(p[1]["state"]["desired"]["light"]["manu"]["lv"] == 25 for p in mqtt.published)
        assert coordinator._already_confirmed_tokens == {}  # Silence and bounded memory.
    finally:
        remove()
        await coordinator.async_shutdown()


async def test_already_confirmed_command_rejection_retains_state_and_logs_failure(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture,
) -> None:
    coordinator, mqtt, (light,) = _setup(hass)
    await _reported_with_metadata(coordinator, level=25, version=100, timestamp=1000, suffix="get/accepted")
    try:
        await light.async_turn_on(brightness=64)
        token = cast("str", mqtt.published[-1][1]["clientToken"])
        assert coordinator._pending_fields == {}
        await _receive(coordinator, "thing-0", "update/rejected", {"clientToken": token, "code": 403})
        assert round(light.brightness * 100 / 255) == 25
        assert "Cloud rejected" in caplog.text
        assert coordinator._already_confirmed_tokens == {}
        assert len(mqtt.published) == 1
    finally:
        await coordinator.async_shutdown()


async def test_explicit_off_stays_off_through_desired_ack_and_old_report(hass: HomeAssistant) -> None:
    coordinator, mqtt, (light,) = _setup(hass)
    await _reported(coordinator, "thing-0", 58, 700)
    changes, remove = _levels(coordinator, light)
    try:
        await light.async_turn_off()
        assert light.is_on is False and light.brightness == 0
        token = cast("str", mqtt.published[-1][1]["clientToken"])
        await _desired_ack(coordinator, "thing-0", 0, 701, token)
        await _reported(coordinator, "thing-0", 58, 701, previous_level=58)
        assert light.is_on is False
        await _reported(coordinator, "thing-0", 0, 702, previous_level=58)
        assert changes == [0]
        assert mqtt.published[-1][1]["state"] == {"desired": {"light": {"mode": 0, "manu": {"lv": 0}}}}
    finally:
        remove()
        await coordinator.async_shutdown()


async def test_rejected_token_reverts_but_unrelated_rejection_does_not(hass: HomeAssistant) -> None:
    coordinator, mqtt, (light,) = _setup(hass)
    await _reported(coordinator, "thing-0", 25, 100)
    try:
        await light.async_turn_on(brightness=148)
        assert round(light.brightness * 100 / 255) == 58
        await _receive(coordinator, "thing-0", "update/rejected", {"clientToken": "other", "code": 409})
        assert round(light.brightness * 100 / 255) == 58
        token = mqtt.published[-1][1]["clientToken"]
        await _receive(coordinator, "thing-0", "update/rejected", {"clientToken": token, "code": 403})
        assert round(light.brightness * 100 / 255) == 25
        await _desired_ack(coordinator, "thing-0", 58, 101, cast("str", token))
        assert round(light.brightness * 100 / 255) == 25
    finally:
        await coordinator.async_shutdown()


async def test_publish_failure_reverts_optimism_without_a_shadow_command(hass: HomeAssistant) -> None:
    coordinator, mqtt, (light,) = _setup(hass)
    await _reported(coordinator, "thing-0", 25, 100)
    changes, remove = _levels(coordinator, light)
    try:
        mqtt.publish_error = RuntimeError("transport send failed")
        with pytest.raises(RuntimeError, match="transport send failed"):
            await light.async_turn_on(brightness=148)
        assert changes == [58, 25]
        assert mqtt.published == []
        assert coordinator._pending_fields == {}
    finally:
        remove()
        await coordinator.async_shutdown()


async def test_no_report_expires_as_unconfirmed_and_late_confirmation_is_applied(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("custom_components.vivosun_growhub.coordinator._FIELD_CONFIRMATION_TIMEOUT", 0.01)
    coordinator, mqtt, (light,) = _setup(hass)
    await _reported(coordinator, "thing-0", 25, 100)
    changes, remove = _levels(coordinator, light)
    try:
        await light.async_turn_on(brightness=148)
        assert round(light.brightness * 100 / 255) == 58
        await asyncio.sleep(0.025)
        assert round(light.brightness * 100 / 255) == 25
        assert coordinator._pending_fields == {}
        assert "remains unconfirmed" in caplog.text
        await _reported(coordinator, "thing-0", 58, 102, previous_level=25)
        assert changes == [58, 25, 58]
        assert len(mqtt.published) == 1  # No automatic retry or replay.
    finally:
        remove()
        await coordinator.async_shutdown()


async def test_new_external_report_wins_immediately_and_stale_get_cannot_revert(hass: HomeAssistant) -> None:
    coordinator, mqtt, (light,) = _setup(hass)
    await _reported(coordinator, "thing-0", 25, 100)
    changes, remove = _levels(coordinator, light)
    try:
        await light.async_turn_on(brightness=148)
        await _reported(coordinator, "thing-0", 40, 102, previous_level=25)
        assert round(light.brightness * 100 / 255) == 40
        assert coordinator._pending_fields == {}
        await _desired_ack(coordinator, "thing-0", 58, 101, cast("str", mqtt.published[-1][1]["clientToken"]))
        await _reported(coordinator, "thing-0", 25, 101)  # Late stale GET response.
        assert round(light.brightness * 100 / 255) == 40
        assert changes == [58, 40]
    finally:
        remove()
        await coordinator.async_shutdown()


async def test_new_shadow_version_without_reported_transition_does_not_confirm(hass: HomeAssistant) -> None:
    coordinator, mqtt, (light,) = _setup(hass)
    await _reported(coordinator, "thing-0", 25, 100)
    try:
        await light.async_turn_on(brightness=148)
        token = cast("str", mqtt.published[-1][1]["clientToken"])
        await _desired_ack(coordinator, "thing-0", 58, 101, token)

        # Newer cloud snapshots may repeat old reported state. These are not
        # evidence of a device transition, even when equal to our requested level.
        await _reported(coordinator, "thing-0", 58, 102)
        assert ("light-0", "light", "level") in coordinator._pending_fields
        await _reported(coordinator, "thing-0", 58, 103, previous_level=58)
        assert ("light-0", "light", "level") in coordinator._pending_fields

        await _reported(coordinator, "thing-0", 70, 104, previous_level=58)
        assert coordinator._pending_fields == {}
        assert round(light.brightness * 100 / 255) == 70
        assert len(mqtt.published) == 1
    finally:
        await coordinator.async_shutdown()


async def test_reported_update_accepted_without_metadata_proves_post_ack_field_write(hass: HomeAssistant) -> None:
    coordinator, mqtt, (light,) = _setup(hass)
    await _reported(coordinator, "thing-0", 25, 100)
    try:
        await light.async_turn_on(brightness=148)
        token = cast("str", mqtt.published[-1][1]["clientToken"])
        await _desired_ack(coordinator, "thing-0", 58, 101, token)
        await _reported(coordinator, "thing-0", 58, 102)
        assert ("light-0", "light", "level") in coordinator._pending_fields  # Only a cached GET.
        await _receive(
            coordinator, "thing-0", "update/accepted",
            {"state": {"reported": {"light": {"lv": 58}}}, "version": 103},
        )
        assert coordinator._pending_fields == {}
        assert round(light.brightness * 100 / 255) == 58
        assert len(mqtt.published) == 1
    finally:
        await coordinator.async_shutdown()


async def test_rapid_commands_do_not_confuse_old_confirmation_with_latest(hass: HomeAssistant) -> None:
    coordinator, mqtt, (light,) = _setup(hass)
    await _reported(coordinator, "thing-0", 25, 100)
    changes, remove = _levels(coordinator, light)
    try:
        await light.async_turn_on(brightness=148)  # 58
        first_token = cast("str", mqtt.published[-1][1]["clientToken"])
        await light.async_turn_on(brightness=179)  # 70
        second_token = cast("str", mqtt.published[-1][1]["clientToken"])
        assert first_token != second_token
        await _receive(coordinator, "thing-0", "update/rejected", {"clientToken": first_token, "code": 409})
        await _reported(coordinator, "thing-0", 58, 101, previous_level=25)
        assert round(light.brightness * 100 / 255) == 70
        await _desired_ack(coordinator, "thing-0", 70, 102, second_token)
        await _reported(coordinator, "thing-0", 70, 103, previous_level=58)
        assert changes == [58, 70]
        assert coordinator._pending_fields == {}
        assert len(mqtt.published) == 2
    finally:
        remove()
        await coordinator.async_shutdown()


async def test_partial_report_and_desired_ack_do_not_end_pending(hass: HomeAssistant) -> None:
    coordinator, mqtt, (light,) = _setup(hass)
    await _reported(coordinator, "thing-0", 25, 100)
    try:
        await light.async_turn_on(brightness=148)
        token = cast("str", mqtt.published[-1][1]["clientToken"])
        await _desired_ack(coordinator, "thing-0", 58, 101, token)
        await _receive(
            coordinator, "thing-0", "update/documents",
            {"current": {"state": {"reported": {"light": {"manu": {"spec": 50}}}}, "version": 101}},
        )
        assert round(light.brightness * 100 / 255) == 58
        assert light.extra_state_attributes["spectrum"] == 50
        assert coordinator._pending_fields
        await _reported(coordinator, "thing-0", 58, 102, previous_level=25)
        assert coordinator._pending_fields == {}
    finally:
        await coordinator.async_shutdown()


async def test_disconnect_cancels_pending_and_reconnect_does_not_replay(hass: HomeAssistant) -> None:
    coordinator, mqtt, (light,) = _setup(hass)
    await _reported(coordinator, "thing-0", 25, 100)
    try:
        await light.async_turn_on(brightness=148)
        await coordinator._disconnect_mqtt_client()
        assert coordinator._pending_fields == {}
        assert light.available is False
        assert round(light.brightness * 100 / 255) == 25
        new_mqtt = _LocalMqtt()
        coordinator._mqtt_client = cast("object", new_mqtt)
        await _reported(coordinator, "thing-0", 58, 102, previous_level=25)
        assert light.is_on and round(light.brightness * 100 / 255) == 58
        assert len(mqtt.published) == 1 and new_mqtt.published == []
    finally:
        await coordinator.async_shutdown()


async def test_two_controllers_keep_pending_isolated(hass: HomeAssistant) -> None:
    coordinator, mqtt, (first, second) = _setup(hass, count=2)
    await _reported(coordinator, "thing-0", 25, 100)
    await _reported(coordinator, "thing-1", 70, 200)
    try:
        await first.async_turn_on(brightness=148)
        assert round(first.brightness * 100 / 255) == 58
        assert round(second.brightness * 100 / 255) == 70
        await _reported(coordinator, "thing-1", 50, 201, previous_level=70)
        assert round(first.brightness * 100 / 255) == 58
        assert round(second.brightness * 100 / 255) == 50
        assert len(mqtt.published) == 1
        await _reported(coordinator, "thing-0", 58, 102, previous_level=25)
        assert coordinator._pending_fields == {}
    finally:
        await coordinator.async_shutdown()


async def test_standalone_desired_fallback_is_unchanged(hass: HomeAssistant) -> None:
    coordinator, mqtt, (light,) = _setup(hass)
    coordinator._devices.append(
        DeviceInfo(
            device_id="standalone", client_id="thing-standalone", topic_prefix="topic/standalone",
            name="AeroStream H05", online=True, scene_id=66090, device_type="humidifier",
        )
    )
    coordinator._build_topic_maps()
    try:
        await _receive(
            coordinator, "thing-standalone", "update/accepted",
            {"state": {"desired": {"hmdf": {"on": 1, "mode": 1}}}, "version": 14},
        )
        assert coordinator.data["shadows"]["standalone"]["hmdf"]["on"] is True
        await _receive(
            coordinator, "thing-0", "update/accepted",
            {"state": {"desired": {"light": {"manu": {"lv": 58}}}}, "version": 15},
        )
        # Legacy startup fallback is allowed until a real report or a local command.
        assert light.is_on is True
        assert ("light-0", "light", "level") not in coordinator._has_reported_fields
        assert coordinator._pending_fields == {}
        assert mqtt.published == []
    finally:
        await coordinator.async_shutdown()


async def test_standalone_heater_desired_only_still_normalizes(hass: HomeAssistant) -> None:
    coordinator, _, _ = _setup(hass)
    coordinator._devices.append(
        DeviceInfo(
            device_id="heater-1", client_id="thing-heater", topic_prefix="topic/heater",
            name="AeroFlux W70", online=True, scene_id=66091, device_type="heater",
        )
    )
    coordinator._build_topic_maps()
    try:
        await _receive(
            coordinator, "thing-heater", "update/accepted",
            {"state": {"desired": {"heat": {"on": 1, "targetTemp": 2400}}}, "version": 21},
        )
        assert coordinator.data["shadows"]["heater-1"]["heat"]["on"] is True
        assert coordinator._pending_fields == {}
    finally:
        await coordinator.async_shutdown()


async def test_local_command_displaces_unconfirmed_startup_desired_fallback(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("custom_components.vivosun_growhub.coordinator._FIELD_CONFIRMATION_TIMEOUT", 0.01)
    coordinator, mqtt, (light,) = _setup(hass)
    try:
        await _receive(
            coordinator, "thing-0", "get/accepted",
            {"state": {"desired": {"light": {"manu": {"lv": 42}}}}, "version": 100},
        )
        assert round(light.brightness * 100 / 255) == 42  # Legacy provisional startup value.
        await light.async_turn_on(brightness=148)
        assert round(light.brightness * 100 / 255) == 58
        assert "level" not in coordinator._shadow_states["light-0"]["light"]
        token = cast("str", mqtt.published[-1][1]["clientToken"])
        await _desired_ack(coordinator, "thing-0", 58, 101, token)
        await asyncio.sleep(0.025)
        assert light.brightness is None  # No device-reported level to restore.
        await _reported(coordinator, "thing-0", 58, 102)
        assert round(light.brightness * 100 / 255) == 58
        assert len(mqtt.published) == 1
    finally:
        await coordinator.async_shutdown()
