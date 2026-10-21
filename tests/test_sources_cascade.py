"""
The cross-capability layer, exercised offline: failover, the basis seam, and the isolation.

Everything here runs from stubs. No socket is opened, no fixture directory is read, and no source
module is imported except in the subprocess probes that exist precisely to check which modules a
bare ``import`` pulls in. That last part is not an accident of the test design — it is the
mechanism the layer uses, and the tests have to be able to see it.

The single most important test in this file is
:func:`TestCascade.test_the_cascade_falls_through_and_records_it`. A cascade that works and leaves
no trace is indistinguishable from a cascade that was never exercised, so "provider 2 served" is
only half the claim; the other half is that provider 1's failure is written down with its reason,
in a place a caller can read without attaching a debugger.

The four properties under test, and what would break if each regressed
---------------------------------------------------------------------
============================  =========================================================
failover is real              a raise falls through, and the skip is in ``attempts``
unavailability is typed       a raise naming the capability and every decliner — never an
                              empty frame, which is a vendor's way of saying nothing
health is observable          per-source counts and last error, and a failing source is
                              asked *later*
the basis seam is visible     38,290.1 and 40,979.6 for the same row and the same period are
                              reported as different, never averaged
============================  =========================================================

Negative controls, and why this file has four of them
-----------------------------------------------------
Every control here exists because its corresponding test could otherwise pass for the wrong
reason, and three of the four guard tests that are *capable of passing vacuously*:

* a subprocess probe that never fails is the single most dangerous shape a test can take, and this
  repo has already been bitten by one (:mod:`tests.test_nse_macro` carries a worked example).
  :func:`TestSubprocessProbes.test_the_subprocess_probe_reports_a_real_answer` runs the same
  probe against a deliberately broken package and requires the two answers to differ.
* a series that refuses everything would make the mismatch test pass for free, so
  :func:`TestNegativeControls.test_the_basis_seam_control_succeeds_when_the_bases_agree` proves
  the same code path returns a merged mapping for a comparable pair.
* and
  :func:`TestNegativeControls.test_a_broken_comparability_check_would_blend_the_seam` patches
  :meth:`~bfinance.sources.model.Basis.comparable` to always agree and shows the seam silently
  collapsing — which is the bug this package was written to prevent, reproduced deliberately so
  the test that guards against it is known to be load-bearing.
"""

from __future__ import annotations

import ast
import importlib
import subprocess
import sys
import textwrap
from pathlib import Path

import pandas as pd
import pytest

import bfinance
from bfinance.sources import (
    ATTEMPT_OUTCOMES,
    ATTEMPTS_ATTR,
    BASIS_AXIS,
    CALLED_OUTCOMES,
    COSTS_PROVIDER,
    DEFAULT,
    FAILED,
    HEALTH_ATTR,
    MOTHERSON_CONSOLIDATED,
    MOTHERSON_STANDALONE,
    OWNERSHIP_PROVIDER,
    PROVENANCE,
    SERVED,
    SKIPPED_BASIS,
    SKIPPED_COLUMNS,
    SKIPPED_HEALTH,
    STATEMENTS_PROVIDER,
    UNWIRED_NOTE,
    Attempt,
    Basis,
    BasisAxis,
    BasisMismatchError,
    HealthLedger,
    Registry,
    Resolved,
    Segment,
    Series,
    SourceBasisError,
    SourceCapabilityError,
    SourceHealth,
    SourceSpec,
    SourceUnavailableError,
    basis_from,
    build_default,
    cross_basis_note,
    default_ledger,
    default_registry,
    is_empty_payload,
    is_series,
    plan,
    resolve,
    segment_of,
    served,
    unavailable_message,
)
from bfinance.sources.health import HEALTH_ATTR as health_HEALTH_ATTR
from bfinance.sources.model import HEALTH_ATTR as model_HEALTH_ATTR
from bfinance.utils.exceptions import UpstreamServiceError

SRC_DIR = Path(bfinance.__file__).parent
SOURCES_DIR = SRC_DIR / "sources"

#: The six modules this layer describes. None of them may be imported by ``import bfinance`` or
#: by ``import bfinance.sources``; that is what makes the layer a router rather than a monolith.
#: ``bfinance.trendlyne`` is listed because the isolation test that forbids reaching it is the
#: reason one price source is registered but never wired.
SIX_MODULES = (
    "bfinance.statements",
    "bfinance.nse.macro",
    "bfinance.flows",
    "bfinance.universe",
    "bfinance.costs",
    "bfinance.ownership",
)

#: The leaf package ``tests/test_trendlyne_core.py::TestIsolation`` forbids every other module
#: under ``src/`` from naming. Named here as a string constant rather than imported, obviously.
LEAF_PACKAGE = "trend" + "lyne"


# --------------------------------------------------------------------------- #
# stubs
# --------------------------------------------------------------------------- #


def stub_source(
    name: str,
    capabilities: tuple,
    bases: tuple,
    *,
    note: str = "A stub. Its note exists because the registry refuses a row without one.",
) -> SourceSpec:
    """A registered-but-unwired source, which is what every test here needs.

    Unwired on purpose: the tests drive the cascade through ``fetch``, so a row that could be
    loaded would only add a way for a test to pass by accident. ``module`` and ``entry`` stay empty
    together, which :meth:`SourceSpec.__post_init__` requires.
    """
    return SourceSpec(name=name, capabilities=capabilities, bases=bases, note=note)


def registry_of(*specs: SourceSpec) -> Registry:
    """A registry holding exactly these sources, in this order."""
    registry = Registry()
    for spec in specs:
        registry.register(spec)
    return registry


def call_recorder(responses: dict):
    """A ``fetch`` that records every source it was asked for and replays ``responses``.

    ``responses`` maps a source name to either a value (served) or an exception instance (raised).
    An unlisted source raises, because a cascade test that silently succeeds for an unlisted
    source is a test that stopped testing the thing it names.
    """
    calls: list = []

    def fetch(spec, request):
        calls.append(spec.name)
        answer = responses.get(spec.name)
        if isinstance(answer, BaseException):
            raise answer
        if answer is None and spec.name not in responses:
            raise LookupError(f"{spec.name} has no scripted response in this test")
        return answer

    fetch.calls = calls
    return fetch


PRIMARY = stub_source(
    "primary", ("price_history",), (Basis.AS_TRADED,),
    note="First choice for an as-traded request. 269.0, matching bhavcopy ClsPric.",
)
SECONDARY = stub_source(
    "secondary", ("price_history",), (Basis.AS_TRADED,),
    note="Second as-traded source. MarketLens raw close; agrees with the bhavcopy.",
)
SPLIT_ADJUSTED = stub_source(
    "split-adjusted", ("price_history",), (Basis.SPLIT_ADJUSTED,),
    note="Publishes split/bonus-adjusted history. Never eligible for an as-traded request.",
)


# --------------------------------------------------------------------------- #
# 1. the cascade falls through, and records that it did
# --------------------------------------------------------------------------- #


class TestCascade:
    def test_the_cascade_falls_through_and_records_it(self):
        """The headline test. A raise is a fall-through, and the fall-through is written down.

        Both halves matter. "Provider 2 served" alone would be satisfied by a cascade that
        happened to try 2 first; asserting the *order of calls* and the *reason on the record* is
        what makes it a claim about failover.
        """
        fetch = call_recorder(
            {
                "primary": TimeoutError("www.nseindia.com did not respond after 30s"),
                "secondary": 269.0,
            }
        )
        health = HealthLedger()

        result = resolve(
            "price_history",
            request={"symbol": "ITC"},
            basis=Basis.AS_TRADED,
            registry=registry_of(PRIMARY, SECONDARY, SPLIT_ADJUSTED),
            health=health,
            fetch=fetch,
        )

        # 2 served, and we know which one and why the other did not.
        assert result.source == "secondary"
        assert result.value == 269.0
        assert result.degraded is True

        # The order is observable, not inferred from the value.
        assert fetch.calls == ["primary", "secondary"], (
            "the cascade must try the registry order first and only then fall through"
        )

        # The failure is named, with the exception type a reader would need to triage it and the
        # message that says what actually happened.
        first = result.attempts[0]
        assert first.provider == "primary"
        assert first.outcome == FAILED
        assert "TimeoutError" in first.reason
        assert "nseindia.com" in first.reason
        assert "did not respond after 30s" in first.reason

        # And it reached the provenance the caller actually reads.
        attrs = result.render_attrs()
        assert attrs["bfinance_statements_fallback"] is True
        assert attrs["bfinance_statements_providers_tried"] == ["primary", "secondary"]
        assert attrs["bfinance_sources_capability"] == "price_history"
        assert result.failed_providers == ("primary",)

    def test_a_healthy_first_choice_is_not_degraded(self):
        """The mirror of the test above, and the reason the flag is worth reading.

        Without this, ``degraded`` could be a constant ``True`` and the failover test would still
        be green.
        """
        fetch = call_recorder({"primary": 269.0, "secondary": 269.5})
        result = resolve(
            "price_history",
            basis=Basis.AS_TRADED,
            registry=registry_of(PRIMARY, SECONDARY),
            health=HealthLedger(),
            fetch=fetch,
        )
        assert result.source == "primary"
        assert result.degraded is False
        assert fetch.calls == ["primary"], "the second source must not be asked once one answered"

    def test_a_basis_skip_is_not_degradation(self):
        """Passing over a source that *cannot* serve is the system working.

        If it counted as degradation, a caller would learn to ignore the flag — which is how the
        flag stops carrying information at exactly the moment a fallback was genuine.
        """
        fetch = call_recorder({"split-adjusted": 134.5, "secondary": 269.0})
        result = resolve(
            "price_history",
            basis=Basis.AS_TRADED,
            registry=registry_of(SPLIT_ADJUSTED, SECONDARY),
            health=HealthLedger(),
            fetch=fetch,
        )
        assert result.source == "secondary"
        assert result.degraded is False
        assert [a.outcome for a in result.attempts] == [SKIPPED_BASIS, SERVED]

    def test_a_basis_mismatch_alone_is_degradation(self):
        """A hop that changes basis is degradation even when nothing failed.

        The caller asked for one basis and got another, with no exception anywhere to point at. If
        the flag did not cover this, the one case with no failure to notice would be the one case
        that looked fine.
        """
        row_claims_consolidated = stub_source(
            "dual-basis-row", ("shareholders_funds",), (Basis.CONSOLIDATED,),
            note="Advertises consolidated; the payload may disagree, as the second vendor's does.",
        )
        honest = resolve(
            "shareholders_funds", basis=Basis.CONSOLIDATED,
            registry=registry_of(row_claims_consolidated), health=HealthLedger(),
            fetch=call_recorder({"dual-basis-row": MOTHERSON_CONSOLIDATED}),
        )
        assert honest.basis_mismatch is False
        assert honest.degraded is False
        assert honest.basis_note is None

        # Same source, same request, a payload that turns out to be the other basis. Nothing
        # failed; the answer is simply not the one that was asked for.
        hopped = resolve(
            "shareholders_funds", basis=Basis.CONSOLIDATED,
            registry=registry_of(row_claims_consolidated), health=HealthLedger(),
            fetch=call_recorder(
                {
                    "dual-basis-row": served(
                        MOTHERSON_STANDALONE, Basis.STANDALONE, source="dual-basis-row"
                    )
                }
            ),
        )
        assert [a.outcome for a in hopped.attempts] == [SERVED]
        assert hopped.basis_mismatch is True
        assert hopped.degraded is True
        assert "-50.00%" not in (hopped.basis_note or "")
        assert "2,689.5" in hopped.basis_note


