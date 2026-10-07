"""Normal X11 input and capture, scoped to the container's private DISPLAY."""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path


ALIASES = {
    "ENTER": "Return", "RETURN": "Return", "ESC": "Escape", "ESCAPE": "Escape",
    "SPACE": "space", "TAB": "Tab", "BACKSPACE": "BackSpace", "DELETE": "Delete",
    "CTRL": "Control_L", "CONTROL": "Control_L", "SHIFT": "Shift_L", "ALT": "Alt_L",
    "META": "Super_L", "SUPER": "Super_L", "UP": "Up", "DOWN": "Down", "LEFT": "Left",
    "RIGHT": "Right", "ARROWUP": "Up", "ARROWDOWN": "Down", "ARROWLEFT": "Left", "ARROWRIGHT": "Right",
    "HOME": "Home", "END": "End", "PAGEUP": "Prior", "PAGEDOWN": "Next",
    "INSERT": "Insert", "CAPSLOCK": "Caps_Lock",
}


def key_name(key: str) -> str:
    if key.upper() in ALIASES:
        return ALIASES[key.upper()]
    if re.fullmatch(r"[A-Za-z0-9]", key) or re.fullmatch(r"F(?:[1-9]|1[0-2])", key):
        return key
    if key in set(ALIASES.values()) | {"Control_R", "Shift_R", "Alt_R", "Super_R", "minus", "equal", "comma", "period", "slash", "semicolon", "apostrophe", "bracketleft", "bracketright", "backslash", "grave"}:
        return key
    raise ValueError(f"Unsupported key: {key!r}")


def button_number(button: int | str) -> int:
    names = {"left": 1, "middle": 2, "right": 3, "back": 8, "forward": 9}
    number = names.get(str(button).lower(), button)
    if str(number) not in {"1", "2", "3", "8", "9"}:
        raise ValueError("Unsupported mouse button")
    return int(number)


class Desktop:
    def __init__(self, display: str = ":99", width: int = 1280, height: int = 720):
        self.display, self.width, self.height = display, width, height
        self.env = {**os.environ, "DISPLAY": display}
        self.capture_quality = 75

    async def command(self, *args: str, timeout: float = 5) -> bytes:
        proc = await asyncio.create_subprocess_exec(*args, env=self.env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            output, error = await asyncio.wait_for(proc.communicate(), timeout)
        except BaseException:
            proc.kill()
            await proc.wait()
            raise
        if proc.returncode:
            raise RuntimeError(error.decode(errors="replace")[-1000:] or f"{args[0]} exited {proc.returncode}")
        return output

    async def input(self, *args: str) -> None:
        await self.command("xdotool", *map(str, args))

    async def focus(self, pid: int) -> None:
        windows = (await self.command("xdotool", "search", "--onlyvisible", "--pid", str(pid))).decode().split()
        if not windows:
            raise RuntimeError("The game has no visible window")
        await self.input("windowactivate", "--sync", windows[-1])

    async def capture(self, destination: Path) -> None:
        qscale = str(max(2, min(31, round(31 - self.capture_quality * 0.29))))
        await self.command("ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "x11grab", "-video_size", f"{self.width}x{self.height}", "-i", self.display, "-frames:v", "1", "-threads", "1", "-q:v", qscale, str(destination), timeout=10)

    async def live_video(self) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "x11grab", "-video_size", f"{self.width}x{self.height}",
            "-framerate", "15", "-i", self.display, "-an", "-vf", "scale=960:-2",
            "-c:v", "mjpeg", "-q:v", "6", "-threads", "1",
            "-f", "mpjpeg", "-boundary_tag", "frame", "pipe:1",
            env=self.env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

    async def release_all(self, keys: set[str], buttons: set[int]) -> None:
        # Include common modifiers even after a controller crash, when local held state is lost.
        for key in sorted(keys | {"Control_L", "Control_R", "Shift_L", "Shift_R", "Alt_L", "Alt_R", "Super_L", "Super_R"}):
            try:
                await self.input("keyup", key)
            except (RuntimeError, OSError):
                pass
        for button in sorted(buttons | {1, 2, 3}):
            try:
                await self.input("mouseup", str(button))
            except (RuntimeError, OSError):
                pass

    async def video(self, directory: Path, segment_seconds: int = 5, fps: int = 8) -> asyncio.subprocess.Process:
        directory.mkdir(parents=True, exist_ok=True)
        log = (directory.parent / "capture.log").open("ab")
        try:
            return await asyncio.create_subprocess_exec(
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning", "-y", "-f", "x11grab",
                "-video_size", f"{self.width}x{self.height}", "-framerate", str(fps), "-i", self.display,
                "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", str(round(40-self.capture_quality/5)), "-pix_fmt", "yuv420p", "-threads", "2",
                "-force_key_frames", f"expr:gte(t,n_forced*{segment_seconds})", "-f", "segment", "-segment_time", str(segment_seconds),
                "-segment_format", "mpegts", "-reset_timestamps", "1", "-strftime", "1", str(directory / "%Y%m%dT%H%M%S.ts"),
                env=self.env, stdout=asyncio.subprocess.DEVNULL, stderr=log)
        finally:
            log.close()
