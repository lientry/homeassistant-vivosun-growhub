"""AWS credential exchange and SigV4 websocket URL signing helpers."""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import quote

import aiohttp

from .const import API_REQUEST_TIMEOUT_SECONDS
from .exceptions import VivosunAuthError, VivosunConnectionError, VivosunResponseError
from .redaction import sanitize_mapping_for_debug

if TYPE_CHECKING:
    from .models import AwsIdentity

COGNITO_IDENTITY_HOST = "cognito-identity.us-east-2.amazonaws.com"
COGNITO_IDENTITY_PATH = "/"
COGNITO_TARGET = "AWSCognitoIdentityService.GetCredentialsForIdentity"
SIGV4_SERVICE = "iotdevicegateway"
SIGV4_ALGORITHM = "AWS4-HMAC-SHA256"
SIGV4_URL_TTL_SECONDS = 900
AWS_CREDENTIAL_REFRESH_SKEW = timedelta(minutes=5)

_LOGGER = logging.getLogger(__name__)
_AUTH_MESSAGE_MARKERS = ("auth", "credential", "forbidden", "invalid", "token", "unauthorized", "notauthorized")


@dataclass(slots=True, frozen=True)
class AwsCredentials:
    """Temporary AWS credentials returned by Cognito."""

    access_key_id: str
    secret_access_key: str
    session_token: str
    expiration: datetime