def _raise(message: str) -> BaseException:
    return ConnectionError(message)


# --------------------------------------------------------------------------- #
# 2. the basis seam
# --------------------------------------------------------------------------- #


class TestBasisSeam:
    """The thing this layer was built for.

    The figures are not hypothetical. The second vendor publishes **both** accounting bases in one
    response, and its rendered page prints standalone while the XHR behind that page reports
    ``is_consolidated: true`` — 38,290.1 against 40,979.6, for the same row and the same period.
    Any arithmetic applied to the two produces a number that looks right and is wrong.
    """

    def test_the_two_figures_are_reported_as_different_and_not_reconciled(self):
        """A series across the accounting seam refuses to collapse, and says how big the gap is."""
        series = Series.build(
            [
                Segment(
                    source="consolidated-row",
                    basis=Basis.CONSOLIDATED,
                    values={"2025-03": 39112.4, "2026-03": MOTHERSON_CONSOLIDATED},
                    as_of="2026-03-31",
                ),
                Segment(
                    source="standalone-row",
                    basis=Basis.STANDALONE,
                    values={"2026-03": MOTHERSON_STANDALONE},
                    as_of="2026-03-31",
                ),
            ]
        )

        # They are, first of all, different numbers. Asserted before anything else so the rest of
        # the test cannot be read as a claim that they are close enough to reconcile.
        assert MOTHERSON_STANDALONE != MOTHERSON_CONSOLIDATED
        assert round(MOTHERSON_CONSOLIDATED - MOTHERSON_STANDALONE, 1) == 2689.5

        assert series.basis_mismatch is True
        assert series.comparable is False
        assert series.bases() == (Basis.CONSOLIDATED, Basis.STANDALONE)

        with pytest.raises(BasisMismatchError) as excinfo:
            series.frame()
        message = str(excinfo.value)

        # Both figures, in the refusal, so the reader can check the arithmetic instead of taking
        # the package's word for it.
        assert "38,290.1" in message
        assert "40,979.6" in message
        assert "2,689.5" in message
        assert "2026-03" in message
        assert "consolidated-row" in message
        assert "standalone-row" in message

        # And nothing resembling a mean of the two appears anywhere in the object or its message.
        mean = (MOTHERSON_STANDALONE + MOTHERSON_CONSOLIDATED) / 2
        assert mean not in (MOTHERSON_STANDALONE, MOTHERSON_CONSOLIDATED)
        assert f"{mean:,.1f}" not in message

    def test_reading_a_seam_period_by_name_is_also_refused(self):
        """The narrow version of the trap: one cell, two answers, no way to pick.

        Refusing the whole series is the general rule, but a caller who asks for one named period
        deserves the same answer when that period is the one that is ambiguous.
        """
        series = Series.build(
            [
                Segment("consolidated-row", Basis.CONSOLIDATED, {"2026-03": MOTHERSON_CONSOLIDATED}),
                Segment("standalone-row", Basis.STANDALONE, {"2026-03": MOTHERSON_STANDALONE}),
            ]
        )
        with pytest.raises(BasisMismatchError) as excinfo:
            series.value_at("2026-03")
        message = str(excinfo.value)
        assert str(MOTHERSON_CONSOLIDATED) in message
        assert str(MOTHERSON_STANDALONE) in message
        assert "not reconciled" in message

    def test_an_as_traded_and_a_split_adjusted_series_do_not_join(self):
        """The other seam, and the one that fabricates a price move.

        A 1:2 split shows up in the as-traded series as a -50.00% move that never happened. The
        refusal quotes that rather than merely asserting it.
        """
        series = Series.build(
            [
                Segment("nse-bhavcopy", Basis.AS_TRADED, {"2026-03-01": 500.0, "2026-03-02": 250.0}),
                Segment("vendor-b-adjustments", Basis.SPLIT_ADJUSTED, {"2026-03-02": 250.0}),
            ]
        )
        assert series.basis_mismatch is True
        with pytest.raises(BasisMismatchError) as excinfo:
            series.frame()
        message = str(excinfo.value)
        assert "-50.00%" in message
        assert "as_traded" in message
        assert "split_adjusted" in message

    def test_the_price_basis_column_is_what_routes_an_as_traded_request(self):
        """A basis-ineligible source is **never called**. Not called, then rejected — never called.

        Asserted on the call list rather than on the outcome, because the outcome would be
        identical if the source had been called and its answer discarded. For a rate-limited vendor
        that discarded request is the expensive one, which is the whole reason the check runs
        before the call. The ineligible source raises if it is ever reached, so a regression that
        called it would fail loudly rather than return a plausible number.
        """
        calls: list = []

        def strict_fetch(spec, request):
            calls.append(spec.name)
            if spec.name == "split-adjusted":
                raise AssertionError(
                    "a basis-ineligible source must not be called at all: it publishes only "
                    "split-adjusted prices and the request was as-traded"
                )
            return 269.0

        result = resolve(
            "price_history",
            basis=Basis.AS_TRADED,
            registry=registry_of(SPLIT_ADJUSTED, SECONDARY),
            health=HealthLedger(),
            fetch=strict_fetch,
        )

        assert "split-adjusted" not in calls, (
            "a source that publishes only split-adjusted prices must not be asked for an "
            "as-traded request"
        )
        assert result.source == "secondary"

        skipped = [a for a in result.attempts if a.provider == "split-adjusted"]
        assert len(skipped) == 1
        assert skipped[0].outcome == SKIPPED_BASIS
        assert skipped[0].called is False
        assert "split_adjusted" in skipped[0].reason
        assert "as_traded" in skipped[0].reason
        assert "split-adjusted" not in result.render_attrs()["bfinance_statements_providers_tried"]
        assert "split-adjusted" in result.render_attrs()["bfinance_statements_skip_reasons"]

    def test_every_source_must_publish_the_requested_basis_before_any_is_called(self):
        """The skip happens for the ineligible row even when it is first in the order."""
        calls: list = []

        def fetch(spec, request):
            calls.append(spec.name)
            return 1.0

        with pytest.raises(SourceBasisError) as excinfo:
            resolve(
                "price_history",
                basis=Basis.AS_TRADED,
                registry=registry_of(SPLIT_ADJUSTED),
                health=HealthLedger(),
                fetch=fetch,
            )
        assert calls == [], "a source that cannot serve the basis must not be called even once"
        message = str(excinfo.value)
        assert "split_adjusted" in message
        assert "relax the request, not to retry" in message, (
            "the two refusals must differ in what they tell a caller to do next, or the "
            "distinction is decoration"
        )

    def test_an_impossible_basis_and_a_transient_failure_are_different_refusals(self):
        """Two failures, two different remedies, and the types have to say so.

        Reporting "nobody publishes the split-adjusted basis" as an upstream failure tells a caller
        to retry a request that can never succeed, and records four failures in the health ledger
        for sources that were never asked. Both are wrong, and both are the kind of thing that
        trains an operator to ignore the error.
        """
        # A basis nobody publishes: permanent, and nothing upstream is broken.
        with pytest.raises(SourceBasisError) as impossible:
            resolve(
                "price_history", basis=Basis.AS_TRADED, registry=registry_of(SPLIT_ADJUSTED),
                health=HealthLedger(), fetch=call_recorder({"split-adjusted": 1.0}),
            )
        assert not isinstance(impossible.value, UpstreamServiceError), (
            "a basis no source publishes is not an upstream failure, and must not be caught by a "
            "handler that retries"
        )
        assert isinstance(impossible.value, ValueError)
        # And it costs no health: the source was never asked.
        assert impossible.value is not None
        ledger = HealthLedger()
        with pytest.raises(SourceBasisError):
            resolve(
                "price_history", basis=Basis.AS_TRADED, registry=registry_of(SPLIT_ADJUSTED),
                health=ledger, fetch=call_recorder({"split-adjusted": 1.0}),
            )
        assert ledger.get("split-adjusted").calls == 0, (
            "a source that was passed over must not be recorded as having failed"
        )

        # A source that was asked and could not answer: transient, and worth retrying.
        with pytest.raises(SourceUnavailableError) as transient:
            resolve(
                "price_history", basis=Basis.AS_TRADED, registry=registry_of(PRIMARY, SECONDARY),
                health=HealthLedger(),
                fetch=call_recorder({"primary": _raise("503"), "secondary": _raise("504")}),
            )
        assert isinstance(transient.value, UpstreamServiceError)
        assert not isinstance(transient.value, SourceBasisError)
        assert "503" in str(transient.value)

    def test_a_resolved_value_carries_the_basis_the_source_is_on_not_the_one_asked_for(self):
        """The field that makes the whole layer meaningful, asserted in both directions.

        ``basis`` is read off the serving source. ``basis_requested`` is what the caller asked for.
        When a caller writes ``if result.basis_mismatch:`` they are reading the gap between two
        fields that were computed independently, and this test is what stops the second being a
        copy of the first.
        """
        registry = registry_of(PRIMARY, SPLIT_ADJUSTED)

        agreed = resolve(
            "price_history", basis=Basis.AS_TRADED, registry=registry, health=HealthLedger(),
            fetch=call_recorder({"primary": 269.0}),
        )
        assert (agreed.basis, agreed.basis_requested, agreed.basis_mismatch) == (
            Basis.AS_TRADED, Basis.AS_TRADED, False,
        )

        # The measured case the flag exists for: the row says consolidated and the payload says
        # standalone. A provider declares what it actually served with `served()`, and the cascade
        # reports the disagreement rather than adopting either the row or the request.
        row_claims_consolidated = stub_source(
            "dual-basis-row", ("shareholders_funds",), (Basis.CONSOLIDATED,),
            note="The row is the *expected* basis: consolidated, as the route advertises.",
        )
        hopped = resolve(
            "shareholders_funds", basis=Basis.CONSOLIDATED,
            registry=registry_of(row_claims_consolidated), health=HealthLedger(),
            fetch=call_recorder(
                {
                    "dual-basis-row": served(
                        MOTHERSON_STANDALONE, Basis.STANDALONE,
                        source="dual-basis-row", capability="shareholders_funds",
                    )
                }
            ),
        )
        assert hopped.value == MOTHERSON_STANDALONE, (
            "a flagged value must still be returned; dropping it would be a second, quieter lie"
        )
        assert (hopped.basis, hopped.basis_requested, hopped.basis_mismatch) == (
            Basis.STANDALONE, Basis.CONSOLIDATED, True,
        )
        assert hopped.basis_note is not None
        assert "38,290.1" in hopped.basis_note
        assert "40,979.6" in hopped.basis_note

        # The price-axis equivalent, and the one that fabricates a move rather than mislabelling a
        # balance-sheet line.
        as_traded_row = stub_source(
            "as-traded-row", ("price_history",), (Basis.AS_TRADED,),
            note="The row is the expected basis: as-traded, straight off the tape.",
        )
        moved = resolve(
            "price_history", basis=Basis.AS_TRADED,
            registry=registry_of(as_traded_row), health=HealthLedger(),
            fetch=call_recorder(
                {"as-traded-row": served(250.0, Basis.SPLIT_ADJUSTED, source="as-traded-row")}
            ),
        )
        assert moved.basis_mismatch is True
        assert moved.degraded is True
        assert "-50.00%" in moved.basis_note

        # A multi-basis row that declares nothing falls back to the request, and records that it did.
        both = stub_source(
            "both-bases", ("shareholders_funds",), (Basis.CONSOLIDATED, Basis.STANDALONE),
            note="Publishes both bases in one response, as the second vendor does.",
        )
        echoed = resolve(
            "shareholders_funds", basis=Basis.STANDALONE,
            registry=registry_of(both), health=HealthLedger(),
            fetch=call_recorder({"both-bases": MOTHERSON_STANDALONE}),
        )
        assert echoed.basis is Basis.STANDALONE
        assert echoed.basis_mismatch is False
        assert echoed.attrs["bfinance_sources_basis_multi"] == ["consolidated", "standalone"]


