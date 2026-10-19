"""Tests for redaction helpers — camelCase key coverage."""

from custom_components.vivosun_growhub.redaction import (
    REDACTED,
    redact_topic,
    sanitize_mapping_for_capture,
    sanitize_mapping_for_debug,
)


def test_camel_case_identity_id_redacted() -> None:
    result = sanitize_mapping_for_debug({"awsIdentityId": "us-east-1:abc-123"})
    assert result["awsIdentityId"] != "us-east-1:abc-123"
    assert "..." in str(result["awsIdentityId"])


def test_camel_case_client_id_redacted() -> None:
    result = sanitize_mapping_for_debug({"clientId": "some-client-id-value"})
    assert result["clientId"] != "some-client-id-value"
    assert "..." in str(result["clientId"])


def test_camel_case_topic_prefix_redacted() -> None:
    result = sanitize_mapping_for_debug({"topicPrefix": "prefix/device/abc123"})
    assert result["topicPrefix"] != "prefix/device/abc123"
    assert "..." in str(result["topicPrefix"])


def test_pascal_case_identity_id_redacted() -> None:
    result = sanitize_mapping_for_debug({"IdentityId": "us-east-1:xyz-789"})
    assert result["IdentityId"] != "us-east-1:xyz-789"
    assert "..." in str(result["IdentityId"])


def test_secret_key_fully_redacted() -> None:
    result = sanitize_mapping_for_debug({"awsOpenIdToken": "eyJhbGci..."})
    assert result["awsOpenIdToken"] == REDACTED


def test_redacted_identifiers_stay_redacted_and_auth_header_spellings_are_masked() -> None:
    for key in ("auth_header", "authHeader", "Auth_Header", "auth-header"):
        result = sanitize_mapping_for_debug({key: 91234567, "deviceId": REDACTED, "model_token": "VSCTLE42A"})
        assert result[key] == REDACTED
        assert result["deviceId"] == REDACTED
        assert result["model_token"] == "VSCTLE42A"
        assert sanitize_mapping_for_debug(result) == result


def test_camera_lan_credentials_are_redacted_in_nested_diagnostics() -> None:
    # getTotalList setting.jf uses devUser/devPass, including in skipped-device exports.
    result = sanitize_mapping_for_debug(
        {"setting": {"jf": {"devUser": "abjd", "devPass": "4kt5em"}}}
    )
    camera = result["setting"]["jf"]
    assert camera["devPass"] == REDACTED
    assert camera["devUser"] != "abjd"


def test_non_sensitive_key_passes_through() -> None:
    result = sanitize_mapping_for_debug({"status": "online", "version": 3})
    assert result["status"] == "online"
    assert result["version"] == 3


def test_capture_redacts_topic_routes_and_shadow_network_identifiers() -> None:
    topic = "$aws/things/vivosun-VSCTLE42A-private-account-device/shadow/get/accepted"
    safe = redact_topic(topic)
    assert safe.endswith("/shadow/get/accepted")
    assert "private-account-device" not in safe
    assert redact_topic(safe) == safe
    result = sanitize_mapping_for_debug(
        {
            "topic": topic,
            "state": {"reported": {"snsExt": {"mac": "raw-mac"}, "location": {"lat": 1, "lon": 2}}},
        }
    )
    assert "raw-mac" not in str(result)
    assert result["state"]["reported"]["location"] == REDACTED


def test_snake_case_still_works() -> None:
    result = sanitize_mapping_for_debug({
        "aws_identity_id": "us-east-1:abc-123",
        "client_id": "client-val",
        "device_id": "dev-val",
    })
    for key in ("aws_identity_id", "client_id", "device_id"):
        assert "..." in str(result[key])


