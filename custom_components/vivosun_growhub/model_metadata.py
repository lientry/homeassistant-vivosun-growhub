"""Bundled model metadata derived from verified recon artifacts."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .models import client_model_token

_MODEL_METADATA_BY_TOKEN: dict[str, dict[str, object]] = {
    "VSCTL001": {
        "default_name": "GrowHub E42",
        "device_type": "controller",
        "comm_mode_list": ["MQTT"],
        "channel_param_key": {
            "0": {"temp": "outTemp", "humi": "outHumi"},
            "1": {"temp": "inTemp", "humi": "inHumi"},
        },
        "data_upload_groups": [
            {
                "group_name": "Inside",
                "group_type": "CLIMATE",
                "shadow_channel": 0,
                "data_keys": ["inTemp", "inHumi", "inVpd"],
            },
            {
                "group_name": "Outside",
                "group_type": "CLIMATE",
                "shadow_channel": 0,
                "data_keys": ["outTemp", "outHumi", "outVpd"],
            },
        ],
    },
    "VSCTLE42A": {
        "default_name": "GrowHub E42A",
        "device_type": "controller",
        "comm_mode_list": ["MQTT"],
        "channel_param_key": {
            "0": {"temp": "outTemp", "humi": "outHumi"},
            "1": {"temp": "inTemp", "humi": "inHumi"},
        },
        "data_upload_groups": [
            {
                "group_name": "Inside",
                "group_type": "CLIMATE",
                "shadow_channel": 0,
                "data_keys": ["inTemp", "inHumi", "inVpd"],
            },
            {
                "group_name": "Outside",
                "group_type": "CLIMATE",
                "shadow_channel": 0,
                "data_keys": ["outTemp", "outHumi", "outVpd"],
            },
        ],
    },
    "VSCTLE42AP": {
        "default_name": "GrowHub E42A+",
        "device_type": "controller",
        "comm_mode_list": ["MQTT"],
        "channel_param_key": {
            "0": {"temp": "bTemp", "humi": "bHumi"},
            "1": {"temp": "pTemp", "humi": "pHumi"},
        },
        "data_upload_groups": [
            {
                "group_name": "Probe",
                "group_type": "CLIMATE",
                "shadow_channel": 0,
                "data_keys": ["pTemp", "pHumi", "pVpd"],
            },
            {
                "group_name": "Built-in",
                "group_type": "CLIMATE",
                "shadow_channel": 0,
                "data_keys": ["bTemp", "bHumi", "bVpd"],
            },
        ],
    },
    "VSCTLY18": {
        "default_name": "VGrow Smart Grow Box",
        "device_type": "controller",
        "comm_mode_list": ["MQTT"],
        "channel_param_key": {
            "0": {"temp": "bTemp", "humi": "bHumi"},
            "1": {"temp": "pTemp", "humi": "pHumi"},
        },
        "data_upload_groups": [
            {
                "group_name": "Probe",
                "group_type": "CLIMATE",
                "shadow_channel": 0,
                "data_keys": ["pTemp", "pHumi", "pVpd", "pCo2"],
            },
            {
                "group_name": "Built-in",
                "group_type": "CLIMATE",
                "shadow_channel": 0,
                "data_keys": ["bTemp", "bHumi", "bVpd"],
            },
            {
                "group_name": "Water Level",
                "group_type": "WATER_LEVEL",
                "shadow_channel": 0,
                "data_keys": ["waterLv", "waterT"],
            },
        ],
    },
    "VSHUMH05": {
        "default_name": "AeroStream H05",
        "device_type": "humidifier",
        "comm_mode_list": ["MQTT"],
        "channel_param_key": {
            "1": {"temp": "pTemp", "humi": "pHumi"},
        },
        "data_upload_groups": [
            {
                "group_name": "Probe",
                "group_type": "CLIMATE",
                "shadow_channel": 0,
                "data_keys": ["pTemp", "pHumi", "pVpd", "pCo2"],
            }
        ],
    },
    "VSHUMH09": {
        "default_name": "AeroStream H09",
        "device_type": "humidifier",
        "comm_mode_list": ["MQTT"],
        "channel_param_key": {
            "1": {"temp": "pTemp", "humi": "pHumi"},
        },
        "data_upload_groups": [
            {
                "group_name": "Probe",
                "group_type": "CLIMATE",
                "shadow_channel": 0,
                "data_keys": ["pTemp", "pHumi", "pVpd"],
            }
        ],
    },
    "VSHUMH19": {
        "default_name": "AeroStream H19",
        "device_type": "humidifier",
        "comm_mode_list": ["MQTT"],
        "channel_param_key": {
            "1": {"temp": "pTemp", "humi": "pHumi"},
        },
        "data_upload_groups": [
            {
                "group_name": "Probe",
                "group_type": "CLIMATE",
                "shadow_channel": 0,
                "data_keys": ["pTemp", "pHumi", "pVpd"],
            }
        ],
    },
}


def support_capture_model_metadata(client_id: str) -> dict[str, Any]:
    """Return a diagnostics-safe metadata summary for the device model token."""
    model_token = client_model_token(client_id)
    metadata = _MODEL_METADATA_BY_TOKEN.get(model_token)
    result: dict[str, Any] = {
        "model_code": model_token,
        "matched": metadata is not None,
        "source": "bundled_recon_catalog",
    }
    if metadata is not None:
        result.update(deepcopy(metadata))
    return result
