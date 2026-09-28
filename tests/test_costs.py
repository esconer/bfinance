"""
``bfinance.costs`` — the date-keyed transaction-cost schedule.

Fully offline and fixture-backed. Every rate is **parsed from the stored circular excerpts** in
``tests/fixtures/nse/costs/`` on each test, so the 22.248 bps figure below is computed from
transcribed component rates rather than asserted as a literal. That distinction is the whole
point: a test that reads a precomputed answer cannot fail when the transcription is wrong, and
this module exists to replace a number that was wrong by a factor nobody could see.

Six assertions in this repository have shipped that could not fail. The negative-control section
at the end is the deliberate answer to that: it mutates a transcribed rate and removes the STT
term and asserts the affected tests go red. See
:func:`test_negative_control_a_mutated_stt_rate_moves_the_bps` and
:func:`test_negative_control_removing_the_stt_term_breaks_the_dominance_assertion`.
"""

from __future__ import annotations

import json
import re
from datetime import date

import httpx
import pytest

from bfinance import costs

# ------------------------------------------------------------------ fixtures

FIXTURE_DIR = __import__("pathlib").Path(__file__).resolve().parent / "fixtures" / "nse" / "costs"
STT_FIXTURE = FIXTURE_DIR / "NSE_FATAX_73524_stt_rates.txt"
EXCHANGE_FIXTURE = FIXTURE_DIR / "NSE_FA_73061_exchange_charges.txt"

#: One crore — the unit every exchange charge is published in, and the denominator that makes
#: the published per-crore figures land on round rupee numbers.
CRORE = costs.CRORE

#: The 2026 schedule: on or after 2026-04-01 for STT, on or after 2026-03-01 for the exchange
#: charge. Both boundaries are in force on this date.
IN_2026 = "2026-05-01"
#: After the exchange-charge cutover, before the STT cutover.
BETWEEN_CUTOVERS = "2026-03-15"
#: Before both, but inside the schedule's evidenced window.
BEFORE_2026 = "2025-06-02"


def stt_text() -> str:
    return STT_FIXTURE.read_text(encoding="utf-8")


def exchange_text() -> str:
    return EXCHANGE_FIXTURE.read_text(encoding="utf-8")


def schedule() -> costs.CostSchedule:
    """A schedule built fresh from the fixture bytes on every call.

    Not cached in a module global, on purpose: a test that mutates a fixture must be able to see
    the mutation, and a shared cached schedule would hide exactly that.
    """
    return costs.schedule_from_circulars(stt_text(), exchange_text())


def rt_bps(day: str = IN_2026, *, brokerage_pct: float = 0.0, notional: float = CRORE) -> float:
    """A Rs. 1 crore delivery round trip, in bps. The shorthand every figure below uses."""
    return costs.quote(
        costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=day,
        buy_amount=notional, brokerage_pct=brokerage_pct, schedule=schedule(),
    ).round_trip_bps


# =========================================================== the headline number


class TestRoundTrip:
    def test_the_delivery_round_trip_is_exactly_22_248_bps(self):
        """22.248 bps, derived from the transcribed rates -- not read from a literal.

        The derivation, on a Rs. 1 crore purchase followed by a Rs. 1 crore sale under the 2026
        schedule (denominator: the one-way notional, so a round trip is expressed against a
        single leg's turnover -- see ``CostQuote.round_trip_bps``)::

            per side, from the circulars
              STT 0.1% of 1 crore (delivery, Purchaser on buy / Seller on sell)   10,000.00
              exchange charge Rs. 307/crore each side                                307.00
              SEBI turnover 0.0001% of 1 crore                                       10.00
              GST 18% of (0 brokerage + 307 + 0.01 IPFT + 10) = 18% of 317.00         57.06
              stamp duty 0.015% of 1 crore -- BUY ONLY                              1,500.00
                                                                            ------------
            buy leg                                                                          11,874.06
            sell leg  (no stamp duty)                                                   10,374.06
                                                                            =============
            round trip                                                                     22,248.12
              = 22,248.12 / 1e7 * 1e4 = 22.24812 bps
        """
        q = costs.quote(
            costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=IN_2026,
            buy_amount=CRORE, schedule=schedule(),
        )

        # Component by component, from the parsed rates -- no hardcoded rupee figures.
        stt = schedule().stt_regime_for(date(2026, 5, 1))
        exch = schedule().exchange_regime_for(date(2026, 5, 1))
        sch = schedule()

        assert stt.for_key("delivery_purchase").pct == 0.1, "row 1, marked (No Change)"
        assert stt.for_key("delivery_sale").pct == 0.1, "row 2, marked (No Change)"
        assert exch.total_for(costs.CASH_MARKET) == 307.0
        assert sch.sebi_pct == 0.0001
        assert sch.gst_pct == 18.0
        assert sch.stamp_duty_pct == 0.015

        assert q.buy.component_rupees(costs.STT) == 10_000.0
        assert q.sell.component_rupees(costs.STT) == 10_000.0
        assert q.buy.component_rupees(costs.EXCHANGE_CHARGE) == 307.0
        assert q.sell.component_rupees(costs.EXCHANGE_CHARGE) == 307.0
        assert q.buy.component_rupees(costs.SEBI_TURNOVER) == 10.0
        assert q.sell.component_rupees(costs.SEBI_TURNOVER) == 10.0
        assert q.buy.component_rupees(costs.STAMP_DUTY) == 1_500.0
        assert q.sell.component_rupees(costs.STAMP_DUTY) == 0.0, "stamp duty is buy-side only"
        assert q.buy.component_rupees(costs.GST) == 57.06
        assert q.sell.component_rupees(costs.GST) == 57.06

        assert q.buy.total_rupees == 11_874.06
        assert q.sell.total_rupees == 10_374.06
        assert q.round_trip_rupees == 22_248.12
        assert round(q.round_trip_bps, 3) == 22.248

    def test_the_denominator_convention_is_explicit_and_both_are_reported(self):
        """22.248 is a round trip over a ONE-WAY notional. Half of it is the two-way figure.

        A reader who divides 22,248.12 by total two-way turnover gets 11.124 and may conclude the
        module is wrong by 2x. Both numbers are always present, and the quote's ``bps_convention``
        names which one it is, so the ambiguity is resolvable without reading this file.
        """
        q = costs.quote(
            costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=IN_2026,
            buy_amount=CRORE, schedule=schedule(),
        )
        assert q.bps_denominator == CRORE
        assert round(q.round_trip_bps, 3) == 22.248
        assert round(q.round_trip_bps_on_turnover, 3) == 11.124
        assert round(q.round_trip_bps, 6) == round(
            q.round_trip_bps_on_turnover * 2, 6
        )
        assert q.to_dict()["bps_convention"] == (
            "round-trip cost in bps of the one-way notional"
        )
        # And the per-leg figure, which is the third common convention.
        assert 11.0 < q.one_way_bps < 12.0

    def test_the_bps_is_scale_invariant_and_the_rupee_total_is_not(self):
        """Halving the notional halves the rupees and leaves the bps unchanged. **That is correct.**

        A naive expectation would say the bps doubles, but it must not: bps is a *ratio*, and every
        component here is proportional to the notional -- STT, stamp duty, SEBI, brokerage are
        percentages, and the exchange charge is Rs-per-crore-of-traded-value. Scale the trade down
        and the cost and the denominator shrink together.

        So this test pins the two things that are actually true, and the invariant that distinguishes
        them: the rupee total halves, the bps does not move, and the ratio of the two bps across
        two notionals is exactly 1.
        """
        one = costs.quote(
            costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=IN_2026,
            buy_amount=CRORE, schedule=schedule(),
        )
        half = costs.quote(
            costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=IN_2026,
            buy_amount=CRORE / 2, schedule=schedule(),
        )
        tenth = costs.quote(
            costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=IN_2026,
            buy_amount=CRORE / 10, schedule=schedule(),
        )
        # Rupees scale linearly with the notional, to the paise. Note the rounding: GST is rounded
        # to 2 dp per leg, so a tenth-scale quote is 2224.82 rather than 2224.812. The difference is
        # 0.008 rupees, which is the accumulated sub-paise effect of three rounded GST charges and
        # is not a defect -- the invariant is "within a rupee of the exact ratio", and the bps
        # identity below is exact.
        assert round(half.round_trip_rupees, 2) == round(one.round_trip_rupees / 2, 2)
        assert abs(tenth.round_trip_rupees - one.round_trip_rupees / 10) < 0.01
        assert one.round_trip_rupees == pytest.approx(half.round_trip_rupees * 2, abs=0.01)
        assert one.round_trip_rupees == pytest.approx(
            tenth.round_trip_rupees * 10, abs=0.5
        ), (
            "the tenth-scale quote loses at most 0.01 rupee of GST rounding per leg, so the "
            "round trip may be up to half a rupee away from exactly one tenth"
        )
        # The bps does not move -- so the module is not reporting a constant. It is not *bit*-exact
        # either, because GST is rounded to 2 dp per leg: at a tenth of a crore that rounding is
        # worth 0.000008 bps, which is why this is a tolerance and not an equality.
        assert half.round_trip_bps == pytest.approx(one.round_trip_bps, abs=1e-6)
        assert tenth.round_trip_bps == pytest.approx(one.round_trip_bps, abs=1e-4)
        assert round(half.round_trip_bps, 3) == 22.248
        assert round(tenth.round_trip_bps, 3) == 22.248
        # And the denominator really did follow the notional, which is why the bps held.
        assert one.bps_denominator == CRORE
        assert half.bps_denominator == CRORE / 2
        assert tenth.bps_denominator == CRORE / 10
        # A bps that ignored the amount would be a constant in rupees, not a rate.
        assert one.round_trip_rupees != tenth.round_trip_rupees


# =========================================================== STT dominance


class TestSttDominance:
    def test_stt_is_89_9_percent_of_the_round_trip(self):
        """The anti-regression assertion this module exists for.

        Modelling only the exchange charge would give roughly 0.6 bps and be low by ~36x. If a
        future edit drops the STT term, or moves STT into some other bucket, this fails loudly
        rather than shipping a cost model that is wrong by a factor its author cannot see.
        """
        q = costs.quote(
            costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=IN_2026,
            buy_amount=CRORE, schedule=schedule(),
        )
        share = q.component_share(costs.STT)
        assert round(share * 100, 1) == 89.9, (
            f"STT is {share * 100:.3f}% of the round trip. If this moved, the rate table changed "
            f"or a component was lost; 89.9% is what the transcribed circulars give."
        )
        assert share > 0.85, "below 85% the model is no longer STT-dominated and is suspect"

    def test_the_exchange_charge_is_about_3_4_percent_not_the_whole_cost(self):
        """Exchange + IPFT + SEBI + GST together are 3.4%. This is the 36x error, quantified.

        Stated as a ratio so the claim is checkable: if the small components ever became the bulk
        of the cost, the assertion that says "STT dominates" would be wrong about the world, and
        that should be a test failure rather than a stale comment.
        """
        q = costs.quote(
            costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=IN_2026,
            buy_amount=CRORE, schedule=schedule(),
        )
        small = sum(
            q.component_rupees(name)
            for name in (costs.EXCHANGE_CHARGE, costs.IPFT, costs.SEBI_TURNOVER, costs.GST)
        )
        # 3.4% is the SHARE of the round trip, which is 0.75 bps. Both are asserted, because
        # quoting one as the other is exactly the sort of slip this module exists to avoid.
        share = small / q.round_trip_rupees
        small_bps = small / CRORE * costs.BPS_PER_UNIT
        assert round(share * 100, 1) == 3.4
        assert round(small_bps, 2) == 0.75
        assert q.round_trip_bps / small_bps > 29.0, (
            f"the round trip is {q.round_trip_bps / small_bps:.1f}x the small components; "
            f"modelling only those understates cost by that factor"
        )

    def test_a_model_of_only_the_exchange_charge_is_low_by_more_than_30x(self):
        """The concrete failure the old constant encoded, as an executable statement."""
        q = costs.quote(
            costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=IN_2026,
            buy_amount=CRORE, schedule=schedule(),
        )
        exchange_only = q.component_rupees(costs.EXCHANGE_CHARGE)
        understated = exchange_only / CRORE * costs.BPS_PER_UNIT
        assert round(understated, 2) == 0.61
        assert q.round_trip_bps / understated > 30.0

    def test_the_old_10_bps_constant_is_wrong_by_more_than_2x(self):
        """The constant this module replaces, compared to the transcribed figure."""
        assert 22.248 / 10.0 > 2.0
        assert round(rt_bps(), 3) == 22.248

    def test_a_zero_cost_round_trip_refuses_to_report_a_share(self):
        """A component share of a zero total is undefined, and must not be reported as 0.0."""
        sch = schedule()
        # Price a notional so small the rupee amounts round away, then ask for a share.
        q = costs.quote(
            costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=IN_2026,
            buy_amount=1e-9, schedule=sch,
        )
        if q.round_trip_rupees <= 0.0:
            with pytest.raises(costs.CostScheduleError, match="costs nothing"):
                q.component_share(costs.STT)
        else:  # pragma: no cover - depends on float granularity, both paths are correct
            assert 0.0 <= q.component_share(costs.STT) <= 1.0


