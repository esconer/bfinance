"""
A fuzzy search must not be allowed to answer a different question than the one asked.

Measured live on 2026-09-30 against the authorised remote search route:

    'niftybees'   -> 1 hit  : NIFTYBEES
    'nifty bees'  -> 5 hits : BANKBEES, INFRABEES, JUNIORBEES, LIQUIDBEES, NIFTYBEES
    'nifty'       -> 5 hits : NIFTY500, NIFTYNXT50, NIFTY100, NIFTY200, NFT100LV30
    'goldbees'    -> 1 hit  : GOLDBEES
    'juniourbees' -> 0 hits
    'juniorbees'  -> 1 hit  : JUNIORBEES

trendlyne's search is FUZZY and RANKED, so its first result is the vendor's best guess, not
an answer. The previous code returned ``matches[0]`` unconditionally, which meant:

    ohlc("nifty bees")  ->  BANKBEES

The user asked for one ETF and received a different ETF's 2,666 bars, with no error, while
the instrument actually meant -- NIFTYBEES -- sat in the same result list in fifth place.
Nothing downstream of that call can tell, because a frame for the wrong security is
indistinguishable from a frame for the right one. This is the single worst outcome this
package can produce, and it was reachable through the ETF fallback.

The rule now: a hit is accepted only when, after normalising, it IS the instrument asked
for. ``nifty bees`` -> NIFTYBEES is defensible, because a space is not part of a ticker.
``nifty bees`` -> BANKBEES is not, and no ranking can make it so.

Every test drives the real client through an ``httpx.MockTransport``, so the gate, the
sitemap lookup, the search and the new matching rule all run for real without a socket.
"""
from __future__ import annotations

import httpx
import pytest

from bfinance.trendlyne import client as tl
from bfinance.trendlyne.errors import TrendlyneSymbolNotFoundError

#: The measured result sets, exactly as they came back live. The vendor's ranking is part of
#: what is being tested, so it is recorded rather than assumed.
SEARCH_HITS = {
    "niftybees": [("NIFTYBEES", 1588)],
    "NIFTYBEES": [("NIFTYBEES", 1588)],
    "GOLDBEES": [("GOLDBEES", 1582)],
    "nifty bees": [("BANKBEES", 1581), ("INFRABEES", 1602), ("JUNIORBEES", 1584),
                   ("LIQUIDBEES", 1587), ("NIFTYBEES", 1588)],
    "goldbees": [("GOLDBEES", 1582)],
    "juniorbees": [("JUNIORBEES", 1584)],
    "juniourbees": [],
    "nifty": [("NIFTY500", 1893), ("NIFTYNXT50", 1888), ("NIFTY100", 1891),
              ("NIFTY200", 1892), ("NFT100LV30", 910384)],
}

#: A sitemap that carries ordinary equities and no ETF, which is what the live document does:
#: 5,924 entries, none containing "BEES".
SITEMAP_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    "<url><loc>https://trendlyne.com/equity/1127/RELIANCE/reliance/</loc></url>"
    "<url><loc>https://trendlyne.com/equity/11536/TCS/tata-consultancy-services/</loc></url>"
    "</urlset>"
)