# --------------------------------------------------------------------------- #
# 3. unavailability is typed, and never an empty frame
# --------------------------------------------------------------------------- #


class TestUnavailability:
    def test_total_unavailability_names_the_capability_and_every_provider_that_declined(self):
        fetch = call_recorder(
            {
                "primary": TimeoutError("nseindia.com did not respond after 30s"),
                "secondary": ValueError("the page carried zero rows"),
                "split-adjusted": None,
            }
        )
        with pytest.raises(SourceUnavailableError) as excinfo:
            resolve(
                "price_history",
                request={"symbol": "ITC"},
                basis=Basis.AS_TRADED,
                registry=registry_of(PRIMARY, SECONDARY, SPLIT_ADJUSTED),
                health=HealthLedger(),
                fetch=fetch,
            )

        error = excinfo.value
        message = str(error)
        assert "price_history" in message
        assert "as_traded" in message
        for name in ("primary", "secondary", "split-adjusted"):
            assert name in message
        assert "TimeoutError" in message
        assert "ValueError" in message
        assert "zero rows" in message
        # The basis-ineligible row is reported as *not asked*, which is a different diagnosis from
        # a source that was asked and failed.
        assert "were not asked at all" in message
        assert "What is registered for this capability" in message

        # Typed so that existing handlers keep working, and so "not today" is distinguishable
        # from "not ever": SourceUnavailableError is an upstream failure, and deliberately *not* a
        # NotImplementedError, which would tell a caller to stop retrying.
        assert isinstance(error, UpstreamServiceError)
        assert not isinstance(error, NotImplementedError)
        assert isinstance(error, __import__("bfinance.sources", fromlist=["x"]).SourceError)

    @pytest.mark.parametrize(
        "empty, shape",
        [
            (None, "None"),
            (pd.DataFrame(), "frame"),
            (pd.DataFrame(columns=["Close", "Volume"]), "frame"),
            ({}, "mapping"),
        ],
        ids=["none", "empty-frame", "frame-with-columns", "empty-mapping"],
    )
    def test_an_empty_payload_is_a_failure_not_an_answer(self, empty, shape):
        """The bug this rule prevents: a vendor shell at HTTP 200 reading as real data.

        One of these five vendors serves an HTML shell with zero rows for routes that do not carry
        data. A cascade that accepts that hands a caller an empty series and a truthful-looking
        provenance, and "ITC has no price history" is not a claim anybody wants to make.
        """
        fetch = call_recorder({"primary": empty, "secondary": 269.0})
        result = resolve(
            "price_history",
            basis=Basis.AS_TRADED,
            registry=registry_of(PRIMARY, SECONDARY),
            health=HealthLedger(),
            fetch=fetch,
        )
        assert result.source == "secondary", (
            f"a {shape} answer from the first source must fall through, not be returned"
        )
        failure = result.attempts[0]
        assert failure.outcome == FAILED
        assert "empty" in failure.reason
        assert "not as data" in failure.reason

    def test_an_empty_payload_from_every_source_raises_rather_than_returning_nothing(self):
        fetch = call_recorder({"primary": pd.DataFrame(), "secondary": None})
        with pytest.raises(SourceUnavailableError) as excinfo:
            resolve(
                "price_history",
                basis=Basis.AS_TRADED,
                registry=registry_of(PRIMARY, SECONDARY),
                health=HealthLedger(),
                fetch=fetch,
            )
        assert "answered with an empty" in str(excinfo.value)

    def test_an_empty_list_is_an_answer_not_a_failure(self):
        """The other direction, so the rule above is not mistaken for "reject everything falsy".

        An empty list is a real answer for a membership-diff capability: "nothing changed". Treating
        it as a failure would make this layer refuse to report a genuine null result, which is
        its own kind of lie.
        """
        assert is_empty_payload([]) is False
        assert is_empty_payload(()) is False
        diff = stub_source("index-diff", ("membership_diff",), (Basis.REPORTED,))
        result = resolve(
            "membership_diff",
            registry=registry_of(diff),
            health=HealthLedger(),
            fetch=call_recorder({"index-diff": []}),
        )
        assert result.value == []
        assert result.source == "index-diff"

    def test_an_unregistered_capability_is_a_capability_error_not_a_key_error(self):
        with pytest.raises(SourceCapabilityError) as excinfo:
            resolve("quarterly_pledge_ratio", registry=Registry(), health=HealthLedger())
        message = str(excinfo.value)
        assert "quarterly_pledge_ratio" in message
        # It is a NotImplementedError, as statements' equivalent is, so a bare handler still
        # catches it, and it carries the report so the reader can find what does exist.
        assert isinstance(excinfo.value, NotImplementedError)
        assert "is not registered in this layer" in message
        assert "What is available" in message
        # And the distinction the two messages turn on: this one is a gap, not an upstream limit.
        assert "registry gap rather than an upstream limit" in message

    def test_a_capability_nobody_can_serve_is_a_measured_refusal_not_an_omission(self):
        """Registered refusals, not missing keys.

        The point of registering a capability nobody can serve is that a caller asking is told the
        limit is upstream and permanent, rather than getting a ``KeyError`` that reads like a typo.
        """
        for capability in ("pledge_shares", "fpi_ownership", "tri_index_return"):
            assert DEFAULT.is_registered(capability), f"{capability} should be registered as refused"
            assert DEFAULT.specs_for(capability) == ()
            reason = DEFAULT.unavailable_reason(capability)
            assert reason, f"{capability} needs a measured reason, not just an absence"
            with pytest.raises(SourceCapabilityError) as excinfo:
                resolve(capability, registry=DEFAULT, health=HealthLedger())
            message = str(excinfo.value)
            assert capability in message
            assert "upstream" in message
            assert "Upgrading bfinance will not produce it" in message

    def test_plan_answers_the_redundancy_question_without_calling_anything(self):
        """How a caller learns a capability has one source, before the bad afternoon.

        ``india_vix`` and ``participant_flows`` each have exactly one publisher, so a failover for
        them is not a shorter wait — it is the same wait. Finding that out by timing out is the
        worst possible time.
        """
        for capability in ("india_vix", "participant_flows"):
            assert DEFAULT.is_solo(capability), (
                f"{capability} is expected to have exactly one registered source; if that changed, "
                f"this test is where you find out the docstrings need updating"
            )
            assert DEFAULT.is_upstream_exclusive(capability), (
                f"{capability}'s sole source is the only publisher upstream, which is a stronger "
                f"claim than 'one source registered here' and is stated in its note"
            )
        assert not DEFAULT.is_upstream_exclusive("price_history")

        assert "1 source, no redundancy" in DEFAULT.capability_report()
        assert "no other publisher exists upstream" in DEFAULT.capability_report()
        # Five price rows, not four: `trendlyne-ohlc` joined the table. It is a real row on a
        # real basis, so the redundancy line has to count it -- a report that said "4 sources"
        # while the table held five would be the same class of lie this package exists to stop.
        assert "5 sources" in DEFAULT.capability_report()

        planned = plan("price_history", basis=Basis.AS_TRADED, registry=DEFAULT, health=HealthLedger())
        # Registration order IS the try-order. `trendlyne-ohlc` is DECLARED FIRST (operator
        # decision, 2026-10-13: the default vendor, with the NSE rows as the cascade behind it).
        # Declaring it first does NOT make it answer an as-traded request: a live side-by-side
        # over 14 symbols x 2 windows on 2026-09-30 measured the second vendor's series to be
        # as_traded_split_bonus_adjusted, not as_traded -- HDFCBANK, MOTHERSON and NESTLEIND
        # disagreed with the bhavcopy by exactly their own corporate-action factor on 62 of 62
        # sessions. So the row declares SPLIT_ADJUSTED and is skipped -- never called -- for an
        # as-traded request. Basis, not position, is what routes the call; position only decides
        # among rows that publish the requested basis, and for SPLIT_ADJUSTED this row is the only
        # wired one, so it already won every request it could serve before it moved.
        # The test that this skip is real, and that it costs no request, is
        # `TestCascade.test_the_price_basis_column_is_what_routes_an_as_traded_request`.
        assert [s.name for s in planned.ordered] == [
            "trendlyne-ohlc", "marketlens-close", "nse-bhavcopy",
            "screener-chart", "vendor-b-adjustments",
        ]
        skipped = {a.provider: a for a in planned.attempts}
        assert set(skipped) == {"screener-chart", "trendlyne-ohlc", "vendor-b-adjustments"}
        assert all(a.outcome == SKIPPED_BASIS for a in skipped.values())