def test_capture_policy_is_recursive_idempotent_and_keeps_debug_policy_stricter() -> None:
    row = {
        "device_id": "device-1", "device-id": "device-2", "sceneId": "scene-2", "programId": "program-3",
        "userPlanStageId": "stage-4", "mode": "auto", "model_token": "VSCTLE42A",
        "topic": "$aws/things/thing-with-private-account/shadow/update/accepted",
        "samples": [{
            "ownerId": 12345, "owner-id": 23456, "tenant_id": 67890, "userName": "private-user",
            "account": {"unexpected": "private-details"},
            "network.ipAddress": "192.0.2.1", "gpsLat": 12.3,
            "apiKey": "private-key", "headers": {"opaque": "private-header"},
            "auth_header": 91234567, "cookie": "private-cookie", "devPass": "private-password",
        }],
    }

    capture = sanitize_mapping_for_capture(row)

    assert capture["device_id"] == "device-1"
    assert capture["device-id"] == "device-2"
    assert capture["sceneId"] == "scene-2"
    assert capture["programId"] == "program-3"
    assert capture["userPlanStageId"] == "stage-4"
    assert capture["mode"] == "auto"
    assert capture["model_token"] == "VSCTLE42A"
    assert "private-account" not in capture["topic"]
    assert all(value == REDACTED for value in capture["samples"][0].values())
    assert sanitize_mapping_for_capture(capture) == capture
    assert sanitize_mapping_for_debug(row)["device_id"] != "device-1"


def test_capture_redacts_nested_credentials_account_network_and_location() -> None:
    raw = {
        "payload": [{
            "Credentials": {"secretAccessKey": "key", "session-token": "session"},
            "request": {"Authorization": "Bearer secret", "Cookie": "session=value"},
            "network": {"ipAddr": "192.0.2.1", "ssid": "Home WiFi"},
            "gpsLat": 48.1,
            "ownerId": "account-number",
            "userPlanStageId": "stage-3",
            "state": {"reported": {"mode": "auto"}},
        }],
    }
    result = sanitize_mapping_for_capture(raw)
    text = str(result)
    for value in ("key", "session", "Bearer secret", "Home WiFi", "192.0.2.1", "account-number", "48.1"):
        assert value not in text
    assert result["payload"][0]["userPlanStageId"] == "stage-3"
    assert result["payload"][0]["state"]["reported"]["mode"] == "auto"


def test_hardware_identifiers_are_fully_masked_in_debug_and_capture() -> None:
    keys = ("hwId", "hwID", "hw_id", "hw-id", "HWID", "hardwareId", "hardware_id", "hardware-id", "network.hwId")
    for sanitize in (sanitize_mapping_for_debug, sanitize_mapping_for_capture):
        result = sanitize({"raw": [{key: "synthetic-hardware-4242" for key in keys}]})
        assert all(result["raw"][0][key] == REDACTED for key in keys)
        assert sanitize(result) == result


def test_hardware_redaction_preserves_capture_protocol_and_resource_ids() -> None:
    sample = {
        "state": {"reported": {"snsExt": {"hwId": "synthetic-hardware-4242", "modelToken": "VSSBS40"},
                               "light": {"manu": {"spec": 50}}, "inTemp": 2350}},
        "deviceId": "synthetic-device-1", "sceneId": 140, "programId": "synthetic-program-2",
        "userPlanStageId": "synthetic-stage-3",
        "client_id": "synthetic-account-client-12345",
        "topic": "$aws/things/synthetic-account-thing/shadow/update/documents",
    }
    result = sanitize_mapping_for_capture(sample)
    assert result["state"]["reported"]["snsExt"] == {"hwId": REDACTED, "modelToken": "VSSBS40"}
    assert result["state"]["reported"]["light"]["manu"]["spec"] == 50
    assert result["state"]["reported"]["inTemp"] == 2350
    assert [result[key] for key in ("deviceId", "sceneId", "programId", "userPlanStageId")] == [
        "synthetic-device-1", 140, "synthetic-program-2", "synthetic-stage-3"]
    assert result["client_id"] != sample["client_id"]
    assert "synthetic-account-thing" not in result["topic"]
    assert result["topic"].endswith("/shadow/update/documents")