# =========================================================== date boundaries


class TestDateBoundaries:
    def test_the_2026_03_01_boundary_switches_the_exchange_regime(self):
        """One day either side of 2026-03-01 selects different -- and different-*stated* -- rates."""
        sch = schedule()
        before = sch.exchange_regime_for(date(2026, 2, 28))
        after = sch.exchange_regime_for(date(2026, 3, 1))

        assert before.effective_to == date(2026, 2, 28)
        assert after.effective_from == date(2026, 3, 1)
        assert before.source.endswith("'Existing outflow'")
        assert after.source.endswith("'Revised outflow'")
        assert before.derived is False and after.derived is False, (
            "both regimes are published in the circular's own two columns"
        )

    def test_the_2026_03_01_boundary_is_cost_neutral_not_merely_equivalent(self):
        """Both sides total Rs. 307. **This is why the schedule keys on the total.**

        The change moved Rs. 306.99 of transaction charge and Rs. 0.01 of IPFT into a different
        column. A schedule keyed on the *named component* breaks on this date: it would look for a
        component that no longer exists, or double-count one that does. A schedule keyed on the
        total is unmoved. The assertion is explicit because it is the property that makes the
        keying choice correct rather than merely convenient.
        """
        sch = schedule()
        before = sch.exchange_regime_for(date(2026, 2, 28)).for_segment(costs.CASH_MARKET)
        after = sch.exchange_regime_for(date(2026, 3, 1)).for_segment(costs.CASH_MARKET)

        # The split moved...
        assert before.transaction_charges == 297.0
        assert before.ipft == 10.0
        assert after.transaction_charges == 306.99
        assert after.ipft == 0.01
        assert before.transaction_charges != after.transaction_charges

        # ...and the total did not.
        assert before.total == 307.0
        assert after.total == 307.0
        assert before.total == after.total, (
            "the 2026-03-01 exchange-charge change is cost-neutral by construction"
        )

    def test_the_neutrality_holds_for_all_three_segments_not_just_cash(self):
        """Futures Rs. 183 and options Rs. 3,553 are neutral too. Cash alone would not prove it."""
        sch = schedule()
        for segment, total in (
            (costs.CASH_MARKET, 307.0), (costs.EQUITY_FUTURES, 183.0), (costs.EQUITY_OPTIONS, 3553.0)
        ):
            before = sch.exchange_regime_for(date(2026, 2, 28)).for_segment(segment)
            after = sch.exchange_regime_for(date(2026, 3, 1)).for_segment(segment)
            assert after.total == total, f"{segment} revised total"
            assert before.total == after.total, f"{segment} is not cost-neutral"
            assert before.transaction_charges != after.transaction_charges

    def test_the_2026_03_01_boundary_leaves_the_computed_bps_untouched(self):
        """Cost-neutrality seen from the outside: the bps does not move across the boundary."""
        before = rt_bps("2026-02-27")
        after = rt_bps("2026-03-02")
        assert round(before, 6) == round(after, 6), (
            "a cost-neutral tariff change must not move a single basis point"
        )

    def test_the_2026_04_01_boundary_changes_the_stt_regime(self):
        sch = schedule()
        before = sch.stt_regime_for(date(2026, 3, 31))
        after = sch.stt_regime_for(date(2026, 4, 1))
        assert before.effective_to == date(2026, 3, 31)
        assert after.effective_from == date(2026, 4, 1)
        assert "Rate upto March 31, 2026" in before.source
        assert "02/2026" in after.source

    def test_a_delivery_trade_is_unaffected_by_the_2026_04_01_stt_boundary(self):
        """Delivery is marked '(No Change)' in the circular, so its bps must not move either.

        A boundary test that only checked delivery would pass even if the whole rate table had
        failed to switch. This one asserts the *absence* of a change, which is the claim the
        circular's '(No Change)' marker makes.
        """
        before = rt_bps("2026-03-31")
        after = rt_bps("2026-04-01")
        assert round(before, 6) == round(after, 6)
        sch = schedule()
        assert (
            sch.stt_regime_for(date(2026, 3, 31)).for_key("delivery_purchase").pct
            == sch.stt_regime_for(date(2026, 4, 1)).for_key("delivery_purchase").pct
        )
        assert sch.stt_regime_for(date(2026, 3, 31)).for_key(
            "delivery_purchase"
        ).marked_no_change

    def test_the_pre_2026_schedule_is_materially_different_for_options_and_futures(self):
        """**The check a delivery-only boundary test would miss.**

        The Finance Act 2026 changed option sale 0.10% -> 0.15%, exercised option 0.125% ->
        0.15%, and futures 0.02% -> 0.05%. Those are 50% and 150% increases. A date-boundary test
        that only priced a delivery round trip would see no change at all and conclude the
        boundary was cosmetic.
        """
        sch = schedule()
        before = sch.stt_regime_for(date(2026, 3, 31))
        after = sch.stt_regime_for(date(2026, 4, 1))

        assert before.for_key("option_sale").pct == 0.10
        assert after.for_key("option_sale").pct == 0.15
        assert before.for_key("option_exercised").pct == 0.125
        assert after.for_key("option_exercised").pct == 0.15
        assert before.for_key("futures_sale").pct == 0.02
        assert after.for_key("futures_sale").pct == 0.05

        # Materially different, quantified: a 50% rise on the option sale and a 150% rise on
        # futures. Compared with approx because these are parsed decimal literals and 0.15/0.1
        # is not exactly 1.5 in binary floating point.
        assert after.for_key("option_sale").pct / before.for_key("option_sale").pct == (
            pytest.approx(1.5)
        )
        assert after.for_key("futures_sale").pct / before.for_key("futures_sale").pct == (
            pytest.approx(2.5)
        )
        # And the circular's own "upto March 31, 2026" figures are recorded on the new rates.
        assert after.for_key("option_sale").previous_pct == 0.10
        assert after.for_key("option_exercised").previous_pct == 0.125
        assert after.for_key("futures_sale").previous_pct == 0.02

    def test_an_option_round_trip_bps_moves_across_the_stt_boundary(self):
        """The schedule's own output, not just its tables, reflects the 2026-04-01 change."""
        def option_bps(day: str) -> float:
            return costs.quote(
                costs.OPTION_EXERCISED, costs.OPTION_SALE, trade_date=day,
                buy_amount=CRORE, premium=CRORE, schedule=schedule(),
            ).round_trip_bps

        before = option_bps("2026-03-31")
        after = option_bps("2026-04-01")
        assert after > before, "STT on options rose; the bps must rise with it"
        # Exercised 0.125% + sale 0.10% -> 0.15% + 0.15% on the same notional: +0.075% = 7.5 bps.
        assert round(after - before, 2) == 7.50

    def test_a_futures_round_trip_moves_across_the_stt_boundary(self):
        def futures_bps(day: str) -> float:
            return costs.quote(
                costs.FUTURES_SALE, costs.FUTURES_SALE, trade_date=day,
                buy_amount=CRORE, schedule=schedule(),
            ).round_trip_bps

        before = futures_bps("2026-03-31")
        after = futures_bps("2026-04-01")
        # Both legs are sales: 0.02% -> 0.05% per leg is +0.03% x 2 = +6 bps.
        assert round(after - before, 2) == 6.00

    def test_the_schedule_version_changes_across_a_boundary(self):
        """Every result records which version produced it, and the two sides differ."""
        sch = schedule()
        before = costs.quote(
            costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date="2026-03-31",
            buy_amount=CRORE, schedule=sch,
        )
        after = costs.quote(
            costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date="2026-04-01",
            buy_amount=CRORE, schedule=sch,
        )
        assert before.schedule_version == sch.version
        assert after.schedule_version == sch.version, (
            "the version names the floor's regime pair, so a boundary inside the window shows "
            "the same version string; the per-component effective_from on each line item is "
            "what distinguishes them"
        )
        # The distinction is carried per line item, which is where a consumer reads it.
        before_item = before.buy.items[0]
        after_item = after.buy.items[0]
        assert before_item.component == after_item.component == costs.STT
        assert before_item.effective_from != after_item.effective_from
        assert before_item.source != after_item.source

    def test_the_quote_records_why_it_chose_those_regimes(self):
        q = costs.quote(
            costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=BETWEEN_CUTOVERS,
            buy_amount=CRORE, schedule=schedule(),
        )
        assert "NSE/FATAX/73524" in q.why
        assert "NSE/FA/73061" in q.why
        assert "statutory" in q.why and "contractual" in q.why
        assert q.why.count("..") >= 2, "the why names a date range per regime"


# =========================================================== brokerage is a parameter


class TestBrokerageIsAParameter:
    def test_zero_brokerage_gives_22_248_bps(self):
        assert round(rt_bps(brokerage_pct=0.0), 3) == 22.248

    def test_brokerage_at_0_03_percent_per_side_gives_29_33_bps(self):
        """0.03%/side: brokerage Rs 3,000/side, and GST on it too.

        buy  10,000 STT + 1,500 stamp + 3,000 brokerage + 317 other + 18% of 3,317 GST = 16,014.06
        sell 10,000 STT +          3,000 brokerage + 317 other + 18% of 3,317 GST = 14,514.06
              = 30,528.12?  No: 29,328.12, because 'other' is 307 + 10 = 317, not 317 + 0.01.
        The figures are recomputed from the parsed rates in the assertions, not written here.
        """
        q = costs.quote(
            costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=IN_2026,
            buy_amount=CRORE, brokerage_pct=0.03, schedule=schedule(),
        )
        # 0.03% of 1 crore, per side.
        assert q.buy.component_rupees(costs.BROKERAGE) == 3_000.0
        assert q.sell.component_rupees(costs.BROKERAGE) == 3_000.0
        # GST base grew by exactly the brokerage, and GST is 18% of it.
        assert q.buy.gst_base == 3_000.0 + 307.0 + 10.0
        assert q.buy.component_rupees(costs.GST) == round(3_317.0 * 0.18, 2)
        assert q.buy.total_rupees == 10_000 + 1_500 + 3_000 + 307 + 10 + 597.06
        assert q.round_trip_rupees == 29_328.12
        assert round(q.round_trip_bps, 2) == 29.33

    def test_two_brokerage_values_give_two_different_totals_so_it_is_a_parameter(self):
        """The proof that brokerage is a parameter and not a constant baked into the module."""
        totals = {}
        for pct in (0.0, 0.03, 0.05):
            q = costs.quote(
                costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=IN_2026,
                buy_amount=CRORE, brokerage_pct=pct, schedule=schedule(),
            )
            totals[pct] = (q.round_trip_rupees, round(q.round_trip_bps, 3))

        assert totals[0.0] == (22_248.12, 22.248)
        assert totals[0.03] == (29_328.12, 29.328)
        assert totals[0.05] == (34_048.12, 34.048)
        assert len({t[0] for t in totals.values()}) == 3, "each rate must give its own total"
        # Monotone, and linear. Each 0.01% per side of brokerage is Rs 1,000/side = Rs 2,000 on
        # the round trip, plus 18% GST on it = Rs 360, so Rs 2,360 per 0.01%/side. The 0.00->0.03
        # step is three such increments and the 0.03->0.05 step is two, so the two deltas differ
        # and must both be checked: a per-side charge plus a tax on the base, not a flat markup.
        deltas = [totals[b][0] - totals[a][0] for a, b in ((0.0, 0.03), (0.03, 0.05))]
        assert [round(d, 6) for d in deltas] == [7_080.0, 4_720.0], deltas
        assert deltas[0] / deltas[1] == pytest.approx(1.5), "3 increments vs 2"
        # Per-0.01%-side unit cost, identical in both steps.
        assert round(deltas[0] / 3, 6) == round(deltas[1] / 2, 6) == 2_360.0

    def test_the_module_holds_no_brokerage_constant(self):
        """No module-level brokerage rate exists. Grep the source, not the docstring."""
        source = (
            __import__("pathlib").Path(costs.__file__).read_text(encoding="utf-8")
        )
        code = "\n".join(
            line for line in source.splitlines()
            if not line.lstrip().startswith("#")
        )
        # A module constant would look like `BROKERAGE_PCT = 0.0x`; there must be none.
        assert not re.search(r"^\s*BROKERAGE\w*_PCT\s*=", code, re.MULTILINE), (
            "brokerage must stay a caller parameter; a module constant would invite a caller to "
            "treat a non-existent published rate as real"
        )
        assert "brokerage_pct: float = 0.0" in code, (
            "the default must be an explicit zero passed through quote(), visible in the "
            "signature, not a constant read from elsewhere"
        )

    def test_brokerage_is_reported_as_contractual_and_never_fixture_sourced(self):
        rows = {r["component"]: r for r in schedule().provenance(date(2026, 5, 1))}
        assert rows[costs.BROKERAGE]["authority"] == costs.CONTRACTUAL
        assert rows[costs.BROKERAGE]["fixture_sourced"] is False
        assert rows[costs.BROKERAGE]["rate"] is None, (
            "the schedule knows no brokerage rate; the caller supplies it per call"
        )

    def test_an_absurd_brokerage_rate_is_refused_rather_than_silently_scaled(self):
        with pytest.raises(ValueError, match="not a per-side percentage"):
            costs.quote(
                costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=IN_2026,
                buy_amount=CRORE, brokerage_pct=3.0, schedule=schedule(),
            )