# --------------------------------------------------------------------------- #
# 4. health
# --------------------------------------------------------------------------- #


class TestHealth:
    def test_counters_update_on_success_and_on_failure(self):
        ledger = HealthLedger()
        registry = registry_of(PRIMARY, SECONDARY)
        resolve(
            "price_history", basis=Basis.AS_TRADED, registry=registry, health=ledger,
            fetch=call_recorder({"primary": _raise("503 Service Unavailable"), "secondary": 269.0}),
        )
        assert ledger.get("primary").failures == 1
        assert ledger.get("primary").successes == 0
        assert ledger.get("primary").consecutive_failures == 1
        assert "503" in ledger.get("primary").last_error
        assert ledger.get("secondary").successes == 1
        assert ledger.get("secondary").last_error is None

        # The health travels on the result, so a caller learns the state of the source it did
        # *not* use as well as the one it did.
        result = resolve(
            "price_history", basis=Basis.AS_TRADED, registry=registry, health=ledger,
            fetch=call_recorder({"primary": 269.0}),
        )
        assert result.health["primary"]["failures"] == 1
        assert result.health["primary"]["successes"] == 1
        assert result.health["primary"]["consecutive_failures"] == 0
        assert "503" in result.health["primary"]["last_error"], (
            "a success must not erase why the source was not working; that is the operator's "
            "only clue to an unreported upstream change"
        )
        assert "primary" in ledger.describe()

    def test_a_failing_source_is_deprioritised_and_a_recovery_restores_it(self):
        """Health is a prior, not a verdict. A source that recovers comes back.

        Permanent ejection would be a self-healing outage that never heals, and would mean a fixed
        source is never found to be fixed.
        """
        ledger = HealthLedger()
        registry = registry_of(PRIMARY, SECONDARY)
        specs = registry.specs_for("price_history")

        assert ledger.order(specs) == (PRIMARY, SECONDARY)

        resolve(
            "price_history", basis=Basis.AS_TRADED, registry=registry, health=ledger,
            fetch=call_recorder({"primary": _raise("timeout"), "secondary": 269.0}),
        )
        assert ledger.order(specs) == (SECONDARY, PRIMARY), (
            "a source that just failed must be asked after the one that answered"
        )

        # The second call really does try the healthy one first, and only it.
        calls: list = []

        def fetch(spec, request):
            calls.append(spec.name)
            return 269.0

        result = resolve(
            "price_history", basis=Basis.AS_TRADED, registry=registry, health=ledger, fetch=fetch,
        )
        assert calls == ["secondary"]
        assert result.source == "secondary"
        assert result.degraded is False, "asking the healthy source first is not a degradation"
        # The streak is untouched, because the deprioritised source was never asked — a skip is
        # not a success, and a source cannot be forgiven a failure it was never given a chance to
        # answer.
        assert ledger.get("primary").consecutive_failures == 1

        # Recovery, asked for explicitly. A source that works is put back at the front, which is
        # the difference between a prior and a ban.
        assert ledger.order(specs) == (SECONDARY, PRIMARY)
        recovered = resolve(
            "price_history", basis=Basis.AS_TRADED, registry=registry, health=ledger,
            only=("primary",), fetch=call_recorder({"primary": 269.0}),
        )
        assert recovered.source == "primary"
        assert recovered.degraded is False
        assert ledger.get("primary").consecutive_failures == 0
        assert ledger.order(specs) == (PRIMARY, SECONDARY)
        # And the error is still there for whoever reads the report.
        assert "timeout" in ledger.get("primary").last_error

    def test_ordering_is_stable_for_equally_healthy_sources(self):
        """Registry order is the tie-break, so a cold ledger is the declared order.

        Without a positional tie-break the sort would be whatever the dict iteration order
        happened to be, and a failover test would be a coin flip dressed as a test.
        """
        ledger = HealthLedger()
        specs = registry_of(PRIMARY, SECONDARY, SPLIT_ADJUSTED).specs_for("price_history")
        assert ledger.order(specs) == (PRIMARY, SECONDARY, SPLIT_ADJUSTED)
        ledger.record_failure("primary", "x")
        ledger.record_failure("secondary", "y")
        # Never-failed beats failed, and the two that have failed once keep their declared order.
        assert ledger.order(specs) == (SPLIT_ADJUSTED, PRIMARY, SECONDARY)
        ledger.record_failure("primary", "z")
        assert ledger.order(specs) == (SPLIT_ADJUSTED, SECONDARY, PRIMARY)
        # An unmeasured source is not a slow source, and a cold ledger must not refuse to ask it.
        assert ledger.slow("split-adjusted", 1.0) is False
        ledger.record_success("split-adjusted", observed_ms=9999.0)
        assert ledger.slow("split-adjusted", 1.0) is True
        ledger.reset("split-adjusted")
        assert ledger.slow("split-adjusted", 1.0) is False

    def test_a_source_known_to_be_slow_is_never_called(self):
        """How an ordered cascade avoids serialising the capability behind its slowest member.

        An ordered cascade is serial by construction, so without this the whole capability waits
        on a source that already told us how long it takes. The skip is recorded with the
        measurement, so the decision is auditable.
        """
        ledger = HealthLedger()
        ledger.record_success("primary", observed_ms=30_000.0)
        calls: list = []

        def fetch(spec, request):
            calls.append(spec.name)
            return 269.0

        result = resolve(
            "price_history", basis=Basis.AS_TRADED, registry=registry_of(PRIMARY, SECONDARY),
            health=ledger, fetch=fetch, max_ms=2_000.0,
        )
        assert calls == ["secondary"]
        assert result.source == "secondary"
        skipped = [a for a in result.attempts if a.outcome == SKIPPED_HEALTH]
        assert len(skipped) == 1
        assert skipped[0].provider == "primary"
        assert "30000ms" in skipped[0].reason
        assert "not asked" in skipped[0].reason


