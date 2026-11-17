from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_secret_file(path: Path, content: str) -> None:
    """Writes `content` to `path` so it's 0600 for the entire time anything
    is visible there, and without ever following a pre-existing symlink at
    `path` - a local attacker who can already write inside this directory
    could otherwise plant one pointing elsewhere, and a plain
    `path.write_text()` would silently write through it (TOCTOU: create at
    the process' default umask, then chmod after the fact, leaves a window
    where the file is wider than 0600 and gives a symlink nothing to stop
    it). Writes to a randomly-named sibling temp file first -
    tempfile.mkstemp() creates it with O_EXCL and mode 0600 up front, so a
    pre-planted symlink/file at that exact name is refused outright rather
    than followed - then atomically renames it over `path`: os.replace() on
    POSIX replaces whatever directory entry is currently at the
    destination (a regular file or a symlink) without ever dereferencing
    it, so even a symlink previously sitting at `path` itself is simply
    replaced, never traversed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        os.unlink(tmp_name)
        raise
