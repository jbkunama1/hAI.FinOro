# app/etoro/errors.py
"""Custom exceptions for eToro client."""

class EtoroApiError(Exception):
    """Base exception for eToro API errors."""
    def __init__(self, message: str, status_code: int = None, response_data: dict = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_data = response_data


class RateLimitError(EtoroApiError):
    """Raised when API rate limit is exceeded."""
    def __init__(self, message: str = "Rate limit exceeded", retry_after: int = None, **kwargs):
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class SafetyViolation(Exception):
    """Raised when a safety guard blocks an operation."""
    def __init__(self, guard_name: str, reason: str, details: dict = None):
        super().__init__(f"Safety violation [{guard_name}]: {reason}")
        self.guard_name = guard_name
        self.reason = reason
        self.details = details or {}