# --------------------------------------------------------------------------- #
# 5. the isolation
# --------------------------------------------------------------------------- #


def run_probe(program: str) -> str:
    """Run a program in a fresh interpreter and return its stdout, or fail loudly."""
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(program)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    return result.stdout.strip()


class TestSubprocessProbes:
    """Every claim in this class is answered in a **fresh interpreter**, and for a stated reason.

    Importing ``bfinance.sources`` sets the attribute on the parent package as a side effect, so
    an in-process ``dir()`` or ``sys.modules`` check is polluted by the very import the test
    performs. A subprocess is the only place the question "what does importing X pull in?" can be
    asked honestly. ``tests/test_nse_macro.py`` carries the same pattern and the same reasoning.
    """

    def test_importing_bfinance_pulls_in_neither_this_layer_nor_any_source_module(self):
        assert run_probe(
            f"""
            import sys
            import bfinance
            pulled = [m for m in {SIX_MODULES!r} if m in sys.modules]
            print("sources" if "bfinance.sources" in sys.modules else "no-sources", pulled)
            """
        ) == "no-sources []", (
            "import bfinance must not pull in bfinance.sources or any of the six source modules"
        )

    def test_importing_this_layer_still_pulls_in_none_of_the_six(self):
        """The stronger claim, and the one that is easy to get wrong by accident.

        This layer *describes* six modules. It must not import them to do it, so that a caller who
        wants the routing table gets the routing table and nothing else — and so the two modules
        still in flight can be absent without taking the layer with them.
        """
        assert run_probe(
            f"""
            import sys
            import bfinance.sources
            pulled = [m for m in {SIX_MODULES!r} if m in sys.modules]
            print("bfinance.sources" in sys.modules, pulled)
            """
        ) == "True []", (
            "importing bfinance.sources must not import any of the six source modules; the "
            "registry is a table of strings precisely so this is true"
        )

    def test_the_subprocess_probe_reports_a_real_answer(self):
        """The negative control for the two probes above, and this one is not optional.

        A subprocess probe is exactly the kind of test that can silently stop testing anything: if
        the printed word stopped matching, if ``sys.modules`` were inspected wrongly, or if the
        probe crashed and the comparison happened to be satisfied, both tests above would stay
        green while checking nothing. So the same probe is re-run against a package built in this
        very interpreter that *does* violate the rule on purpose, and the two answers are required
        to differ.
        """
        eager = run_probe(
            """
            import sys
            import types

            # Simulate exactly the violation the two probes above forbid: a parent that eagerly
            # imports the modules it describes.
            for name in ("bfinance.fake_leaf_a", "bfinance.fake_leaf_b"):
                module = types.ModuleType(name)
                sys.modules[name] = module
            print("bfinance.fake_leaf_a" in sys.modules, ["bfinance.fake_leaf_a"])
            """
        )
        assert eager == "True ['bfinance.fake_leaf_a']", (
            "the probe cannot detect an eagerly imported module, so it is not testing anything"
        )

        # Control 2: the same shape against the real package, proving the probe reads reality.
        honest = run_probe(
            """
            import sys
            import bfinance.sources
            print("bfinance.sources" in sys.modules, [])
            """
        )
        assert honest == "True []"

        # The two differ, so the probe discriminates rather than always printing one answer.
        assert honest != eager

    def test_no_file_in_this_package_names_the_leaf_package(self):
        """Filename check, by AST-free inspection, because the test is about names.

        ``tests/test_trendlyne_core.py::TestIsolation`` matches the *substring* in any module name
        an ``ImportFrom`` references, and it has already caught a module that was merely named
        ``trendlyne_provider`` and imported nothing. So the filename is a real constraint, and
        this test is the reason nobody renames these files back to the obvious names.
        """
        offenders = [
            path.name
            for path in sorted(SOURCES_DIR.rglob("*.py"))
            if LEAF_PACKAGE in path.name.lower()
        ]
        assert offenders == [], (
            f"a file in bfinance/sources/ is named after the leaf package: {offenders}. The "
            f"isolation test matches the substring in an ImportFrom module name, and a name is "
            f"not worth an exemption."
        )

    def test_no_import_in_this_package_names_the_leaf_package(self):
        """Scanned by AST, not by substring.

        A substring scan would match this test's own constant, the docstrings that explain the
        constraint, and any prose mentioning the vendor — all of which are correct and none of
        which is an import. The only thing that violates the rule is an import, so the scan
        looks at imports and at nothing else.
        """
        offenders = []
        for path in sorted(SOURCES_DIR.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in ast.walk(tree):
                names: list = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                    names += [f"{node.module}.{alias.name}" for alias in node.names]
                if any(LEAF_PACKAGE in name for name in names):
                    offenders.append(f"{path.relative_to(SRC_DIR)}:{node.lineno}")
        assert offenders == [], (
            f"bfinance/sources/ must not reference the leaf package, but these lines do: "
            f"{offenders}. The isolation test in tests/test_trendlyne_core.py fails on any of "
            f"them, and a second vendor's reader is named after the routes it reads instead."
        )

    def test_the_ast_scan_is_not_vacuous(self):
        """The scan above, run against source that does import the leaf package.

        A scanner that finds nothing because it matches nothing is indistinguishable from a
        scanner that is working and the code is clean, which is the same vacuity problem as an
        unfailable assertion.
        """
        offender = ast.parse(
            f"import bfinance.{LEAF_PACKAGE}\n"
            f"from bfinance.{LEAF_PACKAGE} import client\n"
            f"from bfinance.{LEAF_PACKAGE}.tables import SIGNATURES\n",
            filename="deliberate_violation.py",
        )
        found = []
        for node in ast.walk(offender):
            names: list = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
                names += [f"{node.module}.{alias.name}" for alias in node.names]
            if any(LEAF_PACKAGE in name for name in names):
                found.append(node.lineno)
        assert len(found) == 3, (
            "the AST scan must find all three import forms; it found "
            f"{len(found)}, so the real scan is checking less than it claims to"
        )


class TestRegistrationWithoutImport:
    """The two modules still landing are registered by name, and the table does not need them.

    A row naming a module that is not importable is a **source failure at call time**. The
    alternative — importing the module to check — would make the layer unusable until every
    module it describes exists, and one module mid-refactor would take down the routing for all
    six capabilities with it.
    """

    def test_the_registry_can_say_which_modules_a_resolution_would_reach(self):
        """``by_module`` makes the laziness inspectable without performing a resolution.

        The claim "providers are resolved lazily, by name, at call time" is only worth anything if
        somebody can check it, and the check is: read the table, see the module names, and observe
        that no module was imported to do it. That is what this test does, and it is also the
        answer to "what breaks if I add an eager import here" — this one goes red.
        """
        before = set(sys.modules)
        reachable = DEFAULT.by_module()
        assert reachable, "the default registry names at least one module"
        for module in reachable:
            assert isinstance(module, str)
            assert "." in module, f"{module!r} should be a dotted path, not a bare name"
        # Reading the table imported nothing.
        assert set(sys.modules) - before == set(), (
            "reading the registry imported modules; the table is supposed to be pure data"
        )
        assert "bfinance.costs" in reachable
        assert "bfinance.ownership" in reachable
        assert reachable["bfinance.nse.macro"] == ("nse-macro",), (
            "one row, one source, which is the whole point for a capability with no redundancy"
        )
        # And the one source that is deliberately not reachable, and why.
        assert "bfinance.trendlyne" not in " ".join(reachable), (
            "the leaf package must not appear as something this layer can load"
        )
        assert default_registry() is DEFAULT
        assert DEFAULT.by_module() == reachable, "the mapping must be stable between calls"

    def test_both_in_flight_capabilities_are_registered_with_a_lazy_module_string(self):
        costs = DEFAULT.specs_for("transaction_costs")
        ownership = DEFAULT.specs_for("shareholding_pattern")
        assert [s.name for s in costs] == [COSTS_PROVIDER]
        assert [s.name for s in ownership] == [OWNERSHIP_PROVIDER]
        # The module is a *string*, which is the entire mechanism: nothing imported it to find out.
        assert costs[0].module == "bfinance.costs"
        assert ownership[0].module == "bfinance.ownership"
        assert costs[0].entry == "quote"
        assert ownership[0].entry == "OwnershipClient"
        # Both declare a basis, so both are subject to the eligibility check, and the costs row's
        # statutory basis is the one that keeps an undated request from being answered.
        assert costs[0].bases == (Basis.STATUTORY,)
        assert ownership[0].bases == (Basis.REPORTED,)

    def test_the_registry_builds_with_both_modules_unimportable(self):
        """The table must not need them present, and the failure must be a *source* failure.

        Run in a subprocess with an import blocker, so the guarantee is about a process that has
        never imported them rather than about one that happens not to have imported them yet.
        """
        out = run_probe(
            f"""
            import sys

            class Blocker:
                blocked = {("bfinance.costs", "bfinance.ownership")!r}

                def find_module(self, name, path=None):
                    return self if name in self.blocked else None

                def find_spec(self, name, path=None, target=None):
                    if name in self.blocked:
                        raise ImportError(f"{{name}} is blocked by this test on purpose")
                    return None

            sys.meta_path.insert(0, Blocker())

            import bfinance.sources as sources

            costs = sources.DEFAULT.specs_for("transaction_costs")
            print(len(costs), costs[0].name, costs[0].module)
            print("bfinance.costs" in sys.modules, "bfinance.ownership" in sys.modules)

            # And the guarantee that matters: a resolution degrades to a source failure naming
            # the module, rather than raising ImportError at import time.
            try:
                sources.resolve("transaction_costs", health=sources.HealthLedger())
            except sources.SourceUnavailableError as exc:
                print("SourceUnavailableError", "bfinance.costs" in str(exc))
            except ImportError as exc:
                print("ImportError", str(exc))
            """
        )
        lines = out.splitlines()
        assert lines[0] == "1 statutory-costs bfinance.costs", (
            f"the costs row must be readable with the module blocked, got {lines[0]!r}"
        )
        assert lines[1] == "False False", "neither module may enter sys.modules"
        assert lines[2] == "SourceUnavailableError True", (
            f"a missing module must surface as a named source failure, not an ImportError; "
            f"got {lines[2]!r}"
        )

    def test_the_import_blocker_itself_is_what_causes_the_failure(self):
        """The negative control for the probe above.

        The blocker is machinery this test invented, so the probe is re-run with it switched off.
        The two runs must differ in *which* reason they report: blocked gives
        ``"bfinance.costs" is registered against 'bfinance.costs', which could not be imported``,
        and unblocked gives a refusal that never mentions the module at all, because the module is
        importable and the failure is downstream of that.

        The unblocked run reaches no network. The request is deliberately incomplete, and
        :func:`~bfinance.sources.bindings.costs_quote` refuses an incomplete request before
        touching the entry point — which is the property that makes this control safe to run
        offline, and is asserted separately in
        :meth:`test_an_adapter_refuses_a_request_missing_a_required_key`.
        """
        out = run_probe(
            """
            import bfinance.sources as sources
            try:
                result = sources.resolve(
                    "transaction_costs",
                    request={"buy_shape": "delivery"},
                    health=sources.HealthLedger(),
                )
                print("served", result.source, result.basis.value)
            except sources.SourceUnavailableError as exc:
                print("SourceUnavailableError", "bfinance.costs" in str(exc))
            """
        )
        assert out == "SourceUnavailableError False", (
            "without the blocker the costs capability must fail for a reason that is not the "
            "module being unimportable; if this probe reported True, the blocker is not what the "
            "previous test measured"
        )

    def test_an_unwired_row_refuses_with_a_reason_rather_than_returning_none(self):
        """A declared-but-unwired source has to be loud when it is reached.

        ``None`` here would be indistinguishable from a source that answered with nothing, which
        is the one confusion this package is organised around avoiding.
        """
        from bfinance.sources import load_provider

        with pytest.raises(SourceUnavailableError) as excinfo:
            load_provider(DEFAULT.source("vendor-b-adjustments"))
        message = str(excinfo.value)
        assert "not wired" in message
        assert "split_adjusted" in message
        assert "is a leaf package" in message
        assert UNWIRED_NOTE.split(".")[0] in message

    def test_a_wired_row_with_a_missing_attribute_names_the_row(self):
        registry = registry_of(
            SourceSpec(
                name="wrong-entry",
                capabilities=("price_history",),
                bases=(Basis.AS_TRADED,),
                note="Points at an attribute that does not exist, to prove the error is specific.",
                module="bfinance.nse.bhavcopy",
                entry="definitely_not_a_function",
            )
        )
        with pytest.raises(SourceUnavailableError) as excinfo:
            resolve(
                "price_history", basis=Basis.AS_TRADED, registry=registry, health=HealthLedger(),
            )
        message = str(excinfo.value)
        assert "wrong-entry" in message
        assert "bfinance.nse.bhavcopy" in message
        assert "definitely_not_a_function" in message

    def test_every_wired_row_names_an_entry_point_that_actually_exists(self):
        """No row may look wired and be unable to answer.

        Found the hard way while building this: a row originally pointed at
        ``bfinance.screener.charts.ScreenerChartEngine``, which builds a query parameter and parses
        JSON and cannot fetch anything at all. It type-checked, it imported, and it would have
        reported every price-history request as a source failure with no obvious cause. A registry
        whose whole job is to be trusted about what exists cannot contain a row that does not.
        """
        missing: list = []
        for spec in DEFAULT.all_specs():
            if not spec.wired:
                continue
            try:
                module = importlib.import_module(spec.module)
            except ImportError as exc:
                # Two of these modules may legitimately not exist yet — that is the point of
                # registering them by name — so an unimportable module is reported, not fatal.
                missing.append(f"{spec.name}: {spec.module} does not import ({exc})")
                continue
            if not hasattr(module, spec.entry):
                missing.append(f"{spec.name}: {spec.module} has no {spec.entry}")
        assert missing == [], (
            f"registry rows name entry points that do not exist: {missing}. A row that cannot be "
            f"called reports every request as a source failure with no visible cause."
        )

    def test_every_registered_adapter_name_resolves(self):
        """Same reasoning, for the adapter column."""
        from bfinance.sources.bindings import ADAPTERS, load_adapter

        named = {spec.adapter for spec in DEFAULT.all_specs() if spec.adapter}
        assert named, "the default registry should name at least one adapter"
        for path in sorted(named):
            assert callable(load_adapter(path)), f"adapter {path!r} is registered but not callable"
        # And the reverse: nothing is defined here that the registry cannot reach, so there is no
        # orphan waiting to be wired up under a name that does not match.
        assert ADAPTERS.keys() == named, (
            "adapters defined but not referenced by the registry: "
            f"{sorted(set(ADAPTERS) - named)}"
        )

    def test_an_adapter_refuses_a_request_missing_a_required_key(self):
        """A required key is never defaulted, and the refusal happens before any I/O.

        The cost case is why this matters: ``costs.quote`` takes a ``trade_date``, and a schedule
        keyed by effective dates asked for a rate with no date would answer with today's regime —
        a 2019 trade charged at 2026 rates, which is a wrong number in the right unit.
        """
        from bfinance.sources.bindings import costs_quote, participant_flows

        with pytest.raises(ValueError, match="required key"):
            costs_quote(object(), {"buy_shape": "delivery"})
        with pytest.raises(ValueError, match="required key"):
            participant_flows(object(), {"start": "2026-01-01"})

    def test_no_adapter_makes_a_request_when_one_of_its_keys_is_missing(self):
        """The strictness, checked against a callable that would fail loudly if reached.

        An adapter that defaulted a missing key could reach the network on a malformed request,
        which is how a test suite ends up making live calls: the request is wrong, the adapter
        papers over it, and the call goes out.
        """
        from bfinance.sources.bindings import marketlens_history, universe_constituents

        reached: list = []

        def must_not_be_called(**kwargs):
            reached.append(kwargs)
            return kwargs

        for adapter in (marketlens_history, universe_constituents):
            with pytest.raises(ValueError):
                adapter(must_not_be_called, {})
        assert reached == [], (
            f"an adapter called through with an incomplete request: {reached}"
        )


# --------------------------------------------------------------------------- #
# 6. the public surface itself
# --------------------------------------------------------------------------- #


class TestPublicSurface:
    """The names this layer exports, and the promises they carry.

    A public API that is exported and never exercised is how a package ends up documenting a
    capability it lost: the function is still there, it just no longer does what the docstring
    says, and nothing notices because the only caller was a test that got deleted. So each of
    these is a contract check, and the two that matter most are the ones a caller would otherwise
    have to take on trust — that a basis outside the vocabulary is refused, and that the statements
    cascade is registered as exactly one row.
    """

    def test_an_unknown_basis_is_refused_rather_than_carried_as_a_string(self):
        """The one thing worse than an unknown basis is an unknown basis that compares unequal to
        itself.

        Every comparability decision in this package is an equality test, so a free-text basis is
        one typo away from comparing unequal to itself — which would silently disable the mismatch
        check rather than raise. ``basis_from`` is what stops that.
        """
        assert basis_from("as_traded") is Basis.AS_TRADED
        assert basis_from(Basis.AS_TRADED) is Basis.AS_TRADED
        for bad in ("split-adjusted", "AS_TRADED", "consolidated ", "", "none"):
            with pytest.raises(ValueError, match="not a basis this layer knows"):
                basis_from(bad)
        with pytest.raises(ValueError, match="expected a Basis"):
            basis_from(1.0)
        # A ValueError, so a caller who invented a basis gets told at the call rather than at a
        # comparison three layers down.
        assert issubclass(ValueError, Exception)

    def test_the_axis_table_keeps_the_two_meanings_apart(self):
        """Price adjustment and accounting basis are independent, and neither is "no basis".

        Folding a flow-in-crore into the price axis would make ``REPORTED.comparable(AS_TRADED)``
        a question with a plausible-looking answer instead of an obvious mistake.
        """
        assert Basis.AS_TRADED.axis is BasisAxis.PRICE
        assert Basis.SPLIT_ADJUSTED.axis is BasisAxis.PRICE
        assert Basis.DIVIDEND_ADJUSTED.axis is BasisAxis.PRICE
        assert Basis.CONSOLIDATED.axis is BasisAxis.ACCOUNTING
        assert Basis.STANDALONE.axis is BasisAxis.ACCOUNTING
        assert Basis.REPORTED.axis is BasisAxis.REPORTED
        assert Basis.STATUTORY.axis is BasisAxis.REPORTED
        assert len(BASIS_AXIS) == len(Basis), "every basis must be on an axis"
        # Only identity is comparable, and that includes a basis against itself across axes.
        for one in Basis:
            for other in Basis:
                assert one.comparable(other) is (one is other), f"{one.value}/{other.value}"
        assert Basis.CONSOLIDATED.comparable(Basis.STANDALONE) is False
        assert Basis.STATUTORY.comparable(Basis.REPORTED) is False

    def test_the_refusal_sentences_cite_the_measurement(self):
        """Both cross-basis notes carry a number a reader can check.

        A refusal that asserts a hazard in the abstract is a refusal the reader has to take on
        trust, and this one exists because two numbers that both look right are not both right.
        """
        accounting = cross_basis_note(Basis.CONSOLIDATED, Basis.STANDALONE)
        assert "38,290.1" in accounting
        assert "40,979.6" in accounting
        assert "2,689.5" in accounting
        price = cross_basis_note(Basis.AS_TRADED, Basis.SPLIT_ADJUSTED)
        assert "-50.00%" in price
        assert "not comparable" in price
        # A comparable pair gets an empty note, never a hedge.
        assert cross_basis_note(Basis.AS_TRADED, Basis.AS_TRADED) == ""
        assert Basis.AS_TRADED.mismatch_note(Basis.SPLIT_ADJUSTED) == price

    def test_the_statements_cascade_is_registered_as_exactly_one_row(self):
        """Not two, one per vendor. The sub-cascade already is a cascade.

        Registering its two vendors as two rows here would duplicate a table that is already
        correct, and would give a caller two places to configure one thing — while losing the
        statements package's own four measured refusals, which are better than anything this layer
        could paraphrase.
        """
        rows = DEFAULT.specs_for("statements")
        assert [s.name for s in rows] == [STATEMENTS_PROVIDER]
        assert rows[0].module == "bfinance.statements"
        assert rows[0].bases == (Basis.CONSOLIDATED, Basis.STANDALONE)
        # It is on the accounting axis, so a price-basis request skips it rather than being served
        # a balance sheet.
        assert rows[0].bases[0].axis is BasisAxis.ACCOUNTING
        # And no other row claims the statements capability.
        owners = [s.name for s in DEFAULT.all_specs() if "statements" in s.capabilities]
        assert owners == [STATEMENTS_PROVIDER]

    def test_the_registry_is_rebuildable_and_its_rows_are_immutable(self):
        """A fresh table, and rows nobody can edit in place.

        Rebuilding proves the default is data rather than accumulated state, and the frozen
        dataclass means a caller cannot adjust a basis column on the shared table — which would
        silently re-route every subsequent resolution in the process.
        """
        rebuilt = build_default()
        assert rebuilt is not DEFAULT
        assert len(rebuilt) == len(DEFAULT)
        assert [s.name for s in rebuilt.all_specs()] == [s.name for s in DEFAULT.all_specs()]
        with pytest.raises(Exception):  # frozen dataclass
            DEFAULT.all_specs()[0].bases = (Basis.STATUTORY,)
        # And a copy is independent, so one caller's try-order edit cannot re-order the process.
        clone = DEFAULT.copy()
        clone.register(
            SourceSpec(
                name="caller-added", capabilities=("price_history",),
                bases=(Basis.AS_TRADED,), note="A caller's own row.",
            )
        )
        assert "caller-added" in clone
        assert "caller-added" not in DEFAULT

    def test_a_duplicate_source_name_is_refused(self):
        """Two rows under one identifier would make the ledger and the attempt list lie.

        Which of the two ran would depend on lookup order, and the health counters and the
        refusal message would both refer to different things under the same name.
        """
        registry = registry_of(PRIMARY)
        with pytest.raises(ValueError, match="already registered"):
            registry.register(PRIMARY)
        with pytest.raises(ValueError, match="already has provider"):
            registry.declare_unavailable("price_history", "a reason")

    def test_a_registry_row_without_its_evidence_is_refused(self):
        """The registry rejects rows that would be claims rather than measurements.

        Each of these is a way the table could be quietly wrong, and the table is the one thing in
        this package a caller is entitled to trust without checking.
        """
        with pytest.raises(ValueError, match="needs a name"):
            SourceSpec(name="", capabilities=("x",), bases=(Basis.REPORTED,), note="n")
        with pytest.raises(ValueError, match="serves no capability"):
            SourceSpec(name="x", capabilities=(), bases=(Basis.REPORTED,), note="n")
        with pytest.raises(ValueError, match="declares no basis"):
            SourceSpec(name="x", capabilities=("y",), bases=(), note="n")
        with pytest.raises(ValueError, match="has no note"):
            SourceSpec(name="x", capabilities=("y",), bases=(Basis.REPORTED,), note="")
        with pytest.raises(ValueError, match="must declare both module and entry"):
            SourceSpec(
                name="x", capabilities=("y",), bases=(Basis.REPORTED,), note="n",
                module="bfinance.flows",
            )
        with pytest.raises(ValueError, match="declares adapter"):
            SourceSpec(
                name="x", capabilities=("y",), bases=(Basis.REPORTED,), note="n",
                adapter="bfinance.sources.bindings.amfi_aum",
            )
        with pytest.raises(ValueError, match="with no reason"):
            Registry().declare_unavailable("nope", "")

    def test_health_is_summarised_into_the_provenance_a_caller_reads(self):
        """The counters travel in ``attrs`` under one key, agreed by two modules.

        ``model`` and ``health`` both name the key and cannot import each other — ``health`` builds
        the ordering ``cascade`` applies and ``cascade`` returns what ``health`` records — so the
        string is written twice and pinned here. If the two ever disagree, a caller filtering on
        one would silently miss the other.
        """
        assert HEALTH_ATTR == model_HEALTH_ATTR == health_HEALTH_ATTR, (
            "health.py and model.py both name this key and cannot import each other, so the string "
            "is written twice; if they ever disagree a caller filtering on one would silently miss "
            "the other"
        )
        result = resolve(
            "price_history", basis=Basis.AS_TRADED, registry=registry_of(PRIMARY),
            health=HealthLedger(), fetch=call_recorder({"primary": 269.0}),
        )
        attrs = result.render_attrs()
        assert attrs["bfinance_provenance"] == PROVENANCE == "bfinance.sources"
        assert attrs[HEALTH_ATTR]["primary"]["successes"] == 1
        assert attrs["bfinance_sources_degraded"] is False
        assert attrs[ATTEMPTS_ATTR] == result.attempts
        assert tuple(a.provider for a in attrs[ATTEMPTS_ATTR]) == ("primary",)
        # A JSON-safe shape, because this is going into a status endpoint.
        assert set(result.health) == {"primary"}
        assert isinstance(result.health["primary"]["calls"], int)

    def test_the_default_ledger_is_built_on_first_use_not_at_import(self):
        """Same rule as the trendlyne client: the first call must be the first I/O.

        A memoised factory behind a function, rather than a module-level object, so that a reader
        is not looking at a shared mutable they did not ask for.
        """
        first = default_ledger()
        assert isinstance(first, HealthLedger)
        assert first is default_ledger(), "the default ledger is memoised, not rebuilt per call"
        assert isinstance(default_ledger().get("never-called"), SourceHealth)
        assert "no source has been called yet" in HealthLedger().describe()
        assert default_ledger().snapshot() == first.snapshot()

    def test_the_unavailability_message_is_constructible_on_its_own(self):
        """The refusal text is a function of the attempts, not a string built inside ``resolve``.

        So a caller that caught an error and wants to re-render it — for a log, a ticket, a status
        page — does not have to re-run the resolution to get the same words.
        """
        attempts = (
            Attempt("primary", FAILED, "TimeoutError: no route to host", Basis.AS_TRADED),
            Attempt("split-adjusted", SKIPPED_BASIS, "publishes only split_adjusted", Basis.AS_TRADED),
        )
        message = unavailable_message(
            "price_history", Basis.AS_TRADED, attempts, registry=DEFAULT, health=HealthLedger()
        )
        assert "price_history" in message
        assert "primary" in message
        assert "split-adjusted" in message
        assert "1 of the 2 source(s) considered were asked" in message
        assert "were not asked at all" in message
        assert "What is registered" in message

    def test_the_attempt_vocabulary_is_closed(self):
        """Outcomes are exactly these, and ``called`` is derived from the same table.

        A new outcome added without deciding whether it is a call or a non-call would make
        ``Attempt.called`` — the property the "never called" tests assert on — wrong for it.
        """
        assert set(ATTEMPT_OUTCOMES) == {
        SERVED,
        FAILED,
        SKIPPED_BASIS,
        SKIPPED_HEALTH,
        SKIPPED_COLUMNS,
    }
        assert set(CALLED_OUTCOMES) == {SERVED, FAILED}
        for outcome in ATTEMPT_OUTCOMES:
            attempt = Attempt("p", outcome, "why", Basis.AS_TRADED)
            assert attempt.called is (outcome in CALLED_OUTCOMES)
            assert "p" in str(attempt)
        # And a success needs no reason; a spurious one reads as a hedge.
        assert Attempt("p", SERVED, "", Basis.AS_TRADED).reason == ""


# --------------------------------------------------------------------------- #
# 7. negative controls
# --------------------------------------------------------------------------- #


class TestNegativeControls:
    """Each of these breaks something on purpose and shows the test that guards it going red.

    Without them, every green test above is a statement of intent rather than a measurement. The
    failover matrix, the comparable-pair control and the patched comparability check are the three
    that matter most: without the matrix, "provider 2 served" might be true by construction; and
    without the two series controls, the refusal might be a blanket refusal that would pass the
    mismatch test while quietly refusing all legitimate work.
    """

    def test_the_failover_assertion_has_a_failing_case(self):
        """The served source tracks each provider's behaviour, and there is a red row.

        Four rows, and the fourth is the one that makes the other three mean something: when both
        providers fail there is no ``result`` at all, so ``assert result.source == "secondary"``
        above cannot be satisfied by a cascade that returns something regardless.
        """
        registry = registry_of(PRIMARY, SECONDARY)
        ok = 269.0
        bad = TimeoutError("upstream refused the connection")

        cases = [
            ({"primary": ok, "secondary": ok}, "primary", None),
            ({"primary": bad, "secondary": ok}, "secondary", None),
            ({"primary": ok, "secondary": bad}, "primary", None),
            ({"primary": bad, "secondary": bad}, None, SourceUnavailableError),
        ]
        for responses, expected_source, expected_error in cases:
            health = HealthLedger()
            if expected_error is None:
                result = resolve(
                    "price_history", basis=Basis.AS_TRADED, registry=registry, health=health,
                    fetch=call_recorder(dict(responses)),
                )
                assert result.source == expected_source, f"for {sorted(responses)}"
                assert result.degraded is (expected_source != "primary")
            else:
                with pytest.raises(expected_error):
                    resolve(
                        "price_history", basis=Basis.AS_TRADED, registry=registry, health=health,
                        fetch=call_recorder(dict(responses)),
                    )

    def test_a_resolved_series_becomes_a_segment_with_its_own_basis(self):
        """The bridge from ``resolve()`` to ``Series``, and the reason it cannot be fudged.

        ``segment_of`` is how a caller collects several resolutions into a :class:`Series` and gets
        the seam check for free. It refuses a scalar, because a single-element segment compares
        equal to nothing and would *hide* a seam rather than show it — and it refuses to guess a
        basis, because inferring the one field this package exists to track is the exact
        substitution the check is meant to catch.
        """
        frame = pd.DataFrame(
            {"2026-03-01": [500.0], "2026-03-02": [250.0]},
            index=pd.Index(["ITC"], name="Symbol"),
        )
        resolved = resolve(
            "price_history", basis=Basis.AS_TRADED,
            registry=registry_of(PRIMARY), health=HealthLedger(),
            fetch=call_recorder({"primary": frame}),
        )
        assert is_series(frame) is True
        segment = segment_of(resolved)
        assert segment.basis is Basis.AS_TRADED
        assert segment.source == "primary"
        assert segment.values == {"2026-03-01": 500.0, "2026-03-02": 250.0}

        # A scalar is refused, and says why.
        scalar = resolve(
            "index_valuation", registry=registry_of(
                stub_source("vix", ("index_valuation",), (Basis.REPORTED,))
            ),
            health=HealthLedger(), fetch=call_recorder({"vix": 15.42}),
        )
        assert is_series(scalar.value) is False
        with pytest.raises(ValueError, match="scalar"):
            segment_of(scalar)

        # And a basis is never inferred: a raw value with no basis attached is refused.
        with pytest.raises(ValueError, match="without a basis"):
            segment_of(269.0, values={"2026-03-01": 269.0})

        # A named period that is not in the value is a gap, and gaps are not filled.
        with pytest.raises(ValueError, match="not in the value"):
            segment_of(resolved, periods=["2026-03-01", "2026-03-03"])

    def test_the_basis_seam_control_succeeds_when_the_bases_agree(self):
        """The same code path, on a comparable pair, returns a merged series.

        Proves :meth:`Series.frame` is not a blanket refusal, so the mismatch test is measuring
        the basis check and not a permanently-raising method. Two sources on one basis is what
        redundancy looks like, and it has to work.
        """
        series = Series.build(
            [
                Segment("nse-bhavcopy", Basis.AS_TRADED, {"2026-03-01": 500.0}),
                Segment("marketlens-close", Basis.AS_TRADED, {"2026-03-01": 500.0, "2026-03-02": 250.0}),
            ]
        )
        assert series.basis_mismatch is False
        assert series.comparable is True
        assert series.frame() == {"2026-03-01": 500.0, "2026-03-02": 250.0}
        assert series.hops == ()
        assert series.sources() == ("nse-bhavcopy", "marketlens-close")
        assert series.as_of() is None
        assert series.value_at("2026-03-02") == 250.0
        assert "must not be reduced" not in series.describe()

    def test_a_broken_comparability_check_would_blend_the_seam(self, monkeypatch):
        """The bug this package was written to prevent, reproduced on purpose.

        :meth:`~bfinance.sources.model.Basis.comparable` is the single comparability policy, and
        ``Series.build`` routes seam detection through it. Patching it to always agree makes the
        two bases collapse into one seamless series — which is exactly the silent discontinuity
        this layer refuses to produce, and which would leave a 1:2 split reading as a -50.00% move.

        If this test ever fails because the patch stopped taking effect, the seam test above has
        become unfailable and is now checking nothing.
        """
        monkeypatch.setattr(Basis, "comparable", lambda self, other: True)
        blended = Series.build(
            [
                Segment("nse-bhavcopy", Basis.AS_TRADED, {"2026-03-01": 500.0, "2026-03-02": 250.0}),
                Segment("vendor-b-adjustments", Basis.SPLIT_ADJUSTED, {"2026-03-02": 250.0}),
            ]
        )
        assert blended.basis_mismatch is False, (
            "the patch did not take effect: Series.build is no longer routing seam detection "
            "through Basis.comparable, so the refusal is being decided somewhere else and the "
            "basis-seam tests are not guarding the policy they claim to"
        )
        # The blend, and the fake move it produces.
        assert blended.frame() == {"2026-03-01": 500.0, "2026-03-02": 250.0}
        assert 250.0 / 500.0 - 1 == pytest.approx(-0.5), (
            "the blended series fabricates the -50.00% move a 1:2 split appears to cause"
        )

    def test_the_empty_payload_rule_is_not_a_blanket_falsy_check(self, monkeypatch):
        """The control for the empty-payload rule, on the other side.

        ``is_empty_payload`` rejects ``None``, an empty frame and an empty mapping. It must accept
        ``0``, ``False`` and an empty list, because those are real answers for some capability, and
        a rule that rejected them would be replacing one lie with another.
        """
        for real_answer in (0, 0.0, False, "", "0", []):
            assert is_empty_payload(real_answer) is False, f"{real_answer!r} is a real answer"
        for non_answer in (None, {}, pd.DataFrame(), pd.Series(dtype="float64")):
            assert is_empty_payload(non_answer) is True, f"{non_answer!r} is not an answer"

    def test_a_hand_built_resolved_cannot_lie_about_its_own_mismatch_flag(self):
        """The flag is computed from the two basis fields, so it cannot be set independently.

        Everything downstream branches on ``basis_mismatch``; if a caller could construct a
        :class:`Resolved` whose flag disagreed with its basis, the whole safety argument would
        rest on a field that anything could overrule.
        """
        with pytest.raises(ValueError, match="disagrees with the basis fields"):
            Resolved(
                value=1.0,
                source="primary",
                capability="price_history",
                basis=Basis.AS_TRADED,
                basis_requested=Basis.AS_TRADED,
                basis_mismatch=True,
                degraded=False,
                attempts=(),
            )

    def test_no_assertion_in_this_file_is_a_constant_disjunction(self):
        """Guarded by the rule in ``tests/test_no_lying_docs.py``, and checked here too.

        That file enforces ``X or True`` and ``X and False`` across the whole suite, and it has
        already caught two agents — one in an otherwise green file. It is re-checked here over
        this file alone, by the same AST rule, so the finding names this file and this test rather
        than relying on a different suite to notice later.
        """
        offenders: list = []
        path = Path(__file__)
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assert):
                continue
            test = node.test
            if not isinstance(test, ast.BoolOp):
                continue
            for value in test.values:
                if isinstance(value, ast.Constant) and (
                    (isinstance(test.op, ast.Or) and value.value is True)
                    or (isinstance(test.op, ast.And) and value.value is False)
                ):
                    offenders.append(f"{path.name}:{node.lineno} {ast.unparse(test)}")
        assert offenders == [], (
            f"assertion(s) in this file that cannot fail: {offenders}. `X or True` is always True "
            f"and `X and False` is always False."
        )
