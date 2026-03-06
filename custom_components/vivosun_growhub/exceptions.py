"""Custom exceptions for the Vivosun GrowHub integration."""

from __future__ import annotations


class VivosunGrowhubError(Exception):
    """Base error for the integration."""


class ConfigValidationError(VivosunGrowhubError):
    """Raised when configuration input is invalid."""


class VivosunApiError(VivosunGrowhubError):
    """Base error for Vivosun REST API failures."""


class VivosunAuthError(VivosunApiError):
    """Raised when credentials or tokens are invalid/expired."""


class VivosunConnectionError(VivosunApiError):
    """Raised for transport-level failures reaching Vivosun API."""


class VivosunResponseError(VivosunApiError):
    """Raised for malformed or unexpected API responses."""
