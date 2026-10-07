"""Idle-only preparation evidence: hashes and native X11 reads, never game/input calls."""
from __future__ import annotations

import asyncio
import ctypes as C
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import shutil
import sys

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

from .models import utc_now
from .runtime import executable_allowed, sha256_file


class PrepareProbe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    build: dict


def _idle_reason(service):
    if service.closed:
        return "Worker is closing"
    # Runtime.stop retains the historical binding; confirmed Ended is reusable.
    if service.binding is not None and service.lifecycle != "ended":
        return "Worker still has an active run binding"
    if service.game_running or service.lifecycle not in {"unbound", "ended"}:
        return "Worker has game or lifecycle ownership"
    if service.lifecycle == "ended" and not (service.termination_result or {}).get("confirmed"):
        return "Worker teardown is unconfirmed"
    if service.human_control or service.desktop_session or service.steam_setup or any(p.returncode is None for p in service.setup_processes):
        return "Human desktop or Steam setup is active"
    if service.held_keys or service.held_buttons:
        return "Worker has held inputs"
    if service.infrastructure_error:
        return "Worker infrastructure requires reconciliation"
    return None


@lru_cache(maxsize=32)
def _cached_sha256(path, size, mtime_ns):
    return sha256_file(Path(path))


def _sha256(path):
    """Probes repeat; large packs are rehashed only when the mounted file's size/mtime change."""
    info = path.stat()
    return _cached_sha256(str(path), info.st_size, info.st_mtime_ns)


