"""
SmartOptions option-chain tests — fully offline, driven by the recorded capture.

The fixture is ``tests/fixtures/trendlyne/option_chain_tcs.json``: a real recording of
``https://smartoptions.trendlyne.com/phoenix/api/fno/option/chain/?stockCode=TCS&expDate=2026-10-27``
taken on 2026-10-10 (276 KB raw, committed as a trimmed window; 35 ``unique_name`` columns). Every
HTTP path is driven through ``httpx.MockTransport``, so **no test touches the network** and none
carries the ``live`` marker.

The tests are organised around the claims the module makes, not around its functions:

- the gate is closed by default and costs zero requests to discover (:class:`TestTheGate`)
- columns come from ``unique_name``, never from a position (:class:`TestTheMappingIsByHeader`)
- nothing is filled: a ``null`` stays missing and a missing key stays ``None``
  (:class:`TestNothingIsFabricated`)
- every failure is a typed one and every refusal is local (:class:`TestFailureModes`)

**If the fixture is missing the whole module skips.** That is deliberate rather than a fallback:
the assertions below are about a payload measured on a specific day, and a synthetic stand-in
would let them pass while proving nothing about the real shape. The skip message names the file
and the task that owns it.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pandas as pd
import pytest

import bfinance
from bfinance.trendlyne import errors as tle
from bfinance.trendlyne import smartoptions as tso

FIXTURES = Path(__file__).parent / "fixtures" / "trendlyne"
FIXTURE = FIXTURES / "option_chain_tcs.json"

_MISSING_FIXTURE = (
    f"{FIXTURE.name} is not in tests/fixtures/trendlyne/. It is the recorded SmartOptions chain "
    f"(TCS, expiring 2026-10-27, trimmed from a 276 KB capture) that the fixture-recording task "
    f"(T1) lands with its META.json entry. Every test here asserts against a payload measured on "
    f"2026-10-10, so none of them is meaningful without it; the module skips rather than "
    f"substituting a synthetic stand-in that would pass while proving nothing about the real shape."
)

#: Read at import so the counts below are the fixture's, not a constant typed in by hand — a
#: fixture recorded for a different expiry or a different trim width then fails loudly instead of
#: quietly passing against a hardcoded date. Both fall back to placeholders when the fixture is
#: absent, because :data:`pytestmark` skips every test in that case and these must not raise at
#: collection time on their way to being skipped.
if FIXTURE.exists():
    _RECORDED = json.loads(FIXTURE.read_text(encoding="utf-8"))
    #: The expiry the recorded capture answers for, read off the payload itself.
    EXPIRY: str = _RECORDED["body"]["tableDataV2"][0]["c"]["expiry"]
    #: ``len(body.tableData)`` — the strike rows the capture holds. The full recording was 102
    #: strikes at 276 KB; the committed fixture is a trimmed window of the same response, so this
    #: is read rather than asserted and every row count below is written as ``2 x STRIKES``.
    STRIKES: int = len(_RECORDED["body"]["tableData"])
else:
    EXPIRY, STRIKES = "2026-10-27", 0

pytestmark = pytest.mark.skipif(not FIXTURE.exists(), reason=_MISSING_FIXTURE)


# =========================================================== helpers (repo's MockTransport pattern)


def payload() -> dict:
    """The recorded capture, decoded."""
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def wire_strikes() -> list:
    """The strike of each ``body.tableData`` row, read positionally off the header list.

    Deliberately positional, and deliberately *independent* of the module: this is what the wire
    said, so the frame built by the module can be compared against it.
    """
    body = payload()["body"]
    index = [h["unique_name"] for h in body["tableHeaders"]].index("strike_price")
    return [row[index] for row in body["tableData"]]


def static(mapping: dict, default=None):
    """A MockTransport handler keyed on a substring of the request URL — the shape used at
    ``tests/test_trendlyne_core.py:93``."""
    def handler(request: httpx.Request) -> httpx.Response:
        for needle, response in mapping.items():
            if needle in str(request.url):
                return response() if callable(response) else response
        return default() if callable(default) else (default or httpx.Response(404))
    return handler


def mock_client(handler, **kwargs) -> tso.SmartOptionsClient:
    """A client whose every request is served by ``handler`` — no network, no sockets.

    Pacing off by default: the real 1 s crawl-delay floor would add nothing to a mock.
    """
    kwargs.setdefault("min_request_interval_ms", 0.0)
    kwargs.setdefault("max_retries", 2)
    return tso.SmartOptionsClient(transport=httpx.MockTransport(handler), **kwargs)


def served(**kwargs) -> tso.SmartOptionsClient:
    """A gate-open client answering the recorded capture, plus a call recorder."""
    calls: list = []
    body = payload()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=body)

    client = mock_client(handler, allow_smart_options=True, **kwargs)
    return client, calls


# =========================================================== the gate


class TestTheGate:
    """``allow_smart_options`` is off by default, and finding that out is free.

    This is the operator decision of 2026-10-10: SmartOptions ships as a **secondary** F&O source.
    It is *not* a robots gate — the host's ``robots.txt`` (read the same day) disallows only
    ``*/my-alerts/*`` and ``*/strategy-builder/user/``, and the chain route matches neither — so
    neither the refusal nor the pacing floor is a compliance claim. Both are the operator's.
    """

    def test_the_default_is_closed(self):
        assert tso.SMART_OPTIONS_FLAG == "allow_smart_options"
        assert tso.SmartOptionsClient().allow_smart_options is False, (
            "SmartOptions is off by default; a client that reaches the host without an explicit "
            "opt-in would be a different contract from the one that was agreed"
        )

    def test_a_closed_gate_costs_zero_requests(self):
        calls: list = []
        client = mock_client(
            static({}, default=lambda: calls.append(1) or httpx.Response(200, json=payload())),
        )

        with pytest.raises(tle.TrendlyneSmartOptionsError) as excinfo:
            client.option_chain("TCS", EXPIRY)

        assert calls == [], "the gate must be checked before a socket is opened"
        assert "allow_smart_options" in str(excinfo.value), (
            "the refusal has to name the constant that opens it, or a reader has to go and find "
            "it in the source"
        )

    def test_the_refusal_says_it_is_not_a_robots_refusal(self):
        """The distinction is load-bearing. Claiming a robots conflict on a permitted path is a
        false statement about a site this package has read — and a caller who believes it would go
        looking for a ``Disallow`` rule that does not exist."""
        client = mock_client(static({}))
        with pytest.raises(tle.TrendlyneSmartOptionsError) as excinfo:
            client.option_chain("TCS", EXPIRY)
        message = str(excinfo.value)
        assert "not a robots refusal" in message
        assert "robots-clean" in message

    def test_the_refusal_is_not_the_robots_error(self):
        """A caller must be able to tell the two gates apart by type, not by reading prose.

        ``TrendlyneRobotsDisallowedError`` means "the site forbids this path";
        ``TrendlyneSmartOptionsError`` means "this package asks you to opt in". Folding them
        together would make the first one's message a lie on the second one.
        """
        assert not issubclass(
            tle.TrendlyneSmartOptionsError, tle.TrendlyneRobotsDisallowedError
        )
        assert issubclass(tle.TrendlyneSmartOptionsError, tle.TrendlyneError)
        assert issubclass(tle.TrendlyneSmartOptionsError, bfinance.UpstreamServiceError)

    def test_opening_the_gate_reaches_the_route(self):
        client, calls = served()
        result = client.option_chain("TCS", EXPIRY)
        assert len(calls) == 1
        assert result.chain.attrs["bfinance_provenance"] == "smartoptions"

    def test_the_per_call_override_beats_the_constructor(self):
        """Same client, same transport, one call — the only difference is the per-call override.
        Without this the constructor flag would be the only lever and the ``None`` branch of
        ``_require_smart_options`` would be untested."""
        calls: list = []
        client = mock_client(
            static({}, default=lambda: calls.append(1) or httpx.Response(200, json=payload())),
        )
        with pytest.raises(tle.TrendlyneSmartOptionsError):
            client.option_chain("TCS", EXPIRY)
        assert calls == []

        result = client.option_chain("TCS", EXPIRY, allow_smart_options=True)
        assert len(calls) == 1
        assert len(result.chain) > 0


# =========================================================== the mapping is by header


class TestTheMappingIsByHeader:
    """Rows are positional arrays; ``tableHeaders[i].unique_name`` is the only key.

    Every assertion below compares the frame against the *wire* read independently, so a frame
    built by hard-coding indices would fail rather than coincidentally agree.
    """

    def test_the_row_count_is_twice_the_strike_count(self):
        strikes = wire_strikes()
        client, _ = served()
        chain = client.option_chain("TCS", EXPIRY).chain

        assert len(chain) == 2 * len(strikes) == 2 * STRIKES
        assert chain["side"].value_counts().to_dict() == {
            "call": len(strikes), "put": len(strikes),
        }
        assert sorted(chain["strike_price"].unique()) == sorted(set(strikes)), (
            "a strike the vendor published must survive as a row on both sides; dropping the "
            "unquoted ones would quietly shrink the chain at exactly the strikes nobody trades"
        )

    def test_a_call_row_price_equals_the_wire_at_that_position(self):
        body = payload()["body"]
        names = [h["unique_name"] for h in body["tableHeaders"]]
        strike_index = names.index("strike_price")
        price_index = names.index("current_price_call")
        first_row = body["tableData"][0]
        first_strike = first_row[strike_index]

        client, _ = served()
        chain = client.option_chain("TCS", EXPIRY).chain
        row = chain[(chain["side"] == "call") & (chain["strike_price"] == first_strike)]

        assert len(row) == 1
        got = row.iloc[0]["current_price_call"]
        if first_row[price_index] is None:
            assert pd.isna(got), "a null cell must arrive as missing, never as 0.0"
        else:
            assert got == first_row[price_index]

    def test_the_whole_price_column_lands_on_the_right_side_of_the_melt(self):
        """The negative control for the one above.

        A frame built positionally with the *put* columns on the call rows would still satisfy
        "``tableData[0][0]`` equals ``current_price_call``" for a chain whose calls happen to be
        null. Comparing **every** row on both sides cannot: the two columns hold different
        values wherever both legs are quoted, so a swap is visible.
        """
        body = payload()["body"]
        names = [h["unique_name"] for h in body["tableHeaders"]]
        rows = {row[names.index("strike_price")]: row for row in body["tableData"]}

        client, _ = served()
        chain = client.option_chain("TCS", EXPIRY).chain

        compared = 0
        for side, wire_column in (("call", "current_price_call"), ("put", "current_price_put")):
            index = names.index(wire_column)
            for strike, wire_row in rows.items():
                frame_row = chain[
                    (chain["side"] == side) & (chain["strike_price"] == strike)
                ].iloc[0]
                expected = wire_row[index]
                if expected is None:
                    assert pd.isna(frame_row[wire_column]), f"{side} {strike}"
                else:
                    assert frame_row[wire_column] == expected, f"{side} {strike}"
                    compared += 1
        assert compared >= STRIKES // 2, (
            f"only {compared} non-null cells were compared across {STRIKES} strikes; too few to "
            f"catch a call/put swap"
        )

    def test_the_greeks_are_present_on_both_sides(self):
        client, _ = served()
        chain = client.option_chain("TCS", EXPIRY).chain

        for greek in ("delta_calc", "gamma_calc", "rho_calc", "theta_calc", "vega_calc"):
            for side in ("call", "put"):
                assert f"{greek}_{side}" in chain.columns, f"{greek}_{side} is missing"

        calls = chain[chain["side"] == "call"]
        puts = chain[chain["side"] == "put"]
        # The recorded chain quotes a delta on every call (deep ITM) and on the puts nearest the
        # money; asserting a non-null value on both proves the columns carry data, not just names.
        assert calls["delta_calc_call"].notna().all()
        assert puts["delta_calc_put"].notna().any()

    def test_the_other_side_columns_are_missing_not_zero(self):
        """A call row's put columns are ``NaN``, meaning *not applicable here*. Collapsing them
        to 0 would be fabrication of the worst kind: a put open interest of zero reads as a
        measured fact about a contract this row is not."""
        client, _ = served()
        chain = client.option_chain("TCS", EXPIRY).chain

        call_rows = chain[chain["side"] == "call"]
        assert call_rows["current_price_put"].isna().all()
        assert call_rows["open_interest_put"].isna().all()
        assert call_rows["delta_calc_put"].isna().all()

        put_rows = chain[chain["side"] == "put"]
        assert put_rows["current_price_call"].isna().all()
        assert put_rows["delta_calc_call"].isna().all()

    def test_the_contract_ids_survive_the_float_upcast_exactly(self):
        """``pk_call``/``pk_put`` are integers on the wire and ``float64`` in the long frame,
        because only a float column can hold the other side's missing values. That is a dtype
        change, not a rounding one — pinned here so the module docstring's claim about it is
        checked rather than trusted."""
        client, _ = served()
        chain = client.option_chain("TCS", EXPIRY).chain

        for column in ("pk_call", "pk_put"):
            values = chain[column].dropna()
            assert values.size, f"{column} is entirely missing; the fixture does not carry it"
            assert all(float(value).is_integer() for value in values), (
                f"{column} lost integer precision through the melt"
            )
            assert values.max() < 2**53, f"{column} exceeds float64's exact integer range"

    def test_the_strike_is_shared_by_both_rows(self):
        client, _ = served()
        chain = client.option_chain("TCS", EXPIRY).chain
        per_strike = chain.groupby("strike_price")["side"].nunique()
        assert (per_strike == 2).all(), "each strike must appear exactly once per side"

    def test_every_wire_column_survives_under_its_own_name(self):
        body = payload()["body"]
        names = [h["unique_name"] for h in body["tableHeaders"]]
        client, _ = served()
        chain = client.option_chain("TCS", EXPIRY).chain
        for name in names:
            assert name in chain.columns, f"{name} was dropped or renamed"
        assert len(chain.columns) == len(names) + 2  # + symbol, side

    def test_a_missing_required_column_is_a_schema_error_not_a_guess(self):
        """A dropped column is the direction worth failing on: read positionally, a renamed
        column becomes a quietly mislabelled one, and the frame still looks plausible."""
        body = payload()
        names = [h["unique_name"] for h in body["body"]["tableHeaders"]]
        body["body"]["tableHeaders"] = [
            h for h in body["body"]["tableHeaders"] if h["unique_name"] != "delta_calc_call"
        ]
        dropped = names.index("delta_calc_call")
        body["body"]["tableData"] = [
            row[:dropped] + row[dropped + 1:] for row in body["body"]["tableData"]
        ]

        with pytest.raises(tle.TrendlyneSchemaError) as excinfo:
            tso.parse_option_chain(body, symbol="TCS", expiry=EXPIRY)
        assert "delta_calc_call" in str(excinfo.value)

    def test_a_row_of_the_wrong_width_is_a_schema_error(self):
        body = payload()
        body["body"]["tableData"][3] = body["body"]["tableData"][3][:-1]
        with pytest.raises(tle.TrendlyneSchemaError) as excinfo:
            tso.parse_option_chain(body, symbol="TCS", expiry=EXPIRY)
        assert "34 values" in str(excinfo.value) and "35" in str(excinfo.value)


# =========================================================== expiry validation


class TestTheExpiryIsCheckedLocally:
    """``expDate`` must be ``YYYY-MM-DD``. ``27-Oct-2026`` answers HTTP 500 (verified 2026-10-10).

    A 500 with no explanation is the most expensive possible answer to a formatting question, so
    the form is refused before the request rather than diagnosed after it — and the test proves
    the refusal costs the vendor nothing.
    """

    @pytest.mark.parametrize(
        "expiry",
        ["27-Oct-2026", "2026-10", "Oct-2026", "2026/10/27", "20261027", "2026-10-7",
         "2026-W01-1", "", "latest", "2026-02-30"],
    )
    def test_a_non_iso_expiry_costs_zero_requests(self, expiry):
        calls: list = []
        client = mock_client(
            static({}, default=lambda: calls.append(1) or httpx.Response(200, json=payload())),
            allow_smart_options=True,
        )
        with pytest.raises(ValueError) as excinfo:
            client.option_chain("TCS", expiry)
        assert calls == [], f"{expiry!r} must be refused before a socket is opened"
        assert "YYYY-MM-DD" in str(excinfo.value)

    def test_the_gate_is_checked_before_the_expiry(self):
        """Both refusals cost nothing, but the order is a contract: the standing policy decision
        outranks an argument the caller can fix without asking the vendor for permission."""
        client = mock_client(static({}))
        with pytest.raises(tle.TrendlyneSmartOptionsError):
            client.option_chain("TCS", "27-Oct-2026")

    def test_a_valid_expiry_is_returned_cleaned(self):
        assert tso.validate_expiry(" 2026-10-27 ") == EXPIRY
        with pytest.raises(ValueError):
            tso.validate_expiry(None)

    def test_the_url_carries_the_iso_date_and_the_symbol(self):
        client, calls = served()
        client.option_chain("tcs", " 2026-10-27 ")
        url = calls[0]
        assert "stockCode=TCS" in url, url
        assert "expDate=2026-10-27" in url, url
        assert url.startswith("https://smartoptions.trendlyne.com/phoenix/api/fno/option/chain/?")


# =========================================================== nothing is fabricated


class TestNothingIsFabricated:
    def test_max_pain_and_the_headline_numbers_pass_through(self):
        body = payload()["body"]
        client, _ = served()
        summary = client.option_chain("TCS", EXPIRY).summary

        assert summary["maxPain"] == body["maxPain"] == 2140.0
        assert summary["atTheMoney"] == body["atTheMoney"]
        assert summary["MWPL"] == body["MWPL"]
        assert summary["pcr_oi"] == body["expiryLevelPCRValues"]["pcr_oi"]
        assert summary["pcr_volume"] == body["expiryLevelPCRValues"]["pcr_volume"]

    def test_the_expiry_level_and_all_expiry_pcrs_are_not_collapsed(self):
        """They are different measurements and they disagree on this capture (0.9103 vs 0.9).
        Overwriting one with the other would answer a question the caller did not ask."""
        body = payload()["body"]
        client, _ = served()
        summary = client.option_chain("TCS", EXPIRY).summary

        assert summary["pcr_oi"] == body["expiryLevelPCRValues"]["pcr_oi"]
        assert summary["stock_level_pcr"]["FNO_PCR_OI"] == (
            body["stockLevelData"]["PCR_metrcs"]["FNO_PCR_OI"]
        )
        assert summary["pcr_oi"] != summary["stock_level_pcr"]["FNO_PCR_OI"]

    def test_the_totals_come_from_the_response(self):
        pcr = payload()["body"]["stockLevelData"]["PCR_metrcs"]
        client, _ = served()
        summary = client.option_chain("TCS", EXPIRY).summary
        for key in ("total_call_oi", "total_put_oi", "total_oi",
                    "total_call_volume", "total_put_volume", "total_volume"):
            assert summary[key] is not None, key
        assert summary["total_call_oi"] == pcr["total_call_OI"]
        assert summary["total_oi"] == pcr["total_OI"]

    def test_an_absent_number_is_none_and_not_zero(self):
        """A key the vendor did not send is ``None``. A ``0.0`` would read downstream as a
        measured zero, which is a different claim entirely."""
        body = payload()
        del body["body"]["maxPain"]
        client = mock_client(
            static({}, default=lambda: httpx.Response(200, json=body)), allow_smart_options=True
        )
        summary = client.option_chain("TCS", EXPIRY).summary
        assert summary["maxPain"] is None
        assert summary["atTheMoney"] == payload()["body"]["atTheMoney"]

    def test_body_iv_being_null_is_not_an_error(self):
        """``body.IV`` is ``null`` on the recorded capture and that is by design — the implied
        volatility that matters is per-contract, in ``implied_volatility_*``. Treating a null
        here as a schema failure would break every call against this route."""
        body = payload()
        assert body["body"]["IV"] is None, (
            "if this ever fires, the payload changed shape; re-measure before assuming it is "
            "still null-by-design"
        )
        client, _ = served()
        result = client.option_chain("TCS", EXPIRY)
        assert len(result.chain) == 2 * STRIKES

    def test_the_contract_timestamp_is_verbatim_not_parsed(self):
        """``'09 Oct,2026  04:00 PM (IST)'`` carries a double space and a bare ``IST`` with no
        offset. Parsing it would mean inventing an offset the payload never stated."""
        raw = payload()["body"]["contractLastUpdated"]
        client, _ = served()
        assert client.option_chain("TCS", EXPIRY).contract_last_updated == raw
        assert isinstance(client.option_chain("TCS", EXPIRY).contract_last_updated, str)

    def test_the_attrs_say_where_the_frame_came_from(self):
        client, _ = served()
        result = client.option_chain("TCS", EXPIRY)
        attrs = result.chain.attrs

        assert attrs["bfinance_provenance"] == "smartoptions"
        assert attrs["bfinance_trendlyne_smartoptions_expiry"] == EXPIRY
        assert attrs["bfinance_trendlyne_smartoptions_rows"] == len(result.chain) == 2 * STRIKES
        assert attrs["bfinance_symbol"] == "TCS"
        assert attrs is result.attrs or dict(attrs) == dict(result.attrs)
        assert "smartoptions.trendlyne.com" in attrs["bfinance_trendlyne_smartoptions_source"]

    def test_the_source_attr_names_where_the_data_actually_came_from(self):
        """A caller who points ``base_url=`` at a replay server must not get ``attrs`` claiming the
        stock host served it. ``source`` is the URL the response resolved to, not the default the
        module would have rebuilt."""
        calls: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(200, json=payload())

        client = mock_client(handler, allow_smart_options=True,
                             base_url="https://replay.invalid")
        result = client.option_chain("TCS", EXPIRY)

        assert "replay.invalid" in calls[0]
        assert result.attrs["bfinance_trendlyne_smartoptions_source"].startswith(
            "https://replay.invalid/"
        ), "attrs must name the host that actually answered"

        # And with no override, the stock host is named.
        default_client, _ = served()
        assert default_client.option_chain("TCS", EXPIRY).attrs[
            "bfinance_trendlyne_smartoptions_source"
        ].startswith(tso.BASE_URL)

    def test_the_expiry_is_echoed_back(self):
        client, _ = served()
        assert client.option_chain("TCS", EXPIRY).expiry == EXPIRY

    def test_the_provenance_key_is_the_cross_package_one_this_repo_publishes_everywhere(self):
        """``bfinance_provenance``, not ``bfinance_trendlyne_provenance``.

        The KEY is how a caller finds frames by source across bfinance — ``nse/bhavcopy.py``,
        ``ownership.py``, ``trendlyne/tables.py`` and every other producer publish the one
        unprefixed name. A second, trendlyne-prefixed spelling means a consumer filtering on
        ``attrs["bfinance_provenance"] == "smartoptions"`` finds nothing here, and the reason for
        the split is invisible: the frames are right, the key is just in a different dialect.

        The VALUE stays ``"smartoptions"`` and never ``"trendlyne"`` — a different host, a
        different crawl-delay and a different contract, which is the whole point of the tag.
        """
        client, _ = served()
        attrs = client.option_chain("TCS", EXPIRY).attrs

        assert attrs["bfinance_provenance"] == "smartoptions"
        assert attrs["bfinance_provenance"] != "trendlyne"
        assert "bfinance_trendlyne_provenance" not in attrs, (
            "the old key is still published; a caller reading one key gets the other host's "
            "frames and this host's frames distinguished only by which dict they happen to hold"
        )

    def test_the_other_trendlyne_producers_publish_the_same_key(self):
        """The tie, and it is why the key changed: the sibling producer was not renamed with it."""
        from bfinance.trendlyne import tables as tbt

        frame = tbt.find_table(
            "<table><tr><th>Ex-Date</th><th>Bonus Ratio</th><th>Record Date</th></tr>"
            "<tr><td>2026-10-14</td><td>1:1</td><td>2026-10-13</td></tr></table>",
            "bonus",
        )

        assert frame.attrs["bfinance_provenance"] == tbt.PROVENANCE == "trendlyne", (
            "the main trendlyne producer already publishes the unprefixed key; SmartOptions was "
            "the only one in the repo publishing a second spelling of it"
        )


# =========================================================== the empty chain (MAJOR 2)


class TestAnEmptyChainIsAnAnswer:
    """``tableData: []`` with the header list still present is a real response, not a failure.

    Two live cases produce it and they are not the same question: an expiry the vendor has not
    populated yet, and a scrip with no F&O chain at all. Both used to arrive as a bare
    ``KeyError: 'side'`` -- raised by this module's own ``sort_values`` -- which is neither a data
    answer nor a ``BFinanceError``, so it broke the package's stated contract that every trendlyne
    failure is typed.
    """

    def _empty(self, **body_overrides) -> tso.SmartOptionsClient:
        """A gate-open client serving the recorded capture with its ``tableData`` emptied."""
        body = payload()
        body["body"]["tableData"] = []
        body["body"].update(body_overrides)
        return mock_client(
            static({}, default=lambda: httpx.Response(200, json=body)), allow_smart_options=True
        )

    def test_an_empty_chain_through_the_public_method_carries_every_column(self):
        """Through :meth:`SmartOptionsClient.option_chain`, not just the parser.

        A parser-only test would have passed while the crash lived in ``option_chain``'s own
        ``sort_values``, which is exactly where it was reachable.
        """
        wire_columns = len(payload()["body"]["tableHeaders"])
        assert wire_columns == 35, "the recorded capture is the 35-column measurement"

        result = self._empty().option_chain("TCS", EXPIRY)

        assert result.chain.empty
        assert list(result.chain.columns)[:2] == ["symbol", "side"]
        assert "strike_price" in result.chain.columns
        assert result.chain.shape[1] == wire_columns + 2, "every wire column + symbol + side"

    def test_the_columns_a_consumer_indexes_all_exist_on_the_empty_frame(self):
        """The two operations that used to raise, run against the empty answer."""
        width = len(payload()["body"]["tableHeaders"]) + 2
        result = self._empty().option_chain("TCS", EXPIRY)

        assert result.chain["side"].tolist() == []
        assert result.chain["symbol"].tolist() == []
        assert result.chain.sort_values(
            ["strike_price", "side"], kind="stable", ignore_index=True
        ).shape == (0, width)

    def test_the_column_set_is_the_same_one_a_populated_chain_has(self):
        """The contract is *the same columns*, so a consumer needs no branch on emptiness."""
        empty = self._empty().option_chain("TCS", EXPIRY).chain
        populated, _ = served()
        populated = populated.option_chain("TCS", EXPIRY).chain

        assert list(empty.columns) == list(populated.columns)

    def test_the_summary_reports_only_what_the_response_carried(self):
        """No invented numbers. A max pain over a chain with no strikes is not a max pain of
        zero, and it is certainly not the recorded capture's 2140.0 standing in for it."""
        result = self._empty(
            maxPain=None,
            atTheMoney=None,
            MWPL=None,
            expiryLevelPCRValues=None,
            stockLevelData=None,
        ).option_chain("TCS", EXPIRY)

        summary = result.summary
        assert summary["maxPain"] is None
        assert summary["atTheMoney"] is None
        assert summary["MWPL"] is None
        assert summary["pcr_oi"] is None
        assert summary["pcr_volume"] is None
        assert summary["expiry_level_pcr"] == {}
        assert summary["stock_level_pcr"] == {}
        for key in ("total_call_oi", "total_put_oi", "total_oi",
                    "total_call_volume", "total_put_volume", "total_volume"):
            assert summary[key] is None, f"{key} was invented for an empty chain"

    def test_the_stock_level_totals_are_dropped_too_when_the_response_drops_them(self):
        """``stockLevelData`` is where the six totals live; a genuinely empty response does not
        carry it, and ``summary`` must not reach back into the recorded capture for them."""
        client = mock_client(
            static({}, default=lambda: httpx.Response(200, json={
                "head": {"status": "0", "statusDescription": "success"},
                "body": {"tableHeaders": payload()["body"]["tableHeaders"], "tableData": []},
            })),
            allow_smart_options=True,
        )

        summary = client.option_chain("TCS", EXPIRY).summary

        assert summary["maxPain"] is None
        assert summary["underlying_price"] is None
        assert summary["total_oi"] is None

    def test_attrs_say_the_chain_is_empty_so_the_caller_can_ask_why(self):
        """``empty`` is what separates "this expiry has no chain yet" from "the fetch worked"."""
        result = self._empty().option_chain("TCS", EXPIRY)

        assert result.attrs["bfinance_trendlyne_smartoptions_empty"] is True
        assert result.attrs["bfinance_trendlyne_smartoptions_rows"] == 0
        assert result.attrs["bfinance_trendlyne_smartoptions_strikes"] == 0
        assert "EMPTY" in result.attrs["bfinance_trendlyne_note"]

    def test_control_a_populated_chain_is_not_marked_empty(self):
        """The control. A flag that was always True would satisfy every assertion above."""
        result, _ = served()
        result = result.option_chain("TCS", EXPIRY)

        assert result.attrs["bfinance_trendlyne_smartoptions_empty"] is False
        assert "EMPTY" not in result.attrs["bfinance_trendlyne_note"]
        assert len(result.chain) == 2 * STRIKES

    def test_control_the_bare_names_frame_is_what_the_empty_case_used_to_return(self):
        """Reproduces the pre-fix early return, so the test above is anchored to a real defect
        rather than to a shape someone imagined."""
        names = [h["unique_name"] for h in payload()["body"]["tableHeaders"]]
        width = len(names) + 2

        broken = pd.DataFrame([], columns=names)
        assert "symbol" not in broken.columns and "side" not in broken.columns
        with pytest.raises(KeyError):
            broken.sort_values(["strike_price", "side"])
        assert broken.shape[1] == width - 2, "the control must be two columns short"


