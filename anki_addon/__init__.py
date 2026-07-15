"""Anki integration that starts MCP AnkiConnect with the application."""

from __future__ import annotations

from pathlib import Path
import subprocess

from aqt import gui_hooks, mw
from aqt.qt import QTimer
from aqt.utils import showWarning

from .launcher import LauncherError, ServerLauncher


_launcher = ServerLauncher(Path(__file__).resolve().parent)
_STARTUP_POLL_INTERVAL_MS = 2_000
_STARTUP_POLL_ATTEMPTS = 15
_startup_scheduled = False
_quitting = False


def _warn(message: str) -> None:
    showWarning(f"MCP AnkiConnect: {message}", parent=mw)


def _check_startup(attempt: int = 1) -> None:
    if _quitting:
        return
    exit_code = _launcher.startup_exit_code()
    if exit_code is not None:
        try:
            _launcher.stop()
        except (LauncherError, OSError, subprocess.SubprocessError) as error:
            _warn(f"unable to clean up the server process tree: {error}")
        _warn(
            f"the server exited during startup with code {exit_code}. "
            f"See {_launcher.log_path} for details."
        )
        return
    if attempt < _STARTUP_POLL_ATTEMPTS:
        QTimer.singleShot(
            _STARTUP_POLL_INTERVAL_MS,
            lambda: _check_startup(attempt + 1),
        )


def _startup_finished(future) -> None:
    """Handle background completion on Anki's Qt main thread."""

    try:
        launched = future.result()
    except LauncherError as error:
        if not _quitting:
            _warn(str(error))
        return
    except Exception as error:
        if not _quitting:
            _warn(f"unexpected runtime setup failure: {error}")
        return
    if launched and not _quitting:
        QTimer.singleShot(_STARTUP_POLL_INTERVAL_MS, _check_startup)


def _start_server() -> None:
    global _startup_scheduled

    if _startup_scheduled or _quitting:
        return
    _startup_scheduled = True
    try:
        config = mw.addonManager.getConfig(__name__)
        mw.taskman.run_in_background(
            lambda: _launcher.start(config),
            _startup_finished,
        )
    except Exception as error:
        _startup_scheduled = False
        if not _quitting:
            _warn(f"unable to schedule runtime setup: {error}")


def _stop_server() -> None:
    global _quitting

    _quitting = True
    try:
        _launcher.stop()
    except (LauncherError, OSError, subprocess.SubprocessError) as error:
        _warn(f"unable to stop the server process tree: {error}")


gui_hooks.main_window_did_init.append(_start_server)
mw.app.aboutToQuit.connect(_stop_server)
