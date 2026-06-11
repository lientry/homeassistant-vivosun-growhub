"""Request-body encryption for Vivosun authenticated POST endpoints.

The Vivosun cloud rejects unencrypted production POST requests with the
misleading message ``Your app version is outdated`` (error code 60001). The
official Android app encrypts those request bodies in its OkHttp interceptor
(``VSHttpHeaderInterceptor``); this module reproduces that scheme exactly.

For each request a fresh AES-128/192/256 CBC (PKCS7) key and IV are derived:

* ``key`` is a random-length slice of ``md5_hex(Request-Time)``
* ``iv`` is a 16-char slice of a random alphanumeric ``salt``

The plaintext JSON body is encrypted, hex-encoded and wrapped as
``{"content": "<hex>"}``. The slice offsets and the salt travel in the
``Request-Code`` header so the server can reconstruct the same key and IV:

    Request-Time: <epoch milliseconds>
    Request-Code: AC5-<key_start>-<key_end>-<iv_start>-<iv_end>-<salt>

Only the request is encrypted; responses are returned as plaintext JSON.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import string

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

_ALPHABET = string.ascii_uppercase + string.ascii_lowercase + string.digits
_AES_KEY_LENGTHS = (16, 24, 32)
_IV_LENGTH = 16
_MD5_HEX_LENGTH = 32


def encrypt_request_body(plaintext: bytes, *, timestamp_ms: int) -> tuple[str, str, bytes]:
    """Encrypt a request body, returning (request_time, request_code, body).

    ``request_time`` and ``request_code`` must be sent as the ``Request-Time``
    and ``Request-Code`` headers; ``body`` is the JSON payload to transmit.
    """
    md5_hex = hashlib.md5(str(timestamp_ms).encode()).hexdigest()  # MD5 matches app

    key_len = secrets.choice(_AES_KEY_LENGTHS)
    key_start = secrets.randbelow(_MD5_HEX_LENGTH - key_len + 1)
    key_end = key_start + key_len
    aes_key = md5_hex[key_start:key_end].encode()

    salt_len = _IV_LENGTH + secrets.randbelow(84)  # 16..99, matches app
    salt = "".join(secrets.choice(_ALPHABET) for _ in range(salt_len))
    iv_start = secrets.randbelow(salt_len - _IV_LENGTH + 1)
    iv_end = iv_start + _IV_LENGTH
    aes_iv = salt[iv_start:iv_end].encode()

    padder = PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(aes_key), modes.CBC(aes_iv)).encryptor()
    content_hex = (encryptor.update(padded) + encryptor.finalize()).hex()

    request_code = f"AC5-{key_start}-{key_end}-{iv_start}-{iv_end}-{salt}"
    body = json.dumps({"content": content_hex}, separators=(",", ":")).encode()
    return str(timestamp_ms), request_code, body
