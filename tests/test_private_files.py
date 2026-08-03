import os
from pathlib import Path

import pytest

from aiops_diagnostics.private_files import (
    PrivatePathError,
    ensure_private_directory,
    validate_private_directory,
    validate_private_file,
    write_private_text,
)


def test_private_directory_and_file_are_valid_for_current_platform(tmp_path: Path) -> None:
    directory = ensure_private_directory(tmp_path / "private")
    file = write_private_text(directory / "secret.txt", "secret\n")

    validate_private_directory(directory)
    validate_private_file(file)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode regression")
def test_posix_private_validation_rejects_group_access(tmp_path: Path) -> None:
    directory = tmp_path / "broad"
    directory.mkdir(mode=0o750)
    directory.chmod(0o750)

    with pytest.raises(PrivatePathError, match="权限过宽"):
        validate_private_directory(directory)
