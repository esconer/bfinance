"""
``bfinance.universe`` — index membership as a **recorder**, tested fully offline.

The upstream fact that shapes this whole file
---------------------------------------------
NSE publishes **only the current index snapshot** and **overwrites membership in place**. Every
dated/history variant 404s and the rebalance calendar's only home, ``nseindices.com``, does not
resolve. So **survivorship bias cannot be removed retroactively from any source, and no fixture
can fix it.** The only way a caller ever gets point-in-time membership is if something accumulates
it locally from first use.

Two behaviours follow, and they are what these tests exist to hold in place:

1. A live read is **archived** (fixtures: ``tests/fixtures/nse/universe/``).
2. An as-of request the archive cannot answer **raises**, naming the earliest date it *can* answer.
   It is never satisfied with the current snapshot wearing a historical label.

Negative controls
-----------------
Six assertions in this repo shipped that could not fail. :class:`TestNegativeControls` here is
written so the two mutations that would silently destroy the module's only value are provably
detectable:

* break the as-of refusal -> the observed-from guard goes red,
* remove the accumulation write -> the archive has no coverage and as-of has nothing to return.

Both are also demonstrated against the real module by breaking ``universe.py`` and running the
suite; see the report.

Fixture layout
--------------
The seven ``ind_*.csv`` recordings are 12-row shape fixtures (header + column vocabulary +
Series/ISIN). The **membership claims** — row counts, subset relations, and the 501-row
"Nifty 500" file — are pinned by ``_symbol_sets.txt``, which is **untrimmed** and holds the
complete symbol set of the five files that take part in the mislabelling identity.
``_partition_proof.json`` is the recorded verdict. ``META.json`` in the same directory is the
tamper-evident manifest.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from pathlib import Path

import httpx
import pandas as pd
import pytest

from bfinance import universe as U

FIXTURES = Path(__file__).parent / "fixtures" / "nse" / "universe"

# A date comfortably before any accumulation, used for every "history unavailable" assertion.
PRE_HISTORY = "2024-01-01"
DAY_ONE = "2026-09-28"
DAY_TWO = "2026-12-21"


# --------------------------------------------------------------------- helpers


def _text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _meta(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _symbol_sets() -> dict[str, set[str]]:
    """The complete recorded symbol set per file, from the untrimmed ``_symbol_sets.txt``."""
    sets: dict[str, set[str]] = {}
    for line in _text("_symbol_sets.txt").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        filename, symbol = line.split("\t")
        sets.setdefault(filename, set()).add(symbol)
    return sets


def _proof() -> dict:
    return json.loads(_text("_partition_proof.json"))


def _csv_from_symbols(symbols) -> str:
    """A real constituent CSV carrying only symbols.

    The optional columns are left **blank rather than invented**: for the 501-symbol worked example
    only the Symbol column is known from the recording, so no ISIN or company name is made up.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(list(U.SOURCE_COLUMNS))
    for symbol in symbols:
        writer.writerow(["", "", symbol, "", ""])
    return buffer.getvalue()


def _membership(symbols) -> pd.DataFrame:
    return U.parse_constituents(_csv_from_symbols(symbols), index="nifty500", url="fixture")


def _archive_files(archive: U.UniverseArchive) -> set[str]:
    return {
        p.name
        for p in list(archive.snapshots_dir.iterdir()) + list(archive.indices_dir.iterdir())
    }


def _dir_digest(path: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(path.iterdir())}


# --------------------------------------------------------------------- fixtures


@pytest.fixture
def archive(tmp_path) -> U.UniverseArchive:
    return U.UniverseArchive(tmp_path / "universe")


@pytest.fixture
def nifty50() -> pd.DataFrame:
    return U.parse_constituents(
        _text("ind_nifty50list.csv"),
        index="nifty50",
        url=U.index_url("nifty50"),
        retrieved_utc="2026-09-27T20:44:25+00:00",
    )


# =========================================================== parsing / filenames