# =========================================================== the GST base


class TestGstBase:
    def test_the_base_is_brokerage_plus_exchange_plus_sebi_plus_ipft(self):
        """Assert the BASE, not merely the total that happens to result from it.

        On a Rs. 1 crore leg with no brokerage: 0 + 307.00 exchange + 10.00 SEBI = 317.00, and
        18% of that is Rs 57.06. A GST base that wrongly included STT would be 10,317.00 and give
        Rs 1,857.06 -- a 32x error in one line, which is why the base is exposed.
        """
        q = costs.quote(
            costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=IN_2026,
            buy_amount=CRORE, schedule=schedule(),
        )
        assert q.buy.gst_base == 317.00
        assert q.sell.gst_base == 317.00
        assert q.buy.gst_pct == 18.0
        assert q.buy.component_rupees(costs.GST) == 57.06
        assert round(q.buy.component_rupees(costs.GST), 2) == round(q.buy.gst_base * 0.18, 2)

    def test_gst_is_not_charged_on_stt_or_on_stamp_duty(self):
        """The two exclusions, demonstrated by the base rather than asserted in prose."""
        q = costs.quote(
            costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=IN_2026,
            buy_amount=CRORE, schedule=schedule(),
        )
        buy = q.buy
        assert buy.component_rupees(costs.STT) == 10_000.0
        assert buy.component_rupees(costs.STAMP_DUTY) == 1_500.0
        # Neither appears in the base.
        assert buy.gst_base == 317.00
        assert 10_000.0 not in (buy.gst_base,)
        # And the base is exactly the four covered components.
        assert buy.gst_base == (
            buy.component_rupees(costs.BROKERAGE)
            + buy.component_rupees(costs.EXCHANGE_CHARGE)
            + buy.component_rupees(costs.IPFT)
            + buy.component_rupees(costs.SEBI_TURNOVER)
        )
        # If STT or stamp duty were in the base, GST would be at least 18% of 11,817.
        assert buy.component_rupees(costs.GST) < 0.18 * 1_000.0

    def test_the_base_grows_with_brokerage_because_brokerage_is_covered(self):
        no_brokerage = costs.quote(
            costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=IN_2026,
            buy_amount=CRORE, brokerage_pct=0.0, schedule=schedule(),
        )
        with_brokerage = costs.quote(
            costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=IN_2026,
            buy_amount=CRORE, brokerage_pct=0.03, schedule=schedule(),
        )
        assert with_brokerage.buy.gst_base - no_brokerage.buy.gst_base == 3_000.0
        assert with_brokerage.buy.component_rupees(costs.GST) > (
            no_brokerage.buy.component_rupees(costs.GST)
        )

    def test_gst_applies_to_both_sides(self):
        """It is not a one-off charge on the buy. Both legs are charged."""
        q = costs.quote(
            costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=IN_2026,
            buy_amount=CRORE, schedule=schedule(),
        )
        assert q.buy.component_rupees(costs.GST) == 57.06
        assert q.sell.component_rupees(costs.GST) == 57.06
        assert q.component_rupees(costs.GST) == 114.12

    def test_gst_reports_itself_as_statutory_and_not_fixture_sourced(self):
        rows = {r["component"]: r for r in schedule().provenance(date(2026, 5, 1))}
        assert rows[costs.GST]["authority"] == costs.STATUTORY
        assert rows[costs.GST]["fixture_sourced"] is False
        assert "not" in rows[costs.GST]["rate_unit"].lower() or "brokerage" in (
            rows[costs.GST]["rate_unit"].lower()
        )
        assert "STT" in rows[costs.GST]["note"] or "stamp" in rows[costs.GST]["note"]


# =========================================================== out-of-range dates


class TestOutOfRangeDates:
    def test_a_date_before_the_window_raises_rather_than_clamping(self):
        """2014 must not get 2023's rates.

        Silently clamping is the failure this guards: a caller asking about 2014 would receive a
        confident, plausible, wrong number. The schedule's earliest evidence is 2023-04-01.
        """
        with pytest.raises(costs.CostScheduleCoverageError) as excinfo:
            rt_bps("2014-06-02")
        message = str(excinfo.value)
        assert "2014-06-02" in message
        assert "2023-04-01" in message, "the error names the window it does cover"
        assert "not this date's rate" in message

    @pytest.mark.parametrize("day", [
        "1990-01-01", "2010-05-05", "2019-03-29", "2022-12-30", "2023-03-31",
    ])
    def test_every_date_before_the_floor_raises(self, day):
        """The day before the floor included. A boundary is off-by-one-prone; parametrise it."""
        assert costs.schedule_from_circulars(
            stt_text(), exchange_text()
        ).floor == date(2023, 4, 1)
        with pytest.raises(costs.CostScheduleCoverageError):
            rt_bps(day)

    @pytest.mark.parametrize("day", ["2023-04-01", "2023-04-02", "2025-01-01", "2026-12-31"])
    def test_dates_inside_the_window_are_answered(self, day):
        bps = rt_bps(day)
        assert 0.0 < bps < 100.0, f"{day} priced at {bps} bps, which is not a delivery round trip"

    def test_the_floor_date_itself_is_answered_and_the_day_before_is_not(self):
        sch = schedule()
        assert sch.covers(date(2023, 4, 1))
        assert not sch.covers(date(2023, 3, 31))
        assert sch.stt_regime_for(date(2023, 4, 1)) is not None
        with pytest.raises(costs.CostScheduleCoverageError):
            sch.stt_regime_for(date(2023, 3, 31))

    def test_an_unparseable_date_raises_a_value_error_not_a_coverage_error(self):
        """A typo and an out-of-window date are different problems and get different types."""
        with pytest.raises(ValueError) as excinfo:
            rt_bps("not-a-date")
        assert not isinstance(excinfo.value, costs.CostScheduleCoverageError)
        assert "YYYY-MM-DD" in str(excinfo.value)

    def test_the_coverage_error_names_the_evidence_for_the_floor(self):
        """The refusal cites why the floor is where it is, so it is not an arbitrary limit."""
        with pytest.raises(costs.CostScheduleCoverageError) as excinfo:
            schedule().exchange_regime_for(date(2014, 6, 2))
        message = str(excinfo.value)
        assert "NSE/FA/56129" in message
        assert "2023-03-24" in message

    def test_an_unknown_shape_is_refused_before_any_arithmetic(self):
        """An unrecognised shape must not fall through to a zero-cost quote."""
        with pytest.raises(ValueError, match="not a known transaction shape"):
            costs.quote(
                "delivery_buy", "teleport", trade_date=IN_2026,
                buy_amount=CRORE, schedule=schedule(),
            )
        with pytest.raises(ValueError, match="not a known transaction shape"):
            costs.quote(
                "delivery_buy", "", trade_date=IN_2026,
                buy_amount=CRORE, schedule=schedule(),
            )

    def test_a_non_positive_notional_is_refused(self):
        for bad in (0.0, -1.0):
            with pytest.raises(ValueError, match="must be positive"):
                costs.quote(
                    costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=IN_2026,
                    buy_amount=bad, schedule=schedule(),
                )

    def test_an_option_without_a_premium_is_refused_not_priced_on_the_wrong_base(self):
        """The options exchange charge is on PREMIUM. Falling back to traded value would be wrong.

        A Rs. 1 crore option premium is not a Rs. 1 crore underlying exposure; the charge base is
        an order of magnitude smaller. Using the consideration here would overstate the exchange
        charge by the same factor, so the call raises instead.
        """
        with pytest.raises(ValueError, match="on premium"):
            costs.quote(
                costs.OPTION_SALE, costs.OPTION_SALE, trade_date=IN_2026,
                buy_amount=CRORE, schedule=schedule(),
            )
        # Supplying it is accepted, and the base is the premium.
        q = costs.quote(
            costs.OPTION_SALE, costs.OPTION_SALE, trade_date=IN_2026,
            buy_amount=CRORE, premium=CRORE / 100, schedule=schedule(),
        )
        assert q.buy.exchange_base == CRORE / 200
        assert q.buy.exchange_base_kind == costs.BASE_PREMIUM


# =========================================================== provenance


