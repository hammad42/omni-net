"""
main.py — entry point.

Elevation: we do NOT shell out to `powershell -Verb RunAs` (that flashes a
console and loses stdio). We re-exec ourselves through ShellExecuteW with the
"runas" verb, then exit the unelevated copy. When frozen, the PyInstaller
manifest already requests requireAdministrator, so the prompt happens before
Python starts and this path is a no-op.

Single instance: a named mutex, not a lock file. Two copies fighting over
interface metrics is the fastest way to end up with no working route at all.
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import logging.handlers
import os
import signal
import sys
from pathlib import Path

# Make `core` / `ui` importable when frozen or run from another cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import LOG_PATH, MODE_AGGREGATE, MODE_AUTO, MODE_FORCE, load as load_config, save as save_config  # noqa: E402
from core import Services  # noqa: E402
from core.netutil import IS_WINDOWS, is_admin, relaunch_as_admin  # noqa: E402

MUTEX_NAME = "Global\\OmniNetMultiWanEngine"
_mutex_handle = None


def setup_logging(verbose: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-18s %(message)s",
                            "%H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=2 << 20,
                                              backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stderr and sys.stderr.isatty():
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


def acquire_single_instance() -> bool:
    global _mutex_handle
    if not IS_WINDOWS:
        return True
    ERROR_ALREADY_EXISTS = 183
    _mutex_handle = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return ctypes.windll.kernel32.GetLastError() != ERROR_ALREADY_EXISTS


def run_cli(args) -> int:
    """Headless mode — useful for scheduled tasks and for testing the engine
    without Qt in the way."""
    cfg = load_config()
    if args.mode:
        cfg.mode = args.mode
    if args.force:
        cfg.mode = MODE_FORCE
        cfg.forced_adapter = args.force
    svc = Services(cfg)
    svc.router.on_event = lambda lvl, msg: print(f"[{lvl}] {msg}")
    svc.start()
    print(f"OmniNet running — mode={cfg.mode} "
          f"socks={cfg.proxy_host}:{cfg.socks_port} http={cfg.proxy_host}:{cfg.http_port}")
    print("Ctrl+C to stop.")
    try:
        signal.pause() if hasattr(signal, "pause") else _wait_forever()
    except KeyboardInterrupt:
        pass
    finally:
        svc.stop()
    return 0


def _wait_forever() -> None:
    import time
    while True:
        time.sleep(1)


def run_gui(args) -> int:
    from PyQt6.QtWidgets import QApplication, QMessageBox
    from ui.main_window import MainWindow
    from ui.tray import TrayController

    app = QApplication(sys.argv)
    app.setApplicationName("OmniNet")
    app.setQuitOnLastWindowClosed(False)

    cfg = load_config()
    svc = Services(cfg)
    win = MainWindow(svc)
    tray = TrayController(win)
    win._tray = tray  # keep a reference alive

    svc.start()

    if cfg.start_minimised or args.minimised:
        tray.tray.showMessage("OmniNet", "Running in the tray",
                              tray.tray.MessageIcon.Information, 2500)
    else:
        win.show()

    if not is_admin():
        QMessageBox.warning(
            win, "Not elevated",
            "OmniNet is running without administrator rights.\n\n"
            "Interface metrics, adapter repair and per-process attribution will "
            "not work. Restart it as administrator for full functionality.")

    rc = app.exec()
    svc.stop()
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description="OmniNet Multi-WAN Engine")
    ap.add_argument("--cli", action="store_true", help="headless daemon, no GUI")
    ap.add_argument("--mode", choices=[MODE_AUTO, MODE_FORCE, MODE_AGGREGATE])
    ap.add_argument("--force", metavar="ADAPTER", help="adapter name to pin traffic to")
    ap.add_argument("--minimised", action="store_true")
    ap.add_argument("--no-elevate", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(args.verbose)

    if IS_WINDOWS and not is_admin() and not args.no_elevate:
        if relaunch_as_admin():
            return 0            # the elevated copy takes over

    if not acquire_single_instance():
        print("OmniNet is already running.")
        return 1

    try:
        return run_cli(args) if args.cli else run_gui(args)
    except Exception:
        logging.exception("fatal")
        raise


if __name__ == "__main__":
    sys.exit(main())