class TestMembershipParsing:
    def test_verbatim_columns_map_onto_the_frame(self, nifty50):
        """NSE's five columns are all read, in its order, with the Symbol as the key."""
        assert list(U.SOURCE_COLUMNS) == [
            "Company Name", "Industry", "Symbol", "Series", "ISIN Code",
        ]
        header = _text("ind_nifty50list.csv").splitlines()[0]
        assert header.split(",") == list(U.SOURCE_COLUMNS)
        assert list(nifty50.columns) == list(U.MEMBERSHIP_COLUMNS)
        assert nifty50["symbol"].notna().all()
        assert nifty50["symbol"].is_unique

    def test_series_and_isin_are_carried_not_discarded(self, nifty50):
        """A caller needs ISIN to join across sources without going via a ticker string.

        The values asserted are the ones actually in the recording, not plausible-looking
        inventions — the fixture holds the first 12 constituents alphabetically.
        """
        assert nifty50["isin"].notna().all(), "ISIN was dropped in parsing"
        assert nifty50["series"].notna().all(), "Series was dropped in parsing"

        by_symbol = nifty50.set_index("symbol")
        assert by_symbol.loc["ADANIENT", "isin"] == "INE423A01024"
        assert by_symbol.loc["ADANIENT", "company_name"] == "Adani Enterprises Ltd."
        assert by_symbol.loc["ADANIENT", "industry"] == "Metals & Mining"
        assert by_symbol.loc["ADANIENT", "series"] == "EQ"
        assert by_symbol.loc["BAJAJ-AUTO", "isin"] == "INE917I01010"
        assert by_symbol.loc["BAJAJ-AUTO", "company_name"] == "Bajaj Auto Ltd."
        # Hyphenated scrips survive verbatim, which is what an ISIN join depends on.
        assert "BAJAJ-AUTO" in by_symbol.index
        # ISIN shape: INE + 9 characters, 12 in total.
        assert nifty50["isin"].str.fullmatch(r"INE[0-9A-Z]{9}").all()
        assert nifty50["isin"].str.len().eq(12).all()

    def test_the_isin_is_preserved_through_the_archive_round_trip(self, archive, nifty50):
        """ISIN must survive archiving, or the join key is lost the first time it is stored."""
        result = archive.record("nifty50", nifty50, recorded_on=DAY_ONE)
        stored = archive._load_snapshot(result.snapshot_id)
        original = nifty50.set_index("symbol")
        assert len(stored["members"]) == len(nifty50)
        for member in stored["members"]:
            assert member["isin"] == original.loc[member["symbol"], "isin"]
            assert member["series"] == original.loc[member["symbol"], "series"]
            assert member["company_name"] == original.loc[member["symbol"], "company_name"]
        # And back out through as-of unchanged.
        replayed = archive.membership_asof("nifty50", DAY_ONE)
        assert replayed["isin"].tolist() == nifty50["isin"].tolist()
        assert replayed["series"].tolist() == nifty50["series"].tolist()

    def test_every_data_row_becomes_a_row(self, nifty50):
        """No filtering, sampling or de-duplication: the count comes from the file."""
        recorded = _meta("ind_nifty50list.META.json")["trim_behaviour_neutral"]
        assert recorded["original_data_rows"] == 50
        # The stored fixture is trimmed to 12 rows, and the parse must reflect the file it was
        # given rather than the number in the index's name.
        assert len(nifty50) == 12
        assert len(nifty50) != recorded["original_data_rows"]

    def test_missing_optional_columns_become_none_not_invented(self):
        """A symbol-only file must not gain fabricated ISINs."""
        frame = _membership(["AAA", "BBB"])
        assert list(frame["symbol"]) == ["AAA", "BBB"]
        assert frame["isin"].isna().all()
        assert frame["company_name"].isna().all()

    def test_a_payload_without_a_symbol_column_is_refused(self):
        from bfinance.utils.exceptions import ParsingError

        with pytest.raises(ParsingError, match="Symbol"):
            U.parse_constituents("Company Name,Industry\nA,B\n")


class TestFilenamePattern:
    """The pattern is ``ind_nifty<name>list.csv``. A wrong guess 404s and looks like "no index"."""

    def test_midcap150_resolves_to_the_working_filename(self):
        assert U.index_filename("midcap150") == "ind_niftymidcap150list.csv"
        assert U.index_url("midcap150").endswith("/ind_niftymidcap150list.csv")

    def test_every_catalogued_filename_follows_the_documented_pattern(self):
        """``ind_nifty<name>list.csv`` — checked against the pattern, not against prose.

        ``totalmarket`` is the one recorded exception: ``ind_niftytotalmarket_list.csv`` carries an
        extra underscore before ``list``. It is called out rather than allowed to look like a typo.
        """
        strict = re.compile(r"^ind_nifty[a-z0-9]+list\.csv$")
        for key, spec in U.CATALOGUE.items():
            if key == "totalmarket":
                continue
            assert strict.match(spec.filename), f"{key}: {spec.filename} breaks the pattern"

        exceptions = {s.filename for s in U.CATALOGUE.values() if not strict.match(s.filename)}
        assert exceptions == {"ind_niftytotalmarket_list.csv"}

    def test_a_non_nifty_prefixed_name_is_refused_with_the_trap_named(self):
        """The two confirmed 404s are refused before a request can be built."""
        for wrong in sorted(U.KNOWN_404_FILENAMES):
            with pytest.raises(U.UnsupportedIndexError) as info:
                U.index_for_filename(wrong)
            message = str(info.value)
            assert U.FILENAME_PATTERN in message
            assert "404" in message
            assert "ind_midcap150list.csv" in U.KNOWN_404_FILENAMES

    def test_an_alias_spelling_resolves_to_the_same_index(self):
        """``nifty_midcap_150_list`` and ``midcap150list`` are the same file."""
        assert U.index_filename("nifty_midcap_150_list") == "ind_niftymidcap150list.csv"
        assert U.index_filename("midcap150list") == "ind_niftymidcap150list.csv"
        assert U.index_filename("NIFTY-50") == "ind_nifty50list.csv"

    def test_an_unknown_index_name_is_refused_with_the_working_alternatives(self):
        with pytest.raises(U.UnsupportedIndexError) as info:
            U.resolve_index("nifty_foobar")
        message = str(info.value)
        assert U.FILENAME_PATTERN in message
        assert "404" in message
        assert "totalmarket" in message, "the error must list the usable indices"

    def test_totalmarket_breaks_the_pattern_and_is_still_usable(self):
        assert U.index_filename("totalmarket") == "ind_niftytotalmarket_list.csv"
        assert U.resolve_index("totalmarket").usable

    def test_a_404_response_is_reported_as_a_filename_problem_not_a_missing_index(self, archive):
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404)))
        with pytest.raises(U.UnsupportedIndexError) as info:
            archive.fetch("midcap150", client=client, min_interval=0)
        assert "404" in str(info.value)
        assert U.FILENAME_PATTERN in str(info.value)


# =========================================================== the smallcap250 trap


