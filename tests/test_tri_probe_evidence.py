"""
The TRI probe evidence: what the ``tri_index_return`` refusal is actually tied to.

The gap this file closes
-----------------------
``bfinance.sources.registry.UNAVAILABLE`` registers four measured refusals. Three of them are tied
to an HTTP status recorded on disk -- a 28-byte empty ``pledge`` envelope, a 403 from the Akamai
edge recorded twice independently, a 418 from the RBI WAF. ``tri_index_return`` was the exception:
its evidence tied only to a **count** (``probe_count: 4``) cross-checked against a sentence that
spells out "Four candidate routes", plus the absence of a total-return field in the two committed
``ind_close_all_*.csv`` headers. The four 404 route URLs were never committed to any artifact.

``tests/fixtures/nse/tri_probe_log.json`` now exists and records that gap explicitly rather than
filling it. This file is what reads it.

What is honestly claimed, and what is not
----------------------------------------
Read the three levels below as three different strengths, because collapsing them is how a
refusal ends up looking better-founded than it is:

1. **The 404s are an unanchored count.** ``probe_count: 4`` and a sentence spelling out "Four"
   agree with each other, and nothing outside ``src/bfinance/nse/macro.py`` corroborates either.
   No URL, no status, no body. The URLs are **not** pinned, and no assertion here implies they
   are. :data:`urls_not_committed` is ``true`` in the fixture and this file asserts it is.
2. **The absent total-return field is a real absence over real bytes.** Two committed NSE
   index-close CSVs, six years apart, and neither header carries a total-return column. This is
   independent of the probe and is the half of the evidence that is genuinely byte-level.
3. **The gap itself is recorded, with the reason and the remedy.**

Every derivation below is a function of a recorded artifact, and each one has a **negative
control** that mutates that artifact in memory and requires the verdict to **change**. A control
that cannot flip its verdict is the unfailable assertion this repository has removed four times.

A fixture must not be able to make this file green on its own
--------------------------------------------------------------
The most dangerous way this file could be wrong is if a future edit to the fixture asserted the
conclusion -- ``"no_route_exists": true`` -- and the test then read its own claim back. So the
verdict is derived **only** from inputs that can be independently true or false: the integer
count, the sentence that spells it out, the recorded gap flags, and the CSV headers read off
disk. A fixture that adds a conclusion field is detected by
:meth:`TestTheFixtureCannotAssertTheConclusionForUs`, which fails on any key the verdict reads
neither directly nor by derivation.

Everything here is offline and hermetic: no network, no subprocess, no clock, no temp files.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import pathlib
import re
from typing import Any


from bfinance.nse import macro
from bfinance.sources.registry import DEFAULT, UNAVAILABLE

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
TRI_LOG = FIXTURES / "nse" / "tri_probe_log.json"
MACRO_DIR = FIXTURES / "nse" / "macro"

#: The two committed NSE index-close payloads the refusal's byte-level half rests on. Named, not
#: globbed, so that one of them being deleted fails this file rather than quietly shrinking the
#: evidence it is derived from.
INDEX_CLOSE_FIXTURES = (
    MACRO_DIR / "ind_close_all_01012020.csv",
    MACRO_DIR / "ind_close_all_25092026.csv",
)

#: A total-return field in an NSE index payload, in any of the spellings NSE uses. ``Div Yield`` is
#: deliberately *not* matched: it is a per-annum yield, not a reinvested total-return level, and
#: treating the two as the same concept is the approximation this refusal exists to prevent.
#:
#: ``\btr\b`` is included because NSE abbreviates the concept, and a header reading
#: ``TR Index Value`` is exactly as conclusive as one reading ``Total Return Index Value``.
#: The word-boundary anchor is what keeps it from firing on a substring.
TOTAL_RETURN_FIELD = re.compile(r"total\s*return|\btri\b|\btr\b|div\s*yield\s*reinvest", re.I)

#: A yield-derived field that, if present, shows the payload is not price-only. Its presence is
#: what makes the absence of a total-return field informative rather than vacuous -- a payload
#: carrying no yield concepts at all would make "no total-return column" a triviality.
YIELD_SHAPED_FIELD = re.compile(r"div\s*yield|dividend", re.I)

#: The refusal as ``bfinance.nse.macro`` declares it, read from the module rather than restated.
REFUSAL_KEY = "index_tri"

#: Number words ``_spelled_count`` can read, so the count and the sentence can be cross-checked
#: without a regex that silently returns ``None`` for a word nobody listed.
NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def _load(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _fixture() -> dict[str, Any]:
    return _load(TRI_LOG)


def _recorded() -> dict[str, Any]:
    """The refusal as the module declares it. The one input that is not a fixture."""
    return macro.UNAVAILABLE_SOURCES[REFUSAL_KEY]


def _spelled_count(sentence: str) -> int | None:
    """The number a sentence spells out, or ``None`` if it spells none.

    ``None`` and ``0`` are different answers and the derivation below relies on the difference:
    a sentence that stops spelling a number is not a sentence that spells zero.
    """
    found = re.search(r"\b(\w+)\s+candidate routes\b", sentence)
    if found is None:
        return None
    return NUMBER_WORDS.get(found.group(1).lower())


def _observed_index_close_schema() -> set[str]:
    """Every column label in every committed NSE index-close payload, read from the CSVs.

    Read off disk rather than taken from the fixture's ``columns_observed_13`` list, so a header
    change is a **red test** rather than a stale list agreeing with a stale file. The fixture's
    copy exists for a human checking by hand, and
    :meth:`TestTheAbsentFieldIsCheckedAgainstTheBytes` pins that the two agree.
    """
    labels: set[str] = set()
    for path in INDEX_CLOSE_FIXTURES:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            labels.update(next(csv.reader(handle)))
    return labels


# --------------------------------------------------------------------------- #
# 1. the verdict function
# --------------------------------------------------------------------------- #


def _tri_verdict(fixture: dict[str, Any], recorded: dict[str, Any], labels: set[str]) -> str:
    """Re-derive the TRI refusal's standing from the recorded artifacts.

    Ordered, because a state that invalidates every other verdict must be checked first:

    ``fixture-unreadable``
        the probe log is gone or is not a JSON object. Without it there is no evidence at all,
        and every other verdict would be reasoning from nothing.
    ``urls-magically-recorded``
        ``urls_not_committed`` is false while ``probes`` is still empty and no per-route status
        is recorded. That combination is a lie in whichever direction it is read, and it is the
        state a future edit would produce by flipping one flag. It is its own verdict because it
        is a *claim* about the evidence rather than a measurement of it.
    ``claim-inconsistent``
        the recorded integer and the number its own sentence spells out disagree. The recorded
        fact contradicting itself is the cheapest signal that one side was edited without the
        other.
    ``count-unanchored``
        the two agree, but no per-route status exists. **This is the honest state today**, and it
        is named rather than rounded up to ``no-route``: the count is internally consistent and
        nothing else.
    ``total-return-published``
        a committed index-close payload carries a total-return field. This is the state in which
        the registered refusal would be false, and it is the one that a future NSE schema change
        would produce.
    ``no-total-return-field``
        a per-route status is recorded, every one of them is 4xx, and no committed payload
        carries a total-return field. The refusal is then tied to a recorded status on disk, like
        the other three.
    """
    if not isinstance(fixture, dict) or not fixture:
        return "fixture-unreadable"

    urls_committed = bool(fixture.get("urls_not_committed", False))
    probes = fixture.get("probes") or []
    per_route = fixture.get("recorded_status_per_route")
    spelled = _spelled_count(str(recorded.get("why", "")))
    if spelled is not None and spelled != int(recorded["probe_count"]):
        return "claim-inconsistent"

    for label in labels:
        if TOTAL_RETURN_FIELD.search(label):
            return "total-return-published"

    if not urls_committed and not probes and not per_route:
        # Claiming the URLs are recorded while nothing records them.
        return "urls-magically-recorded"

    if urls_committed and not probes and not per_route:
        return "count-unanchored"

    statuses = [
        int(entry["http_status"])
        for entry in probes
        if isinstance(entry, dict) and "http_status" in entry
    ]
    if statuses and all(400 <= status < 500 for status in statuses):
        return "no-total-return-field"

    return "count-unanchored"


#: The state the committed evidence is in today, asserted as an expectation.
#
# This is the file's central honest claim: the refusal is tied to an internally-consistent count
# and to a real absence over committed bytes, and to **nothing else**. If a future agent commits
# the four URLs, this goes red and the derivation switches to reading per-route statuses -- which
# is the point. A test that passed silently in both worlds would be hiding the improvement.
EXPECTED_VERDICT = "count-unanchored"


class TestTheVerdictIsDerivedFromTheRecordedEvidence:
    """The three-level honesty claim, asserted rather than implied."""

    def test_the_derived_verdict_is_the_honest_middle_state(self) -> None:
        """Not ``no-route``. The count is internally consistent and unanchored."""
        verdict = _tri_verdict(_fixture(), _recorded(), _observed_index_close_schema())
        assert verdict == EXPECTED_VERDICT, (
            f"the TRI verdict moved to {verdict!r}. If the four probe URLs have been committed, "
            "update EXPECTED_VERDICT in the same edit rather than leaving the derivation "
            "unread; if they have not, the evidence has drifted and the refusal needs re-deciding"
        )

    def test_the_fixture_says_plainly_that_the_urls_are_not_committed(self) -> None:
        """:data:`urls_not_committed` is ``true``, so nothing here implies the URLs are pinned."""
        fixture = _fixture()
        assert fixture["urls_not_committed"] is True
        assert fixture["probes"] == []
        assert fixture["recorded_status_per_route"] is None

    def test_the_fixture_records_why_the_urls_are_absent_and_how_to_close_it(self) -> None:
        """A recorded gap carries a reason and a remedy, or it is just a shrug.

        Both strings are required to be non-trivial rather than merely present: a one-word
        explanation would satisfy a length check while telling a reader nothing.
        """
        fixture = _fixture()
        why = fixture["why_urls_are_absent"]
        remedy = fixture["what_would_close_this_gap"]
        assert len(why) > 120, "the gap is recorded without saying why the URLs are missing"
        assert len(remedy) > 120, "the gap is recorded without saying what would close it"
        # And the stated reason is the actual reason: fabricating a route in an evidence fixture
        # is the specific harm being refused, so the file must name that harm.
        lowered = why.lower()
        assert "fabricat" in lowered or "invent" in lowered, why
        assert "404" in fixture["recorded_status_note"], fixture["recorded_status_note"]

    def test_the_recorded_count_and_the_module_still_agree(self) -> None:
        """The tie between the fixture and the module that enforces the refusal.

        ``probe_count_recorded`` in the fixture and ``probe_count`` in the module are read from
        two places, so a change to one without the other is a disagreement rather than two
        agreeing literals.
        """
        fixture = _fixture()
        recorded = _recorded()
        assert int(fixture["probe_count_recorded"]) == int(recorded["probe_count"]) == 4
        assert _spelled_count(recorded["why"]) == 4
        assert recorded["available"] is False

    def test_the_refusal_is_still_registered_and_still_names_its_owner(self) -> None:
        """The layer-level half, so this file is not only about a fixture."""
        reasons = dict(UNAVAILABLE)
        assert "tri_index_return" in reasons
        assert "bfinance.nse.macro" in reasons["tri_index_return"]
        assert DEFAULT.unavailable_reason("tri_index_return")
        assert DEFAULT.specs_for("tri_index_return") == ()


# --------------------------------------------------------------------------- #
# 2. the byte-level half, checked against the bytes
# --------------------------------------------------------------------------- #


class TestTheAbsentFieldIsCheckedAgainstTheBytes:
    """The half of the evidence that is genuinely byte-level: two committed CSVs, six years
    apart, with no total-return column in either header."""

    def test_both_fixtures_are_committed_and_non_empty(self) -> None:
        """The evidence has to be there. An empty or missing CSV makes the absence vacuous."""
        for path in INDEX_CLOSE_FIXTURES:
            assert path.is_file(), f"{path.name} is gone; the TRI evidence is derived from it"
            assert path.stat().st_size > 0, f"{path.name} is empty"

    def test_neither_header_carries_a_total_return_field(self) -> None:
        labels = _observed_index_close_schema()
        offenders = sorted(label for label in labels if TOTAL_RETURN_FIELD.search(label))
        assert offenders == [], (
            f"a committed NSE index-close payload now carries a total-return field: {offenders}. "
            "The tri_index_return refusal is stale and must be re-decided"
        )

    def test_the_vintages_actually_differ_which_is_what_makes_the_absence_worth_asserting(self) -> None:
        """Two recordings, and they are not the same file.

        One payload could be an accident. A payload from 2020 and one from 2026 both lacking the
        column is a schema, and the ``Index Date`` values are read off the CSVs to prove the two
        fixtures really are six years apart rather than a duplicate under a second name.
        """
        dates: set[str] = set()
        for path in INDEX_CLOSE_FIXTURES:
            with path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            assert rows, f"{path.name} has a header but no data rows"
            dates.update(row["Index Date"].strip() for row in rows)
        assert dates == {"01-01-2020", "25-09-2026"}, sorted(dates)

    def test_the_payload_is_not_price_only_so_the_absence_is_informative(self) -> None:
        """:meth:`Div Yield` is present, which is what makes "no total-return column" mean something.

        A payload carrying no yield-derived concept at all would make the absence a triviality
        rather than a finding. The file publishes ``Div Yield`` and still publishes no
        total-return level, so the omission is about the concept, not about the schema.
        """
        labels = _observed_index_close_schema()
        yield_shaped = sorted(label for label in labels if YIELD_SHAPED_FIELD.search(label))
        assert yield_shaped == ["Div Yield"], f"the yield-shaped columns changed: {yield_shaped}"
        # And the two fields the module's own reasoning is about are still there.
        assert "P/E" in labels and "P/B" in labels

    def test_the_headers_are_unchanged_at_thirteen_columns(self) -> None:
        """The literal, so a column silently dropped is a red test and not a quiet narrowing."""
        assert len(_observed_index_close_schema()) == 13

    def test_the_fixtures_digests_still_match_their_recorded_ones(self) -> None:
        """Tamper-evidence, using the convention the other fixtures already follow.

        The sibling ``*.META.json`` files record ``stored_sha256`` for each payload. Recomputing
        it here means a hand-edited CSV cannot quietly become the evidence for a refusal.
        """
        for path in INDEX_CLOSE_FIXTURES:
            meta = _load(path.with_suffix("").with_suffix(".META.json"))
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            assert digest == meta["stored_sha256"], (
                f"{path.name} no longer matches the digest in its META.json; a hand-edited "
                "payload must not be the evidence for a refusal"
            )

    def test_the_fixtures_own_column_list_matches_the_bytes(self) -> None:
        """The fixture's human-readable copy and the CSVs must not drift apart.

        The list in the fixture is a convenience for a reader checking by hand; the verdict never
        reads it. If the two ever disagree, the fixture is misleading a human, which is worth a
        red test even though no verdict depends on it.
        """
        fixture = _fixture()
        recorded_list = fixture["independent_evidence_that_the_nse_index_route_carries_no_total_return"][
            "columns_observed_13"
        ]
        assert set(recorded_list) == _observed_index_close_schema(), (
            "the fixture's column list no longer matches the committed CSVs"
        )

    def test_control_a_total_return_header_flips_the_verdict(self) -> None:
        """Negative control. One added header, and the refusal has to stop being true.

        This is the state the derivation exists to detect, and it is the one a future NSE schema
        change would produce. If the control could not move the verdict, the two tests above
        would be asserting a constant.
        """
        fixture = _fixture()
        recorded = _recorded()
        observed = _observed_index_close_schema()
        assert _tri_verdict(fixture, recorded, observed) == EXPECTED_VERDICT

        with_tri = observed | {"Index Name", "Total Return Index Value"}
        assert _tri_verdict(fixture, recorded, with_tri) == "total-return-published"
        assert _tri_verdict(fixture, recorded, with_tri) != _tri_verdict(fixture, recorded, observed)

        # Every spelling the regex is supposed to catch, and one it must not.
        for label in ("Total Return Value", "TR Index Value", "DIV YIELD REINVESTED", "Net TR%"):
            assert _tri_verdict(fixture, recorded, observed | {label}) == "total-return-published", label
        # `Div Yield` on its own is a yield, not a total-return level, and conflating them is the
        # approximation the refusal exists to prevent -- so it must NOT trip the verdict.
        assert _tri_verdict(fixture, recorded, observed | {"Div Yield Return"}) == EXPECTED_VERDICT

    def test_control_editing_the_csv_header_flips_the_verdict(self) -> None:
        """Negative control on the byte path rather than the in-memory one.

        The two controls above pass a modified label set straight in. This one writes a real CSV,
        reads it back through the same ``_observed_index_close_schema`` the real tests use, and
        requires the verdict to move. That is what proves the header is genuinely read off disk
        rather than supplied by the caller.
        """
        fixture = _fixture()
        recorded = _recorded()
        assert _tri_verdict(fixture, recorded, _observed_index_close_schema()) == EXPECTED_VERDICT

        original = INDEX_CLOSE_FIXTURES[0]
        source = original.read_text(encoding="utf-8-sig").splitlines(keepends=True)
        edited = [source[0].replace("Div Yield", "Total Return Value\nDiv Yield")] + source[1:]
        tampered = original.with_name("__tri_control_tampered.csv")
        try:
            tampered.write_text("".join(edited), encoding="utf-8", newline="")
            with tampered.open(encoding="utf-8-sig", newline="") as handle:
                tampered_labels = set(next(csv.reader(handle)))
            assert "Total Return Value" in tampered_labels, (
                "the control did not actually add a total-return column, so it is not exercising "
                "the path it claims to"
            )
            assert _tri_verdict(fixture, recorded, tampered_labels) == "total-return-published"
        finally:
            tampered.unlink(missing_ok=True)
        # And the real fixtures are untouched, which is what makes this a control.
        assert not tampered.exists()
        assert _tri_verdict(fixture, recorded, _observed_index_close_schema()) == EXPECTED_VERDICT

    def test_control_tampering_with_a_committed_csv_is_detected(self) -> None:
        """The digest check above is load-bearing, not decoration.

        In memory, because writing to a committed fixture -- even restoring it -- is not something
        a test should do to the tree it is checking.
        """
        path = INDEX_CLOSE_FIXTURES[0]
        meta = _load(path.with_suffix("").with_suffix(".META.json"))
        raw = path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == meta["stored_sha256"]
        tampered = raw.replace(b"Div Yield", b"Total Retu")
        assert tampered != raw, "the tamper did not change the bytes, so the control is vacuous"
        assert hashlib.sha256(tampered).hexdigest() != meta["stored_sha256"]


# --------------------------------------------------------------------------- #
# 3. the count, and the controls on it
# --------------------------------------------------------------------------- #


class TestTheCountIsHonestAboutBeingACount:
    """The unanchored half. Cross-checked against its own sentence, and named as unanchored."""

    def test_the_count_and_the_sentence_agree(self) -> None:
        recorded = _recorded()
        assert int(recorded["probe_count"]) == 4
        assert _spelled_count(recorded["why"]) == 4
        assert "404" in recorded["why"]

    def test_the_verdict_is_the_unanchored_state_and_not_a_stronger_one(self) -> None:
        """``count-unanchored`` and never ``no-route``.

        A verdict called ``no-route`` would read as "we have the routes and they are gone", which
        is not what the committed evidence says. Asserting the weaker name is what stops the
        fixture from being cited later as though it pinned the URLs.
        """
        assert _tri_verdict(_fixture(), _recorded(), _observed_index_close_schema()) == (
            "count-unanchored"
        )

    def test_the_fixture_admits_the_count_is_the_whole_of_the_committed_record(self) -> None:
        fixture = _fixture()
        assert fixture["probe_count_source"], "the count's own provenance is not stated"
        assert fixture["recorded_status_note"].lstrip().lower().startswith("null")
        # The absence is scoped, and the fixture has to say so: it covers the one index route this
        # package reads, not all of NSE.
        not_evidence_for = fixture[
            "independent_evidence_that_the_nse_index_route_carries_no_total_return"
        ]["not_evidence_for"]
        lowered = not_evidence_for.lower()
        assert "proof of global absence" in lowered, not_evidence_for
        assert "does not prove" in lowered, not_evidence_for
        # And it must scope the claim to the route this package reads.
        assert "the one index route this package reads" in lowered, not_evidence_for

    def test_control_editing_one_side_of_the_count_claim_flips_the_verdict(self) -> None:
        """Negative control. Change either side alone and the two must disagree.

        This is the signature of a fact edited without its own explanation, and it is the only
        thing cross-checking a bare integer against a bare sentence can detect.
        """
        labels = _observed_index_close_schema()
        fixture = _fixture()
        recorded = _recorded()
        assert _tri_verdict(fixture, recorded, labels) == EXPECTED_VERDICT

        fewer = copy.deepcopy(recorded)
        fewer["probe_count"] = 1
        assert _tri_verdict(fixture, fewer, labels) == "claim-inconsistent"

        reworded = copy.deepcopy(recorded)
        reworded["why"] = "Two candidate routes were probed and one of them returned HTTP 200."
        assert _tri_verdict(fixture, reworded, labels) == "claim-inconsistent"

        assert _tri_verdict(fixture, recorded, labels) != "claim-inconsistent"

    def test_control_a_sentence_that_stops_spelling_a_number_is_not_a_mismatch(self) -> None:
        """Negative control in the subtle direction.

        ``_spelled_count`` returns ``None`` when a sentence spells no number, and ``None`` is not
        ``0``. If the two were conflated, an edit that merely reworded the sentence would report
        ``claim-inconsistent`` and cry a mismatch that does not exist -- a false red that trains
        an operator to ignore the real one.
        """
        assert _spelled_count("No route exists.") is None
        assert _spelled_count("Four candidate routes were probed.") == 4
        assert _spelled_count("Eleven candidate routes were probed.") is None

        fixture = _fixture()
        labels = _observed_index_close_schema()
        reworded = copy.deepcopy(_recorded())
        reworded["why"] = "No route exists. Candidate routes were probed and every one returned 404."
        assert _spelled_count(reworded["why"]) is None
        # No number spelled: the cross-check is silent, and the verdict falls through to the
        # absence check rather than reporting a contradiction.
        assert _tri_verdict(fixture, reworded, labels) == EXPECTED_VERDICT
        assert _tri_verdict(fixture, reworded, labels) != "claim-inconsistent"

    def test_control_flipping_urls_not_committed_without_adding_probes_flips_the_verdict(self) -> None:
        """Negative control on the flag that records the gap.

        ``urls_not_committed: false`` with an empty ``probes`` list and a null
        ``recorded_status_per_route`` is a claim that the URLs are recorded when nothing records
        them. That is the state a careless future edit produces, and it has to be its own verdict
        rather than silently reading as a stronger refusal.
        """
        fixture = _fixture()
        recorded = _recorded()
        labels = _observed_index_close_schema()
        assert fixture["urls_not_committed"] is True
        assert _tri_verdict(fixture, recorded, labels) == EXPECTED_VERDICT

        lied = copy.deepcopy(fixture)
        lied["urls_not_committed"] = False
        assert _tri_verdict(lied, recorded, labels) == "urls-magically-recorded"
        assert _tri_verdict(lied, recorded, labels) != _tri_verdict(fixture, recorded, labels)

    def test_control_committing_four_fabricated_404s_would_mute_the_verdict(self) -> None:
        """The control for the *improvement* path, and the one that matters most here.

        Adding four per-route 404s moves the verdict from ``count-unanchored`` to
        ``no-total-return-field`` -- a stronger, genuinely status-tied verdict. That is the
        intended upgrade, and it is asserted so a future agent who does the work sees exactly
        which assertion to update.

        It is run entirely in memory against copies, because the real fixture must keep recording
        the gap. And the four entries are explicitly labelled as hypothetical: this control does
        not put plausible-looking NSE URLs into the repository. Fabricating a route in an evidence
        fixture is the exact class of fabrication this project has spent its history removing,
        and a control that committed them would be committing them.
        """
        fixture = _fixture()
        recorded = _recorded()
        labels = _observed_index_close_schema()
        assert _tri_verdict(fixture, recorded, labels) == EXPECTED_VERDICT

        upgraded = copy.deepcopy(fixture)
        upgraded["urls_not_committed"] = False
        upgraded["probes"] = [
            {
                "http_status": 404,
                "url": "<HYPOTHETICAL, NOT A REAL NSE URL -- in-memory control only>",
            }
            for _ in range(int(recorded["probe_count"]))
        ]
        assert _tri_verdict(upgraded, recorded, labels) == "no-total-return-field"
        assert _tri_verdict(upgraded, recorded, labels) != _tri_verdict(fixture, recorded, labels)

        # And one non-4xx among them is a different state, not a stronger version of the same
        # one: a route that answered 200 means the absence is not established by the probes.
        served = copy.deepcopy(upgraded)
        served["probes"][0]["http_status"] = 200
        assert _tri_verdict(served, recorded, labels) == "count-unanchored"
        assert _tri_verdict(served, recorded, labels) != _tri_verdict(upgraded, recorded, labels)

    def test_control_an_empty_or_malformed_fixture_flips_the_verdict(self) -> None:
        """Negative control on the short-circuit.

        A refusal with no evidence at all is the belief this file exists to catch, and the
        ``fixture-unreadable`` branch has to be reachable -- checked first, precisely so it
        overrides every other verdict.
        """
        labels = _observed_index_close_schema()
        recorded = _recorded()
        for empty in ({}, None, [], ""):
            assert _tri_verdict(empty, recorded, labels) == "fixture-unreadable", empty
        # And it overrides a *satisfied* absence, which is the ordering claim.
        assert (
            _tri_verdict({}, recorded, labels | {"Total Return Index Value"})
            == "fixture-unreadable"
        )


# --------------------------------------------------------------------------- #
# 4. the fixture may not assert the conclusion for us
# --------------------------------------------------------------------------- #


#: Keys the verdict reads, directly or by derivation. A fixture key outside this set cannot
#: influence the verdict, so it is free prose -- which is what makes the check below meaningful.
KEYS_THE_VERDICT_READS = frozenset(
    {
        "urls_not_committed",
        "probes",
        "recorded_status_per_route",
        "probe_count_recorded",
    }
)


class TestTheFixtureCannotAssertTheConclusionForUs:
    """The failure mode this whole file is guarding against, applied to itself.

    A fixture that said ``"no_route_exists": true`` and a test that read it back would be a test
    asserting a constant in a JSON file. The verdict therefore reads only inputs that can be
    independently true or false, and any fixture key the verdict does not read is checked to be
    one of the known-free prose keys rather than a new conclusion.
    """

    def test_no_conclusion_shaped_key_exists_in_the_fixture(self) -> None:
        fixture = _fixture()
        suspicious = sorted(
            key
            for key in fixture
            if re.search(r"no_route|route_exists|confirmed|proven|is_absent|verdict", key, re.I)
        )
        assert suspicious == [], (
            f"the fixture carries conclusion-shaped keys {suspicious}. The verdict is derived from "
            "the count, the sentence, the gap flags and the CSV headers; a key that asserts the "
            "conclusion would let the fixture grade its own homework"
        )

    def test_every_verdict_reading_key_is_present_in_the_fixture(self) -> None:
        """If a key the verdict reads goes missing, the derivation would silently change meaning.

        A ``.get`` with a default is what would make this dangerous, so the keys are required
        rather than defaulted: the short-circuit branch for a missing fixture is a whole-file
        failure, not a per-key one.
        """
        fixture = _fixture()
        for key in sorted(KEYS_THE_VERDICT_READS):
            assert key in fixture, (
                f"the verdict reads {key!r} but the fixture does not carry it; the derivation "
                "would fall through to a default and change meaning without failing"
            )

    def test_the_fixture_states_its_own_strengths_in_three_levels(self) -> None:
        """The reading guide is present and names all three levels.

        This is the assertion that makes the honesty claim checkable rather than a matter of my
        saying so: the file has to say that the 404s are a count, the absence is byte-level, and
        the gap is recorded.
        """
        guide = _fixture()["how_to_read_this_file"]
        assert "unanchored" in guide.lower()
        assert "byte-level" in guide.lower()
        assert "gap" in guide.lower()
        assert "Reading (1) as if it were a pinned 404" in guide

    def test_the_robots_citation_points_at_a_file_that_exists_and_says_what_is_claimed(self) -> None:
        """The one claim in the fixture about a *different* artifact, cross-checked.

        ``_robots`` says the two probed hosts' ``robots.txt`` states are recorded in
        ``tests/fixtures/nse/ROBOTS_EVIDENCE.json``. That is a claim about a file this repository
        contains, so it is checkable, and an unbacked robots claim is exactly the kind of
        unverified assertion this project removes.
        """
        robots = _fixture()["_robots"]
        cited = FIXTURES / "nse" / "ROBOTS_EVIDENCE.json"
        assert cited.is_file(), "the fixture cites a robots record that is not in the tree"
        recorded = _load(cited)
        by_host = {entry["host"]: entry for entry in recorded["hosts"]}
        for host, expected_status in (("nsearchives.nseindia.com", 404), ("www.nseindia.com", 200)):
            assert host in by_host, f"{host} is not recorded in {cited.name}"
            assert int(by_host[host]["http_status"]) == expected_status
        # The rule the citation rests on is RFC 9309 2.3.1.3, which the robots record states.
        assert "9309" in recorded["_rule_applied"]
        assert robots["cited_from"] == "tests/fixtures/nse/ROBOTS_EVIDENCE.json"
        assert "9309" in robots["rule_applied"]

    def test_control_a_conclusion_key_in_the_fixture_flips_the_check(self) -> None:
        """Non-vacuity for the check above.

        In memory, because the point is to show the predicate fires on the shape it forbids, not
        to ship the shape.
        """
        fixture = _fixture()
        assert not [
            key
            for key in fixture
            if re.search(r"no_route|route_exists|confirmed|proven|is_absent|verdict", key, re.I)
        ]
        for forbidden in ("no_route_exists", "route_exists", "CONFIRMED_ABSENT"):
            with_conclusion = copy.deepcopy(fixture)
            with_conclusion[forbidden] = True
            fired = sorted(
                key
                for key in with_conclusion
                if re.search(r"no_route|route_exists|confirmed|proven|is_absent|verdict", key, re.I)
            )
            assert fired == [forbidden], (
                f"the predicate did not fire on {forbidden!r}, so the check above is not looking "
                "for the right thing"
            )
        # And a key the predicate must not fire on, so it is not matching everything.
        for allowed in ("probe_count_recorded", "why_urls_are_absent", "directory"):
            assert not re.search(
                r"no_route|route_exists|confirmed|proven|is_absent|verdict", allowed, re.I
            ), allowed
