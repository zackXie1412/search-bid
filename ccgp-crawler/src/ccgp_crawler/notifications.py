from __future__ import annotations

import platform
import subprocess


def send_native_notification(title: str, message: str) -> None:
    """Send one backend-owned macOS notification without invoking a shell."""

    if platform.system() != "Darwin":
        return
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    safe_message = message.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{safe_message}" with title "{safe_title}"'
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        pass