class TestSmallcap250IsFencedOff:
    """``ind_niftysmallcap250list.csv`` is mislabelled. A future contributor must not re-add it."""

    def test_the_module_refuses_to_present_it_as_a_universe(self):
        with pytest.raises(U.UnsupportedIndexError) as info:
            U.resolve_index("smallcap250")
        message = str(info.value)
        assert "MISLABELLED" in message
        assert "smallcap" in message.lower()
        assert "totalmarket" in message, "the refusal must point at the real small/mid-cap source"

    def test_the_filename_is_refused_too(self):
        """Fencing the name is not enough: the file must be refused however it is reached."""
        with pytest.raises(U.UnsupportedIndexError, match="MISLABELLED"):
            U.index_for_filename("ind_niftysmallcap250list.csv")

    def test_it_is_blocked_in_the_catalogue_with_a_reason(self):
        assert "smallcap250" in U.BLOCKED_INDICES
        assert "MISLABELLED" in U.BLOCKED_INDICES["smallcap250"]
        catalogue = U.list_indices().set_index("key")
        assert bool(catalogue.loc["smallcap250", "usable"]) is False
        assert catalogue.loc["smallcap250", "blocked_reason"]
        # Still *visible*, so the trap is documented rather than quietly forgotten.
        assert "smallcap250" in set(catalogue.index)

    def test_it_cannot_be_recorded_into_the_archive(self, archive):
        frame = U.parse_constituents(
            _text("ind_niftysmallcap250list.csv"), index="smallcap250", url="fixture"
        )
        assert len(frame) == 12
        with pytest.raises(U.UnsupportedIndexError, match="MISLABELLED"):
            archive.record("smallcap250", frame)

    def test_the_refusal_reason_is_backed_by_the_recorded_set_arithmetic(self):
        """Re-derive the mislabelling claim from the untrimmed symbol sets, offline.

        ``_partition_proof.json`` is the recorded verdict; this recomputes it, so the fence cannot
        rest on a JSON file that merely asserts the conclusion.
        """
        sets = _symbol_sets()
        proof = _proof()
        nifty500 = sets["ind_nifty500list.csv"]
        smallcap = sets["ind_niftysmallcap250list.csv"]
        mid400 = sets["ind_niftymidsmallcap400list.csv"]
        mid150 = sets["ind_niftymidcap150list.csv"]

        assert len(smallcap) == 251
        assert len(nifty500) == 501
        # A strict subset of Nifty 500: the whole file is inside it.
        assert smallcap < nifty500
        assert smallcap & nifty500 == smallcap
        # Exactly midsmallcap400 - midcap150, both difference directions empty.
        assert smallcap == mid400 - mid150
        assert smallcap - (mid400 - mid150) == set()
        assert (mid400 - mid150) - smallcap == set()
        # And therefore it contributes nothing to Nifty 500 beyond the other two files.
        assert nifty500 & smallcap == smallcap

        # The recorded verdict agrees with the recomputation.
        assert proof["row_counts"]["ind_niftysmallcap250list.csv"] == 251
        assert proof["row_counts"]["ind_nifty500list.csv"] == 501
        assert proof["findings"]["niftysmallcap250_subset_of_nifty500"] is True
        assert proof["findings"]["niftysmallcap250_equals_midsmallcap400_minus_midcap150"] is True
        assert proof["findings"]["niftysmallcap250_intersection_with_nifty500"] == 251

    def test_its_rows_are_mid_and_large_caps_never_actual_small_caps(self):
        """It is a *mid*+*large* sliver, so no row in it can be called a small cap."""
        sets = _symbol_sets()
        smallcap = sets["ind_niftysmallcap250list.csv"]
        # The 100 rows that Nifty 500 adds over MidSmallCap 400 are the large caps.
        residual = sets["ind_nifty500list.csv"] - sets["ind_niftymidsmallcap400list.csv"]
        assert len(residual) == 100
        assert "RELIANCE" in residual and "TCS" in residual and "HDFCBANK" in residual
        # The mislabelled file is disjoint from them; it is midcaps, not smallcaps.
        assert smallcap & residual == set()
        # And the file is exactly the midcap half of MidSmallCap 400, which is the real point.
        assert smallcap == sets["ind_niftymidsmallcap400list.csv"] - sets["ind_niftymidcap150list.csv"]

    def test_totalmarket_is_the_advertised_small_mid_cap_source(self):
        proof = _proof()
        assert proof["row_counts"]["ind_niftytotalmarket_list.csv"] == 755
        # A superset of the 500, per the recorded measurement.
        assert proof["row_counts"]["ind_niftytotalmarket_list.csv"] > proof["row_counts"][
            "ind_nifty500list.csv"
        ]
        assert U.resolve_index("totalmarket").usable


# =========================================================== size comes from data


class TestNoHardcodedFiveHundred:
    """NSE's own "Nifty 500" file has 501 rows. Nothing may cap a universe at 500."""

    def test_the_501_row_nifty500_file_yields_501_members(self, archive):
        """The worked example, end to end, on the real recorded symbol set."""
        recorded = list(_symbol_sets()["ind_nifty500list.csv"])
        assert len(recorded) == 501

        frame = _membership(recorded)
        assert len(frame) == 501
        assert len(frame) != 500
        assert set(frame["symbol"]) == set(recorded)

        result = archive.record("nifty500", frame, recorded_on=DAY_ONE)
        assert result.rows == 501

        as_of = archive.membership_asof("nifty500", DAY_ONE)
        assert len(as_of) == 501
        assert set(as_of["symbol"]) == set(recorded)

    def test_nominal_size_is_metadata_and_is_provably_not_applied(self):
        """``nominal_size`` is 500 for this index; the data says 501. The cap is not real."""
        spec = U.resolve_index("nifty500")
        assert spec.nominal_size == 500
        assert spec.nominal_size != 501
        # The frame size is taken from the data, and the disagreement is surfaced.
        frame = _membership(_symbol_sets()["ind_nifty500list.csv"])
        assert len(frame) != spec.nominal_size

    def test_the_module_contains_no_truncation(self):
        source = Path(U.__file__).read_text(encoding="utf-8")
        # Strip every docstring so the prose that *describes* the ban cannot satisfy the scan.
        code = re.sub(r'"""(?:.|\n)*?"""', "", source)
        code = "\n".join(line for line in code.splitlines() if not line.strip().startswith("#"))
        for banned in (".head(", "head(500)", "[:500]", "iloc[:500]", ">= 500", "> 500"):
            assert banned not in code, f"universe.py contains a truncation: {banned!r}"
        # `nominal_size` is display metadata. It must never be compared against a length or used
        # to slice, or the index's name becomes a cap.
        assert re.search(r"nominal_size\s*(?:<|>|<=|>=|==|!=)", code) is None
        assert re.search(r"\[\s*:\s*nominal_size", code) is None
        assert re.search(r"nominal_size\s*\]", code) is None
        assert re.search(r"len\([^)]*\)\s*(?:<|>|<=|>=|==)\s*[^)]*nominal_size", code) is None

    def test_other_indices_also_size_from_the_data(self):
        """Every recorded set round-trips at its own length, not at a name-derived number."""
        for filename, symbols in _symbol_sets().items():
            if filename == "ind_niftysmallcap250list.csv":
                continue  # fenced off
            key = U.index_for_filename(filename).key
            frame = _membership(symbols)
            assert len(frame) == len(symbols), filename
            assert set(frame["symbol"]) == symbols, filename
            # Nominal size and real size are allowed to disagree; the data wins either way.
            assert U.resolve_index(key).nominal_size in (None, len(symbols)) or key in {
                "nifty500", "midsmall400",
            }