def measure_build(build):
    """Same launch paths/root policy and hash function as Runtime; no execution."""
    if type(build.get("structured_version")) is not int or build["structured_version"] != 1 or type(build.get("integration_version")) is not int or build["integration_version"] != 2:
        raise ValueError("Unsupported structured/integration version; requires 1/2")
    if build.get("engine_version") != "GodotSteam 4.5.1 / 4.17 / Steamworks 1.63":
        raise ValueError("Unsupported pinned engine version; requires GodotSteam 4.5.1 / 4.17 / Steamworks 1.63")
    command = build.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(v, str) for v in command) or command.count("--main-pack") != 1:
        raise ValueError("Supported executable/main-pack command is missing")
    position = command.index("--main-pack") + 1
    if position >= len(command):
        raise ValueError("Pinned game pack is missing")
    paths = {"engine_sha256": Path(command[0]), "artifact_sha256": Path(command[position])}
    for key, path in paths.items():
        if not path.is_absolute() or ".." in path.parts or not executable_allowed(path) or not path.is_file():
            raise ValueError(f"{key} file is missing or outside approved build/runtime roots")
    executable, pack = paths["engine_sha256"], paths["artifact_sha256"]
    if not os.access(executable, os.X_OK):
        raise ValueError("Pinned engine is not executable")
    manifest_path = pack.parent / "manifest.json"
    if not executable_allowed(manifest_path) or not manifest_path.is_file() or manifest_path.stat().st_size > 2_000_000:
        raise ValueError("Mounted build manifest is missing or invalid")
    expected_manifest = build.get("manifest_sha256")
    if not isinstance(expected_manifest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", expected_manifest) or _sha256(manifest_path) != expected_manifest.lower():
        raise ValueError("Mounted manifest identity is missing or mismatched")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if not isinstance(manifest, dict):
        raise ValueError("Mounted build manifest is not an object")
    for key in ("command", "structured_version", "integration_version", "source_revision", "engine_version"):
        if not build.get(key) or type(manifest.get(key)) is not type(build[key]) or manifest.get(key) != build[key]:
            raise ValueError(f"Mounted {key} pin is missing or mismatched")
    measured = {key: _sha256(path) for key, path in paths.items()}
    for key, actual in measured.items():
        expected = build.get(key)
        mounted = manifest.get(key)
        if not isinstance(expected, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", expected) or actual != expected.lower() or not isinstance(mounted, str) or mounted.lower() != actual:
            raise ValueError(f"Measured {key} pin is missing or mismatched")
    return {**measured, "executable_sha256": measured["engine_sha256"], "manifest_sha256": expected_manifest.lower(),
            **{key: manifest[key] for key in ("structured_version", "integration_version", "source_revision", "engine_version")},
            "command": command, "verified_at": utc_now()}


class _XImage(C.Structure):
    # Public Xlib XImage prefix; function table is unused.
    _fields_ = [("width", C.c_int), ("height", C.c_int), ("xoffset", C.c_int), ("format", C.c_int),
                ("data", C.c_void_p), ("byte_order", C.c_int), ("bitmap_unit", C.c_int),
                ("bitmap_bit_order", C.c_int), ("bitmap_pad", C.c_int), ("depth", C.c_int),
                ("bytes_per_line", C.c_int), ("bits_per_pixel", C.c_int)]


def _function(library, name, arguments, result):
    function = getattr(library, name)
    function.argtypes, function.restype = arguments, result
    return function


# Xlib's default error/IO-error handlers exit() the whole worker; record and fail the probe instead.
_X_ERRORS: list[str] = []
_ERROR_HANDLER = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_void_p)(lambda display, event: _X_ERRORS.append("X11 protocol error during probe") or 0)
_IO_ERROR_HANDLER = C.CFUNCTYPE(C.c_int, C.c_void_p)(lambda display: _X_ERRORS.append("X11 connection lost during probe") or 0)
_IO_EXIT_HANDLER = C.CFUNCTYPE(None, C.c_void_p, C.c_void_p)(lambda display, data: _X_ERRORS.append("X11 connection lost during probe"))


def _x_check():
    if _X_ERRORS:
        raise RuntimeError(_X_ERRORS[0])


def probe_desktop(desktop):
    """Read root pixels with the Xlib installed for Xvfb/xdotool. No focus or events."""
    result = {"rendered": False, "capture_ready": False, "input_ready": False, "checks": {}}
    if sys.platform != "linux" or not Path("/.dockerenv").is_file() or not re.fullmatch(r":\d+", desktop.display):
        result["checks"]["rendered"] = "Requires the private local Linux Docker X11 desktop"
        return result
    display = image = None
    try:
        x11 = C.CDLL("libX11.so.6")
        open_display = _function(x11, "XOpenDisplay", [C.c_char_p], C.c_void_p)
        close_display = _function(x11, "XCloseDisplay", [C.c_void_p], C.c_int)
        root_window = _function(x11, "XDefaultRootWindow", [C.c_void_p], C.c_ulong)
        geometry = _function(x11, "XGetGeometry", [C.c_void_p, C.c_ulong, C.POINTER(C.c_ulong), C.POINTER(C.c_int), C.POINTER(C.c_int), *([C.POINTER(C.c_uint)] * 4)], C.c_int)
        get_image = _function(x11, "XGetImage", [C.c_void_p, C.c_ulong, C.c_int, C.c_int, C.c_uint, C.c_uint, C.c_ulong, C.c_int], C.POINTER(_XImage))
        destroy_image = _function(x11, "XDestroyImage", [C.POINTER(_XImage)], C.c_int)
        _X_ERRORS.clear()
        _function(x11, "XSetErrorHandler", [C.c_void_p], C.c_void_p)(C.cast(_ERROR_HANDLER, C.c_void_p))
        _function(x11, "XSetIOErrorHandler", [C.c_void_p], C.c_void_p)(C.cast(_IO_ERROR_HANDLER, C.c_void_p))
        set_io_exit = _function(x11, "XSetIOErrorExitHandler", [C.c_void_p, C.c_void_p, C.c_void_p], None)  # libX11 >= 1.7
        display = open_display(desktop.display.encode())
        if not display:
            raise RuntimeError("Private X11 display is unavailable")
        set_io_exit(display, C.cast(_IO_EXIT_HANDLER, C.c_void_p), None)
        root = root_window(display)
        returned_root, x, y = C.c_ulong(), C.c_int(), C.c_int()
        width, height, border, depth = C.c_uint(), C.c_uint(), C.c_uint(), C.c_uint()
        if not geometry(display, root, C.byref(returned_root), C.byref(x), C.byref(y), C.byref(width), C.byref(height), C.byref(border), C.byref(depth)):
            raise RuntimeError("X11 root geometry is unavailable")
        if (width.value, height.value) != (desktop.width, desktop.height) or not (0 < width.value <= 8192 and 0 < height.value <= 8192 and depth.value >= 16):
            raise RuntimeError("Rendered root dimensions/depth do not match the configured desktop")
        image = get_image(display, root, 0, 0, width.value, height.value, C.c_ulong(-1).value, 2)
        if not image:
            raise RuntimeError("X11 root pixel capture failed")
        frame = image.contents
        size = frame.bytes_per_line * frame.height
        if (frame.width, frame.height) != (width.value, height.value) or frame.bits_per_pixel not in {16, 24, 32} or frame.bytes_per_line < (frame.width * frame.bits_per_pixel + 7) // 8 or not (0 < size <= 64 * 1024**2) or not frame.data:
            raise RuntimeError("X11 root capture buffer is invalid")
        pixels = C.string_at(frame.data, size)
        if len(pixels) != size:
            raise RuntimeError("X11 root pixel read is incomplete")
        _x_check()
        result.update(rendered=True, capture_ready=True, width=frame.width, height=frame.height, capture_bytes=len(pixels), depth=depth.value, capture_backend="X11 root XGetImage")
        result["checks"].update(rendered="Root geometry and actual pixel read verified", capture_ready="Root capture dimensions and byte buffer verified")
        del pixels  # Pixels are neither persisted nor returned to the controller.
        try:
            xtst = C.CDLL("libXtst.so.6")
            query = _function(xtst, "XTestQueryExtension", [C.c_void_p, *([C.POINTER(C.c_int)] * 4)], C.c_int)
            event, error, major, minor = C.c_int(), C.c_int(), C.c_int(), C.c_int()
            if not query(display, C.byref(event), C.byref(error), C.byref(major), C.byref(minor)):
                raise RuntimeError("XTEST extension is unavailable on the rendered display")
            _x_check()
            # Resolve the installed input API without calling any injection function.
            for name in ("XTestFakeKeyEvent", "XTestFakeButtonEvent", "XTestFakeMotionEvent"):
                getattr(xtst, name)
            if not shutil.which("xdotool"):
                raise RuntimeError("Runtime xdotool control API is unavailable")
            result.update(input_ready=True, input_backend="XTEST/xdotool", xtest_version=f"{major.value}.{minor.value}")
            result["checks"]["input_ready"] = "Live XTEST extension and installed control API verified; no input sent"
        except (OSError, AttributeError, RuntimeError) as exc:
            result["checks"]["input_ready"] = str(exc)
    except (OSError, AttributeError, RuntimeError) as exc:
        result["checks"]["rendered"] = result["checks"]["capture_ready"] = str(exc)
    finally:
        if image:
            destroy_image(image)
        if display:
            close_display(display)
    for key in ("rendered", "capture_ready", "input_ready"):
        result["checks"].setdefault(key, "Capability not measured")
    return result


async def prepare_probe(service, build):
    if reason := _idle_reason(service):  # Busy workers are rejected before any reads.
        raise HTTPException(409, reason)
    checks = {}

    def hash_build():
        try:
            measured = measure_build(build)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            checks["build"] = {"ready": False, "reason": str(exc)}
            return None
        checks["build"] = {"ready": True, "reason": "Mounted manifest and actual pack/engine hashes match the supported pinned integration"}
        return measured

    # Hashing reads only the read-only build mount, so lifecycle commands are not blocked behind it.
    measured = await asyncio.to_thread(hash_build)
    # Hold the lifecycle lock for the idle recheck and native desktop reads, including HTTP cancellation.
    async with service.state_lock:
        if reason := _idle_reason(service):
            raise HTTPException(409, reason)
        status = service.status()
        result = {**status, "ready": False, "independent_desktop": False, "build": measured,
                  "desktop": {"rendered": False, "capture_ready": False, "input_ready": False}, "checks": checks}
        result["steam"] = {"setup_active": False, "authenticated": False, "identity_verified": False,
                           "app_id_verified": False, "license_verified": False, "requires_human_setup": True,
                           "verified": False, "reason": "unknown: fresh Steam identity/license evidence requires game initialization; passive probe never launches or signs in"}

        def read_desktop():
            if not measured:
                return
            result["desktop"] = probe_desktop(service.desktop)
            for key in ("rendered", "capture_ready", "input_ready"):
                result["checks"][key] = {"ready": result["desktop"][key], "reason": result["desktop"]["checks"].get(key, "Capability not measured")}

        task = asyncio.create_task(asyncio.to_thread(read_desktop))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
        result["ready"] = bool(result["build"] and all(result["desktop"][key] for key in ("rendered", "capture_ready", "input_ready")))
        result["independent_desktop"] = bool(result["desktop"]["rendered"] and result["desktop"]["capture_ready"])
        result["detail"] = "; ".join(check["reason"] for check in result["checks"].values() if not check["ready"]) or "Read-only build and desktop capabilities verified; gameplay untested"
        result["timestamp"] = utc_now()
        return result
