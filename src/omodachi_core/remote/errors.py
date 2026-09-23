"""One error type for the whole Remote subsystem.

``code`` is the wire code and ``status`` the HTTP status the boundary uses, so
no adapter has to translate between three private exception hierarchies.
"""
from __future__ import annotations


class RemoteError(ValueError):
    def __init__(self, code: str, status: int = 409, **detail):
        self.code, self.status, self.detail = code, status, detail
        super().__init__(code)
