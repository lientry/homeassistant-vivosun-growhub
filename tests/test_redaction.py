"""Tests for redaction helpers — camelCase key coverage."""

from custom_components.vivosun_growhub.redaction import (
    REDACTED,
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


def test_non_sensitive_key_passes_through() -> None:
    result = sanitize_mapping_for_debug({"status": "online", "version": 3})
    assert result["status"] == "online"
    assert result["version"] == 3


def test_snake_case_still_works() -> None:
    result = sanitize_mapping_for_debug({
        "aws_identity_id": "us-east-1:abc-123",
        "client_id": "client-val",
        "device_id": "dev-val",
    })
    for key in ("aws_identity_id", "client_id", "device_id"):
        assert "..." in str(result[key])