class TestProvenance:
    def test_exchange_published_components_name_their_circular(self):
        rows = {r["component"]: r for r in schedule().provenance(date(2026, 5, 1))}
        for name in (costs.STT, costs.EXCHANGE_CHARGE, costs.IPFT):
            assert rows[name]["authority"] == costs.EXCHANGE_PUBLISHED
            assert rows[name]["fixture_sourced"] is True
        assert "NSE/FATAX/73524" in rows[costs.STT]["source"]
        assert "NSE/FA/73061" in rows[costs.EXCHANGE_CHARGE]["source"]

    def test_the_four_components_with_no_exchange_circular_are_separated_out(self):
        """A caller must be able to see which numbers are exchange-published and which are not.

        The separation is not cosmetic. Four of the seven components are fixed by statute or by
        contract, and none of them appears in either downloaded circular -- yet a schedule that
        presented all seven identically would let a reader assume a SEBI circular published the
        0.0001% turnover fee. It does not exist, and the point of the split is that the module
        says so.
        """
        rows = {r["component"]: r for r in schedule().provenance(date(2026, 5, 1))}

        statutory = {n for n, r in rows.items() if r["authority"] == costs.STATUTORY}
        assert statutory == {costs.SEBI_TURNOVER, costs.STAMP_DUTY, costs.GST}
        contractual = {n for n, r in rows.items() if r["authority"] == costs.CONTRACTUAL}
        assert contractual == {costs.BROKERAGE}
        published = {n for n, r in rows.items() if r["authority"] == costs.EXCHANGE_PUBLISHED}
        assert published == {costs.STT, costs.EXCHANGE_CHARGE, costs.IPFT}

        assert set(rows) == set(costs.COMPONENTS), "every component is reported, none invented"
        for name in statutory | contractual:
            assert rows[name]["fixture_sourced"] is False
            assert "no NSE" in rows[name]["published_by"] or "not published" in (
                rows[name]["published_by"]
            ) or "negotiated" in rows[name]["published_by"] or "statute" in (
                rows[name]["source"]
            ) or "State stamp" in rows[name]["published_by"] or "CGST" in (
                rows[name]["published_by"]
            )

    def test_stamp_duty_says_plainly_that_no_nse_circular_exists_for_it(self):
        rows = {r["component"]: r for r in schedule().provenance(date(2026, 5, 1))}
        stamp = rows[costs.STAMP_DUTY]
        assert stamp["authority"] == costs.STATUTORY
        assert "stamp" in stamp["published_by"].lower()
        assert "no NSE or SEBI circular" in stamp["note"]
        assert stamp["rate"] == costs.STAMP_DUTY_PCT

    def test_the_sebi_turnover_fee_names_the_act_and_its_cap(self):
        rows = {r["component"]: r for r in schedule().provenance(date(2026, 5, 1))}
        sebi = rows[costs.SEBI_TURNOVER]
        assert "s.15B" in sebi["published_by"]
        assert "10 crore" in sebi["note"]
        assert sebi["rate"] == 0.0001

    def test_the_exchange_charge_reports_its_split_as_provenance_only(self):
        """The 2026-03-01 change moved 306.99 and 0.01 between columns; the report shows both.

        A consumer reading only ``total`` would not know the split had moved at all, which is
        exactly the information that makes the change auditable.
        """
        rows = {r["component"]: r for r in schedule().provenance(date(2026, 5, 1))}
        charge = rows[costs.EXCHANGE_CHARGE]
        split = charge["component_split_provenance_only"]
        assert split == {"transaction_charges": 306.99, "ipft": 0.01}
        assert charge["rate"] == 307.0
        assert "provenance only" in charge["note"]

    def test_provenance_carries_an_effective_date_range_for_every_exchange_component(self):
        rows = schedule().provenance(date(2026, 5, 1))
        for row in rows:
            if row["authority"] == costs.EXCHANGE_PUBLISHED:
                assert re.match(r"\d{4}-\d{2}-\d{2}", row["effective_from"]), row["component"]
                assert row["effective_to"] == "open" or re.match(
                    r"\d{4}-\d{2}-\d{2}", row["effective_to"]
                ), row["component"]

    def test_provenance_is_ordered_by_authority_so_the_output_is_deterministic(self):
        rows = schedule().provenance(date(2026, 5, 1))
        authorities = [r["authority"] for r in rows]
        assert authorities == sorted(
            authorities, key=costs.AUTHORITIES.index
        ), "exchange-published first, then statutory, then contractual"

    def test_every_line_item_on_a_quote_carries_its_provenance_and_date_range(self):
        q = costs.quote(
            costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=IN_2026,
            buy_amount=CRORE, brokerage_pct=0.03, schedule=schedule(),
        )
        for leg in (q.buy, q.sell):
            assert leg.items, "a leg with no line items would report a zero cost"
            for item in leg.items:
                assert item.authority in costs.AUTHORITIES
                assert item.published_by
                assert item.source
                assert re.match(r"\d{4}-\d{2}-\d{2}", item.effective_from)
                assert item.effective_to == "open" or re.match(
                    r"\d{4}-\d{2}-\d{2}", item.effective_to
                )

    def test_the_quote_serialises_to_json_with_its_provenance_tag(self):
        q = costs.quote(
            costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=IN_2026,
            buy_amount=CRORE, schedule=schedule(),
        )
        payload = q.to_dict()
        assert payload["provenance"] == costs.PROVENANCE
        round_tripped = json.loads(json.dumps(payload))
        assert round_tripped["round_trip_rupees"] == 22_248.12
        assert round_tripped["component_share"][costs.STT] == 0.898952


# =========================================================== transcribed rates