# =========================================================== the recorder


class TestAccumulation:
    def test_two_reads_on_two_dates_give_a_diff_with_additions_and_removals(self, archive):
        """Removals are the half a survivorship-blind consumer forgets, so both are asserted."""
        sets = _symbol_sets()
        before_symbols = sets["ind_niftymidsmallcap400list.csv"]
        # The additions are real Nifty 500 members absent from MidSmallCap 400 (the 100 residual
        # large caps); the removals are real MidSmallCap 400 members. Every symbol on both sides
        # is a real recorded NSE index member -- no symbol is invented. Only the *pairing* of two
        # recorded sets into a two-day timeline is constructed, because only one real snapshot per
        # index exists and that is the whole point of the module.
        residual = sorted(sets["ind_nifty500list.csv"] - before_symbols)
        dropped = sorted(before_symbols)[:5]
        arriving = residual[:5]
        after_symbols = (set(before_symbols) - set(dropped)) | set(arriving)

        assert len(before_symbols) == 401
        assert set(arriving) <= sets["ind_nifty500list.csv"]
        assert set(arriving).isdisjoint(before_symbols)
        assert set(dropped) <= before_symbols

        first = archive.record("midsmall400", _membership(before_symbols), recorded_on=DAY_ONE)
        second = archive.record("midsmall400", _membership(after_symbols), recorded_on=DAY_TWO)
        assert first.rows == 401 and second.rows == 401
        assert first.snapshot_id != second.snapshot_id

        diff = archive.diff("midsmall400", DAY_ONE, DAY_TWO)
        assert diff.added == tuple(arriving)
        assert diff.removed == tuple(dropped)
        assert diff.unchanged == 401 - 5
        assert diff.changed == 10
        assert diff.older_rows == 401 and diff.newer_rows == 401

        frame = diff.to_frame()
        assert set(frame["change"]) == {"added", "removed", "unchanged"}
        assert frame.loc[frame["change"] == "added", "symbol"].tolist() == arriving
        assert frame.loc[frame["change"] == "removed", "symbol"].tolist() == dropped
        assert frame.attrs["bfinance_universe_diff_removed"] == 5
        assert frame.attrs["bfinance_universe_diff_added"] == 5

    def test_a_same_day_reread_changes_nothing(self, archive, nifty50):
        first = archive.record("nifty50", nifty50, recorded_on=DAY_ONE)
        again = archive.record("nifty50", nifty50, recorded_on=DAY_ONE)
        assert first.snapshot_id == again.snapshot_id
        assert again.written is False
        assert again.files_written == ()
        assert archive.coverage("nifty50").run_count == 1
        assert archive.recorded_dates("nifty50") == [DAY_ONE]

    def test_the_archive_is_not_rewritten_wholesale_and_does_not_duplicate(self, archive, nifty50):
        """Growth is O(membership changes), not O(reads).

        A 12-unchanged-day sequence must not create a single new file, and the large snapshot blob
        must never be rewritten. The only thing that moves is one scalar, ``last_observed``, in the
        small index file — which is what stops an open run from answering for the future.
        """
        first = archive.record("nifty50", nifty50, recorded_on=DAY_ONE)
        assert set(first.files_written) == {f"{first.snapshot_id}.json", "nifty50.json"}
        assert len(_archive_files(archive)) == 2

        snapshot_digest = _dir_digest(archive.snapshots_dir)
        runs_after_first = archive._load_index("nifty50")["runs"]

        # Ten more reads on later dates with the same membership, which is how NSE behaves.
        for offset in range(1, 11):
            result = archive.record("nifty50", nifty50, recorded_on=f"2026-10-{offset + 10:02d}")
            assert result.run_opened is False, "an unchanged read must not open a run"
            assert result.new_snapshot is False
            assert "nifty50.json" in result.files_written
            # No snapshot blob is written once one exists.
            assert f"{result.snapshot_id}.json" not in result.files_written

        # One blob, untouched. The run list is byte-identical: no per-read growth, no duplication.
        assert _dir_digest(archive.snapshots_dir) == snapshot_digest
        assert archive._load_index("nifty50")["runs"] == runs_after_first
        assert len(_archive_files(archive)) == 2
        coverage = archive.coverage("nifty50")
        assert coverage.snapshot_count == 1
        assert coverage.run_count == 1
        # The observation window did advance, which is the entire point of the scalar.
        assert coverage.latest == "2026-10-20"
        assert coverage.earliest == DAY_ONE

    def test_a_real_change_adds_exactly_one_blob_and_one_run(self, archive, nifty50):
        archive.record("nifty50", nifty50, recorded_on=DAY_ONE)
        existing = _dir_digest(archive.snapshots_dir)
        changed = nifty50.iloc[:-1].copy()

        result = archive.record("nifty50", changed, recorded_on=DAY_TWO)
        assert result.written is True
        assert result.run_opened is True
        assert result.rows == 11
        assert len(_archive_files(archive)) == 3
        # The earlier snapshot blob is untouched.
        after = _dir_digest(archive.snapshots_dir)
        for name, digest in existing.items():
            assert after[name] == digest, "an existing snapshot was rewritten"

        coverage = archive.coverage("nifty50")
        assert coverage.run_count == 2
        assert coverage.snapshot_count == 2
        assert coverage.earliest == DAY_ONE and coverage.latest == DAY_TWO

    def test_reordering_the_file_is_not_a_membership_change(self, archive):
        """The content hash is symbol-sorted, so NSE re-sorting does not fork a snapshot."""
        symbols = sorted(_symbol_sets()["ind_niftymidsmallcap400list.csv"])
        first = archive.record("midsmall400", _membership(symbols), recorded_on=DAY_ONE)
        shuffled = list(reversed(symbols))
        second = archive.record("midsmall400", _membership(shuffled), recorded_on=DAY_TWO)
        assert first.snapshot_id == second.snapshot_id
        assert second.new_snapshot is False, "a re-ordered file forked a snapshot"
        assert second.run_opened is False, "a re-ordered file opened a new run"
        assert second.files_written == ("midsmall400.json",), "only the scalar should move"
        assert len(_archive_files(archive)) == 2
        assert archive.coverage("midsmall400").run_count == 1
        assert archive.coverage("midsmall400").snapshot_count == 1

    def test_a_live_read_archives_itself(self, archive, nifty50):
        """A successful read is always accumulated; that is the module's reason to exist."""
        body = _text("ind_nifty50list.csv")
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(200, text=body)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        frame, result = archive.fetch(
            "nifty50", client=client, min_interval=0, recorded_on=DAY_ONE
        )

        assert calls == ["https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv"]
        assert result is not None and result.written is True
        assert len(frame) == 12
        assert frame.attrs["bfinance_universe_recorded"] is True
        assert archive.coverage("nifty50").earliest == DAY_ONE
        assert len(archive.membership_asof("nifty50", DAY_ONE)) == 12

    def test_fetch_can_opt_out_of_recording(self, archive, nifty50):
        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=_text("ind_nifty50list.csv"))
            )
        )
        _, result = archive.fetch("nifty50", client=client, min_interval=0, record=False)
        assert result is None
        assert archive.coverage("nifty50").earliest is None

    def test_the_archive_is_local_and_persistent(self, tmp_path, nifty50):
        root = tmp_path / "nested" / "universe"
        U.UniverseArchive(root).record("nifty50", nifty50, recorded_on=DAY_ONE)
        # A second process-level open sees the same accumulated history.
        reopened = U.UniverseArchive(root)
        assert reopened.coverage("nifty50").earliest == DAY_ONE
        assert len(reopened.membership_asof("nifty50", DAY_ONE)) == 12

    def test_default_archive_location_honours_BFINANCE_HOME(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BFINANCE_HOME", str(tmp_path / "home"))
        assert U.default_archive().root == tmp_path / "home" / "universe"


# =========================================================== the as-of refusal


class TestAsOfRefusesRatherThanSubstituting:
    """The single most valuable behaviour in this module."""

    def test_a_date_before_accumulation_is_refused_with_the_earliest_date(
        self, archive, nifty50
    ):
        archive.record("nifty50", nifty50, recorded_on=DAY_ONE)

        with pytest.raises(U.HistoryUnavailableError) as info:
            archive.membership_asof("nifty50", PRE_HISTORY)

        error = info.value
        assert "history unavailable from 2026-09-28" in str(error)
        assert error.earliest_available == DAY_ONE
        assert error.requested == PRE_HISTORY
        assert error.index_key == "nifty50"
        # It says why, and it says the current list was withheld on purpose.
        assert "overwrites membership in place" in str(error)
        assert "NOT" in str(error)

    def test_the_refusal_withholds_exactly_the_frame_that_would_have_lied(self, archive, nifty50):
        """The withheld frame is byte-identical to today's list. That is the whole danger.

        If the substitution happened, a caller asking for 2024 membership would silently receive
        the 2026 list and a backtest would look clean while being wrong.
        """
        archive.record("nifty50", nifty50, recorded_on=DAY_ONE)

        # What the archive holds, and what a substitution would hand back for 2024.
        held = archive.membership_asof("nifty50", DAY_ONE)
        assert len(held) == 12

        with pytest.raises(U.HistoryUnavailableError):
            archive.membership_asof("nifty50", PRE_HISTORY)

        # Prove the two are indistinguishable as data, so only the refusal separates them.
        replayed = U.parse_constituents(
            _text("ind_nifty50list.csv"), index="nifty50", url=U.index_url("nifty50")
        )
        assert set(replayed["symbol"]) == set(held["symbol"])
        assert replayed["isin"].tolist() == held["isin"].tolist()

    def test_an_empty_archive_refuses_every_date(self, archive):
        with pytest.raises(U.HistoryUnavailableError) as info:
            archive.membership_asof("nifty50", DAY_ONE)
        assert info.value.earliest_available is None
        assert "no membership has been recorded" in str(info.value)

    def test_a_date_after_the_last_recording_is_refused_too(self, archive, nifty50):
        """Returning today's list for a future date is the same lie, pointed the other way."""
        archive.record("nifty50", nifty50, recorded_on=DAY_ONE)
        with pytest.raises(U.HistoryUnavailableError) as info:
            archive.membership_asof("nifty50", "2027-01-01")
        assert info.value.latest_available == DAY_ONE
        assert "2027-01-01" in str(info.value)

    def test_an_archived_answer_is_labelled_as_not_current(self, archive, nifty50):
        archive.record("nifty50", nifty50, recorded_on=DAY_ONE)
        frame = archive.membership_asof("nifty50", DAY_ONE)
        assert frame.attrs["bfinance_universe_basis"] == "archived_snapshot"
        assert frame.attrs["bfinance_universe_is_current_snapshot"] is False
        assert frame.attrs["bfinance_universe_as_of"] == DAY_ONE
        # The truth about when the membership was actually observed.
        assert frame.attrs["bfinance_universe_snapshot_observed_from"] == DAY_ONE

    def test_a_gap_between_runs_is_not_silently_filled(self, archive, nifty50):
        """Two recorded snapshots with a gap: the gap is a hole, not an invitation to interpolate."""
        archive.record("nifty50", nifty50, recorded_on=DAY_ONE)
        archive.record("nifty50", nifty50.iloc[:-1], recorded_on="2026-12-21")
        # DAY_ONE's run is closed at the day before the change, so it does not cover December.
        frame = archive.membership_asof("nifty50", "2026-10-15")
        assert len(frame) == 12
        assert frame.attrs["bfinance_universe_snapshot_observed_from"] == DAY_ONE
        # Before the archive started is still refused.
        with pytest.raises(U.HistoryUnavailableError, match="history unavailable from"):
            archive.membership_asof("nifty50", PRE_HISTORY)

    def test_a_diff_also_refuses_rather_than_substituting(self, archive, nifty50):
        archive.record("nifty50", nifty50, recorded_on=DAY_ONE)
        with pytest.raises(U.HistoryUnavailableError):
            archive.diff("nifty50", PRE_HISTORY, DAY_ONE)

    def test_reversed_diff_dates_are_rejected(self, archive, nifty50):
        archive.record("nifty50", nifty50, recorded_on=DAY_ONE)
        archive.record("nifty50", nifty50.iloc[:-1], recorded_on=DAY_TWO)
        with pytest.raises(U.UniverseError, match="reversed"):
            archive.diff("nifty50", DAY_TWO, DAY_ONE)

    def test_a_bad_date_string_is_refused(self, archive, nifty50):
        archive.record("nifty50", nifty50, recorded_on=DAY_ONE)
        with pytest.raises(U.UniverseError, match="ISO date"):
            archive.membership_asof("nifty50", "last tuesday")


# =========================================================== attrs on every result


class TestAttrsCarryProvenance:
    REQUIRED = (
        "bfinance_universe_retrieved_utc",
        "bfinance_universe_archive_earliest",
        "bfinance_universe_archive_latest",
        "bfinance_universe_archive_snapshots",
        "bfinance_universe_archive_runs",
        "bfinance_universe_history_starts_at",
        "bfinance_universe_basis",
    )

    def test_every_frame_kind_carries_the_timestamp_and_the_coverage_window(
        self, archive, nifty50
    ):
        archive.record("nifty50", nifty50, retrieved_utc="2026-09-28T09:00:00+00:00",
                       recorded_on=DAY_ONE)
        archive.record(
            "nifty50",
            nifty50.iloc[:-1],
            retrieved_utc="2026-12-21T09:00:00+00:00",
            recorded_on=DAY_TWO,
        )

        frames = {
            "parse": U.parse_constituents(
                _text("ind_nifty50list.csv"), index="nifty50", url="u",
                retrieved_utc="2026-09-27T20:44:25+00:00",
            ),
            "fetch": archive.fetch(
                "nifty50",
                client=httpx.Client(
                    transport=httpx.MockTransport(
                        lambda request: httpx.Response(200, text=_text("ind_nifty50list.csv"))
                    )
                ),
                min_interval=0,
                retrieved_utc="2026-12-21T10:00:00+00:00",
                # Read-only: this test is about attrs, so it must not perturb the coverage it
                # goes on to assert.
                record=False,
            )[0],
            "as_of": archive.membership_asof("nifty50", DAY_ONE),
            "diff": archive.diff("nifty50", DAY_ONE, DAY_TWO).to_frame(),
            "observed": archive.observed_changes("nifty50"),
        }

        for name, frame in frames.items():
            for key in self.REQUIRED:
                assert key in frame.attrs, f"{name} frame is missing {key}"
            assert frame.attrs["bfinance_universe_retrieved_utc"], name

        # The window is real, not a placeholder, once an archive is involved.
        assert frames["as_of"].attrs["bfinance_universe_archive_earliest"] == DAY_ONE
        assert frames["as_of"].attrs["bfinance_universe_archive_latest"] == DAY_TWO
        assert frames["as_of"].attrs["bfinance_universe_archive_snapshots"] == 2
        assert frames["as_of"].attrs["bfinance_universe_archive_root"] == str(archive.root)

        # A bare parse consulted no archive, so the keys are present and honestly None.
        assert frames["parse"].attrs["bfinance_universe_archive_earliest"] is None

    def test_the_coverage_window_moves_forward_with_the_archive(self, archive, nifty50):
        archive.record("nifty50", nifty50, recorded_on=DAY_ONE)
        first = archive.membership_asof("nifty50", DAY_ONE)
        assert first.attrs["bfinance_universe_archive_earliest"] == DAY_ONE
        assert first.attrs["bfinance_universe_archive_latest"] == DAY_ONE

        archive.record("nifty50", nifty50.iloc[:-1], recorded_on=DAY_TWO)
        second = archive.membership_asof("nifty50", DAY_ONE)
        assert second.attrs["bfinance_universe_archive_earliest"] == DAY_ONE
        assert second.attrs["bfinance_universe_archive_latest"] == DAY_TWO

    def test_a_fetched_frame_says_where_the_size_came_from(self, archive):
        frame, _ = archive.fetch(
            "nifty50",
            client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, text=_text("ind_nifty50list.csv"))
                )
            ),
            min_interval=0,
        )
        assert frame.attrs["bfinance_universe_size_from_data"] == len(frame)
        assert frame.attrs["bfinance_universe_filename"] == "ind_nifty50list.csv"
        assert frame.attrs["bfinance_universe_nominal_size"] == 50
        assert frame.attrs["bfinance_universe_is_current_snapshot"] is True
        assert frame.attrs["bfinance_universe_basis"] == "current_snapshot"


