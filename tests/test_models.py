"""Tests for Vivosun device type inference."""

from __future__ import annotations

from custom_components.vivosun_growhub.models import infer_device_type


def test_infer_device_type_prefers_dehumidifier_over_humidifier_substring() -> None:
    assert infer_device_type("Tent dehumidifier", "") == "dehumidifier"


def test_infer_device_type_uses_dehumidifier_model_token() -> None:
    assert infer_device_type("Tent", "vivosun-VSDRYD12-acc-device-1") == "dehumidifier"


def test_infer_device_type_uses_vsctl_controller_model_token() -> None:
    assert infer_device_type("Tent", "vivosun-VSCTL002-acc-device-1") == "controller"


def test_infer_device_type_uses_vscb_curing_box_model_token() -> None:
    assert infer_device_type("Post Harvest", "vivosun-VSCBC80-acc-device-1") == "curing_box"