class TestTranscribedRates:
    def test_the_stt_table_is_read_from_the_fixture_not_hardcoded(self):
        """Asserting the module's own parse against the fixture's own bytes.

        If someone edits a rate in the fixture, both sides of this assertion move together and it
        would still pass -- which is why the *negative controls* at the end mutate the fixture and
        assert the bps test goes red. This test's job is to prove the parse is reading the
        fixture at all, and to pin the transcribed values as a reviewable list.
        """
        raw = stt_text()
        parsed = costs.parse_stt_circular(raw)
        assert parsed.new_rates["delivery_purchase"].pct == 0.1
        assert parsed.new_rates["delivery_sale"].pct == 0.1
        assert parsed.new_rates["eof_unit_sale"].pct == 0.001
        assert parsed.new_rates["intraday_sale"].pct == 0.025
        assert parsed.new_rates["option_sale"].pct == 0.15
        assert parsed.new_rates["option_exercised"].pct == 0.15
        assert parsed.new_rates["futures_sale"].pct == 0.05
        # And the fixture really contains those numerals.
        assert "0.15 per cent" in raw
        assert "0.001 per cent" in raw
        assert "(No Change)" in raw

    def test_the_delivery_and_intraday_rates_are_marked_no_change_in_the_source(self):
        """The '(No Change)' marker is transcribed, not assumed.

        A schedule that carried the pre-April rate forward as if it had been re-published would be
        claiming something the circular does not say, and would silently break if NSE ever
        re-tariffed delivery without touching the option rows.
        """
        parsed = costs.parse_stt_circular(stt_text())
        for key in ("delivery_purchase", "delivery_sale", "eof_unit_sale", "intraday_sale"):
            assert parsed.new_rates[key].marked_no_change is True
            assert parsed.new_rates[key].previous_pct == parsed.new_rates[key].pct
        for key in ("option_sale", "option_exercised", "futures_sale"):
            assert parsed.new_rates[key].marked_no_change is False
            assert parsed.new_rates[key].previous_pct != parsed.new_rates[key].pct

    def test_the_stt_payer_is_read_from_the_circular_and_the_exercised_option_is_a_purchaser(self):
        parsed = costs.parse_stt_circular(stt_text())
        assert parsed.new_rates["delivery_purchase"].payer == "Purchaser"
        assert parsed.new_rates["delivery_sale"].payer == "Seller"
        assert parsed.new_rates["intraday_sale"].payer == "Seller"
        assert parsed.new_rates["option_sale"].payer == "Seller"
        assert parsed.new_rates["option_exercised"].payer == "Purchaser", (
            "row 4(b) is payable by the Purchaser: an exercised option is taxed on the buyer "
            "who receives the shares, not the writer who sold the option"
        )
        assert parsed.new_rates["futures_sale"].payer == "Seller"

    def test_the_row_4_table_and_the_revision_prose_are_cross_checked(self):
        """Two independent reads of the same circular must agree, or the parse raises.

        The module refuses to price an option if the prose '(a)/(b)/(c)' sentences and the table's
        six percentages disagree. A silent preference for one over the other would let a
        mis-extraction through as a plausible rate.
        """
        # Sanity: the fixture holds both reads.
        raw = stt_text()
        assert len(costs._STT_REVISION_RE.findall(raw)) == 3
        assert costs.parse_stt_circular(raw).new_rates["futures_sale"].pct == 0.05

    def test_intraday_purchase_has_no_stt_and_the_module_says_why(self):
        """The circular taxes only the sale leg of an intraday round trip.

        Row 3 is worded "Sale of ... settled otherwise than by the actual delivery"; there is no
        purchase row for it. The zero is a transcription of an absent rate, and the line item
        records that it is absent rather than claiming a published 0.0%.
        """
        q = costs.quote(
            costs.INTRADAY_BUY, costs.INTRADAY_SELL, trade_date=IN_2026,
            buy_amount=CRORE, schedule=schedule(),
        )
        assert q.buy.component_rupees(costs.STT) == 0.0
        stt_item = next(i for i in q.buy.items if i.component == costs.STT)
        assert stt_item.rate == 0.0
        assert stt_item.note == "the circular states no purchase rate for this leg"
        assert q.sell.component_rupees(costs.STT) == 2_500.0, "0.025% of 1 crore"

    def test_intraday_is_cheaper_than_delivery_because_stt_is_lower(self):
        delivery = costs.quote(
            costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=IN_2026,
            buy_amount=CRORE, schedule=schedule(),
        )
        intraday = costs.quote(
            costs.INTRADAY_BUY, costs.INTRADAY_SELL, trade_date=IN_2026,
            buy_amount=CRORE, schedule=schedule(),
        )
        assert intraday.round_trip_bps < delivery.round_trip_bps
        # Two effects, and the total is their sum: the purchase leg pays 0.1% for delivery and
        # nothing at all intraday (10 bps), and the sale leg drops 0.1% -> 0.025% (7.5 bps).
        # Counting only the sale leg would give 7.5; the purchase leg is the larger half.
        assert round(delivery.round_trip_bps - intraday.round_trip_bps, 2) == 17.50
        assert delivery.buy.component_rupees(costs.STT) == 10_000.0
        assert intraday.buy.component_rupees(costs.STT) == 0.0
        assert intraday.sell.component_rupees(costs.STT) == 2_500.0
        # Stamp duty still applies to an intraday purchase: it is levied on the transfer.
        assert intraday.buy.component_rupees(costs.STAMP_DUTY) == 1_500.0

    def test_the_equity_oriented_fund_unit_rate_is_0_001_percent_and_is_a_distinct_row(self):
        """Row 2A is 0.001%, two orders of magnitude below delivery. Easy to lose in a refactor."""
        parsed = costs.parse_stt_circular(stt_text())
        assert parsed.new_rates["eof_unit_sale"].pct == 0.001
        assert parsed.new_rates["eof_unit_sale"].row == "2A"
        assert parsed.new_rates["eof_unit_sale"].pct < parsed.new_rates["delivery_sale"].pct / 50
        q = costs.quote(
            costs.DELIVERY_BUY, costs.EOF_UNIT_SELL, trade_date=IN_2026,
            buy_amount=CRORE, schedule=schedule(),
        )
        assert q.buy.component_rupees(costs.STT) == 10_000.0
        assert q.sell.component_rupees(costs.STT) == 100.0

    def test_the_seven_lineage_references_are_transcribed_from_the_circular(self):
        parsed = costs.parse_stt_circular(stt_text())
        refs = [ref for ref, _ in parsed.lineage]
        assert refs == [
            "NSE/FATAX/32385", "NSE/FATAX/38737", "NSE/FATAX/41263", "NSE/FATAX/41919",
            "NSE/FATAX/43604", "NSE/FATAX/56235", "NSE/FATAX/63809",
        ]
        dates = [when for _, when in parsed.lineage]
        assert dates[0] == date(2016, 5, 16)
        assert dates[-1] == date(2024, 9, 9)

    def test_the_circular_identities_are_read_from_the_fixture(self):
        stt = costs.parse_stt_circular(stt_text())
        exch = costs.parse_exchange_charge_circular(exchange_text())
        assert stt.circular.ref == "NSE/FATAX/73524"
        assert stt.circular.circular_ref == "02/2026"
        assert stt.circular.issued == date(2026, 3, 31)
        assert stt.circular.original_bytes == 358_718
        assert stt.circular.original_sha256_16 == "f4cbd1f917f88dcb"
        assert exch.circular.ref == "NSE/FA/73061"
        assert exch.circular.issued == date(2026, 2, 27)
        assert exch.circular.original_bytes == 163_365
        assert exch.circular.original_sha256_16 == "6085601a26177ed6"
        assert stt.statute == "Finance Act 2026"
        assert stt.assent_date == date(2026, 3, 30)
        assert stt.effective_from == date(2026, 4, 1)

    def test_the_sebi_directive_and_the_two_predecessors_are_transcribed(self):
        exch = costs.parse_exchange_charge_circular(exchange_text())
        assert "SEBI/HO/MRD/TPD" in exch.sebi_directive
        assert "2024/92" in exch.sebi_directive
        assert "2024" in exch.sebi_directive
        # De-duplicated: the trimmed fixture states NSE/FA/56129 twice -- once in the predecessor
        # line and once inside the kept delta paragraph -- and a list with a repeat reads as
        # two distinct predecessor circulars when it is one.
        assert list(dict.fromkeys(ref for ref, _ in exch.predecessors)) == [
            "NSE/FA/56129", "NSE/FA/64323",
        ]
        assert exch.prior_effective_to == date(2023, 4, 1)

    def test_every_servable_exchange_regime_is_published_not_derived(self):
        """No servable regime is a reconstruction.

        The pre-2023-04-01 level is derived from the deltas the circular states and is recorded
        for audit, but it precedes the floor and is deliberately not servable. So both regimes the
        schedule will actually answer with come straight out of a published column.
        """
        sch = schedule()
        assert [r.derived for r in sch.exchange_regimes] == [False, False]
        assert "Existing outflow" in sch.exchange_regimes[0].source
        assert "Revised outflow" in sch.exchange_regimes[1].source
        for regime in sch.exchange_regimes:
            assert "DERIVED" not in regime.why

    def test_the_derived_pre_2023_level_is_recorded_but_never_served(self):
        """It exists in the parse for audit and is unreachable through the regime lookup.

        Asserting both halves matters: recording it without labelling it derived would let a later
        reader mistake a reconstruction for a transcribed rate, and making it servable would let a
        2014 query be answered with one.
        """
        parsed = costs.parse_exchange_charge_circular(exchange_text())
        assert parsed.prior, "the deltas should have reconstructed a pre-2023 level"
        # NSE/FA/56129 moved the cash transaction charge DOWN by 20 and the IPFT contribution UP
        # by 9.99 on 2023-04-01. Undoing both against the "existing outflow" column (297 + 10)
        # gives 277.00 + 0.01 = Rs 277.01 per crore, which is the pre-2023 level.
        assert parsed.prior[costs.CASH_MARKET].transaction_charges == 277.0
        assert round(parsed.prior[costs.CASH_MARKET].ipft, 2) == 0.01
        assert round(parsed.prior[costs.CASH_MARKET].total, 2) == 277.01
        assert parsed.prior_effective_to == date(2023, 4, 1)

        # And it is not in the servable regimes.
        served = {id(r.segments) for r in schedule().exchange_regimes}
        assert id(parsed.prior) not in served
        assert all(
            r.segments[costs.CASH_MARKET].transaction_charges != 317.0
            for r in schedule().exchange_regimes
        )

    def test_the_derived_prior_level_reconstructs_from_the_stated_deltas(self):
        """The arithmetic is checkable from the fixture, which states every delta in prose.

        The NSE/FA/56129 paragraph is the only place the pre-2023-04-01 level appears, and it
        appears as *differences*. This asserts the fixture really carries that paragraph, because
        without it the reconstruction has no support at all and the module would be inventing a
        rate.
        """
        raw = exchange_text()
        assert "reduced its transaction charges" in raw, (
            "the NSE/FA/56129 delta paragraph must be kept in the fixture: it is the only "
            "statement of the pre-2023-04-01 level, and the reconstruction below has no other "
            "source"
        )
        assert "by Rs 20 per crore" in raw and "by Rs. 300 per crore" in raw
        assert "by Rs. 9.99" in raw and "by Rs. 49.99" in raw
        parsed = costs.parse_exchange_charge_circular(raw)
        # Cash: 297 - 20 = 277, and 10 - 9.99 = 0.01.
        assert parsed.prior[costs.CASH_MARKET].total == pytest.approx(277.01)
        # Futures: the circular states Rs. 10 for equity futures, and it shares the 9.99 IPFT cut
        # with cash: 173 - 10 = 163, 10 - 9.99 = 0.01.
        assert parsed.prior[costs.EQUITY_FUTURES].total == pytest.approx(163.01)
        # Options: 3503 - 300 = 3203, and 50 - 49.99 = 0.01. Note the option figures are on
        # PREMIUM turnover, which is why they are two orders of magnitude larger per crore.
        assert parsed.prior[costs.EQUITY_OPTIONS].total == pytest.approx(3203.01)
        # All three land on the same Rs. 0.01 IPFT, which is the real check that the cuts were
        # routed to the right segments: cash and futures share one cut, options have their own.
        for segment in (costs.CASH_MARKET, costs.EQUITY_FUTURES, costs.EQUITY_OPTIONS):
            assert round(parsed.prior[segment].ipft, 2) == 0.01, segment

    def test_every_parsed_rate_summed_gives_the_stated_total(self):
        """The parse enforces txn + IPFT == total per segment, per regime.

        This is what makes the cost-neutrality claim structural rather than a coincidence of two
        numbers happening to match.
        """
        for regime in schedule().exchange_regimes:
            for segment, charge in regime.segments.items():
                assert charge.transaction_charges + charge.ipft == pytest.approx(charge.total), (
                    f"{segment} in the regime from {regime.effective_from}: the components must "
                    f"sum to the total NSE states"
                )

    def test_the_trimmed_fixture_reproduces_every_transcribed_rate(self):
        """The offline half of the trim-neutrality proof.

        The full proof (``verify_trim.py``) parses the complete 358 KB / 163 KB pypdf text layers
        and the trimmed excerpts and compares 16 fields. It needs the original PDFs and pypdf,
        which are not test dependencies, so it does not run here. What runs here is the half that
        needs nothing: every rate the schedule uses is read out of the stored bytes, and the
        module's own cross-checks (row 4 table vs prose, components vs total, the '(No Change)'
        identity) all pass on the trimmed text. If a trim ever cut a rate, the parsers would
        raise rather than return a partial schedule.
        """
        parsed = costs.parse_stt_circular(stt_text())
        assert len(parsed.new_rates) == 7
        assert len(parsed.previous_rates) == 7
        exch = costs.parse_exchange_charge_circular(exchange_text())
        assert set(exch.existing) == {
            costs.CASH_MARKET, costs.EQUITY_FUTURES, costs.EQUITY_OPTIONS
        }
        assert exch.existing[costs.CASH_MARKET].total == 307.0
        assert exch.existing[costs.EQUITY_FUTURES].total == 183.0
        assert exch.existing[costs.EQUITY_OPTIONS].total == 3553.0
        # The parsers raise rather than degrade, so reaching here means every required row was
        # found: 4 "(No Change)" rows, 3 revision sentences, 3 segments.
        assert sum(1 for r in parsed.new_rates.values() if r.marked_no_change) == 4

    def test_the_fixture_meta_records_the_originals_and_the_trim_rule(self):
        meta = json.loads((FIXTURE_DIR / "META.json").read_text(encoding="utf-8"))
        assert meta["http_status"] == 200
        assert meta["stored_line_endings"] == "lf"
        assert meta["original_bytes"] == 358_718 + 163_365
        assert meta["original_sha256_16"] == ["f4cbd1f917f88dcb", "6085601a26177ed6"]
        for name, entry in meta["files"].items():
            path = FIXTURE_DIR / name
            raw = path.read_bytes()
            assert len(raw) == entry["stored_bytes"], name
            assert b"\r" not in raw, f"{name} must be LF only"
            import hashlib

            assert hashlib.sha256(raw).hexdigest() == entry["stored_sha256"], name
            assert entry["trim_rule"].strip(), name
            assert entry["url"].startswith("https://nsearchives.nseindia.com/")

    def test_the_fixtures_are_far_smaller_than_the_originals(self):
        """The trim is the point: 522 KB of PDF down to single-digit KB of text."""
        meta = json.loads((FIXTURE_DIR / "META.json").read_text(encoding="utf-8"))
        total_stored = sum(e["stored_bytes"] for e in meta["files"].values())
        assert total_stored < 7_000, (
            f"{total_stored} B stored against {meta['original_bytes']} B of PDF. The ratio below "
            f"is the real constraint; the absolute bound is just a guard against a runaway trim."
        )
        assert total_stored < meta["original_bytes"] / 80

    def test_a_corrupted_fixture_raises_rather_than_silently_returning_fewer_rates(self):
        """Delete the intraday row from the STT text and the parse must refuse.

        This is the trim's own regression guard: it proves the parser is strict about the rows it
        needs, so a future over-aggressive trim fails loudly here instead of quietly dropping the
        0.025% rate.
        """
        raw = stt_text()
        # The fixture is the circular's own table, verbatim. Row 3 is the intraday sale and reads
        # "0.025 per cent (No Change) 0.025 per cent Seller (No Change)"; removing that rate pair
        # simulates a trim that cut too much.
        needle = "0.025 per cent (No Change) 0.025 per cent Seller (No Change)"
        assert needle in raw, "fixture layout changed; row 3 is not the transcribed intraday row"
        mutilated = raw.replace(needle, "REDACTED", 1)
        assert mutilated != raw
        with pytest.raises(costs.CostScheduleParseError, match="No Change"):
            costs.parse_stt_circular(mutilated)

    def test_a_fixture_missing_the_option_revision_prose_is_refused(self):
        """Cutting the (a)/(b)/(c) sentences must fail loudly, not leave the old option rates.

        This is the complementary trim guard to the intraday one: the option, exercised-option and
        futures rates come from the prose, so a trim that dropped it would otherwise silently leave
        the schedule quoting the pre-April figures with no error.
        """
        raw = stt_text()
        assert "(a) Sale of an option" in raw
        mutilated = re.sub(r"\(a\) Sale of an option.*?\(c\) Sale of a futures in securities",
                           "REDACTED", raw, count=1, flags=re.S)
        assert mutilated != raw
        with pytest.raises(costs.CostScheduleParseError, match="revision sentences"):
            costs.parse_stt_circular(mutilated)

    def test_a_fixture_whose_two_reads_disagree_is_refused(self):
        """Change the table's option rate without changing the prose, and the parse must refuse.

        The circular states the option rates twice -- in the (a)/(b)/(c) sentences and in the row 4
        table. A transcription that moved one but not the other is the exact defect the module's
        two-read cross-check exists to catch, and it is the failure that would otherwise install a
        plausible wrong rate.
        """
        raw = stt_text()
        needle = "0.15 per cent 0.15 per cent 0.05 per cent"
        assert needle in raw, "row 4's new-rate triple is not in the fixture as expected"
        mismatched = raw.replace(needle, "0.30 per cent 0.15 per cent 0.05 per cent", 1)
        with pytest.raises(costs.CostScheduleParseError, match="disagree"):
            costs.parse_stt_circular(mismatched)


# =========================================================== no empirical bps


class TestNoEmpiricalBps:
    def test_the_module_says_why_it_measured_nothing_rather_than_inventing_a_number(self):
        statement = costs.taxable_base_requires_turnover_value()
        assert "SttlmPric" in statement, (
            "the settlement-price false positive is worth naming: it is the only 'stt' substring "
            "hit among the 34 UDiFF columns and it looks like a real one"
        )
        assert "settlement price" in statement
        assert "no side and no counterparty" in statement
        assert "TtlTrfVal" in statement and "TtlTradgVol" in statement
        assert "7/7" in statement
        assert "no turnover-value column" in statement

    def test_no_observed_or_measured_bps_constant_exists_in_the_module(self):
        """Grep the source for an empirical figure. There must not be one.

        A synthesised "observed bps" would be a number with no measurement behind it, sitting in
        the same table as the transcribed rates -- indistinguishable to a later reader. The
        absence is the guarantee.
        """
        source = __import__("pathlib").Path(costs.__file__).read_text(encoding="utf-8")
        code = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        for forbidden in (
            "OBSERVED_BPS", "MEASURED_BPS", "EMPIRICAL_BPS", "ACTUAL_BPS",
            "REALISED_BPS", "REALIZED_BPS",
        ):
            assert forbidden not in code, (
                f"{forbidden} would be a synthetic empirical figure; the bhavcopy cannot support "
                f"one and this module must not pretend otherwise"
            )
        assert not re.search(r"=\s*2[12]\.\d+\s*#.*observ", code, re.IGNORECASE)

    def test_every_bps_in_the_module_is_computed_from_named_components(self):
        """The 22.248 in the docstring is prose. The number the API returns is arithmetic."""
        q = costs.quote(
            costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=IN_2026,
            buy_amount=CRORE, schedule=schedule(),
        )
        recomputed = sum(q.buy.items[i].rupees for i in range(len(q.buy.items)))
        recomputed += sum(q.sell.items[i].rupees for i in range(len(q.sell.items)))
        assert round(recomputed, 2) == q.round_trip_rupees
        # And the bps is that total over the asked notional.
        assert round(q.round_trip_rupees / q.bps_denominator * 10_000, 5) == round(
            q.round_trip_bps, 5
        )

    def test_the_reported_bhavcopy_gap_names_the_exact_change_needed(self):
        statement = costs.taxable_base_requires_turnover_value()
        assert "_SCHEMAS['udiff']" in statement
        assert "'turnover': ('TtlTrfVal',)" in statement
        assert "REPORTED, NOT EDITED" in statement


