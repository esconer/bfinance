"""
Prove the ``-m "not live"`` selection is genuinely network-free, on a cold cache.

A marker convention only works if nobody forgets it. Three times in this project a test
inside the required job reached the network anyway, and every one was invisible locally:

* two tests written during the sector work called ``hasattr(Ticker(...), "sector")``, which
  invokes the property getter, which calls ``_ensure_profile()`` and fetches a profile from
  screener.in. ``hasattr`` also swallows exceptions, so on a 403 they asserted almost nothing
  *while still making the request*.
* four tests passed ``actions=True`` without injecting a corporate-actions client, so
  ``CorporateActionsEngine.resolve_actions_async`` built a **real** client and called
  ``www.nseindia.com``.

The reason a warm developer machine hides all of this: those calls are satisfied by the
default ``~/.bfinance`` SQLite cache. Point ``HOME`` at an empty directory and the cache
cannot answer, so the request goes out. A CI runner is permanently in that state, which is
why this is checked there and not on a laptop.

Run directly::

    uv run python tests/hermetic_offline_suite.py

Exits non-zero, naming the offending test, if any non-loopback socket is opened.
"""

from __future__ import annotations

import os
import pathlib
import socket
import sys
import tempfile
from collections import Counter

# Redirect HOME *before* anything imports bfinance, so the real cache is invisible and
# nothing can be served from it. Defaults to a temp dir so the check never leaves a
# directory behind in the repository; CI can point BF_HERMETIC_HOME anywhere.
_DEFAULT_HOME = pathlib.Path(tempfile.gettempdir()) / "bfinance-hermetic-home"
_HOME = pathlib.Path(os.environ.get("BF_HERMETIC_HOME", str(_DEFAULT_HOME))).resolve()
_HOME.mkdir(parents=True, exist_ok=True)
(_HOME / ".bfinance").mkdir(parents=True, exist_ok=True)
os.environ["HOME"] = str(_HOME)
os.environ["USERPROFILE"] = str(_HOME)

_LOOPBACK = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}
_real_connect = socket.socket.connect
_real_getaddrinfo = socket.getaddrinfo

_current: dict[str, str | None] = {"test": None}
_attempts: Counter = Counter()


def _record(kind: str, host: str) -> None:
    _attempts[(f"{kind} {host}", _current["test"])] += 1


def _connect(self, address):  # noqa: ANN001
    host = address[0] if isinstance(address, tuple) else str(address)
    if host not in _LOOPBACK:
        _record("connect", host)
        raise AssertionError(f"external socket connect to {host!r} during {_current['test']}")
    return _real_connect(self, address)


def _getaddrinfo(host, *args, **kwargs):  # noqa: ANN001
    name = host.decode() if isinstance(host, bytes) else str(host)
    if name not in _LOOPBACK:
        _record("dns", name)
        raise AssertionError(f"external DNS lookup for {name!r} during {_current['test']}")
    return _real_getaddrinfo(host, *args, **kwargs)


socket.socket.connect = _connect
socket.getaddrinfo = _getaddrinfo

import pytest  # noqa: E402


class _TrackCurrentTest:
    """Records which test is running, so a violation is attributable."""

    def pytest_runtest_setup(self, item):  # noqa: ANN001, D102
        _current["test"] = item.nodeid


def main() -> int:
    exit_code = pytest.main(
        [
            "-p", "no:cacheprovider",
            "-m", "not live",
            "-q", "--no-header", "--tb=short",
            "tests/",
        ],
        plugins=[_TrackCurrentTest()],
    )
    socket.socket.connect = _real_connect
    socket.getaddrinfo = _real_getaddrinfo

    print(f"\n{'=' * 68}")
    print(f"offline suite exit code : {exit_code}")
    print(f"external socket attempts: {sum(_attempts.values())}")
    print("=" * 68)
    for (kind, test), count in sorted(_attempts.items()):
        print(f"  {count}x {kind}\n      during: {test}")
    if _attempts:
        print(
            "\nFAILED: the '-m \"not live\"' selection is NOT network-free.\n"
            "Each attempt above is a test that reached an upstream it should have stubbed.\n"
            "Inject the client (e.g. OHLCVEngine(..., corporate_actions_client=Stub...)),\n"
            "assert on the class rather than a Ticker instance, or add @pytest.mark.live."
        )
        return 1
    print("PASS: the offline suite opened no external socket with a cold cache.")
    return 0 if exit_code == 0 else exit_code


if __name__ == "__main__":
    sys.exit(main())
