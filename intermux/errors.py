from __future__ import annotations

from typing import Any, Optional


class InterMuxError(Exception):
    """Base error. `code` is machine-readable and travels over the helper protocol."""

    code: str = "error"

    def __init__(self, message: str, code: Optional[str] = None, data: Any = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.data = data

    def __str__(self) -> str:
        return self.message


class ValidationError(InterMuxError):
    code = "invalid_request"


class PermissionDeniedError(InterMuxError):
    code = "permission_denied"


class NotFoundError(InterMuxError):
    code = "not_found"


class ConflictError(InterMuxError):
    code = "conflict"


class MissingToolError(InterMuxError):
    code = "missing_tool"


class SystemCommandError(InterMuxError):
    """A system command failed; `result` holds the CmdResult when available."""

    code = "command_failed"

    def __init__(self, message: str, result: Any = None, code: Optional[str] = None):
        super().__init__(message, code=code)
        self.result = result


class HelperError(InterMuxError):
    code = "helper_error"