class AwsAuthClient:
    """Fetch temporary AWS credentials and presign MQTT websocket URLs."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._timeout = aiohttp.ClientTimeout(total=API_REQUEST_TIMEOUT_SECONDS)

    async def get_credentials_for_identity(self, aws_identity: AwsIdentity) -> AwsCredentials:
        """Call Cognito GetCredentialsForIdentity and return typed credentials."""
        _LOGGER.info("Requesting temporary AWS credentials from Cognito")
        url = f"https://{COGNITO_IDENTITY_HOST}{COGNITO_IDENTITY_PATH}"
        headers = {
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": COGNITO_TARGET,
        }
        body = {
            "IdentityId": aws_identity.aws_identity_id,
            "Logins": {"cognito-identity.amazonaws.com": aws_identity.aws_open_id_token},
        }

        try:
            async with self._session.post(url, headers=headers, json=body, timeout=self._timeout) as response:
                payload = await self._read_json_payload(response)
                self._raise_for_cognito_error(payload, status=response.status)
                credentials_payload = self._expect_mapping(payload, "Credentials")
                credentials = AwsCredentials(
                    access_key_id=self._expect_str(credentials_payload, "AccessKeyId"),
                    secret_access_key=self._expect_str(credentials_payload, "SecretKey"),
                    session_token=self._expect_str(credentials_payload, "SessionToken"),
                    expiration=self._parse_expiration(credentials_payload.get("Expiration")),
                )
                _LOGGER.debug("Cognito credentials retrieved and parsed")
                return credentials
        except (aiohttp.ClientError, TimeoutError) as err:
            raise VivosunConnectionError("Cognito credential request failed") from err

    def credentials_need_refresh(
        self,
        credentials: AwsCredentials,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Return True when credentials are inside pre-expiry refresh skew."""
        current = now.astimezone(UTC) if now is not None else datetime.now(tz=UTC)
        return credentials.expiration - current <= AWS_CREDENTIAL_REFRESH_SKEW

    def sigv4_sign_mqtt_url(
        self,
        *,
        endpoint: str,
        region: str,
        credentials: AwsCredentials,
        now: datetime | None = None,
    ) -> str:
        """Return SigV4-signed MQTT websocket URL.

        AWS IoT requires that the session token is appended AFTER signing,
        not included in the canonical request.  The X-Amz-Expires parameter
        is also omitted from the canonical query during signing.
        """
        timestamp = now.astimezone(UTC) if now is not None else datetime.now(tz=UTC)
        amz_date = timestamp.strftime("%Y%m%dT%H%M%SZ")
        datestamp = timestamp.strftime("%Y%m%d")
        credential_scope = f"{datestamp}/{region}/{SIGV4_SERVICE}/aws4_request"
        payload_hash = hashlib.sha256(b"").hexdigest()

        # Only these four params are part of the canonical request for signing.
        # X-Amz-Security-Token and X-Amz-Expires are NOT included here.
        query_params = {
            "X-Amz-Algorithm": SIGV4_ALGORITHM,
            "X-Amz-Credential": f"{credentials.access_key_id}/{credential_scope}",
            "X-Amz-Date": amz_date,
            "X-Amz-SignedHeaders": "host",
        }
        canonical_query = _canonical_query_string(query_params)
        canonical_request = (
            "GET\n"
            "/mqtt\n"
            f"{canonical_query}\n"
            f"host:{endpoint}\n"
            "\n"
            "host\n"
            f"{payload_hash}"
        )
        string_to_sign = (
            f"{SIGV4_ALGORITHM}\n"
            f"{amz_date}\n"
            f"{credential_scope}\n"
            f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
        )
        signing_key = _get_signature_key(credentials.secret_access_key, datestamp, region, SIGV4_SERVICE)
        signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        # Append signature, then session token AFTER signing.
        signed_query = canonical_query + f"&X-Amz-Signature={signature}"
        signed_query += f"&X-Amz-Security-Token={quote(credentials.session_token, safe='')}"
        return f"wss://{endpoint}/mqtt?{signed_query}"

    async def _read_json_payload(self, response: aiohttp.ClientResponse) -> dict[str, object]:
        """Read response body as JSON object."""
        try:
            payload = await response.json(content_type=None)
        except (aiohttp.ContentTypeError, ValueError) as err:
            if response.status in (401, 403):
                raise VivosunAuthError(f"Cognito authentication failed with HTTP {response.status}") from err
            raise VivosunResponseError("Cognito response body is not valid JSON") from err

        if not isinstance(payload, dict):
            raise VivosunResponseError("Cognito response JSON root must be an object")
        return payload

    def _raise_for_cognito_error(self, payload: dict[str, object], *, status: int) -> None:
        """Raise typed integration errors for non-success Cognito responses."""
        if status in (401, 403):
            raise VivosunAuthError(f"Cognito authentication failed with HTTP {status}")
        if status < 400:
            return

        error_type = payload.get("__type")
        message = payload.get("message")
        detail = "Cognito request failed"
        if isinstance(error_type, str) and isinstance(message, str):
            detail = f"{error_type}: {message}"
        elif isinstance(error_type, str):
            detail = error_type
        elif isinstance(message, str):
            detail = message

        _LOGGER.debug("Cognito request error payload: %s", sanitize_mapping_for_debug(payload))
        lowered = detail.lower()
        if any(marker in lowered for marker in _AUTH_MESSAGE_MARKERS):
            raise VivosunAuthError("Cognito authentication failed")
        raise VivosunResponseError(f"Cognito request failed (HTTP {status})")

    def _expect_mapping(self, payload: dict[str, object], key: str) -> dict[str, object]:
        value = payload.get(key)
        if not isinstance(value, dict):
            raise VivosunResponseError(f"Expected object at '{key}'")
        return value

    def _expect_str(self, payload: dict[str, object], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str):
            raise VivosunResponseError(f"Expected string at '{key}'")
        return value

    def _parse_expiration(self, value: object) -> datetime:
        if isinstance(value, bool):
            raise VivosunResponseError("Expected timestamp at 'Expiration'")
        if isinstance(value, int | float):
            return datetime.fromtimestamp(float(value), tz=UTC)
        if isinstance(value, str):
            parsed_epoch = _maybe_parse_epoch_string(value)
            if parsed_epoch is not None:
                return parsed_epoch
            iso_value = value.replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(iso_value)
            except ValueError as err:
                raise VivosunResponseError("Expected ISO-8601 timestamp at 'Expiration'") from err
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        raise VivosunResponseError("Expected timestamp at 'Expiration'")


def _maybe_parse_epoch_string(value: str) -> datetime | None:
    try:
        epoch = float(value)
    except ValueError:
        return None
    return datetime.fromtimestamp(epoch, tz=UTC)


def _canonical_query_string(params: dict[str, str]) -> str:
    pairs = sorted(params.items(), key=lambda item: (item[0], item[1]))
    return "&".join(f"{_rfc3986_encode(key)}={_rfc3986_encode(value)}" for key, value in pairs)


def _rfc3986_encode(value: str) -> str:
    return quote(value, safe="-_.~")


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _get_signature_key(secret_key: str, datestamp: str, region: str, service: str) -> bytes:
    k_date = _sign(f"AWS4{secret_key}".encode(), datestamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    return _sign(k_service, "aws4_request")