def handler_for(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "sitemap" in url:
        return httpx.Response(200, text=SITEMAP_XML)
    if "ac_snames" in url:
        term = request.url.params.get("term", "")
        return httpx.Response(200, json=[
            {"k": k, "id": sym, "value": sym, "slugname": f"{sym.lower()}-slug"}
            for sym, k in SEARCH_HITS.get(term, [])
        ])
    return httpx.Response(404, text="")


def client(gate: bool = True) -> tl.TrendlyneClient:
    return tl.TrendlyneClient(
        transport=httpx.MockTransport(handler_for),
        min_request_interval_ms=0.0,
        max_retries=1,
        allow_member_search=gate,
    )


def resolve(symbol: str, gate: bool = True) -> tl.TrendlyneSymbol:
    return tl._run_async(client(gate).resolve_any_async(symbol))


# ------------------------------------------------------------------ normalisation


class TestNormalisation:
    @pytest.mark.parametrize("typed, expected", [
        ("nifty bees", "NIFTYBEES"),
        ("NIFTYBEES", "NIFTYBEES"),
        ("niftybees", "NIFTYBEES"),
        ("  nifty   bees  ", "NIFTYBEES"),
        ("BAJAJ-AUTO", "BAJAJAUTO"),
        ("500325.BO", "500325BO"),
        ("junior bees", "JUNIORBEES"),
        ("", ""),
        (None, ""),
    ])
    def test_separators_and_case_are_not_part_of_a_ticker(self, typed, expected):
        assert tl._normalise_symbol_query(typed) == expected

    def test_it_cannot_turn_a_typo_into_a_match(self):
        """The point: normalisation removes noise, it does not forgive misspelling."""
        assert tl._normalise_symbol_query("juniourbees") != tl._normalise_symbol_query("JUNIORBEES")

    def test_a_spaced_query_collides_with_its_own_ticker_and_nothing_else(self):
        """
        The intended effect, stated positively. "bank bees" reduces to the same string as
        BANKBEES -- that is the feature -- and to nothing else, so it cannot be confused with
        NIFTYBEES despite both ending in BEES.
        """
        assert tl._normalise_symbol_query("bank bees") == tl._normalise_symbol_query("BANKBEES")
        assert tl._normalise_symbol_query("bank bees") != tl._normalise_symbol_query("NIFTYBEES")
        assert tl._normalise_symbol_query("nifty bees") != tl._normalise_symbol_query("BANKBEES")


# ------------------------------------------------------- the wrong-instrument bug


class TestAFuzzyHitIsNotAnAnswer:
    def test_nifty_bees_resolves_to_niftybees_not_the_vendors_first_guess(self):
        """
        The regression. The vendor ranks BANKBEES first for this query; the instrument asked
        for is fifth in the very same list.
        """
        record = resolve("nifty bees")
        assert record.symbol == "NIFTYBEES", (
            f"resolved to {record.symbol}; taking the vendor's first hit hands back a different "
            f"security's prices with no error to notice it by"
        )
        assert record.stock_id == 1588

    def test_the_previous_behaviour_is_exactly_what_this_forbids(self):
        """Named explicitly, so the reason the rule exists is not lost later."""
        ranked = [sym for sym, _ in SEARCH_HITS["nifty bees"]]
        assert ranked[0] == "BANKBEES", "the wrong answer is the vendor's TOP hit"
        assert ranked[-1] == "NIFTYBEES", "the right one is in the same list, ranked last"

    @pytest.mark.parametrize("typed, wanted, stock_id", [
        ("niftybees", "NIFTYBEES", 1588),
        ("nifty bees", "NIFTYBEES", 1588),
        ("NIFTYBEES", "NIFTYBEES", 1588),
        ("goldbees", "GOLDBEES", 1582),
        ("juniorbees", "JUNIORBEES", 1584),
    ])
    def test_the_intended_instrument_is_accepted(self, typed, wanted, stock_id):
        c = client(True)
        record = tl._run_async(c.resolve_any_async(typed))
        assert (record.symbol, record.stock_id) == (wanted, stock_id)
        # Recorded on the SAME client that did the search, not a fresh one -- checking a new
        # client would read None and prove nothing.
        assert c.symbol_resolution == "member_search"

    def test_an_ambiguous_prefix_refuses_rather_than_guessing(self):
        """
        A bare prefix cannot pick between five indices. Returning the top-ranked one is a
        guess wearing the costume of an answer.
        """
        c = client(True)
        with pytest.raises(TrendlyneSymbolNotFoundError):
            tl._run_async(c.resolve_any_async("nifty"))
        assert c.symbol_resolution == "member_search_ambiguous"

    def test_the_refusal_names_every_candidate_with_its_id(self):
        c = client(True)
        with pytest.raises(TrendlyneSymbolNotFoundError) as excinfo:
            tl._run_async(c.resolve_any_async("nifty"))
        message = str(excinfo.value)
        assert "NONE is the instrument asked for" in message
        for symbol, stock_id in SEARCH_HITS["nifty"]:
            assert symbol in message, f"{symbol} must be offered to the caller"
            assert str(stock_id) in message, "with its stock_id, so the choice is actionable"

    def test_a_misspelling_is_explained_rather_than_reported_as_an_unknown_symbol(self):
        c = client(True)
        with pytest.raises(TrendlyneSymbolNotFoundError) as excinfo:
            tl._run_async(c.resolve_any_async("juniourbees"))
        message = str(excinfo.value)
        assert "misspelling" in message
        assert "not typo-tolerant" in message

    def test_an_exact_normalised_match_is_taken_rather_than_called_a_guess(self):
        """
        The rule is exact equality after normalisation, not "was it the first hit". When the
        vendor does return the instrument whose ticker normalises to the query, that is a
        match: 'nifty' -> NIFTY is one.
        """
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "sitemap" in url:
                return httpx.Response(200, text=SITEMAP_XML)
            if "ac_snames" in url:
                return httpx.Response(200, json=[{"k": 100, "id": "NIFTY", "value": "NIFTY",
                                                  "slugname": "nifty"}])
            return httpx.Response(404, text="")

        c = tl.TrendlyneClient(transport=httpx.MockTransport(handler),
                               min_request_interval_ms=0.0, max_retries=1,
                               allow_member_search=True)
        assert tl._run_async(c.resolve_any_async("nifty")).symbol == "NIFTY"
        assert c.symbol_resolution == "member_search"


# ---------------------------------------------------------------- the gate is intact


class TestTheGateIsUnaffected:
    def test_the_search_is_never_reached_with_the_gate_closed(self):
        """
        Ordering matters. The matching rule must not have moved the robots gate behind the
        search, or refusing would cost a request to a disallowed path.
        """
        c = client(gate=False)
        with pytest.raises(TrendlyneSymbolNotFoundError) as excinfo:
            tl._run_async(c.resolve_any_async("nifty bees"))
        assert c.symbol_resolution == "skipped_robots_disallowed"
        assert "disallowed" in str(excinfo.value)

    def test_the_closed_gate_costs_no_request_at_all(self):
        """A refused request that still made the call would be a partial fix."""
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return handler_for(request)

        c = tl.TrendlyneClient(transport=httpx.MockTransport(handler),
                               min_request_interval_ms=0.0, max_retries=1,
                               allow_member_search=False)
        with pytest.raises(TrendlyneSymbolNotFoundError):
            tl._run_async(c.resolve_any_async("nifty bees"))
        assert not any("ac_snames" in u for u in seen), (
            f"the disallowed route was contacted: {seen}"
        )

    def test_an_ordinary_equity_still_resolves_from_the_sitemap_with_the_gate_shut(self):
        """The gate gates the search, not the package."""
        c = client(gate=False)
        record = tl._run_async(c.resolve_any_async("RELIANCE"))
        assert record.symbol == "RELIANCE"
        assert c.symbol_resolution == "sitemap"
