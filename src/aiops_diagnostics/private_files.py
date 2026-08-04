from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


class PrivatePathError(RuntimeError):
    """A runtime secret or workspace path is not private to the current user."""


def ensure_private_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise PrivatePathError(f"私有目录不存在或是符号链接: {path}")
    if os.name == "nt":
        _restrict_windows_acl(path, directory=True)
    else:
        os.chmod(path, 0o700)
    validate_private_directory(path)
    return path


def validate_private_directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise PrivatePathError(f"私有目录不存在或不安全: {path}")
    if os.name == "nt":
        _validate_windows_acl(path)
        return
    stat = path.stat()
    if stat.st_uid != os.getuid():
        raise PrivatePathError(f"私有目录不属于当前用户: {path}")
    mode = stat.st_mode & 0o777
    if mode & 0o077:
        raise PrivatePathError(f"私有目录权限过宽: {path} mode={mode:o}")


def validate_private_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise PrivatePathError(f"私有文件不存在或不安全: {path}")
    if os.name == "nt":
        _validate_windows_acl(path)
        return
    stat = path.stat()
    if stat.st_uid != os.getuid():
        raise PrivatePathError(f"私有文件不属于当前用户: {path}")
    mode = stat.st_mode & 0o777
    if mode & 0o077:
        raise PrivatePathError(f"私有文件权限过宽: {path} mode={mode:o}")


def protect_private_file(path: Path) -> Path:
    if path.is_symlink() or not path.is_file():
        raise PrivatePathError(f"私有文件不存在或不安全: {path}")
    _restrict_private_file(path)
    validate_private_file(path)
    return path


def write_private_text(path: Path, content: str) -> Path:
    ensure_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        _restrict_private_file(temporary)
        temporary.replace(path)
        _restrict_private_file(path)
        validate_private_file(path)
        return path
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def append_private_text(path: Path, content: str) -> None:
    ensure_private_directory(path.parent)
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, content.encode("utf-8"))
    finally:
        os.close(descriptor)
    _restrict_private_file(path)


def _restrict_private_file(path: Path) -> None:
    if os.name == "nt":
        _restrict_windows_acl(path, directory=False)
    else:
        os.chmod(path, 0o600)


def _restrict_windows_acl(path: Path, *, directory: bool) -> None:
    ntsecuritycon, win32api, win32con, win32security = _win32_modules()
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        current_sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    acl = win32security.ACL()
    flags = win32con.CONTAINER_INHERIT_ACE | win32con.OBJECT_INHERIT_ACE if directory else 0
    acl.AddAccessAllowedAceEx(
        win32security.ACL_REVISION,
        flags,
        ntsecuritycon.FILE_ALL_ACCESS,
        current_sid,
    )
    win32security.SetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION
        | win32security.DACL_SECURITY_INFORMATION
        | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        current_sid,
        None,
        acl,
        None,
    )


def _validate_windows_acl(path: Path) -> None:
    ntsecuritycon, win32api, win32con, win32security = _win32_modules()
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        current_sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    descriptor = win32security.GetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
    )
    owner_sid = descriptor.GetSecurityDescriptorOwner()
    if owner_sid != current_sid:
        raise PrivatePathError(f"私有路径不属于当前 Windows 用户: {path}")
    acl = descriptor.GetSecurityDescriptorDacl()
    if acl is None:
        raise PrivatePathError(f"私有路径缺少 Windows DACL: {path}")
    current_user_has_access = False
    for index in range(acl.GetAceCount()):
        header, access_mask, sid = acl.GetAce(index)
        if header[0] != win32security.ACCESS_ALLOWED_ACE_TYPE:
            continue
        if sid != current_sid and access_mask:
            raise PrivatePathError(f"私有路径允许其他 Windows 身份访问: {path}")
        if (
            sid == current_sid
            and (access_mask & ntsecuritycon.FILE_ALL_ACCESS) == ntsecuritycon.FILE_ALL_ACCESS
        ):
            current_user_has_access = True
    if not current_user_has_access:
        raise PrivatePathError(f"当前 Windows 用户没有私有路径完全控制权: {path}")


def _win32_modules():
    try:
        import ntsecuritycon
        import win32api
        import win32con
        import win32security
    except ImportError as exc:
        raise PrivatePathError("Windows 私有目录校验需要 pywin32") from exc
    return ntsecuritycon, win32api, win32con, win32security
