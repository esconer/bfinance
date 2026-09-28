"""
Direct tests for :class:`bfinance.cache.sqlite_cache.SQLiteCache`.

These did not exist. The suite's ``temp_cache`` fixture pointed at ``":memory:"``, which
``_connect()`` cannot make work (every connection is a distinct throwaway database), so the
``CREATE TABLE`` was discarded immediately and every later call raised
``"no such table: bfinance_cache"`` -- swallowed by the broad ``except Exception`` in
``get``/``set``. The result was that the cache was *exercised* on every screener test while
asserting nothing whatsoever about it, and reporting 0% meaningful coverage.

The regression that matters is :func:`test_set_then_get_returns_value`: it is the smallest
possible proof that the cache is a cache.

The fixture now uses a real on-disk database, so these tests exercise the shipped path
including the WAL and busy_timeout pragmas, which are no-ops on ``:memory:``.

NOTE for issue 06 -- two ``_connect`` gaps found while writing this file. Neither is testable
here, because proving either requires creating a file with ``:`` or ``?`` in its name, which on
Windows becomes an NTFS alternate data stream and a stray zero-byte file. Both are recorded
rather than tested, and should be fixed in ``_connect`` directly:

1. ``uri=True`` is never passed. ``sqlite3.connect(db_path, timeout=5.0)`` therefore treats a
   ``file:name?mode=memory&cache=shared`` argument as a *literal filename*, silently producing a
   real on-disk database with ``journal_mode=wal``. So "just switch the cache to shared in-memory"
   appears to work while quietly littering the filesystem. This is the trap the ``:memory:``
   fixture fell into; the shared-memory escape hatch is closed too.

2. Every caller used ``with _connect(...) as conn:``. In ``sqlite3``, a connection's context
   manager is a *transaction* manager -- it commits or rolls back, it does not close -- so no
   Python-level ``close()`` was ever called and ``get``/``set``/``clear`` appeared to leak one per
   call.

   **That inference was wrong, and is retracted.** The connections are *cyclic* garbage, not
   unrefcounted: CPython cannot reclaim them by reference counting, so the generational collector
   does, bounded by its own thresholds rather than growing with call count. Measured over 8,500
   operations: 10 live before, 55 at the instant of sampling, **1 after ``gc.collect()``**. There
   is no unbounded leak, and "needs an explicit ``contextlib.closing``" was a remedy for a problem
   that did not exist.

   ``contextlib.closing`` was added anyway, for a different and real reason:
   ``logger.exception`` on a corrupt read pins the traceback, which pins the frame, which pins the
   connection -- 50 corrupt reads left 51 *open* connections until the retained ``LogRecord``s were
   dropped. Closing explicitly makes that independent of the log handler's retention policy. Note
   the honest measurement: **open** connections, via ``SELECT 1``, not object count, because a
   closed connection still lives on as cyclic garbage and an object count would prove nothing.
"""

import json
import sqlite3
import time

import pytest

from bfinance.cache.sqlite_cache import SQLiteCache


# --------------------------------------------------------------- core round trip


def test_set_then_get_returns_value(temp_cache):
    """The regression test the broken fixture made impossible to write honestly."""
    temp_cache.set("k1", {"a": 1})
    assert temp_cache.get("k1") == {"a": 1}


def test_get_missing_key_returns_none(temp_cache):
    assert temp_cache.get("no-such-key") is None


@pytest.mark.parametrize(
    "payload",
    [
        {"nested": {"list": [1, 2, 3]}},
        [1, 2, 3],
        "a string",
        42,
        3.14,
        True,
        None,
        [],
        {},
    ],
    ids=["dict", "list", "str", "int", "float", "bool", "none", "empty-list", "empty-dict"],
)
def test_round_trips_any_json_payload(temp_cache, payload):
    temp_cache.set("k", payload)
    assert temp_cache.get("k") == payload


def test_keys_are_isolated(temp_cache):
    temp_cache.set("a", 1)
    temp_cache.set("b", 2)
    assert temp_cache.get("a") == 1
    assert temp_cache.get("b") == 2


