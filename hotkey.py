"""
hotkey.py — Global hotkey listener for bud.
Ctrl+Shift+Space opens the bud window from anywhere on the desktop.
Runs in a background daemon thread.

Requires: pip install keyboard
"""

import threading
import webbrowser
import logging

logger = logging.getLogger(__name__)
_port = 8082


def _open_bud():
    webbrowser.open(f"http://127.0.0.1:{_port}")


def start(port: int = 8082):
    global _port
    _port = port

    def _run():
        try:
            import keyboard
            keyboard.add_hotkey("ctrl+shift+space", _open_bud, suppress=False)
            logger.info("Global hotkey registered: Ctrl+Shift+Space → opens bud")
            keyboard.wait()   # block until process exits
        except ImportError:
            logger.info("'keyboard' not installed — global hotkey disabled. Run: pip install keyboard")
        except Exception as exc:
            logger.warning("Global hotkey error: %s", exc)

    t = threading.Thread(target=_run, daemon=True, name="hotkey-listener")
    t.start()