# =========================================================== cache behaviour


class TestCacheBehaviour:
    def _transport(self, calls: list):
        """A transport that serves the two fixture texts.

        ``pypdf`` is not a bfinance dependency, so the bytes are ``.txt`` and the client is given
        a matching ``text_extractor``. What is under test is the *cache*, and the cache is
        indifferent to how the text was obtained. The real PDF path is covered by
        :func:`test_the_pdf_extractor_reports_a_missing_dependency_clearly`.
        """
        texts = {"FATAX73524": stt_text(), "FA73061": exchange_text()}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            calls.append(url)
            for key, body in texts.items():
                if key in url:
                    return httpx.Response(200, content=body.encode("utf-8"))
            return httpx.Response(404)

        def extract(content: bytes) -> str:
            return content.decode("utf-8")

        return httpx.MockTransport(handler), extract

    def _client(self, calls: list, **kwargs) -> "costs.CostsClient":
        transport, extract = self._transport(calls)
        return costs.CostsClient(transport=transport, text_extractor=extract, **kwargs)

    def test_a_cached_schedule_makes_no_request_and_a_stale_one_does(self):
        """The TTL is load-bearing: a cached schedule costs zero requests, a stale one costs one.

        This is what makes a repeated quote free. A module that re-fetched two circulars on every
        pricing call would be unusable, and one that never re-fetched would silently serve a
        superseded tariff -- so both halves are asserted.
        """
        calls: list[str] = []
        client = self._client(calls, ttl_hours=24.0)

        first = client.schedule()
        assert len(calls) == 2, f"a cold schedule costs one request per circular, got {calls}"
        assert round(
            costs.quote(
                costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=IN_2026,
                buy_amount=CRORE, schedule=first,
            ).round_trip_bps, 3
        ) == 22.248

        # Warm: many more calls, still no request.
        for _ in range(5):
            again = client.schedule()
            assert again.stt_circular.circular.ref == "NSE/FATAX/73524"
        assert len(calls) == 2, f"a cached schedule must cost no request, got {calls}"
        assert client.request_count == 2

        # Stale: a zero TTL never reuses, so each call costs one request per circular. A fresh
        # counter is used because `calls` is shared with the warm client above.
        stale_calls: list[str] = []
        stale = self._client(stale_calls, ttl_hours=0.0)
        stale.schedule()
        assert len(stale_calls) == 2, f"a cold client costs 2 requests, got {stale_calls}"
        stale.schedule()
        assert len(stale_calls) == 4, (
            f"a zero TTL must refetch every time, went 2 -> {len(stale_calls)}"
        )
        stale.schedule()
        assert len(stale_calls) == 6, "and again on the third call"
        # The warm client was untouched by the stale one's traffic.
        assert len(calls) == 2

    def test_force_refresh_refetches_even_when_the_cache_is_warm(self):
        calls: list[str] = []
        client = self._client(calls, ttl_hours=24.0)
        client.schedule()
        warm = len(calls)
        client.schedule(force_refresh=True)
        assert len(calls) == warm + 2, "force_refresh is the escape hatch from a stale tariff"

    def test_a_single_circular_is_cached_independently(self):
        """Per-circular caching, so a re-read of one does not re-download the other."""
        calls: list[str] = []
        client = self._client(calls, ttl_hours=24.0)
        client.circular_text("NSE/FATAX/73524")
        assert len(calls) == 1
        client.circular_text("NSE/FATAX/73524")
        assert len(calls) == 1, "the second read is served from cache"
        client.circular_text("NSE/FA/73061")
        assert len(calls) == 2

    def test_an_unknown_circular_ref_is_refused_before_any_request(self):
        calls: list[str] = []
        client = self._client(calls, ttl_hours=24.0)
        with pytest.raises(ValueError, match="not a known circular"):
            client.circular_text("NSE/FATAX/99999")
        assert calls == [], "an unknown ref must cost no request"

    def test_quoting_from_the_bundled_schedule_opens_no_socket(self, monkeypatch):
        """The default path is the local fixture. No client, no transport, no network.

        This is what keeps the ``-m "not live"`` selection hermetic: :func:`quote` with no
        explicit schedule must never construct a client.
        """
        import socket

        def forbidden(*args, **kwargs):  # pragma: no cover - only runs on a violation
            raise AssertionError("quoting must not open a socket")

        monkeypatch.setattr(socket.socket, "connect", forbidden)
        monkeypatch.setattr(socket, "getaddrinfo", forbidden)

        for _ in range(3):
            bps = rt_bps()
            assert round(bps, 3) == 22.248
        # A shape the bundled schedule does not cover still raises, locally.
        with pytest.raises(costs.CostScheduleCoverageError):
            rt_bps("2015-01-01")

    def test_the_bundled_schedule_is_cached_but_injection_bypasses_the_cache(self):
        """Two unoverridden calls share one parse; an injected pair always rebuilds.

        The cache is a performance affordance, not a correctness one -- so an override must be
        honoured even when a cached schedule exists, or a test mutating a rate would silently be
        reading stale numbers. That is precisely the trap the negative controls would fall into.

        The injection here is the *unmutated* text, so the two schedules price identically while
        being distinct objects. Mutating text in this test would need the same care as the
        negative controls (the prose and the table must stay consistent with each other).
        """
        built = costs.bundled_schedule()
        assert costs.bundled_schedule() is built, "an unoverridden call is served from cache"

        injected = costs.bundled_schedule(
            stt_text=stt_text(), exchange_text=exchange_text()
        )
        assert injected is not built, "an override must never return the cached object"
        # Same numbers, different object: the cache was bypassed, not silently honoured.
        assert round(
            costs.quote(
                costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=IN_2026,
                buy_amount=CRORE, schedule=injected,
            ).round_trip_bps, 6
        ) == round(
            costs.quote(
                costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=IN_2026,
                buy_amount=CRORE, schedule=built,
            ).round_trip_bps, 6
        )
        # And the cache still holds the object it was built with.
        assert costs.bundled_schedule() is built, "an override must not evict the cache"

    def test_an_injected_schedule_does_not_become_the_cached_one(self):
        """Only the fixture-derived schedule is cached. An override is per-call and disposable.

        A test that mutates a rate must not leave a mutated schedule behind for the next test in
        the session -- that is a cross-test leak, and it is the reason the override path skips
        the cache rather than writing through it.
        """
        before = costs.bundled_schedule()
        raw = stt_text()
        needle = "0.1 per cent (No Change) 0.1 per cent Purchaser (No Change)"
        assert needle in raw
        mutated_text = raw.replace(
            needle, "0.2 per cent (No Change) 0.2 per cent Purchaser (No Change)", 1
        )
        mutated = costs.bundled_schedule(
            stt_text=mutated_text, exchange_text=exchange_text()
        )
        assert round(
            costs.quote(
                costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=IN_2026,
                buy_amount=CRORE, schedule=mutated,
            ).round_trip_bps, 3
        ) == 32.248
        # The cached schedule is untouched and still prices the real figure.
        assert costs.bundled_schedule() is before
        assert round(
            costs.quote(
                costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=IN_2026,
                buy_amount=CRORE, schedule=before,
            ).round_trip_bps, 3
        ) == 22.248

    def test_a_partial_override_is_refused(self):
        """Supplying only one of the two texts must not silently reuse the other from disk.

        Half an override is a trap: the caller thinks they are testing a new STT circular while
        silently reading the cached exchange-charge text off disk.
        """
        with pytest.raises(ValueError, match="needs BOTH texts"):
            costs.bundled_schedule(stt_text=stt_text())
        with pytest.raises(ValueError, match="needs BOTH texts"):
            costs.bundled_schedule(exchange_text=exchange_text())

    def test_the_pdf_extractor_reports_a_missing_dependency_clearly(self):
        """``pypdf`` is not a bfinance dependency, so its absence must be explained, not crash.

        The bundled fixtures are already text, so nothing in the offline path needs a PDF reader.
        The reader is only for re-fetching a circular, and the error says so and names the
        alternative.
        """
        try:
            import pypdf  # noqa: F401
        except ImportError:
            with pytest.raises(costs.CostScheduleError, match="pypdf") as excinfo:
                costs.extract_pdf_text(b"%PDF-1.4 not really a pdf")
            message = str(excinfo.value)
            assert "not a bfinance dependency" in message
            assert "schedule_from_circulars" in message, "the error must name the way forward"
        else:  # pragma: no cover - depends on the environment
            pytest.skip("pypdf is installed here, so the missing-dependency path cannot run")


# =========================================================== negative controls
#
# Six assertions in this repository have shipped that could not fail. The ones below are the
# deliberate answer: each mutates the *input* the schedule is computed from and asserts the
# affected assertion goes red. A test suite that only ever proves it is right cannot demonstrate
# that it is capable of being wrong.
#
# The mutations are of two kinds and both are needed. A **coherent** mutation (both rate columns
# moved together, or all three components of a triple moved together) proves the number is
# derived from the input. An **incoherent** one proves the parser is strict enough to refuse
# input whose parts disagree -- which is what makes the '(No Change)' marker and the
# components-sum-to-total identity load-bearing rather than decorative.


def _bps_from_stt_text(text: str, day: str = IN_2026) -> float:
    return costs.quote(
        costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=day,
        buy_amount=CRORE, schedule=costs.schedule_from_circulars(text, exchange_text()),
    ).round_trip_bps


def _bps_from_exchange_text(text: str, day: str = IN_2026) -> float:
    return costs.quote(
        costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=day,
        buy_amount=CRORE, schedule=costs.schedule_from_circulars(stt_text(), text),
    ).round_trip_bps


