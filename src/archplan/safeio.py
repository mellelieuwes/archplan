"""Reading repository files, which are untrusted.

A cloned repository can hold a symlink to /dev/zero or to a file outside
the repository, a FIFO that never ends, or a file of many gigabytes. Every
read of repository content goes through read_file or read_text: they read
only regular files that are not symlinks, and at most limit bytes. A larger
file is skipped, not truncated. Directory walks go through walk, which
never follows a symlinked directory."""

from __future__ import annotations

import bisect
import os
import re
import stat
from pathlib import Path

MAX_READ = 2 << 20  # 2 MB

# git options for running git in a repository: no fsmonitor or hook command
# from the repository's .git/config runs.
GIT_SAFE = ["-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "log.showSignature=false"]


class Unsafe(OSError):
    """The path is not a regular file inside the repository, or it is too large."""


def inside(root: Path, rel) -> Path | None:
    """root/rel when it stays inside root, symlinks included; None otherwise."""
    rel = str(rel).replace(os.sep, "/")
    if not rel or rel.startswith("/") or ".." in rel.split("/"):
        return None
    fp = Path(root) / rel
    try:
        fp.resolve().relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return None
    return fp


def read_bytes(path, limit: int = MAX_READ, truncate: bool = False) -> bytes:
    """The bytes of a regular file that is not a symlink, at most limit of
    them. Raises Unsafe (an OSError) for anything else, and for a larger file
    unless truncate is set: then it returns the first limit bytes."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise Unsafe(f"{path}: not a regular file")
    if st.st_size > limit and not truncate:
        raise Unsafe(f"{path}: larger than {limit} bytes")
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)  # the file opened, which may differ from the one checked
        if not stat.S_ISREG(st.st_mode):
            raise Unsafe(f"{path}: not a regular file")
        chunks, n = [], 0
        while n <= limit:
            b = os.read(fd, min(1 << 20, limit + 1 - n))
            if not b:
                break
            chunks.append(b)
            n += len(b)
        data = b"".join(chunks)
        if n > limit:
            if not truncate:
                raise Unsafe(f"{path}: larger than {limit} bytes")
            data = data[:limit]
        return data
    finally:
        os.close(fd)


def read_file(path, limit: int = MAX_READ, encoding: str = "utf-8", errors: str = "replace",
              truncate: bool = False) -> str:
    """read_bytes, decoded."""
    return read_bytes(path, limit, truncate).decode(encoding, errors)


def read_text(root: Path, rel, limit: int = MAX_READ, encoding: str = "utf-8", errors: str = "replace",
              truncate: bool = False) -> str:
    """The text of root/rel; rel is repository data, so a path that leaves
    the repository raises Unsafe, as read_file does for a symlink, a device,
    a FIFO or a file larger than limit."""
    fp = inside(root, rel)
    if fp is None:
        raise Unsafe(f"{rel}: outside the repository")
    return read_file(fp, limit, encoding, errors, truncate)


def is_file(path) -> bool:
    """Whether path is a regular file and not a symlink."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def walk(root, skip=(), hidden: bool = False):
    """os.walk that never follows symlinked directories, sorted, leaving out
    the folder names in skip and, unless hidden, names starting with a dot."""
    for d, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(x for x in dirs if x not in skip and (hidden or not x.startswith("."))
                         and not os.path.islink(os.path.join(d, x)))
        yield d, dirs, sorted(names)


def files(root, suffix: str, skip=()) -> list:
    """Regular files under root whose name ends with suffix, as walk finds them:
    a safe replacement for Path.rglob."""
    out = []
    for d, _, names in walk(root, skip):
        out.extend(Path(d) / n for n in names if n.endswith(suffix) and is_file(os.path.join(d, n)))
    return out


_lines = (None, [])


def line_at(text: str, pos: int) -> int:
    """The 1-based line of offset pos in text. The newline offsets of the last
    text are kept, so calling this for every match in a file stays linear
    (text.count from 0 for each match is quadratic)."""
    global _lines
    t, nl = _lines
    if t is not text:
        nl = [m.start() for m in re.finditer("\n", text)]
        _lines = (text, nl)
    return bisect.bisect_left(nl, pos) + 1
