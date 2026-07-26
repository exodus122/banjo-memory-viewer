"""
app_paths.py - Shared helpers for resolving file paths whether the app is
run from source (`python main.py`) or as a frozen PyInstaller executable.

Two different needs:
  - resource_path(): bundled, read-only data (e.g. enums.h). When frozen
    via --onefile, PyInstaller extracts bundled data into a temp dir
    (sys._MEIPASS) that only exists for the life of the process.
  - app_dir(): a stable place to read/write user data (watches JSON, CSV
    dumps) that should persist across runs. This must be the real folder
    containing the .exe, NOT sys._MEIPASS (which is wiped on exit).
"""
import os
import sys


def app_dir():
    """Directory for user-writable files that should persist between runs."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(filename):
    """Path to a bundled read-only data file, e.g. resource_path('enums.h')."""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, filename)


def watches_dir():
    """Directory the per-profile watches JSON files live in - a "watches"
    subfolder next to main.py (or next to the .exe when frozen), created on
    first use so a fresh checkout/install doesn't need it pre-created."""
    d = os.path.join(app_dir(), "watches")
    os.makedirs(d, exist_ok=True)
    return d
