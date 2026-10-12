"""Symlink-safe filesystem helpers for code that runs as root and touches user-owned paths."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from intermux.errors import ValidationError

_PARENT = os.pardir


def _is_root() -> bool:
    return os.geteuid() == 0


def is_within(path: str, base: str) -> bool:
    """True if path, with symlinks resolved, lies strictly inside base (also resolved)."""
    real_path = os.path.realpath(path)
    real_base = os.path.realpath(base)
    if real_path == real_base:
        return False
    try:
        return os.path.commonpath([real_path, real_base]) == real_base
    except ValueError:
        return False


def _open_child(parent_fd: int, name: str, uid: int, gid: int) -> int:
    """Open (creating if missing) a directory named `name` under parent_fd without following symlinks."""
    created = False
    try:
        st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        st = None
    if st is None:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        created = True
    elif stat.S_ISLNK(st.st_mode):
        raise ValidationError("symlink in profile path", code="unsafe_path")
    elif not stat.S_ISDIR(st.st_mode):
        raise ValidationError("non-directory in profile path", code="unsafe_path")

    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise ValidationError("non-directory in profile path", code="unsafe_path")
        if created and _is_root():
            os.fchown(fd, uid, gid)
    except BaseException:
        os.close(fd)
        raise
    return fd


def ensure_user_dir(path: Path, base: Path, uid: int, gid: int, mode: int = 0o700) -> None:
    """Create path (and missing parents below base) without following symlinks.

    path must lie strictly inside base. Every component below base is checked with a
    no-follow lookup relative to its parent directory fd. Created components are chowned
    to uid/gid when running as root. The final directory gets mode, and when running as
    root it is also chowned to uid/gid. All changes go through file descriptors.
    """
    raw_path = os.path.abspath(os.fspath(path))
    raw_base = os.path.abspath(os.fspath(base))
    if not is_within(raw_path, raw_base):
        raise ValidationError("profile path is outside the user's home", code="unsafe_path")
    rel = os.path.relpath(raw_path, raw_base)
    if rel in (".", "") or rel == _PARENT or rel.startswith(_PARENT + os.sep):
        raise ValidationError("profile path is outside the user's home", code="unsafe_path")

    dirfd = os.open(os.path.realpath(raw_base), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in Path(rel).parts:
            child = _open_child(dirfd, name, uid, gid)
            os.close(dirfd)
            dirfd = child
        if _is_root():
            os.fchown(dirfd, uid, gid)
        os.fchmod(dirfd, mode)
    finally:
        os.close(dirfd)
