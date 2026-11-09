"""Storage for the session token.

The token is a credential *and* carries personal data in its payload, so the
stores never print it: their ``repr`` only says whether a token is present.
"""

import contextlib
import os
from pathlib import Path
from typing import Protocol

from asw_bot.config import Settings


class TokenStore(Protocol):
    """Anything that can keep a token between runs."""

    def load(self) -> str | None: ...

    def save(self, token: str) -> None: ...

    def clear(self) -> None: ...


class FileTokenStore:
    """Token in a file only the current user can read (mode 600).

    On Windows ``chmod`` cannot express that; the file is protected by the
    ACLs of the user profile instead, which is why the default path lives in
    the home directory and not in the project.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def __repr__(self) -> str:
        return f"FileTokenStore(path={str(self._path)!r})"

    def load(self) -> str | None:
        try:
            token = self._path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        return token or None

    def save(self, token: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self._path.with_name(self._path.name + ".tmp")
        # Create with 600 from the start: the token is never readable by others,
        # not even for an instant. Then replace atomically.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token)
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)

    def clear(self) -> None:
        self._path.unlink(missing_ok=True)


class MemoryTokenStore:
    """Token kept in memory, optionally seeded (e.g. from the ASW_TOKEN variable).

    Used on Railway, where the container's disk does not survive the run, and in tests.
    """

    def __init__(self, token: str | None = None) -> None:
        self._token = token

    def __repr__(self) -> str:
        return f"MemoryTokenStore(has_token={self._token is not None})"

    def load(self) -> str | None:
        return self._token

    def save(self, token: str) -> None:
        self._token = token

    def clear(self) -> None:
        self._token = None


def build_token_store(settings: Settings) -> TokenStore:
    """Pick the store from the configuration: env token if given, else the file."""
    if settings.asw_token is not None:
        return MemoryTokenStore(settings.asw_token.get_secret_value())
    return FileTokenStore(settings.token_file)