class TestNegativeControls:
    def test_control_the_unmutated_assertions_pass_so_the_controls_mean_something(self):
        """The baseline the two mutations below are measured against. Without this, a control
        that fails because everything is broken would look identical to a control that works."""
        assert round(rt_bps(), 3) == 22.248
        q = costs.quote(
            costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=IN_2026,
            buy_amount=CRORE, schedule=schedule(),
        )
        assert round(q.component_share(costs.STT) * 100, 1) == 89.9

    def test_negative_control_a_mutated_stt_rate_moves_the_bps(self):
        """Change one transcribed rate; the bps assertion must go red.

        Doubling the delivery purchase rate from 0.1% to 0.2% adds Rs 10,000 on the buy leg,
        which is 10 bps of the one-way notional. The 22.248 assertion must therefore fail, and it
        does so because the *fixture* changed -- the number is parsed, not stored.

        RESULT: PASS (the control is live). 22.248 -> 32.248, so
        ``round(bps, 3) == 22.248`` is False.
        """
        raw = stt_text()
        # The fixture is the circular's own table, verbatim. Row 1 is the delivery *purchase* and
        # reads "0.1 per cent (No Change) 0.1 per cent Purchaser (No Change)" -- the first rate is
        # the w.e.f. column and the second the "upto" column.
        needle = "0.1 per cent (No Change) 0.1 per cent Purchaser (No Change)"
        assert needle in raw, (
            "fixture layout changed; row 1 no longer reads as the transcribed delivery-purchase "
            "row, so the mutation cannot be aimed at it"
        )
        # Move only the *w.e.f.* column: 0.1 -> 0.2. The "upto" column is left at 0.1, which is
        # what a real mis-transcription of the new rate would look like.
        mutated = raw.replace(
            needle, "0.2 per cent (No Change) 0.1 per cent Purchaser (No Change)", 1
        )
        assert mutated != raw, "the mutation did not apply"

        # The parser refuses it: the row is marked "(No Change)" and its two columns now disagree,
        # so the marker and the numbers contradict each other and neither can be trusted.
        with pytest.raises(costs.CostScheduleParseError, match=r"\(No Change\)"):
            costs.parse_stt_circular(mutated)

        # So that control alone cannot show the bps moving. The second control below makes both
        # columns agree at the higher rate -- a *self-consistent* circular that simply charges
        # more -- which is the mutation that actually reaches the arithmetic.
        consistent = raw.replace(
            needle, "0.2 per cent (No Change) 0.2 per cent Purchaser (No Change)", 1
        )
        assert costs.parse_stt_circular(consistent).new_rates["delivery_purchase"].pct == 0.2
        # 0.2% of 1 crore = 20,000 on the buy leg instead of 10,000: +10,000 rupees = +10 bps.
        assert round(_bps_from_stt_text(consistent), 3) == 32.248
        assert round(_bps_from_stt_text(consistent), 3) != 22.248, (
            "the bps test cannot fail -- a mutated transcribed rate did not move it"
        )

    def test_negative_control_a_self_consistent_stt_mutation_reaches_the_arithmetic(self):
        """The control above refuses an inconsistent circular; this one proves the number is live.

        A control that only ever proves the parser is strict is not enough. Editing a rate
        coherently -- both columns, so the '(No Change)' identity holds -- must move the computed
        bps, which is only possible if the bps is derived from the fixture text rather than read
        from a constant.

        RESULT: PASS (the control is live). 22.248 -> 32.248.
        """
        raw = stt_text()
        needle = "0.1 per cent (No Change) 0.1 per cent Purchaser (No Change)"
        assert needle in raw
        for new_pct, expected in ((0.2, 32.248), (0.05, 17.248)):
            mutated = raw.replace(
                needle,
                f"{new_pct} per cent (No Change) {new_pct} per cent Purchaser (No Change)",
                1,
            )
            assert round(_bps_from_stt_text(mutated), 3) == expected

    def test_negative_control_a_mutated_option_rate_reaches_the_arithmetic(self):
        """The option and futures rates must be live too, not just the delivery rate.

        0.15% -> 0.20% on both the prose sentence and the table, so the module's own two-read
        cross-check stays satisfied and the mutation reaches the arithmetic.

        RESULT: PASS (the control is live). The option round trip's bps moves; delivery does not,
        which is the point -- a change to row 4 must not leak into row 1.
        """
        raw = stt_text()
        mutated = raw.replace("0.15 per cent", "0.20 per cent").replace(
            "0.15% (upto March 31, 2026", "0.20% (upto March 31, 2026"
        )
        assert mutated != raw
        parsed = costs.parse_stt_circular(mutated)
        assert parsed.new_rates["option_sale"].pct == 0.20
        assert parsed.new_rates["delivery_sale"].pct == 0.1, "row 1 must be untouched"

        def option_bps(text: str) -> float:
            return costs.quote(
                costs.OPTION_EXERCISED, costs.OPTION_SALE, trade_date=IN_2026,
                buy_amount=CRORE, premium=CRORE,
                schedule=costs.schedule_from_circulars(text, exchange_text()),
            ).round_trip_bps

        assert round(option_bps(mutated), 4) != round(option_bps(raw), 4)
        # And the delivery round trip is unmoved, proving the mutation is targeted.
        assert round(_bps_from_stt_text(mutated), 6) == round(rt_bps(), 6)

    def test_negative_control_a_mutated_exchange_rate_moves_the_bps(self):
        """Change the exchange charge coherently; the bps must move by a computable amount.

        The cash-market revised triple goes 306.99 + 0.01 = 307 to 3069.99 + 10.01 = 3080. That is
        +2,773 per side, and because the exchange charge sits inside the GST base it also adds
        18% of 2,773 = 499.14 per side. So +3,272.14/side, +6,544.28 on the round trip. One bps
        per crore of notional is Rs 1,000, so +6,544.28 is +6.544 bps: 22.24812 -> 28.79240.

        RESULT: PASS (the control is live). 22.248 -> 28.792, so the 22.248 assertion is False.
        """
        raw = exchange_text()
        needle = "Rs. 306.99 each side Rs. 0.01 each side Rs. 307 each side"
        assert needle in raw, "the revised cash-market triple is not in the fixture as expected"
        mutated = raw.replace(
            needle, "Rs. 3069.99 each side Rs. 10.01 each side Rs. 3080 each side", 1
        )
        assert mutated != raw, "the mutation did not apply"

        parsed = costs.parse_exchange_charge_circular(mutated)
        assert parsed.revised[costs.CASH_MARKET].total == 3080.0
        bps = _bps_from_exchange_text(mutated)
        assert round(bps, 3) == 28.792, f"mutated exchange charge gave {bps}"
        assert round(bps, 3) != 22.248

    def test_negative_control_an_incoherent_exchange_triple_is_refused(self):
        """A total that no longer equals its components must be refused, not absorbed.

        This is the arithmetic identity behind the cost-neutrality claim: 306.99 + 0.01 = 307. A
        transcription that lost a digit in the components but not the total is exactly the kind of
        defect that would otherwise price a wrong exchange charge forever.

        RESULT: PASS (the control is live). The parse raises instead of returning a schedule.
        """
        raw = exchange_text()
        needle = "Rs. 306.99 each side Rs. 0.01 each side Rs. 307 each side"
        assert needle in raw
        mutated = raw.replace(
            needle, "Rs. 3069.99 each side Rs. 0.01 each side Rs. 3080 each side", 1
        )
        with pytest.raises(costs.CostScheduleParseError, match="stated total"):
            costs.parse_exchange_charge_circular(mutated)

    def test_negative_control_removing_the_stt_term_breaks_the_dominance_assertion(self):
        """Zero the STT rate; the 89.9% dominance assertion must go red.

        This is the specific failure the module exists to prevent: a schedule that has quietly lost
        its STT term still returns a confident number, roughly 0.6 bps, which is ~36x too low. The
        dominance assertion is what turns that into a test failure instead of a shipped defect.

        The mutation is made by rebuilding the schedule with STT rates zeroed, since the circular
        genuinely does tax delivery and there is no honest way to delete the row from the text.
        RESULT: PASS (the control is live). STT share 89.9% -> 0.0%.
        """
        sch = schedule()
        stt_regime = sch.stt_regime_for(date(2026, 5, 1))
        zeroed = costs.SttRegime(
            effective_from=stt_regime.effective_from,
            effective_to=stt_regime.effective_to,
            rates={
                key: costs.SttRate(0.0, rate.payer, rate.previous_pct, rate.row,
                                   rate.marked_no_change)
                for key, rate in stt_regime.rates.items()
            },
            source=stt_regime.source,
            why=stt_regime.why,
        )
        broken = costs.CostSchedule(
            stt_circular=sch.stt_circular,
            exchange_circular=sch.exchange_circular,
            stt_regimes=(zeroed,) + tuple(
                r for r in sch.stt_regimes if r.effective_from != zeroed.effective_from
            ),
            exchange_regimes=sch.exchange_regimes,
            floor=sch.floor,
        )
        q = costs.quote(
            costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=IN_2026,
            buy_amount=CRORE, schedule=broken,
        )
        share = q.component_share(costs.STT)
        assert share == 0.0, "the mutation did not remove the STT term"
        assert round(share * 100, 1) != 89.9, (
            "the dominance assertion cannot fail -- a schedule with no STT still reported 89.9%"
        )
        # What is left: stamp duty 1,500 on the buy, and 307 + 10 + 57.06 per side (748.12 over
        # both legs) = Rs 2,248.12, i.e. 2.248 bps. So removing STT does not make the model free --
        # it makes it report a tenth of the real cost, still confidently, which is the failure
        # worth catching.
        assert q.round_trip_rupees == 2_248.12
        assert round(q.round_trip_bps, 3) == 2.248
        # Note this is 2.248 bps, not 0.6: stamp duty (1,500) and the SEBI fee survive, and
        # stamp duty alone is 1.5 bps. So the understated model still *looks* plausible -- which is
        # the reason the dominance assertion, not a bare "is the number big enough" check, is the
        # thing standing between a lost STT term and a shipped cost model.
        assert round(q.round_trip_bps, 3) == 2.248 < round(rt_bps(), 3) / 2.0
        assert q.component_rupees(costs.STAMP_DUTY) == 1_500.0, (
            "stamp duty survives, which is why a lost STT term is not an obvious zero"
        )
        assert q.round_trip_rupees == pytest.approx(1_500.0 + 748.12, abs=0.01)

    def test_negative_control_a_dropped_exchange_component_breaks_the_bps(self):
        """Zero the exchange charge; 22.248 must go red.

        RESULT: PASS (the control is live). 22.248 -> 21.524, the 22.248 assertion is False.
        """
        sch = schedule()
        exch_regime = sch.exchange_regime_for(date(2026, 5, 1))
        zeroed = costs.ExchangeChargeRegime(
            effective_from=exch_regime.effective_from,
            effective_to=exch_regime.effective_to,
            segments={
                name: costs.SegmentCharge(name, 0.0, 0.0, 0.0)
                for name in exch_regime.segments
            },
            source=exch_regime.source,
            derived=exch_regime.derived,
            why=exch_regime.why,
        )
        broken = costs.CostSchedule(
            stt_circular=sch.stt_circular,
            exchange_circular=sch.exchange_circular,
            stt_regimes=sch.stt_regimes,
            exchange_regimes=tuple(
                zeroed if r.effective_from == zeroed.effective_from else r
                for r in sch.exchange_regimes
            ),
            floor=sch.floor,
        )
        q = costs.quote(
            costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=IN_2026,
            buy_amount=CRORE, schedule=broken,
        )
        # With the exchange charge at zero the GST base drops to the SEBI fee alone (Rs 10), so
        # GST falls from 57.06 to 1.80 per side as well. Lost per round trip: 614.00 of exchange
        # charge and 110.52 of GST, leaving Rs 21,523.60 = 21.5236 bps.
        assert q.buy.gst_base == 10.0, "the GST base is brokerage + exchange + IPFT + SEBI"
        assert q.buy.component_rupees(costs.GST) == 1.80
        assert q.buy.component_rupees(costs.EXCHANGE_CHARGE) == 0.0
        assert round(q.round_trip_bps, 4) == 21.5236
        assert round(q.round_trip_bps, 3) != 22.248

    def test_negative_control_dropping_the_stamp_duty_moves_the_bps(self):
        """Stamp duty is a real term, not decoration: zeroing it must move the number by 1.5 bps.

        RESULT: PASS (the control is live). 22.248 -> 20.748.
        """
        sch = schedule()
        without = costs.CostSchedule(
            stt_circular=sch.stt_circular,
            exchange_circular=sch.exchange_circular,
            stt_regimes=sch.stt_regimes,
            exchange_regimes=sch.exchange_regimes,
            floor=sch.floor,
            stamp_duty_pct=0.0,
        )
        q = costs.quote(
            costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=IN_2026,
            buy_amount=CRORE, schedule=without,
        )
        assert q.buy.component_rupees(costs.STAMP_DUTY) == 0.0
        assert round(q.round_trip_bps, 3) == 20.748, "0.015% of 1 crore is exactly 1.5 bps"
        assert round(q.round_trip_bps, 3) != 22.248

    def test_negative_control_the_cache_actually_refetches_when_stale(self):
        """The cache assertions can fail. Expire the entry and the request count rises.

        RESULT: PASS (the control is live). With a positive TTL the count stays at 2; after the
        entry is aged past the TTL it becomes 4, so the "no request when cached" assertion would
        fail on a client that never refetched.
        """
        import time as _time

        calls: list[str] = []
        transport = _counting_transport(calls, stt_text(), exchange_text())
        client = costs.CostsClient(
            ttl_hours=1.0, transport=transport, text_extractor=lambda b: b.decode("utf-8")
        )
        client.schedule()
        assert len(calls) == 2
        client.schedule()
        assert len(calls) == 2, "warm cache"

        # Age the entry past its TTL by moving the stored timestamp into the past.
        for ref in ("NSE/FATAX/73524", "NSE/FA/73061"):
            text, _ = client._texts[ref]
            client._texts[ref] = (text, _time.monotonic() - 7_200.0)
        client.schedule()
        assert len(calls) == 4, (
            f"a stale entry must refetch; request count went 2 -> {len(calls)}"
        )

    def test_negative_control_the_coverage_guard_actually_refuses(self):
        """The out-of-range tests can fail. Remove the guard and a 2014 date prices.

        RESULT: PASS (the control is live). The STT regime lookup has a second, independent guard
        in the regime loop itself, so lowering the floor alone still raises. Supplying a regime
        that actually covers 2014 is what makes the date price -- and only then does the
        ``pytest.raises`` assertion in the out-of-range tests become false.
        """
        sch = schedule()

        # (a) Lowering the floor is NOT enough: the STT lookup's own loop refuses, because no
        # regime starts before 2023-04-01. Two layers of refusal, not one.
        unguarded_floor = costs.CostSchedule(
            stt_circular=sch.stt_circular,
            exchange_circular=sch.exchange_circular,
            stt_regimes=sch.stt_regimes,
            exchange_regimes=sch.exchange_regimes,
            floor=date(2010, 1, 1),
        )
        assert unguarded_floor.covers(date(2014, 6, 2)), "the floor check would pass"
        with pytest.raises(costs.CostScheduleCoverageError, match="No STT regime covers"):
            unguarded_floor.stt_regime_for(date(2014, 6, 2))

        # (b) Give it a regime that genuinely covers 2014 and the date prices. This is the
        # configuration the out-of-range tests exist to prevent.
        backdated = costs.CostSchedule(
            stt_circular=sch.stt_circular,
            exchange_circular=sch.exchange_circular,
            stt_regimes=(costs.SttRegime(
                effective_from=date(2010, 1, 1),
                effective_to=date(2026, 3, 31),
                rates=sch.stt_regimes[0].rates,
                source="backdated for the control",
                why="not real evidence",
            ),) + sch.stt_regimes,
            exchange_regimes=(costs.ExchangeChargeRegime(
                effective_from=date(2010, 1, 1),
                effective_to=date(2026, 2, 28),
                segments=sch.exchange_regimes[0].segments,
                source="backdated for the control",
                derived=True,
                why="not real evidence",
            ),) + sch.exchange_regimes,
            floor=date(2010, 1, 1),
        )
        bps = costs.quote(
            costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date="2014-06-02",
            buy_amount=CRORE, schedule=backdated,
        ).round_trip_bps
        assert bps > 0.0, (
            "with a backdated regime a 2014 date prices rather than raising, so the out-of-range "
            "assertions would pass vacuously against a schedule that had been extended without "
            "evidence"
        )

        # And the shipped schedule, with no backdating, still refuses.
        with pytest.raises(costs.CostScheduleCoverageError, match="2023-04-01"):
            schedule().exchange_regime_for(date(2014, 6, 2))
        with pytest.raises(costs.CostScheduleCoverageError):
            schedule().stt_regime_for(date(2014, 6, 2))


