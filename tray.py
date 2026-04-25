"""
tray.py — System tray icon for bud.
Runs in a background thread so it doesn't block the FastAPI server.
Icon: gold dot on dark background (generated programmatically — no file needed).
"""

import threading
import webbrowser
import os
import logging

logger = logging.getLogger(__name__)

_tray_thread: threading.Thread | None = None
_tray_icon   = None


def _make_icon():
    """Generate a simple gold 'b' icon using Pillow."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        size = 64
        img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        # Dark circle background
        draw.ellipse([2, 2, size - 2, size - 2], fill="#0e0c0a", outline="#c8a45e", width=2)
        # Gold 'b' letter
        try:
            font = ImageFont.truetype("arial.ttf", 34)
        except Exception:
            font = ImageFont.load_default()
        draw.text((size // 2, size // 2), "b", font=font, fill="#c8a45e", anchor="mm")
        return img
    except Exception as exc:
        logger.warning("Could not create tray icon image: %s", exc)
        return None


def _build_menu(host: str, port: int):
    try:
        import pystray
        from pystray import MenuItem as Item

        def on_open(_icon, _item):
            webbrowser.open(f"http://{host}:{port}")

        def on_new_chat(_icon, _item):
            webbrowser.open(f"http://{host}:{port}")

        def on_quit(_icon, _item):
            _icon.stop()
            # Ask uvicorn to exit
            os.kill(os.getpid(), 15)  # SIGTERM

        return pystray.Menu(
            Item("Open bud",    on_open, default=True),
            Item("New Chat",    on_new_chat),
            pystray.Menu.SEPARATOR,
            Item("Quit",        on_quit),
        )
    except ImportError:
        return None


def start(host: str = "127.0.0.1", port: int = 8000):
    """Start the system tray icon in a background thread."""
    global _tray_thread, _tray_icon

    try:
        import pystray
    except ImportError:
        logger.info("pystray not installed — system tray disabled. Run: pip install pystray")
        return

    img    = _make_icon()
    if img is None:
        return

    menu = _build_menu(host, port)
    if menu is None:
        return

    _tray_icon = pystray.Icon(
        name  = "bud",
        icon  = img,
        title = "bud — Personal AI Agent",
        menu  = menu,
    )

    def _run():
        try:
            _tray_icon.run()
        except Exception as exc:
            logger.warning("Tray icon error: %s", exc)

    _tray_thread = threading.Thread(target=_run, daemon=True)
    _tray_thread.start()
    logger.info("System tray icon started")


def stop():
    global _tray_icon
    if _tray_icon:
        try:
            _tray_icon.stop()
        except Exception:
            pass
        _tray_icon = None
