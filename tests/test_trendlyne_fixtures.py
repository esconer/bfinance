"""
trendlyne fixture provenance -- fully offline, no network, no ``live`` marker.

Every file under ``tests/fixtures/trendlyne/`` is a recording of something trendlyne.com actually
served. The parsers built on top of them (``trendlyne/tables.py``, and later the section routes)
are only as honest as those bytes, so this module treats the fixture set as evidence rather than
as test data:

* ``META.json`` must account for **every** file on disk, in both directions -- a payload nobody
  recorded is a payload whose provenance nobody can state, and an entry for a file that is gone
  is a claim about bytes that cannot be checked.
* Each entry's ``stored_sha256`` must match the file. Git checks these out with CRLF on Windows
  and LF elsewhere (``.gitattributes`` pins ``eol=lf`` under ``tests/fixtures/``), so the sha is
  computed over the **normalised** content: a raw size assertion passes in the working tree where
  Python wrote the file and then fails on any fresh clone, which is the "green locally, red in
  CI" failure the offline suite exists to prevent.
* Every ``.html`` fixture must parse with the **shipped** reader, ``tables.find_tables``, without
  raising. A fixture that the shipped parser cannot read is not evidence for that parser, and the
  failure it causes in a later task is an opaque ``TrendlyneSchemaError`` from a table index.
* Every ``.json`` fixture must load and carry the trendlyne envelope. Success on this site is
  ``head.status == "0"``, so a payload without ``head`` is not a payload this client can judge.

The 2026-10-10 batch covers the section routes that were fixtures-only before. One file,
``sitemap_excerpt_tcs.xml``, is a documented exception: it is a byte-identical copy of the
2026-09-27 excerpt, because recording a fresh TCS window would have needed a network request.
Its ``notes`` say so, its ``captured_at_utc`` is the 2026-09-27 timestamp rather than a borrowed
one, and :class:`TestTheNewRouteFixturesArePresent` pins that discrepancy instead of hiding it.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from bfinance.trendlyne import tables as tbt

FIXTURES = Path(__file__).parent / "fixtures" / "trendlyne"
META = json.loads((FIXTURES / "META.json").read_text(encoding="utf-8"))
ENTRIES: dict[str, dict] = META["files"]

#: The contract from the plan: every one of these is mandatory, ``trim_rule`` most of all --
#: a trimmed recording whose trim is undocumented is a paraphrase wearing a recording's clothes.
REQUIRED_KEYS = (
    "url",
    "http_status",
    "captured_at_utc",
    "original_bytes",
    "original_sha256_16",
    "stored_bytes",
    "trim_rule",
    "notes",
    "stored_sha256",
    "stored_line_endings",
)

ENTRY_NAMES = sorted(ENTRIES)
HTML_NAMES = sorted(p.name for p in FIXTURES.glob("*.html"))
JSON_NAMES = sorted(p.name for p in FIXTURES.glob("*.json") if p.name != "META.json")

#: What the 2026-10-10 recording pass was asked to produce. Named here because a fixture set is
#: only useful if the next task can rely on it: a missing name is a silent gap in the suite.
NEW_ROUTE_FIXTURES = (
    "adv_technical_analysis_ntpc.json",
    "adv_technical_analysis_tcs.json",
    "board_meeting_tcs.html",
    "buy_sell_zone_tcs.html",
    "insider_trading_tcs.html",
    "latest_news_tcs.html",
    "monthly_mf_holding_tcs.html",
    "option_chain_tcs.json",
    "overview_second_part_tcs.json",
    "price_performance_tcs.json",
    "research_reports_tcs.html",
    "shareholding_tcs.html",
    "sitemap_excerpt_tcs.xml",
    "stock_alerts_tcs.html",
    "swot_tcs.html",
)


def stored_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ============================================================ the manifest itself


class TestTheManifestAccountsForEveryFile:
    def test_no_file_on_disk_is_unrecorded_and_no_record_is_missing(self):
        on_disk = {p.name for p in FIXTURES.iterdir() if p.suffix != ".pyc"}
        assert on_disk == set(ENTRIES) | {"META.json"}

    @pytest.mark.parametrize("name", ENTRY_NAMES)
    def test_the_entry_carries_the_whole_contract(self, name: str):
        entry = ENTRIES[name]
        missing = [key for key in REQUIRED_KEYS if key not in entry]
        assert missing == [], f"{name} is missing {missing}"
        assert entry["http_status"] == 200, name
        assert entry["original_bytes"] > 0, name
        assert entry["url"].startswith("https://"), name
        assert re.fullmatch(r"[0-9a-f]{16}", entry["original_sha256_16"]), name
        assert re.fullmatch(r"[0-9a-f]{64}", entry["stored_sha256"]), name

    @pytest.mark.parametrize("name", ENTRY_NAMES)
    def test_a_trimmed_recording_says_what_was_trimmed(self, name: str):
        """
        ``trim_rule`` is the one field a reviewer cannot reconstruct from the file alone, so it
        is the one that is checked hardest: it must exist, and where the bytes differ it must not
        be the string that claims nothing was done.
        """
        entry = ENTRIES[name]
        assert entry["trim_rule"].strip(), f"{name} has no trim_rule"
        if entry["stored_bytes"] != entry["original_bytes"]:
            assert not entry["trim_rule"].startswith("none"), (
                f"{name} stores {entry['stored_bytes']} of {entry['original_bytes']} bytes but "
                "claims no trim"
            )


class TestEveryFixtureMatchesItsRecordedBytes:
    @pytest.mark.parametrize("name", ENTRY_NAMES)
    def test_the_sha_and_the_byte_count_are_the_recorded_ones(self, name: str):
        raw = stored_bytes(name).replace(b"\r\n", b"\n")
        entry = ENTRIES[name]
        assert hashlib.sha256(raw).hexdigest() == entry["stored_sha256"], (
            f"{name} does not match the content META.json describes"
        )
        assert len(raw) == entry["stored_bytes"], name

    @pytest.mark.parametrize("name", ENTRY_NAMES)
    def test_the_line_endings_are_the_recorded_ones(self, name: str):
        """
        A lone CR with no LF is invisible to the ``\\r\\n`` normalisation above, so it is checked
        directly: git would keep it, and a recording whose bytes depend on which platform wrote
        them is not a recording.
        """
        entry = ENTRIES[name]
        raw = stored_bytes(name)
        if entry["stored_line_endings"] == "lf":
            assert b"\r" not in raw, f"{name} carries a CR byte"

    def test_a_verbatim_claim_means_identical_bytes(self):
        """
        The fixtures whose ``trim_rule`` opens with "none" claim to hold the response unchanged.
        That claim is only meaningful if it is checked in both directions: identical byte count
        *and* the stored digest matching the recorded prefix of the original response. Without
        this, a future "none (stored verbatim)" that quietly lost a row would pass every other
        test in this file.
        """
        verbatim = [name for name, entry in ENTRIES.items()
                    if entry["trim_rule"].startswith("none")]
        assert verbatim, "no fixture claims a verbatim store, so the claim is untested"
        for name in verbatim:
            entry = ENTRIES[name]
            assert entry["stored_bytes"] == entry["original_bytes"], name
            assert entry["stored_sha256"].startswith(entry["original_sha256_16"]), (
                f"{name} claims a verbatim store but its digest does not match the original"
            )


# ============================================================ the new-route batch


class TestTheNewRouteFixturesArePresent:
    @pytest.mark.parametrize("name", NEW_ROUTE_FIXTURES)
    def test_the_recorded_route_has_a_fixture(self, name: str):
        assert name in ENTRIES, f"{name} was recorded for a route but has no META entry"
        assert (FIXTURES / name).is_file(), name

    def test_the_batch_is_dated_and_only_the_copied_sitemap_claims_another_day(self):
        """
        The 2026-10-10 pass produced 14 of the 15. ``sitemap_excerpt_tcs.xml`` is a byte-identical
        copy of the 2026-09-27 excerpt -- a TCS window could not be cut without a request, and
        inventing ``<url>`` blocks would be inventing vendor data -- so it is the single file
        whose ``captured_at_utc`` is not the batch date, and the exception is pinned here rather
        than left to be discovered by whoever reaches for the file first.
        """
        dates = {name: ENTRIES[name]["captured_at_utc"] for name in NEW_ROUTE_FIXTURES}
        borrowed = {name for name, stamp in dates.items() if not stamp.startswith("2026-10-10")}
        assert borrowed == {"sitemap_excerpt_tcs.xml"}, borrowed
        copy_entry = ENTRIES["sitemap_excerpt_tcs.xml"]
        assert copy_entry["stored_sha256"] == ENTRIES["sitemap_excerpt.xml"]["stored_sha256"]
        assert "TCS" not in (FIXTURES / "sitemap_excerpt_tcs.xml").read_text(encoding="utf-8")

    @pytest.mark.parametrize("name", NEW_ROUTE_FIXTURES)
    def test_the_notes_say_what_a_parser_must_not_do_with_the_page(self, name: str):
        """
        Every entry has a ``notes`` key, but a key is not a reason. These are the recordings
        whose whole value is a measured trap -- month-named headers, byte-identical tables, a
        positional discriminator -- so each of them has to carry the trap in writing.
        """
        entry = ENTRIES[name]
        assert entry["notes"].strip(), f"{name} records a page but says nothing about it"
        if name in ("shareholding_tcs.html", "monthly_mf_holding_tcs.html"):
            assert "rot" in entry["notes"], (
                f"{name} has month-named headers; the note must say they cannot be a signature"
            )
        if name == "buy_sell_zone_tcs.html":
            assert "BYTE-IDENTICAL" in entry["notes"], (
                "buy_sell_zone_tcs.html has two tables that no header signature can tell apart"
            )
        if name == "insider_trading_tcs.html":
            assert "not 500" in entry["notes"], (
                "the 500-row cap is the whole reason this fixture's row count matters"
            )


# ============================================================ parseability


class TestHtmlFixturesParseWithTheShippedReader:
    @pytest.mark.parametrize("name", HTML_NAMES)
    def test_find_tables_returns_frames_without_raising(self, name: str):
        """
        ``find_tables(signature=None)`` sweeps every outermost table, so this is the widest
        possible read of each fixture: layout tables included. If a trimmed block left a table
        unbalanced or a fragment unparseable, this raises ``TrendlyneSchemaError`` naming an
        index instead of the missing markup.
        """
        html = (FIXTURES / name).read_text(encoding="utf-8")
        frames = tbt.find_tables(html)
        assert isinstance(frames, list), name
        assert len(frames) == len(tbt.page_signatures(html)), (
            f"{name}: find_tables returned {len(frames)} frames for "
            f"{len(tbt.page_signatures(html))} outermost tables"
        )

    @pytest.mark.parametrize("name", HTML_NAMES)
    def test_page_signatures_maps_every_outermost_table(self, name: str):
        html = (FIXTURES / name).read_text(encoding="utf-8")
        signatures = tbt.page_signatures(html)
        assert isinstance(signatures, dict), name
        assert list(signatures) == list(range(len(signatures))), (
            f"{name}: page_signatures must be keyed by table position, got {list(signatures)}"
        )
        for index, matched in signatures.items():
            assert matched is None or matched in tbt.known_signatures(), (
                f"{name}[{index}]: {matched!r} is not a registered signature"
            )

    def test_the_pages_that_are_card_lists_really_carry_no_table(self):
        """
        ``/latest-news/`` and ``/alerts/stock-alerts/`` render zero ``<table>`` elements. That is
        a finding, not a missing fixture: a parser for those routes reads cards, and the zero is
        what stops someone from writing a table extractor against them.
        """
        for name in ("latest_news_tcs.html", "stock_alerts_tcs.html"):
            assert tbt.page_signatures((FIXTURES / name).read_text(encoding="utf-8")) == {}, name


class TestJsonFixturesCarryTheEnvelope:
    @pytest.mark.parametrize("name", JSON_NAMES)
    def test_it_loads_and_carries_the_trendlyne_head(self, name: str):
        """
        Every trendlyne JSON response is ``{"head": ..., "body": ...}`` and success is
        ``head.status == "0"``. A payload without ``head`` cannot be judged at all, so the client
        would have nothing to decide on -- which is a worse failure than a wrong value.
        """
        payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        assert isinstance(payload, dict), name
        assert isinstance(payload.get("head"), dict), f"{name} has no head envelope"
        assert "status" in payload["head"], f"{name}: head carries no status to decide on"

    def test_the_option_chain_fixture_still_carries_its_body(self):
        """
        The one JSON fixture that was trimmed rather than stored whole, so the check that its
        envelope survived belongs to it explicitly: a JSON payload can parse perfectly while
        having lost the block the client exists to read.
        """
        payload = json.loads((FIXTURES / "option_chain_tcs.json").read_text(encoding="utf-8"))
        body = payload["body"]
        for key in ("tableHeaders", "tableData", "tableDataV2", "maxPain", "atTheMoney", "MWPL",
                    "stockLevelData", "expiryLevelPCRValues", "contractLastUpdated"):
            assert key in body, f"option_chain_tcs.json lost body.{key}"
        assert body["IV"] is None, "body.IV is null by design upstream, not a missing value"

    def test_the_option_chain_rows_still_line_up_with_their_headers(self):
        """
        The strike trim dropped rows, not columns, and rows are positional arrays whose names
        live in ``tableHeaders``. Both facts are load-bearing for the client, and a trim that
        broke either would leave a chain frame that is quietly mislabelled.
        """
        body = json.loads((FIXTURES / "option_chain_tcs.json").read_text(encoding="utf-8"))["body"]
        headers = body["tableHeaders"]
        assert headers[0]["unique_name"] == "current_price_call", headers[0]
        width = len(headers)
        assert all(isinstance(row, list) and len(row) == width for row in body["tableData"]), (
            "every tableData row must be a positional array of len(tableHeaders)"
        )
        strike = next(i for i, h in enumerate(headers) if h["unique_name"] == "strike_price")
        strikes = [row[strike] for row in body["tableData"]]
        assert body["atTheMoney"] in strikes, (
            "the kept window must still contain the at-the-money strike"
        )
        assert len(body["tableData"]) == len(body["tableDataV2"]), (
            "the two arrays are the same strikes; trimming one but not the other would misalign "
            "every row"
        )
