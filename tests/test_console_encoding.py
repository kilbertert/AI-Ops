from unittest.mock import Mock

from aiops_diagnostics.console_encoding import configure_windows_stdio


def test_windows_stdio_is_reconfigured_for_redirected_unicode_output() -> None:
    stdout = Mock()
    stderr = Mock()

    configure_windows_stdio((stdout, stderr), platform_name="nt")

    stdout.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")
    stderr.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")


def test_non_windows_stdio_is_unchanged() -> None:
    stdout = Mock()

    configure_windows_stdio((stdout,), platform_name="posix")

    stdout.reconfigure.assert_not_called()
