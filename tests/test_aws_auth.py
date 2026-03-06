"""Unit tests for WP-3 AWS auth exchange and SigV4 signer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import parse_qs, urlparse

import aiohttp
import pytest

from custom_components.vivosun_growhub.aws_auth import (
    AWS_CREDENTIAL_REFRESH_SKEW,
    COGNITO_IDENTITY_HOST,
    COGNITO_TARGET,
    AwsAuthClient,
    AwsCredentials,
)
from custom_components.vivosun_growhub.exceptions import (
    VivosunAuthError,
    VivosunConnectionError,
    VivosunResponseError,
)
from custom_components.vivosun_growhub.models import AwsIdentity


class _MockResponse:
    """Minimal async response context manager used by unit tests."""

    def __init__(self, *, status: int, payload: object) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self) -> _MockResponse:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        _ = (exc_type, exc, tb)
        return None

    async def json(self, *, content_type: str | None = None) -> object:
        _ = content_type
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _MockSession:
    """Minimal request dispatcher for aws_auth tests."""

    def __init__(self, responses: list[_MockResponse] | None = None, *, request_error: Exception | None = None) -> None:
        self._responses = responses or []
        self._request_error = request_error
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, **kwargs: object) -> _MockResponse:
        self.calls.append({"url": url, "kwargs": kwargs})
        if self._request_error is not None:
            raise self._request_error
        if not self._responses:
            raise AssertionError("No mock responses left")
        return self._responses.pop(0)


def _aws_identity(*, identity_id: str = "us-east-2:identity-1") -> AwsIdentity:
    return AwsIdentity(
        aws_host="a1u2b1xxsghagk-ats.iot.us-east-2.amazonaws.com",
        aws_region="us-east-2",
        aws_identity_id=identity_id,
        aws_open_id_token="openid-token-value",
        aws_port=443,
    )


async def test_get_credentials_for_identity_success_epoch_expiration() -> None:
    """Cognito payload should parse into AwsCredentials with UTC expiration."""
    expiration_epoch = 1761600000
    payload = {
        "IdentityId": "us-east-2:identity-1",
        "Credentials": {
            "AccessKeyId": "ASIAXYZ",
            "SecretKey": "secret-value",
            "SessionToken": "session-token-value",
            "Expiration": expiration_epoch,
        },
    }
    mock_session = _MockSession(responses=[_MockResponse(status=200, payload=payload)])
    client = AwsAuthClient(cast("aiohttp.ClientSession", mock_session))

    credentials = await client.get_credentials_for_identity(_aws_identity())

    assert credentials.access_key_id == "ASIAXYZ"
    assert credentials.secret_access_key == "secret-value"
    assert credentials.session_token == "session-token-value"
    assert credentials.expiration == datetime.fromtimestamp(expiration_epoch, tz=UTC)

    request = mock_session.calls[0]
    assert request["url"] == f"https://{COGNITO_IDENTITY_HOST}/"
    kwargs = cast("dict[str, object]", request["kwargs"])
    headers = cast("dict[str, str]", kwargs["headers"])
    assert headers["Content-Type"] == "application/x-amz-json-1.1"
    assert headers["X-Amz-Target"] == COGNITO_TARGET
    body = cast("dict[str, object]", kwargs["json"])
    assert body["IdentityId"] == "us-east-2:identity-1"
    assert body["Logins"] == {"cognito-identity.amazonaws.com": "openid-token-value"}


async def test_get_credentials_for_identity_parses_iso_expiration() -> None:
    payload = {
        "Credentials": {
            "AccessKeyId": "ASIAXYZ",
            "SecretKey": "secret-value",
            "SessionToken": "session-token-value",
            "Expiration": "2026-03-05T12:00:00Z",
        }
    }
    session = cast("aiohttp.ClientSession", _MockSession(responses=[_MockResponse(status=200, payload=payload)]))
    client = AwsAuthClient(session)

    credentials = await client.get_credentials_for_identity(_aws_identity())

    assert credentials.expiration == datetime(2026, 3, 5, 12, 0, tzinfo=UTC)


async def test_get_credentials_for_identity_missing_fields_raise_response_error() -> None:
    payload = {"Credentials": {"AccessKeyId": "ASIAXYZ", "SecretKey": "secret-value", "Expiration": 1761600000}}
    session = cast("aiohttp.ClientSession", _MockSession(responses=[_MockResponse(status=200, payload=payload)]))
    client = AwsAuthClient(session)

    with pytest.raises(VivosunResponseError):
        await client.get_credentials_for_identity(_aws_identity())


async def test_get_credentials_for_identity_auth_error_classification() -> None:
    payload = {"__type": "NotAuthorizedException", "message": "Token is invalid"}
    session = cast("aiohttp.ClientSession", _MockSession(responses=[_MockResponse(status=400, payload=payload)]))
    client = AwsAuthClient(session)

    with pytest.raises(VivosunAuthError):
        await client.get_credentials_for_identity(_aws_identity())


async def test_get_credentials_for_identity_transport_failure_maps_connection_error() -> None:
    client = AwsAuthClient(
        cast("aiohttp.ClientSession", _MockSession(request_error=aiohttp.ClientError("socket closed"))),
    )

    with pytest.raises(VivosunConnectionError):
        await client.get_credentials_for_identity(_aws_identity())


def test_sigv4_sign_mqtt_url_shape_and_required_params() -> None:
    client = AwsAuthClient(cast("aiohttp.ClientSession", _MockSession()))
    now = datetime(2026, 3, 5, 11, 30, 45, tzinfo=UTC)
    credentials = AwsCredentials(
        access_key_id="ASIAEXAMPLE",
        secret_access_key="secret-key-example",
        session_token="session-token-example",
        expiration=now + timedelta(hours=1),
    )

    url = client.sigv4_sign_mqtt_url(
        endpoint="a1u2b1xxsghagk-ats.iot.us-east-2.amazonaws.com",
        region="us-east-2",
        credentials=credentials,
        now=now,
    )

    parsed = urlparse(url)
    assert parsed.scheme == "wss"
    assert parsed.path == "/mqtt"
    query = parse_qs(parsed.query)
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert query["X-Amz-Date"] == ["20260305T113045Z"]
    assert query["X-Amz-SignedHeaders"] == ["host"]
    assert query["X-Amz-Security-Token"] == ["session-token-example"]
    assert "X-Amz-Expires" not in query  # token is appended after signing
    assert "X-Amz-Credential" in query
    assert "X-Amz-Signature" in query


def test_sigv4_sign_mqtt_url_deterministic_signature() -> None:
    client = AwsAuthClient(cast("aiohttp.ClientSession", _MockSession()))
    now = datetime(2026, 3, 5, 0, 0, 0, tzinfo=UTC)
    credentials = AwsCredentials(
        access_key_id="ASIADETTEST",
        secret_access_key="deterministic-secret",
        session_token="deterministic-session-token",
        expiration=now + timedelta(hours=1),
    )

    url = client.sigv4_sign_mqtt_url(
        endpoint="a1u2b1xxsghagk-ats.iot.us-east-2.amazonaws.com",
        region="us-east-2",
        credentials=credentials,
        now=now,
    )

    expected = (
        "wss://a1u2b1xxsghagk-ats.iot.us-east-2.amazonaws.com/mqtt"
        "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
        "&X-Amz-Credential=ASIADETTEST%2F20260305%2Fus-east-2%2Fiotdevicegateway%2Faws4_request"
        "&X-Amz-Date=20260305T000000Z"
        "&X-Amz-SignedHeaders=host"
        "&X-Amz-Signature=72bba12787945b5537a504643b8f0bcbe3263de56e7f42e8f1e139576b6e2d7a"
        "&X-Amz-Security-Token=deterministic-session-token"
    )
    assert url == expected


def test_credentials_need_refresh_boundary() -> None:
    client = AwsAuthClient(cast("aiohttp.ClientSession", _MockSession()))
    now = datetime(2026, 3, 5, 12, 0, 0, tzinfo=UTC)

    far_expiration = AwsCredentials(
        access_key_id="AKIA",
        secret_access_key="secret",
        session_token="token",
        expiration=now + AWS_CREDENTIAL_REFRESH_SKEW + timedelta(seconds=1),
    )
    near_expiration = AwsCredentials(
        access_key_id="AKIA",
        secret_access_key="secret",
        session_token="token",
        expiration=now + AWS_CREDENTIAL_REFRESH_SKEW,
    )

    assert client.credentials_need_refresh(far_expiration, now=now) is False
    assert client.credentials_need_refresh(near_expiration, now=now) is True


async def test_get_credentials_for_identity_accepts_identity_rotation() -> None:
    payload_one = {
        "Credentials": {
            "AccessKeyId": "ASIAONE",
            "SecretKey": "secret-value-1",
            "SessionToken": "session-token-1",
            "Expiration": 1761600000,
        }
    }
    payload_two = {
        "Credentials": {
            "AccessKeyId": "ASIATWO",
            "SecretKey": "secret-value-2",
            "SessionToken": "session-token-2",
            "Expiration": 1761600100,
        }
    }
    mock_session = _MockSession(
        responses=[
            _MockResponse(status=200, payload=payload_one),
            _MockResponse(status=200, payload=payload_two),
        ]
    )
    client = AwsAuthClient(cast("aiohttp.ClientSession", mock_session))

    await client.get_credentials_for_identity(_aws_identity(identity_id="us-east-2:identity-old"))
    await client.get_credentials_for_identity(_aws_identity(identity_id="us-east-2:identity-new"))

    first_body = cast("dict[str, object]", cast("dict[str, object]", mock_session.calls[0]["kwargs"])["json"])
    second_body = cast("dict[str, object]", cast("dict[str, object]", mock_session.calls[1]["kwargs"])["json"])
    assert first_body["IdentityId"] == "us-east-2:identity-old"
    assert second_body["IdentityId"] == "us-east-2:identity-new"