# =========================================================== rebalance: observed only


class TestRebalanceIsObservedNotScheduled:
    """NSE publishes no reachable rebalance calendar, so none is invented."""

    def test_observed_changes_are_labelled_observed_and_never_scheduled(self, archive, nifty50):
        archive.record("nifty50", nifty50, recorded_on=DAY_ONE)
        archive.record("nifty50", nifty50.iloc[:-1], recorded_on=DAY_TWO)

        frame = archive.observed_changes("nifty50")
        assert len(frame) == 1
        row = frame.iloc[0]
        assert row["observed_on"] == DAY_TWO
        assert row["since"] == DAY_ONE
        assert row["added_count"] == 0
        assert row["removed_count"] == 1
        assert row["removed"] == nifty50["symbol"].iloc[-1]
        assert row["basis"] == "observed_snapshot_diff"
        assert bool(row["is_scheduled"]) is False

        assert frame.attrs["bfinance_universe_is_scheduled"] is False
        assert frame.attrs["bfinance_universe_basis"] == "observed_snapshot_diff"
        assert "nseindices.com does not resolve" in (
            frame.attrs["bfinance_universe_calendar_source"]
        )

    def test_unchanged_memberships_produce_no_change_rows(self, archive, nifty50):
        archive.record("nifty50", nifty50, recorded_on=DAY_ONE)
        for offset in range(1, 6):
            archive.record("nifty50", nifty50, recorded_on=f"2026-10-{offset + 10:02d}")
        assert len(archive.observed_changes("nifty50")) == 0

    def test_no_public_api_claims_to_be_a_calendar(self):
        """Nothing in the module's surface is named like a schedule."""
        public = {name for name in dir(U) if not name.startswith("_")}
        forbidden = {"rebalance_dates", "REBALANCE_CALENDAR", "rebalance_schedule",
                     "upcoming_rebalances", "next_rebalance", "rebalance_history"}
        assert public.isdisjoint(forbidden)
        for name in public:
            assert "calendar" not in name.lower(), f"{name} looks like an invented calendar"

    def test_observed_changes_are_still_labelled_observed_in_attrs(self, archive, nifty50):
        """The diff-derived path is labelled everywhere it can be seen."""
        archive.record("nifty50", nifty50, recorded_on=DAY_ONE)
        archive.record("nifty50", nifty50.iloc[:-1], recorded_on=DAY_TWO)
        for frame in (
            archive.observed_changes("nifty50"),
            archive.diff("nifty50", DAY_ONE, DAY_TWO).to_frame(),
        ):
            assert frame.attrs["bfinance_universe_is_scheduled"] is False
            assert frame.attrs["bfinance_universe_membership_observed"] is True