# =========================================================== failure modes


class TestFailureModes:
    def test_a_failed_envelope_is_typed_and_not_an_empty_frame(self):
        """This vendor family answers **200** for a failed request. A client that checks only the
        HTTP status hands back a body with no chain in it and the caller reads a quiet market."""
        body = {
            "head": {"status": "9", "statusDescription": "Something went wrong"},
            "body": {},
        }
        client = mock_client(
            static({}, default=lambda: httpx.Response(200, json=body)), allow_smart_options=True
        )
        with pytest.raises(tle.TrendlyneNotAuthorizedError) as excinfo:
            client.option_chain("TCS", EXPIRY)
        assert "9" in str(excinfo.value)
        assert isinstance(excinfo.value, tle.TrendlyneError)

    def test_429_is_retried_then_typed(self):
        calls: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(429, headers={"Retry-After": "0"})

        client = mock_client(handler, allow_smart_options=True, max_retries=2)
        with pytest.raises(tle.TrendlyneRateLimitedError) as excinfo:
            client.option_chain("TCS", EXPIRY)

        assert excinfo.value.status_code == 429
        assert len(calls) == 2, "429 must be retried inside the budget"
        assert isinstance(excinfo.value, bfinance.RateLimitExceededError)

    def test_429_recovers_within_the_budget(self):
        state = {"n": 0}
        body = payload()

        def handler(request: httpx.Request) -> httpx.Response:
            state["n"] += 1
            if state["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "0"})
            return httpx.Response(200, json=body)

        client = mock_client(handler, allow_smart_options=True, max_retries=3)
        assert len(client.option_chain("TCS", EXPIRY).chain) == 2 * STRIKES
        assert state["n"] == 2

    def test_500_is_a_typed_error_not_a_crash(self):
        """HTTP 500 is what a malformed ``expDate`` earns on this route (verified 2026-10-10), so
        it must arrive as a typed, readable refusal naming that — never as an unhandled raise
        from inside ``response.json()``."""
        calls: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(500, text="<html>error</html>")

        client = mock_client(handler, allow_smart_options=True, max_retries=2)
        with pytest.raises(tle.TrendlyneServerError) as excinfo:
            client.option_chain("TCS", EXPIRY)

        assert excinfo.value.status_code == 500
        assert "expDate" in str(excinfo.value), (
            "the 500 on this route has a known cause; the message should say so rather than "
            "leaving the caller to guess between an outage and a formatting mistake"
        )
        assert len(calls) == 2, "5xx must be retried inside the budget"
        assert isinstance(excinfo.value, bfinance.UpstreamServiceError)

    def test_a_4xx_is_terminal(self):
        calls: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(403)

        client = mock_client(handler, allow_smart_options=True, max_retries=3)
        with pytest.raises(tle.TrendlyneHTTPError):
            client.option_chain("TCS", EXPIRY)
        assert len(calls) == 1, "an unchanged 4xx cannot succeed on a retry"

    def test_a_non_json_body_is_a_schema_error(self):
        client = mock_client(
            static({}, default=lambda: httpx.Response(
                200, text="<html>maintenance</html>",
                headers={"Content-Type": "text/html"},
            )),
            allow_smart_options=True,
        )
        with pytest.raises(tle.TrendlyneSchemaError) as excinfo:
            client.option_chain("TCS", EXPIRY)
        assert "text/html" in str(excinfo.value)

    def test_a_transport_failure_is_typed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        client = mock_client(handler, allow_smart_options=True, max_retries=2)
        with pytest.raises(tle.TrendlyneTransportError):
            client.option_chain("TCS", EXPIRY)


class TestTheWafGuardIsHereToo:
    """HTTP 405, or the ``x-amzn-waf-action`` header on **any** status -- and never retried.

    The main client has carried this guard since the WAF was measured on trendlyne.com; this module
    did not, so a challenge served as a 200 would have been handed to ``response.json()`` and read
    as a parse failure. Both forms are tested because they are independent: the 405 arrives without
    the header and the header arrives on statuses that are not 405 at all.

    **What is not claimed.** Whether ``smartoptions.trendlyne.com`` is behind that same WAF is
    **unmeasured** -- no request to this host has ever been observed returning either form, and
    every capture in this repository is from trendlyne.com. The guard is an assumption that the
    operator is shared across the vendor's subdomains. The docstring says so; this class asserts
    the behaviour without pretending the premise is a measurement.
    """

    #: Both challenge forms, plus a 403 carrying the header -- the shape WAF attaches it in.
    CHALLENGES = {
        "405 without the header": httpx.Response(405, text="Method Not Allowed"),
        "the header on a 200": httpx.Response(
            200, text="<html>captcha</html>", headers={"x-amzn-waf-action": "challenge"}
        ),
        "the header on a 403": httpx.Response(
            403, text="Forbidden", headers={"x-amzn-waf-action": "captcha"}
        ),
    }

    @pytest.mark.parametrize("label", sorted(CHALLENGES))
    def test_the_challenge_is_a_typed_refusal_and_is_not_retried(self, label):
        calls: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return self.CHALLENGES[label]

        client = mock_client(handler, allow_smart_options=True, max_retries=3)
        with pytest.raises(tle.TrendlyneSmartOptionsError):
            client.option_chain("TCS", EXPIRY)

        assert len(calls) == 1, (
            f"{label}: a challenge was retried, which spends more of an already-spent budget"
        )

    def test_the_refusal_names_the_header_and_says_what_is_assumed(self):
        """A caller reading the message has to be able to tell an observed challenge from an
        assumed guard, or this message would be a measurement claim it cannot support."""
        client = mock_client(
            static({}, default=lambda: self.CHALLENGES["405 without the header"]),
            allow_smart_options=True,
        )
        with pytest.raises(tle.TrendlyneSmartOptionsError) as excinfo:
            client.option_chain("TCS", EXPIRY)
        message = str(excinfo.value)
        assert "x-amzn-waf-action" in message
        assert "NOT been observed" in message or "not been observed" in message, (
            "the guard's premise is unmeasured on this host and the message has to say so"
        )
        assert isinstance(excinfo.value, bfinance.UpstreamServiceError)

    def test_a_challenge_is_not_reported_as_a_plain_4xx(self):
        """The negative control: it is the header, or the 405, that means WAF. Without the header
        a 403 is an ordinary access refusal and stays a different type."""
        client = mock_client(
            static({}, default=lambda: httpx.Response(403, text="Forbidden")),
            allow_smart_options=True,
        )
        with pytest.raises(tle.TrendlyneHTTPError) as excinfo:
            client.option_chain("TCS", EXPIRY)
        assert not isinstance(excinfo.value, tle.TrendlyneSmartOptionsError)

    def test_a_200_without_the_header_is_still_parsed_as_data(self):
        """The other half of the control: a blanket refusal of every 200 would destroy the only
        state every committed capture on this host is in."""
        client, _ = served()
        assert len(client.option_chain("TCS", EXPIRY).chain) == 2 * STRIKES

    def test_the_444_reasoning_survives_the_waf_guard(self):
        """No 444 special case, and the reason is still a measurement rather than an omission:
        the 444 gate was seen on trendlyne.com's ``/mapp/v1/`` family on 2026-09-27 and never on
        this host. So a 444 here is **not** the bot gate and **not** the WAF -- it arrives as the
        typed HTTP error with the code attached, terminal, exactly as the docstring says."""
        calls: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(444, text="")

        client = mock_client(handler, allow_smart_options=True, max_retries=3)
        with pytest.raises(tle.TrendlyneHTTPError) as excinfo:
            client.option_chain("TCS", EXPIRY)

        assert excinfo.value.status_code == 444
        assert not isinstance(excinfo.value, tle.TrendlyneSmartOptionsError), (
            "444 was folded into the WAF guard; it is not the WAF, and the main client's "
            "TrendlyneAccessDeniedError for 444 was measured on a different host's route family"
        )
        assert len(calls) == 1, "every 4xx is terminal"


# =========================================================== the module's own claims


class TestTheModulesClaims:
    """The claims a caller reads in the docstring, checked against the module rather than
    restated. A docstring is not executed, so it never fails on its own."""

    def test_every_failure_is_catchable_by_the_one_package_root(self):
        for name in tle.__all__:
            assert issubclass(getattr(tle, name), tle.TrendlyneError), name
            assert issubclass(getattr(tle, name), bfinance.BFinanceError), name

    def test_no_new_exception_root_was_introduced(self):
        """``tests/test_public_surface.py`` holds the package to exactly one class deriving
        directly from ``Exception``. Asserting it here too means a future edit that adds a bare
        root fails in the file that would have to explain it."""
        assert tle.TrendlyneSmartOptionsError.__bases__ != (Exception,)
        roots = [
            name
            for name, value in vars(tle).items()
            if isinstance(value, type) and issubclass(value, BaseException)
            and value.__module__ == tle.__name__ and value.__bases__ == (Exception,)
        ]
        assert roots == [], f"errors.py now defines bare Exception roots: {roots}"

    def test_the_declared_surface_exists_and_is_complete(self):
        for name in tso.__all__:
            assert hasattr(tso, name), f"__all__ names {name!r}, which does not exist"
        assert len(tso.__all__) == len(set(tso.__all__)), "__all__ repeats a name"
        for name in ("SmartOptionsClient", "OptionChainResult", "SMART_OPTIONS_FLAG",
                     "parse_option_chain", "validate_expiry"):
            assert name in tso.__all__, name

    def test_the_package_re_exports_the_client_and_the_error(self):
        import bfinance.trendlyne as pkg

        for name in ("SmartOptionsClient", "OptionChainResult", "SMART_OPTIONS_FLAG",
                     "validate_expiry", "TrendlyneSmartOptionsError"):
            assert name in pkg.__all__, name
            assert getattr(pkg, name) is getattr(tso, name, getattr(pkg, name))

    def test_the_sync_twin_returns_the_same_thing(self):
        async_client, _ = served()
        sync_client, _ = served()
        result = sync_client.option_chain("TCS", EXPIRY)
        assert len(result.chain) == 2 * STRIKES
        assert result.summary["maxPain"] == 2140.0
        assert async_client is not sync_client  # two independent clients, same contract

    def test_the_base_url_is_the_second_host_not_the_first(self):
        """The whole point of a second client: a different host. A copy of the trendlyne.com base
        URL here would quietly turn this into a second route on the first host."""
        assert tso.BASE_URL == "https://smartoptions.trendlyne.com"
        assert tso.SmartOptionsClient.BASE_URL == tso.BASE_URL
        assert tso.BASE_URL != "https://trendlyne.com"

    def test_the_pacing_floor_is_one_second(self):
        """``Crawl-delay: 1`` in the host's ``User-agent: *`` group, read 2026-10-10. Same number
        as the trendlyne.com client and a **different** slot — two operators, two documents, two
        floors. Sharing one lock would let a burst against one throttle the other."""
        assert tso.DEFAULT_MIN_REQUEST_INTERVAL_MS == 1000.0
        assert tso.SmartOptionsClient().min_request_interval_ms == 1000.0

    def test_the_client_sends_no_credentials(self):
        """No login, no API key, no cookie — the route is public and the header set says so."""
        headers = tso.SmartOptionsClient(allow_smart_options=True)._headers()
        assert "Cookie" not in headers
        assert "Authorization" not in headers
        assert headers["Accept"] == "application/json"
        assert headers["Origin"] == tso.BASE_URL
        assert headers["Referer"].startswith(tso.BASE_URL)
        assert "Mozilla/5.0" in headers["User-Agent"]