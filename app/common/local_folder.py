"""Open an application-owned directory on the backend host."""

import os
import subprocess
import sys


def reveal_in_file_manager(path: str) -> tuple[bool, str]:
    """Callers must resolve and validate the directory before calling this helper."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)
        else:
            command = "open" if sys.platform == "darwin" else "xdg-open"
            process = subprocess.Popen([command, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                code = process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                # Some file managers remain attached while their window is open.
                return True, ""
            if code:
                return False, f"{command} exited with status {code}"
        return True, ""
    except Exception as exc:
        return False, str(exc)
