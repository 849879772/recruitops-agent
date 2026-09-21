"""Isolated, fail-closed desktop runtime (no business settings imports)."""

PROTOCOL_VERSION = 1


class RuntimeFailure(RuntimeError):
    """A stable, non-secret error code suitable for shell events."""

    def __init__(self, code: str, *, exit_code: int | None = None, os_error: int | None = None):
        self.code = code
        self.exit_code = exit_code
        self.os_error = os_error
        super().__init__(code)
