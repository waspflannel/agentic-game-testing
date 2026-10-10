"""Shared pytest setup.

The controller rejects every request whose Host is not loopback (DNS rebinding
defense in ashad/app.py), and Starlette's TestClient defaults to Host
"testserver". Rather than allowlisting a test-only host in production, every
TestClient here defaults to http://127.0.0.1 — what a real browser sends to the
local controller. Tests may still pass an explicit base_url (or Host header) to
exercise the boundary itself; httpx.AsyncClient users must choose a loopback
base_url themselves.
"""
import functools

from starlette.testclient import TestClient

_init = TestClient.__init__


@functools.wraps(_init)
def _loopback_init(self, app, base_url="http://127.0.0.1", *args, **kwargs):
    _init(self, app, base_url, *args, **kwargs)


TestClient.__init__ = _loopback_init


import keyring
import pytest


def _refuse_keyring(*args, **kwargs):
    raise AssertionError("test touched the real keyring")


@pytest.fixture(autouse=True)
def no_real_claude_or_keyring(monkeypatch):
    for name in ("get_password", "set_password", "delete_password"):
        monkeypatch.setattr(keyring, name, _refuse_keyring)