def test_repeat_set_same_key_overwrites(temp_cache):
    """``ON CONFLICT ... DO UPDATE`` must upsert, not raise and not duplicate."""
    temp_cache.set("k", "first")
    temp_cache.set("k", "second")
    assert temp_cache.get("k") == "second"

    conn = sqlite3.connect(temp_cache.db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM bfinance_cache WHERE cache_key = 'k'").fetchone()[0]
    finally:
        conn.close()
    assert n == 1


# --------------------------------------------------------------- expiry


def test_expired_entry_is_not_returned(temp_cache):
    """A negative TTL is already expired, so this needs no sleeping."""
    temp_cache.set("k", "v", ttl_hours=-1)
    assert temp_cache.get("k") is None


def test_expired_entry_is_purged_from_disk(temp_cache):
    """An expired row must be deleted, not merely hidden -- otherwise the table grows forever."""
    temp_cache.set("k", "v", ttl_hours=-1)
    assert temp_cache.get("k") is None

    conn = sqlite3.connect(temp_cache.db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM bfinance_cache WHERE cache_key = 'k'").fetchone()[0]
    finally:
        conn.close()
    assert n == 0


def test_unexpired_entry_is_not_purged(temp_cache):
    temp_cache.set("k", "v", ttl_hours=1)
    assert temp_cache.get("k") == "v"

    conn = sqlite3.connect(temp_cache.db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM bfinance_cache WHERE cache_key = 'k'").fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_per_call_ttl_overrides_default(cache_path):
    """An explicit ``ttl_hours`` must win over ``default_ttl_hours``."""
    c = SQLiteCache(db_path=cache_path, default_ttl_hours=24.0)
    c.set("k", "v", ttl_hours=-1)
    assert c.get("k") is None

    c.set("j", "v", ttl_hours=24.0)
    assert c.get("j") == "v"


def test_expiry_is_compared_against_stored_expires_at_not_ttl_at_read_time(temp_cache):
    """Guards the boundary: expires_at is computed once, at write time."""
    now = time.time()
    temp_cache.set("k", "v", ttl_hours=1)
    conn = sqlite3.connect(temp_cache.db_path)
    try:
        created, expires = conn.execute(
            "SELECT created_at, expires_at FROM bfinance_cache WHERE cache_key = 'k'"
        ).fetchone()
    finally:
        conn.close()
    assert expires - created == pytest.approx(3600.0, abs=1.0)
    assert created == pytest.approx(now, abs=5.0)


# --------------------------------------------------------------- unserializable payloads


def test_unserializable_payload_is_skipped_not_raised(temp_cache):
    """
    ``set`` must swallow a non-JSON payload and leave the cache usable.

    A ``set`` that raises would propagate out of a screener fetch and turn a metrics
    problem into a total outage.
    """
    temp_cache.set("k", object())
    assert temp_cache.get("k") is None
    # Still usable afterwards.
    temp_cache.set("j", {"ok": True})
    assert temp_cache.get("j") == {"ok": True}


def test_unserializable_does_not_poison_an_existing_value(temp_cache):
    temp_cache.set("k", "good")
    temp_cache.set("k", object())
    assert temp_cache.get("k") == "good"


# --------------------------------------------------------------- clear


def test_clear_all_removes_every_category(temp_cache):
    temp_cache.set("a", 1, category="history")
    temp_cache.set("b", 2, category="fundamentals")
    temp_cache.clear()
    assert temp_cache.get("a") is None
    assert temp_cache.get("b") is None


def test_clear_by_category_leaves_others(temp_cache):
    temp_cache.set("a", 1, category="history")
    temp_cache.set("b", 2, category="fundamentals")
    temp_cache.clear(category="history")
    assert temp_cache.get("a") is None
    assert temp_cache.get("b") == 2


def test_clear_unknown_category_is_a_no_op(temp_cache):
    temp_cache.set("a", 1, category="history")
    temp_cache.clear(category="does-not-exist")
    assert temp_cache.get("a") == 1


def test_clear_on_empty_cache_does_not_raise(temp_cache):
    temp_cache.clear()


# --------------------------------------------------------------- persistence + schema


def test_cache_survives_a_new_client_instance(cache_path):
    """A cache that only works within one process is not a cache."""
    SQLiteCache(db_path=cache_path, default_ttl_hours=1.0).set("k", {"v": 1})
    assert SQLiteCache(db_path=cache_path, default_ttl_hours=1.0).get("k") == {"v": 1}


def test_init_db_is_idempotent(cache_path):
    """Constructing a second client over an existing file must not fail or duplicate schema."""
    SQLiteCache(db_path=cache_path)
    SQLiteCache(db_path=cache_path)
    conn = sqlite3.connect(cache_path)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    finally:
        conn.close()
    assert tables.count("bfinance_cache") == 1


def test_payload_is_stored_as_json_text_not_a_python_repr(cache_path):
    """
    The on-disk format is a contract: other tooling reads this file.

    Guards against a refactor to ``str(payload)``/``pickle``, which would break the
    documented JSON column type.
    """
    c = SQLiteCache(db_path=cache_path, default_ttl_hours=1.0)
    c.set("k", {"a": 1})
    conn = sqlite3.connect(cache_path)
    try:
        raw = conn.execute("SELECT payload FROM bfinance_cache WHERE cache_key = 'k'").fetchone()[0]
    finally:
        conn.close()
    assert isinstance(raw, str)
    assert json.loads(raw) == {"a": 1}


def test_category_defaults_to_general(temp_cache):
    temp_cache.set("k", "v")
    conn = sqlite3.connect(temp_cache.db_path)
    try:
        cat = conn.execute("SELECT category FROM bfinance_cache WHERE cache_key = 'k'").fetchone()[0]
    finally:
        conn.close()
    assert cat == "general"


# --------------------------------------------------------------- the :memory: trap, pinned


def test_memory_path_is_not_a_usable_cache():
    """
    Pins the exact bug that made the old fixture a no-op.

    This is a *characterisation* test, not a fix: ``SQLiteCache(":memory:")`` genuinely
    cannot work, because ``_connect`` opens a new connection per call and every
    ``:memory:`` database is private to its connection. It is pinned so that nobody
    "fixes" a test by pointing a fixture back at ``":memory:"``.
    """
    c = SQLiteCache(db_path=":memory:", default_ttl_hours=1.0)
    c.set("k", "v")
    assert c.get("k") is None  # the table never survived to the next connection