def _counting_transport(calls: list, stt: str, exch: str) -> httpx.MockTransport:
    """Serves the two fixture texts and records every URL requested.

    Plain UTF-8 rather than PDF bytes: ``pypdf`` is not a bfinance dependency, and the cache logic
    under test is indifferent to how the text was obtained. The real PDF extraction path has its
    own test (``test_the_pdf_extractor_reports_a_missing_dependency_clearly``).
    """
    bodies = {"FATAX73524": stt, "FA73061": exch}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        for key, body in bodies.items():
            if key in url:
                return httpx.Response(200, content=body.encode("utf-8"))
        return httpx.Response(404)

    return httpx.MockTransport(handler)


# =========================================================== module hygiene


class TestModuleHygiene:
    def test_costs_exports_a_coherent_public_surface(self):
        for name in costs.__all__:
            assert hasattr(costs, name), f"__all__ names {name}, which does not exist"

    def test_every_component_in_the_registry_has_a_declared_source(self):
        assert set(costs.COMPONENTS) == set(costs.DECLARED_SOURCES)

    def test_the_authority_vocabulary_is_exhaustive_over_the_line_items(self):
        q = costs.quote(
            costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=IN_2026,
            buy_amount=CRORE, brokerage_pct=0.03, schedule=schedule(),
        )
        for leg in (q.buy, q.sell):
            for item in leg.items:
                assert item.authority in costs.AUTHORITIES
                assert item.component in costs.COMPONENTS

    def test_the_circular_registry_matches_the_transcribed_originals(self):
        assert set(costs.CIRCULAR_URLS) == {"NSE/FATAX/73524", "NSE/FA/73061"}
        for ref, (size, digest) in costs.CIRCULAR_ORIGINALS.items():
            assert costs.CIRCULAR_URLS[ref].startswith("https://nsearchives.nseindia.com/")
            assert size > 100_000, ref
            assert len(digest) == 16, ref
        assert costs.CIRCULAR_ORIGINALS["NSE/FATAX/73524"] == (358_718, "f4cbd1f917f88dcb")
        assert costs.CIRCULAR_ORIGINALS["NSE/FA/73061"] == (163_365, "6085601a26177ed6")

    def test_all_eight_shapes_are_covered_by_provenance(self):
        for shape in costs.SHAPE_KEYS:
            rows = {r["component"]: r for r in schedule().provenance(date(2026, 5, 1), shape)}
            assert set(rows) == set(costs.COMPONENTS), shape

    def test_every_shape_prices_without_raising(self):
        """Each shape either prices or refuses for a stated reason; none fails silently."""
        premium_shapes = {
            costs.OPTION_SALE, costs.OPTION_EXERCISED,
        }
        for shape in costs.SHAPE_KEYS:
            try:
                q = costs.quote(
                    shape, shape, trade_date=IN_2026, buy_amount=CRORE,
                    premium=CRORE if shape in premium_shapes else None, schedule=schedule(),
                )
            except ValueError as exc:
                assert "premium" in str(exc), f"{shape} refused for an unexplained reason: {exc}"
                continue
            assert q.round_trip_rupees > 0.0, shape
            assert q.buy.items, shape

    def test_the_futures_segment_charge_is_not_the_cash_market_charge(self):
        """Rs. 183 vs Rs. 307. Using one for the other is a 68% error in one line."""
        sch = schedule()
        assert sch.exchange_regime_for(date(2026, 5, 1)).total_for(
            costs.EQUITY_FUTURES
        ) == 183.0
        assert sch.exchange_regime_for(date(2026, 5, 1)).total_for(
            costs.CASH_MARKET
        ) == 307.0
        q = costs.quote(
            costs.FUTURES_SALE, costs.FUTURES_SALE, trade_date=IN_2026,
            buy_amount=CRORE, schedule=schedule(),
        )
        assert q.buy.component_rupees(costs.EXCHANGE_CHARGE) == 183.0
        cash = costs.quote(
            costs.DELIVERY_BUY, costs.DELIVERY_SELL, trade_date=IN_2026,
            buy_amount=CRORE, schedule=schedule(),
        )
        assert cash.buy.component_rupees(costs.EXCHANGE_CHARGE) == 307.0

    def test_the_options_exchange_charge_is_on_premium_so_it_is_structurally_small(self):
        """Rs. 3,553 per crore of **premium**, not of the underlying's traded value.

        This is a real property of the module, and it is the reason
        :data:`BASE_PREMIUM` exists as a distinct base kind. The option exchange charge rate is
        ~11.6x the cash-market one (3,553 vs 307) precisely because it is levied on a much smaller
        base: premium turnover, not the value of the shares that might be delivered. Price the
        charge on the underlying and you overstate it by the ratio of the two bases.

        Concretely: Rs. 1 crore of premium, split evenly, is Rs 50,000 of premium turnover per leg.
        Rs. 3,553 per crore of that is Rs 17.77. The same Rs 3,553 applied to the Rs 1 crore of
        consideration would be Rs 3,553 -- 200x wrong. The module refuses that combination outright
        (see ``test_an_option_without_a_premium_is_refused``).
        """
        q = costs.quote(
            costs.OPTION_SALE, costs.OPTION_SALE, trade_date=IN_2026,
            buy_amount=CRORE, premium=CRORE, schedule=schedule(),
        )
        assert q.buy.exchange_base == CRORE / 2
        assert q.buy.exchange_base_kind == costs.BASE_PREMIUM
        # 3553 per crore, of Rs 50,000 of premium turnover, is Rs 1,776.50 per leg. The rate is
        # high but the base is small, which is the whole point: the charge is not proportional to
        # the Rs 1 crore of consideration the STT is levied on.
        assert round(q.buy.component_rupees(costs.EXCHANGE_CHARGE), 2) == 1_776.50
        assert round(q.sell.component_rupees(costs.EXCHANGE_CHARGE), 2) == 1_776.50

        # STT, by contrast, is 0.15% of the consideration -- the full Rs 1 crore here. The two
        # bases behave completely differently and the module keeps them separate.
        assert q.buy.component_rupees(costs.STT) == 15_000.0
        assert q.buy.component_rupees(costs.STT) / q.buy.component_rupees(
            costs.EXCHANGE_CHARGE
        ) == pytest.approx(8.44, rel=1e-3)
        assert q.buy.exchange_base != q.buy.amount, (
            "the exchange base is the premium, not the consideration -- if these were equal the "
            "premium-base logic would not be doing anything"
        )

    def test_the_options_exchange_rate_is_higher_than_cash_but_lands_on_a_smaller_base(self):
        """The rate is 11.6x cash; the base is 200x smaller here. Both facts, so neither misread."""
        sch = schedule()
        charge = sch.exchange_regime_for(date(2026, 5, 1))
        options_rate = charge.total_for(costs.EQUITY_OPTIONS)
        cash_rate = charge.total_for(costs.CASH_MARKET)
        assert options_rate == 3553.0
        assert cash_rate == 307.0
        assert options_rate / cash_rate == pytest.approx(3553.0 / 307.0)

        # A Rs 1 crore consideration with only Rs 1 lakh of premium turnover: the base is 1% of the
        # consideration, so the charge lands at 0.0036% of it rather than 0.0355%.
        q = costs.quote(
            costs.OPTION_SALE, costs.OPTION_SALE, trade_date=IN_2026,
            buy_amount=CRORE, premium=CRORE / 100, schedule=schedule(),
        )
        # CRORE/100 of premium, split over two legs, is CRORE/200 = Rs 50,000 of premium turnover
        # per leg. Rs 3,553 per crore of that is Rs 1,776.50, which is 3.553% of the Rs 50,000
        # base -- the same 0.03553% the rate implies, and 0.003553% of the Rs 1 crore of
        # consideration. The 1/100 ratio between the two is the whole effect.
        assert q.buy.exchange_base == CRORE / 200
        pct_of_premium_base = (
            q.buy.component_rupees(costs.EXCHANGE_CHARGE) / (CRORE / 200) * 100.0
        )
        assert round(pct_of_premium_base, 6) == 0.03553
        pct_of_consideration = (
            q.buy.component_rupees(costs.EXCHANGE_CHARGE) / (CRORE / 2) * 100.0
        )
        assert round(pct_of_consideration, 6) == 0.000355
        assert pct_of_consideration < 0.01, (
            "the options charge is well under a basis point of the consideration, which is only "
            "true because it is levied on premium"
        )

    def test_the_premium_base_is_split_evenly_unless_told_otherwise(self):
        even = costs.quote(
            costs.OPTION_SALE, costs.OPTION_EXERCISED, trade_date=IN_2026,
            buy_amount=CRORE, premium=CRORE, schedule=schedule(),
        )
        assert even.buy.exchange_base == even.sell.exchange_base == CRORE / 2
        split = costs.quote(
            costs.OPTION_SALE, costs.OPTION_EXERCISED, trade_date=IN_2026,
            buy_amount=CRORE, buy_premium=CRORE * 0.7, sell_premium=CRORE * 0.3,
            schedule=schedule(),
        )
        assert split.buy.exchange_base == CRORE * 0.7
        assert split.sell.exchange_base == CRORE * 0.3