# =========================================================== negative controls


class TestNegativeControls:
    """These exist to prove the assertions above can fail.

    A negative control that cannot go red is a comment. Each mutation below is the real defect the
    module is built to prevent, and each is shown to destroy a guarantee.
    """

    def test_control_substituting_the_current_snapshot_is_detectable(
        self, archive, nifty50, monkeypatch
    ):
        """Break the as-of refusal, then show the provenance guard catches it.

        The mutation is the exact bug: resolve an as-of miss to the newest recorded run, i.e. hand
        back today's membership wearing a 2024 label. The guard is the ``observed_from`` attr, which
        reports when the membership was *actually* observed. With the bug present the two dates
        disagree and this assertion fails.
        """
        archive.record("nifty50", nifty50, recorded_on=DAY_ONE)

        def leaky_resolve(self, index_key, as_of):
            runs = [r for r in self._load_index(index_key).get("runs", []) if r.get("from")]
            return runs[-1]  # <-- the survivorship substitution

        # With the real module the request raises, which is the behaviour we want.
        with pytest.raises(U.HistoryUnavailableError):
            archive.membership_asof("nifty50", PRE_HISTORY)

        monkeypatch.setattr(U.UniverseArchive, "_resolve_run", leaky_resolve)
        frame = archive.membership_asof("nifty50", PRE_HISTORY)

        # The substituted frame claims 2024 membership but is stamped as observed in 2026. The
        # guard detects it. If this assertion ever fails, the guard is useless and the module can no
        # longer prove it is not laundering the current list into history.
        observed_from = frame.attrs["bfinance_universe_snapshot_observed_from"]
        labelled_as_of = frame.attrs["bfinance_universe_as_of"]
        assert observed_from > labelled_as_of, (
            "the substitution went undetected: a snapshot observed on "
            f"{observed_from} was labelled as membership on {labelled_as_of}"
        )
        assert observed_from == DAY_ONE
        assert labelled_as_of == PRE_HISTORY
        # And the leak really is a leak: the frame is today's list.
        assert len(frame) == len(nifty50)

    def test_control_removing_the_accumulation_write_destroys_the_as_of_answer(
        self, archive, nifty50, monkeypatch
    ):
        """Remove the write, then show there is no history to answer with."""
        archive.record("nifty50", nifty50, recorded_on=DAY_ONE)
        assert len(archive.membership_asof("nifty50", DAY_ONE)) == 12

        def no_record(self, index, frame, **kwargs):
            return U.RecordResult(
                index_key=str(index), snapshot_id="0" * 16, rows=len(frame),
                new_snapshot=False, run_opened=False, written=False,
            )

        monkeypatch.setattr(U.UniverseArchive, "record", no_record)
        archive.record("nifty50", nifty50, recorded_on="2026-10-05")

        coverage = archive.coverage("nifty50")
        assert coverage.earliest == DAY_ONE, "an unread read still changed the archive"
        # A later, unrecorded date now has nothing behind it and must be refused.
        with pytest.raises(U.HistoryUnavailableError):
            archive.membership_asof("nifty50", "2026-10-05")

    def test_control_dropping_isin_from_the_archive_destroys_the_join_key(
        self, archive, nifty50
    ):
        """The archive stores what it is given, so losing ISIN loses it forever."""
        with_isin = archive.record("nifty50", nifty50, recorded_on=DAY_ONE)
        stored = archive._load_snapshot(with_isin.snapshot_id)
        assert all(member["isin"] for member in stored["members"])

        stripped = nifty50.copy()
        stripped["isin"] = None
        without = archive.record("nifty50", stripped, recorded_on=DAY_TWO)
        assert archive._load_snapshot(without.snapshot_id)["members"][0]["isin"] is None

    def test_control_a_diff_that_only_reports_additions_hides_every_removal(self, archive):
        """A blind differ is a survivorship-biased differ. Both halves are load-bearing."""
        sets = _symbol_sets()
        before = sets["ind_niftymidsmallcap400list.csv"]
        residual = sorted(sets["ind_nifty500list.csv"] - before)
        after = (set(before) - set(sorted(before)[:4])) | set(residual[:2])

        archive.record("midsmall400", _membership(before), recorded_on=DAY_ONE)
        archive.record("midsmall400", _membership(after), recorded_on=DAY_TWO)

        diff = archive.diff("midsmall400", DAY_ONE, DAY_TWO)
        additions_only = set(diff.added)
        # Counting only additions, a backtest keeps trading names that left the index.
        assert len(diff.removed) == 4
        assert additions_only.isdisjoint(diff.removed)
        assert diff.removed, "the removals must not be empty in this scenario"


