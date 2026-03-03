#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Standalone server-side speedcopy.

- Detects CIFS/SMB2 using statfs via libc.
- Uses Linux CIFS IOCTL (COPYCHUNK_FILE) for server-side copy when both sides are CIFS/SMB2.
- Falls back to sendfile(), then to shutil.copyfileobj().
- Supports file and directory copy.
"""

import os
import sys
import stat
import errno
import shutil
import time
import argparse
import ctypes
import ctypes.util
try:
    from fcntl import ioctl
except ImportError:
    ioctl = None

from pathlib import Path
from typing import Dict, Tuple

SPEEDCOPY_DEBUG = False


def debug(msg: str) -> None:
    """Print debug messages when --debug is enabled."""
    if SPEEDCOPY_DEBUG:
        print(msg)


# ----- statfs (standalone) -----
if sys.platform == "linux":
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
else:
    libc = None



class statfs_t(ctypes.Structure):
    """statfs(2) result structure (simplified; enough for f_type)."""
    _fields_ = [
        ("f_type",    ctypes.c_long),
        ("f_bsize",   ctypes.c_long),
        ("f_blocks",  ctypes.c_long),
        ("f_bfree",   ctypes.c_long),
        ("f_bavail",  ctypes.c_long),
        ("f_files",   ctypes.c_long),
        ("f_ffree",   ctypes.c_long),
        ("f_fsid",    ctypes.c_int * 2),
        ("f_namelen", ctypes.c_long),
        ("padding",   ctypes.c_char * 1024),
    ]


_FS_TYPES = {
    0xFF534D42: "CIFS",
    0xFE534D42: "SMB2",
    0xEF53: "EXT2/3/4",
    0x6969: "NFS",
    0x01021994: "TMPFS",
    0x58465342: "XFS",
}


def get_fs_magic(path: str) -> Tuple[int, str]:
    if not libc:
        return -1, "UNKNOWN"
    buf = statfs_t()
    bpath = os.fsencode(os.path.abspath(path))
    ret = libc.statfs(ctypes.c_char_p(bpath), ctypes.byref(buf))
    if ret == -1:
        err = ctypes.get_errno()
        raise OSError(err, f"statfs failed for {path}: {os.strerror(err)}")
    magic = int(buf.f_type)
    return magic, _FS_TYPES.get(magic, "UNKNOWN")


def fs_name(path: str) -> str:
    try:
        magic, name = get_fs_magic(path)
        debug(f"statfs: {path} f_type=0x{magic:08x} ({name})")
        return name
    except Exception as e:
        debug(f"statfs failed on {path}: {e}")
        return "UNKNOWN"


def is_on_project_share(path: str) -> bool:
    """Return True if path is under the known project share root.

    This bypasses statfs permission issues by assuming SMB2 for the project share.
    """
    ap = os.path.abspath(path)
    root = "/mnt/production/project"
    return ap == root or ap.startswith(root + "/")


def fs_name_for_copy(path: str) -> str:
    """Resolve filesystem name for copy logic.

    - Assume SMB2 for anything under the project share root
    - Otherwise, run statfs on a directory (never on a file path)
    """
    if is_on_project_share(path):
        return "SMB2"
    candidate = path if os.path.isdir(path) else (os.path.dirname(path) or ".")
    return fs_name(candidate)


def both_cifs_or_smb2(src: str, dst_parent: str) -> bool:
    s = fs_name_for_copy(src)
    d = fs_name_for_copy(dst_parent)
    debug(f">>> Source FS: {s}")
    debug(f">>> Destination FS: {d}")
    return s in ("CIFS", "SMB2") and d in ("CIFS", "SMB2")


def both_cifs_or_smb2_paths(src_path: str, dst_path: str) -> bool:
    """Check CIFS/SMB2 for two paths (dirs or files)."""
    s = fs_name_for_copy(src_path)
    d = fs_name_for_copy(dst_path)
    debug(f"[tree-init] Source FS: {s}")
    debug(f"[tree-init] Destination FS: {d}")
    return s in ("CIFS", "SMB2") and d in ("CIFS", "SMB2")


# ----- IOCTL helpers for CIFS COPYCHUNK_FILE -----
_IOC_NRBITS = 8
_IOC_TYPEBITS = 8
_IOC_SIZEBITS = 14
_IOC_DIRBITS = 2

_IOC_NRMASK = (1 << _IOC_NRBITS) - 1
_IOC_TYPEMASK = (1 << _IOC_TYPEBITS) - 1
_IOC_SIZEMASK = (1 << _IOC_SIZEBITS) - 1
_IOC_DIRMASK = (1 << _IOC_DIRBITS) - 1

_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS

IOC_NONE = 0
IOC_WRITE = 1
IOC_READ = 2


def _IOC(dir_: int, type_: int, nr: int, size: int) -> int:
    return ((dir_ << _IOC_DIRSHIFT) |
            (type_ << _IOC_TYPESHIFT) |
            (nr << _IOC_NRSHIFT) |
            (size << _IOC_SIZESHIFT))


def IOW(type_: int, nr: int, ctype_obj) -> int:
    size = ctypes.sizeof(ctype_obj)
    return _IOC(IOC_WRITE, type_, nr, size)


CIFS_IOCTL_MAGIC = 0xCF
CIFS_IOC_COPYCHUNK_FILE = IOW(CIFS_IOCTL_MAGIC, 3, ctypes.c_int())


# ----- Fallback fast path (sendfile) -----
try:
    _sendfile = os.sendfile
except AttributeError:
    _sendfile = None

_SENDFILE_SOFT_ERRORS = {
    getattr(errno, name, None)
    for name in ("EINVAL", "ENOSYS", "ENOTSUP", "EBADF", "ENOTSOCK", "EOPNOTSUPP")
}
_SENDFILE_SOFT_ERRORS.discard(None)


def _copyfile_sendfile(fsrc, fdst) -> bool:
    if not _sendfile:
        return False
    max_bcount = 2**31 - 1
    remaining = max_bcount
    offset = 0
    used = False
    try:
        while remaining > 0:
            sent = _sendfile(fdst.fileno(), fsrc.fileno(), offset, max_bcount)
            if sent == 0:
                break
            offset += sent
            remaining -= sent
            used = True
    except OSError as e:
        if e.errno in _SENDFILE_SOFT_ERRORS:
            debug(f"sendfile unsupported/soft error: {e.errno}")
            return False
        raise
    return used


# ----- Core copy -----

def copyfile(src: str, dst: str, follow_symlinks: bool = True, serverside_ok: bool = None) -> str:
    """Copy a single file using server-side copy on CIFS/SMB2 when possible.

    Behavior
    - If both source and destination are on CIFS/SMB2, attempt the Linux CIFS
      COPYCHUNK_FILE ioctl (server-side copy).
    - If server-side copy is not available or fails, fall back to sendfile(),
      and then to shutil.copyfileobj().
    - Ensures destination parent directory exists.

    Args:
        src: Absolute or relative path to source file.
        dst: Absolute or relative path to destination file.
        follow_symlinks: If False and src is a symlink, create a symlink at dst
            instead of copying file contents.
        serverside_ok: Optional precomputed flag indicating that both source and
            destination are on CIFS/SMB2 and server-side copy should be tried.
            If None, the function will auto-detect per-call. Pass a boolean to
            skip repeated detection in tree copies.

    Returns:
        The destination path.

    Raises:
        shutil.SameFileError: If src and dst refer to the same file.
        shutil.SpecialFileError: If src or dst is a FIFO (named pipe).
        OSError/IOError: Any underlying filesystem or IO errors encountered.

    Notes:
        - Linux-specific: server-side copy uses the CIFS client ioctl.
        - Auto-detection assumes SMB2 for paths under '/mnt/production/project'.
          Adjust fs_name_for_copy() if your environment uses a different root.
        - To verify server-side copy in logs, enable module-level debug by
          setting SPEEDCOPY_DEBUG = True before calling.
    """
    if shutil._samefile(src, dst):
        raise shutil.SameFileError(f"{src!r} and {dst!r} are the same file")

    for fn in (src, dst):
        try:
            st = os.stat(fn)
        except OSError as e:
            debug(f">>> {fn} doesn't exist [{e}]")
        else:
            if stat.S_ISFIFO(st.st_mode):
                raise shutil.SpecialFileError(f"`{fn}` is a named pipe")

    if not follow_symlinks and os.path.islink(src):
        debug(">>> creating symlink ...")
        os.symlink(os.readlink(src), dst)
        return dst

    dst_parent = os.path.dirname(os.path.abspath(dst)) or "."
    os.makedirs(dst_parent, exist_ok=True)

    if serverside_ok is None:
        serverside_ok = both_cifs_or_smb2(src, dst_parent)
    else:
        debug(f"[tree] Using precomputed serverside_ok={serverside_ok}")

    if serverside_ok:
        debug(">>> Attempting server-side CIFS copy...")
        fsrc = os.open(src, os.O_RDONLY)
        try:
            fdst = os.open(dst, os.O_WRONLY | os.O_CREAT)
        except Exception:
            os.close(fsrc)
            raise
        try:
            if ioctl is None:
                raise RuntimeError("ioctl is not available on this platform")
            ret = ioctl(fdst, CIFS_IOC_COPYCHUNK_FILE, fsrc)

            if ret == 0:
                debug(">>> Server-side copy succeeded.")
                os.close(fsrc)
                os.close(fdst)
                return dst
            else:
                debug(f"!!! Server-side copy returned {ret}, falling back.")
        except Exception as e:
            debug(f"!!! Server-side copy exception: {e}")
        finally:
            try:
                os.close(fsrc)
            except Exception:
                pass
            try:
                os.close(fdst)
            except Exception:
                pass

    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
        if not _copyfile_sendfile(fsrc, fdst):
            debug(">>> sendfile not available/failed; using copyfileobj")
            shutil.copyfileobj(fsrc, fdst)

    return dst


def copytree(src_dir: str, dst_dir: str) -> Dict[str, int]:
    t0 = time.time()
    src_path = Path(src_dir)
    dst_path = Path(dst_dir)
    if not src_path.exists():
        raise FileNotFoundError(f"Source directory does not exist: {src_dir}")
    if not src_path.is_dir():
        raise NotADirectoryError(f"Source is not a directory: {src_dir}")

    os.makedirs(dst_path, exist_ok=True)

    # Compute serverside_ok once for the whole tree
    precomputed_serverside_ok = both_cifs_or_smb2_paths(str(src_path), str(dst_path))
    debug(f"[tree-init] Precomputed serverside_ok={precomputed_serverside_ok}")

    files = 0
    dirs = 0
    bytes_copied = 0

    for root, dirnames, filenames in os.walk(str(src_path)):
        rel = os.path.relpath(root, str(src_path))
        target_root = dst_path if rel == "." else (dst_path / rel)

        for d in dirnames:
            target_dir = target_root / d
            os.makedirs(target_dir, exist_ok=True)
            dirs += 1
            debug(f"Created directory: {target_dir}")

        for fn in filenames:
            s = os.path.join(root, fn)
            d = os.path.join(str(target_root), fn)
            debug(f"Copying: {s} -> {d}")
            copyfile(s, d, serverside_ok=precomputed_serverside_ok)
            try:
                shutil.copystat(s, d)
            except Exception as e:
                debug(f"copystat failed for {d}: {e}")
            try:
                sz = os.stat(s).st_size
            except Exception:
                sz = 0
            files += 1
            bytes_copied += sz

    dt = int((time.time() - t0) * 1000)
    return {"files": files, "dirs": dirs, "bytes": bytes_copied, "duration_ms": dt}


def main() -> int:
    ap = argparse.ArgumentParser(description="Standalone serverside speedcopy")
    ap.add_argument("src", help="Source path (file or directory)")
    ap.add_argument("dst", help="Destination path (file or directory)")
    ap.add_argument("--tree", action="store_true", help="Copy a directory tree")
    ap.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = ap.parse_args()

    global SPEEDCOPY_DEBUG
    SPEEDCOPY_DEBUG = bool(args.debug)

    src = os.path.abspath(args.src)
    dst = os.path.abspath(args.dst)

    # Quick visibility on target mount
    debug(f"Mount check: /mnt/production/project -> {fs_name('/mnt/production/project')}")

    if args.tree:
        stats = copytree(src, dst)
        mb = stats["bytes"] / (1024 * 1024) if stats["bytes"] else 0.0
        secs = stats["duration_ms"] / 1000.0 if stats["duration_ms"] else 0.0
        rate = (mb / secs) if secs > 0 else 0.0
        print(f"Copied files: {stats['files']}, dirs: {stats['dirs']}, bytes: {stats['bytes']} ({mb:.2f} MB)")
        print(f"Duration: {secs:.2f}s, Avg speed: {rate:.2f} MB/s)")
        return 0
    else:
        copyfile(src, dst)
        try:
            shutil.copystat(src, dst)
        except Exception as e:
            debug(f"copystat failed: {e}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