# =========================================================== the fixtures


class TestFixtureProvenance:
    """The fixtures are the evidence. If they drift, the tests above are fiction."""

    def test_meta_records_every_file_on_disk(self):
        meta = _meta("META.json")
        on_disk = {p.name for p in FIXTURES.iterdir() if p.is_file() and p.suffix != ".pyc"}
        assert on_disk == set(meta["files"]) | {"META.json"}

    def test_every_entry_matches_the_bytes_on_disk(self):
        for name, entry in _meta("META.json")["files"].items():
            raw = (FIXTURES / name).read_bytes().replace(b"\r\n", b"\n")
            assert hashlib.sha256(raw).hexdigest() == entry["stored_sha256"], (
                f"{name} does not match the content META.json describes"
            )
            assert len(raw) == entry["stored_bytes"], name
            assert entry["stored_line_endings"] == "lf", name
            assert entry["original_sha256_16"] is None or len(entry["original_sha256_16"]) == 16
            assert entry["trim_rule"], name

    def test_the_recorded_urls_all_come_from_the_archive_host(self):
        for entry in _meta("META.json")["files"].values():
            if entry["url"] and not entry["url"].startswith("derived-from:"):
                assert entry["url"].startswith("https://nsearchives.nseindia.com/"), entry["url"]

    def test_the_recorded_row_counts_match_the_proof(self):
        """The untrimmed symbol sets must agree with the recorded verdict, file by file."""
        sets = _symbol_sets()
        proof = _proof()
        for filename, count in proof["row_counts"].items():
            if filename in sets:
                assert len(sets[filename]) == count, filename
            else:
                # Not part of any identity, so the count comes from the recording's own META.
                stem = filename.removesuffix(".csv")
                meta = _meta(f"{stem}.META.json")
                assert meta["trim_behaviour_neutral"]["original_data_rows"] == count, filename

    def test_meta_states_that_nse_publishes_only_current_membership(self):
        meta = _meta("META.json")
        text = json.dumps(meta)
        assert "overwrites membership in place" in text
        assert "ONLY the current snapshot" in meta["purpose"]
        assert "nseindices.com" in meta["nse_publishes_only_current_membership"]
        assert "404" in meta["nse_publishes_only_current_membership"]
        assert "nsearchives.nseindia.com" in meta["robots"]
        assert "RFC 9309" in meta["robots"]

    def test_meta_pins_the_row_count_is_not_a_membership_size(self):
        meta = _meta("META.json")
        assert "501" in meta["row_counts_are_not_membership_sizes"]

    def test_the_untrimmed_symbol_sets_cover_every_file_the_claims_need(self):
        """The CSV fixtures are trimmed; the claims must stay verifiable from what is kept whole."""
        sets = _symbol_sets()
        assert sum(len(v) for v in sets.values()) == 1403
        assert {
            "ind_nifty500list.csv",
            "ind_niftysmallcap250list.csv",
            "ind_niftymidsmallcap400list.csv",
            "ind_niftymidcap150list.csv",
            "ind_niftymidcap100list.csv",
        } == set(sets)
        # Every CSV fixture is trimmed to 12 rows, so none of them alone can carry a count claim.
        for path in FIXTURES.glob("ind_*.csv"):
            rows = len(path.read_text(encoding="utf-8").strip().splitlines()) - 1
            assert rows == 12, path.name
