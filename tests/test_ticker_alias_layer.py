"""The yfinance spelling layer: every alias is a forwarder, never a second implementation.

yfinance 1.7.0 exposes the same member twice -- ``ticker.info`` and ``ticker.get_info()``,
``ticker.income_stmt`` and ``ticker.incomestmt``, and so on (``yfinance/base.py:234-488``,
``yfinance/ticker.py:213-251``) -- so a drop-in caller may have copied either spelling. The
aliases added for that are only worth having if they are *the same member under another name*:
an alias that re-fetched, re-derived or re-shaped anything would be a second implementation that
drifts from the first, and the drift would be invisible because both spellings claim parity.

This module pins that structurally rather than by comparing return values (a re-implementation
would often agree on one fixture):

* :func:`test_each_alias_touches_its_target_and_nothing_else` walks each alias's **bytecode** and
  requires the only attribute it reads from ``self`` to be its target, and requires it to load no
  module or global at all. ``return self.dividends`` passes; ``return
  CorporateActionsEngine.build_dividends(self._ensure_profile())`` does not.
* :func:`test_a_reimplemented_alias_fails_that_check` is the negative control: a body built the
  wrong way must fail the same predicate, so the predicate cannot pass vacuously.
* :func:`test_each_alias_costs_no_extra_request` warms the target, then compares the request
  recorder before and after the alias call. The recorder sits at the two collaborators a ``Ticker``
  reaches for profile and OHLCV data -- the screener client and the OHLCV engine -- and the NSE
  corporate-actions stub is counted the same way.
  :func:`test_a_reimplemented_alias_costs_a_request` proves the recorder sees a real fetch, so
  "zero extra" is a measurement and not an empty list.

**``ttm_financials`` / ``ttm_incomestmt`` are absent on purpose.** yfinance spells them as
forwarders to ``ttm_income_stmt``, and in this package that property raises
``NotImplementedError`` because no quarterly/TTM statement is served. An alias to a raiser only
moves the failure one name further from its cause, so they are not added.
:func:`test_the_ttm_spellings_are_absent_on_purpose` documents the refusal: re-adding one is a
conscious act that has to delete that test, not an oversight.

Everything here is offline. The profile is injected onto a real ``Ticker`` through the private
``_profile`` attribute that ``_ensure_profile()`` short-circuits on, both the screener client and
the OHLCV engine are counting stubs passed through the constructor / instance seams, and NSE's
corporate-actions client is stubbed at ``bfinance.nse.corporate_actions.CorporateActionsClient``
because ``splits`` became a real read of that feed in D6 of the 2026-10-22 yfinance-parity work
(``plans/yfinance-parity-and-speed.md``) -- a bare ``Ticker("X").get_splits()`` now performs a live
request, so without this stub the ``-m "not live"`` selection reaches ``www.nseindia.com``.
"""

import dis
import inspect
from itertools import pairwise
from typing import Any, Dict, List, Tuple

import pandas as pd
import pytest

from bfinance.models.company import CompanyProfile, TopRatios
from bfinance.models.statements import FinancialStatement
from bfinance.nse import corporate_actions as ca
from bfinance.ticker import INFO_HISTORY_COLUMNS, Ticker

#: ``(alias, the member it must forward to)``, in the order yfinance documents them. The four
#: statement spellings come last because they are properties, not ``get_*`` methods.
ALIASES: Tuple[Tuple[str, str], ...] = (
    ("get_info", "info"),
    ("get_fast_info", "fast_info"),
    ("get_calendar", "calendar"),
    ("get_news", "news"),
    ("get_dividends", "dividends"),
    ("get_splits", "splits"),
    ("get_actions", "actions"),
    ("get_capital_gains", "capital_gains"),
    ("get_major_holders", "major_holders"),
    ("get_institutional_holders", "institutional_holders"),
    ("get_mutualfund_holders", "mutualfund_holders"),
    ("get_analyst_price_targets", "analyst_price_targets"),
    ("get_valuation_measures", "valuation_measures"),
    ("incomestmt", "income_stmt"),
    ("balancesheet", "balance_sheet"),
    ("quarterly_incomestmt", "quarterly_income_stmt"),
    ("quarterly_balancesheet", "quarterly_balance_sheet"),
)

#: ``(alias, parameter, default)`` for every keyword the alias must expose with yfinance's own
#: default. ``as_dict`` is only on the three holder tables because it is only on those in
#: ``yfinance/base.py:240-260``. ``period``/``freq``/``periods`` cannot narrow anything here and
#: are documented as such -- they are pinned so the signature stays yfinance's.
SIGNATURES: Tuple[Tuple[str, str, Any], ...] = (
    ("get_news", "count", 10),
    ("get_dividends", "period", "max"),
    ("get_splits", "period", "max"),
    ("get_actions", "period", "max"),
    ("get_capital_gains", "period", "max"),
    ("get_major_holders", "as_dict", False),
    ("get_institutional_holders", "as_dict", False),
    ("get_mutualfund_holders", "as_dict", False),
    ("get_valuation_measures", "freq", "quarterly"),
    ("get_valuation_measures", "periods", 5),
)

_HEADERS = ["Mar 2023", "Mar 2024"]

#: Two rows in the recorded NSE shape: one bonus, which is a real ``Stock Splits`` factor, and one
#: cash dividend. Both are needed -- a stub returning an empty frame would let ``splits`` and
#: ``actions`` agree while neither carried a value.
_ACTION_ROWS: List[Dict[str, Any]] = [
    {
        "symbol": "TEST",
        "subject": "Bonus 1:1",
        "exDate": "24-Jul-2024",
        "recDate": "25-Jul-2024",
        "isin": "INE000A01001",
        "faceVal": "10",
    },
    {
        "symbol": "TEST",
        "subject": "Interim Dividend - Rs 2.50 Per Share",
        "exDate": "10-Jun-2025",
        "recDate": "11-Jun-2025",
        "isin": "INE000A01001",
        "faceVal": "10",
    },
]

#: Where the fake feed reports its own URL; carried into the frames' ``bfinance_source_url``.
_FEED_URL = "https://nse.test/corporate-actions"

#: The alias targets that read the corporate-actions feed -- the two the stub exists for.
#: ``dividends`` is not one of them: it is the Screener.in fiscal-year-end approximation.
_FEED_TARGETS = ("splits", "actions")


# --------------------------------------------------------------- offline fixtures


def _history_frame() -> pd.DataFrame:
    """A 1y daily OHLCV frame -- the shape ``INFO_HISTORY_COLUMNS`` narrows to Close/Volume."""
    index = pd.date_range("2024-01-01", periods=300, freq="B", tz="Asia/Kolkata", name="Date")
    return pd.DataFrame(
        {
            "Open": [100.0] * 300,
            "High": [110.0] * 300,
            "Low": [95.0] * 300,
            "Close": [100.0 + i * 0.1 for i in range(300)],
            "Volume": [1_000 + i for i in range(300)],
        },
        index=index,
    )


class _Recorder:
    """Profile and history requests, recorded at the two collaborators that make them.

    ``Ticker`` reaches those two kinds of data through exactly two objects: ``screener_client``
    (the profile) and ``ohlcv_engine`` (every history frame). Both are replaced here. The one
    remaining path to data -- NSE's corporate-actions feed, read by ``splits``/``actions``/``isin``
    -- is :class:`_FakeActionsFeed`, which counts its calls the same way, so a count taken around
    an alias call covers every request that call could have caused.

    ``fetch_history`` also models the real engine's ``requires=`` contract: when a caller asks for
    named columns it returns exactly those. That matters for the ``fast_info`` half of this file,
    where the reused frame is the column-narrow one ``info`` asked for.
    """

    def __init__(self, profile: CompanyProfile) -> None:
        self.profile = profile
        self.calls: List[str] = []

    async def get_company_profile(self, symbol: str, force_refresh: bool = False):
        self.calls.append(f"profile:{symbol}")
        return self.profile

    async def fetch_history(self, symbol: str, *args: Any, **kwargs: Any) -> pd.DataFrame:
        self.calls.append(f"history:{symbol}")
        frame = _history_frame()
        requires = kwargs.get("requires")
        return frame[list(requires)] if requires else frame


class _SplitBasisRecorder(_Recorder):
    """The same symbol on two price bases, chosen by the request shape ``info`` uses.

    The narrowed request (``requires=INFO_HISTORY_COLUMNS``) is the as-traded series ``info``
    fetches; the full request is what a plain ``history(period="1y")`` returns, which the engine
    auto-adjusts. The split sits 20 bars before the end, so it moves the 50-day mean, the 200-day
    mean and the year change. A uniform rescale of the whole frame would leave ``year_change``
    unchanged and the ordering test would pass without catching the defect.
    """

    def __init__(self, profile: CompanyProfile) -> None:
        super().__init__(profile)
        self.as_traded = _history_frame()
        self.auto_adjusted = _history_frame()
        # A 2:1 split 20 bars before the end: pre-split closes are halved on the adjusted basis,
        # post-split bars are already as traded.
        self.auto_adjusted.iloc[:-20, :4] = self.auto_adjusted.iloc[:-20, :4] / 2.0
        self.shapes: List[Any] = []

    async def fetch_history(self, symbol: str, *args: Any, **kwargs: Any) -> pd.DataFrame:
        requires = kwargs.get("requires")
        self.calls.append(f"history:{symbol}")
        self.shapes.append(tuple(requires) if requires else None)
        frame = self.as_traded if requires else self.auto_adjusted
        return frame[list(requires)].copy() if requires else frame.copy()


class _FakeActionsFeed:
    """NSE's corporate-actions client seam: one async method, its symbols recorded.

    ``Ticker._corporate_actions_events`` constructs
    ``bfinance.nse.corporate_actions.CorporateActionsClient(...)``, so replacing that class puts
    this object where the real NSE client would have been -- no ``httpx`` transport is involved.
    The frame is built by the package's own parser from the recorded row shape
    ``tests/test_ticker_isin_and_splits.py`` also feeds its fake, so ``splits`` and ``actions``
    read a genuinely parsed frame rather than a hand-assembled one.
    """

    def __init__(self) -> None:
        self.calls: List[Any] = []

    async def corporate_actions_async(self, symbol: Any = None, *args: Any, **kwargs: Any):
        self.calls.append(symbol)
        return ca.parse_corporate_actions(_ACTION_ROWS, symbol=symbol, url=_FEED_URL)


def _actions_feed(monkeypatch: pytest.MonkeyPatch) -> _FakeActionsFeed:
    """Install the fake as the client class and return it, so the test can count its calls."""
    feed = _FakeActionsFeed()
    monkeypatch.setattr(ca, "CorporateActionsClient", lambda **kwargs: feed)
    return feed


def _profile(announcements: int = 3) -> CompanyProfile:
    """A complete-enough profile: statements, holders, dividends and news, all local."""
    return CompanyProfile(
        symbol="TEST",
        company_id=1,
        name="Test Co",
        ratios=TopRatios(
            market_cap=1000.0,
            current_price=250.0,
            high_52w=300.0,
            low_52w=180.0,
            stock_pe=20.0,
            book_value=500.0,
        ),
        quarters=FinancialStatement(
            headers=list(_HEADERS[:1]),
            rows={"Sales": [250.0], "Net Profit": [25.0]},
        ),
        profit_loss=FinancialStatement(
            headers=list(_HEADERS),
            rows={
                "Sales": [1000.0, 1200.0],
                "Net Profit": [100.0, 130.0],
                # Needed for `dividends` -> `actions` -> `calendar` to carry a real ex-date.
                "Dividend Payout %": [20.0, 25.0],
                "EPS in Rs": [10.0, 13.0],
            },
        ),
        balance_sheet=FinancialStatement(
            headers=list(_HEADERS),
            rows={"Equity Capital": [100.0, 100.0], "Borrowings": [40.0, 40.0]},
        ),
        cash_flow=FinancialStatement(
            headers=list(_HEADERS),
            rows={"Cash from Operating Activity": [90.0, 110.0]},
        ),
        shareholding=FinancialStatement(
            headers=list(_HEADERS),
            rows={"Promoters": [50.0, 50.0], "FIIs": [20.0, 21.0], "DIIs": [10.0, 11.0]},
        ),
        announcements=[
            {"title": f"Announcement {i}", "url": f"https://example.test/{i}"}
            for i in range(announcements)
        ],
    )


def _ticker(announcements: int = 3) -> Tuple[Ticker, _Recorder]:
    """A real ``Ticker`` whose two data collaborators are the recorder. No network, ever."""
    recorder = _Recorder(_profile(announcements))
    ticker = Ticker("TEST", cache_ttl_hours=0, screener_client=recorder)
    # `_ensure_profile` returns this without a request: nothing below is measuring the profile.
    ticker._profile = recorder.profile
    ticker.ohlcv_engine = recorder
    return ticker, recorder


def _split_basis_ticker() -> Tuple[Ticker, _SplitBasisRecorder]:
    """A ``Ticker`` whose history stub answers the two call shapes with different price bases."""
    recorder = _SplitBasisRecorder(_profile())
    ticker = Ticker("TEST", cache_ttl_hours=0, screener_client=recorder)
    ticker._profile = recorder.profile
    ticker.ohlcv_engine = recorder
    return ticker, recorder


# --------------------------------------------------------------- the delegation predicate


def _function_of(descriptor: Any):
    """The plain function behind a method or a property, so bytecode can be read either way."""
    function = descriptor.fget if isinstance(descriptor, property) else descriptor
    return getattr(function, "__func__", function)


def _self_attributes(descriptor: Any) -> List[str]:
    """Attributes read directly off ``self`` in this alias's body, in bytecode order.

    An exact forwarder reads exactly one: its target. A re-implementation that reaches for
    ``self._ensure_profile()`` or ``self.trendlyne_news()`` reads that instead, and the list says
    which one, so the failure names the defect rather than saying "not a forwarder".
    """
    instructions = list(dis.get_instructions(_function_of(descriptor)))
    return [
        current.argval
        for previous, current in pairwise(instructions)
        if current.opname in ("LOAD_ATTR", "LOAD_METHOD")
        and previous.opname == "LOAD_FAST"
        and previous.argval == "self"
    ]


def _globals_loaded(descriptor: Any) -> List[str]:
    """Global/module names the body loads. A forwarder needs none; a re-derivation needs several."""
    return [
        instruction.argval
        for instruction in dis.get_instructions(_function_of(descriptor))
        if instruction.opname in ("LOAD_GLOBAL", "LOAD_NAME", "IMPORT_NAME", "IMPORT_FROM")
    ]


# --------------------------------------------------------------- structural: exact forwarders


@pytest.mark.parametrize("alias,target", ALIASES)
def test_each_alias_touches_its_target_and_nothing_else(alias: str, target: str) -> None:
    descriptor = getattr(Ticker, alias)

    assert _self_attributes(descriptor) == [target], (
        f"Ticker.{alias} reads {_self_attributes(descriptor)!r} off self, but a forwarder may "
        f"read only {target!r}; anything else is a second implementation"
    )
    assert _globals_loaded(descriptor) == [], (
        f"Ticker.{alias} loads {_globals_loaded(descriptor)!r}; a forwarder derives nothing itself"
    )


def test_a_reimplemented_alias_fails_that_check() -> None:
    """Negative control. If this body passed the predicate above, the predicate asserts nothing."""

    class _Reimplemented:
        # The shape the predicate exists to reject: it builds its own answer from another member.
        def get_news(self, count: int = 10) -> List[Dict[str, Any]]:
            return [
                {"id": f"own-{i}", "content": {"title": item["content"]["title"]}}
                for i, item in enumerate(self.news[:count])
            ]

    assert _self_attributes(_Reimplemented.get_news) == ["news"], (
        "the control must be caught for the reason a real re-implementation would be"
    )
    assert _globals_loaded(_Reimplemented.get_news) != []
    assert _self_attributes(_Reimplemented.get_news) != ["get_news"]


@pytest.mark.parametrize("alias,target", ALIASES)
def test_each_alias_exists_under_both_spellings(alias: str, target: str) -> None:
    assert hasattr(Ticker, alias), f"yfinance spelling {alias} is missing"
    assert hasattr(Ticker, target), f"the member {alias} forwards to is missing"


@pytest.mark.parametrize("alias,parameter,default", SIGNATURES)
def test_each_alias_carries_yfinance_keyword_and_default(
    alias: str, parameter: str, default: Any
) -> None:
    parameters = inspect.signature(getattr(Ticker, alias)).parameters
    assert parameter in parameters, f"{alias} does not accept yfinance's {parameter}= keyword"
    assert parameters[parameter].default == default


# --------------------------------------------------------------- behavioural: same answer


def _read(owner: Ticker, name: str) -> Any:
    """Read one yfinance member: call it when it is a ``get_*`` method, evaluate it otherwise."""
    value = getattr(owner, name)
    return value if isinstance(getattr(Ticker, name), property) else value()


def _outcome(thunk) -> Tuple[str, Any]:
    """``("value", result)`` or ``("raised", exception type)`` -- both are answers to compare.

    A delegating alias must fail exactly where its target fails (``quarterly_balancesheet`` is the
    live case: bfinance serves no quarterly balance sheet), so the exception type is compared
    instead of the value, and the broad catch is deliberate: the type *is* the assertion.
    """
    try:
        return "value", thunk()
    except Exception as exc:  # noqa: BLE001 - the exception type is the assertion
        return "raised", type(exc)


def _assert_same(actual: Any, expected: Any) -> None:
    if isinstance(expected, pd.DataFrame):
        pd.testing.assert_frame_equal(actual, expected)
    elif isinstance(expected, pd.Series):
        pd.testing.assert_series_equal(actual, expected)
    else:
        assert actual == expected


@pytest.mark.parametrize("alias,target", ALIASES)
def test_each_alias_answers_exactly_as_its_target(
    alias: str, target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticker, _ = _ticker()
    feed = _actions_feed(monkeypatch)

    target_kind, target_answer = _outcome(lambda: _read(ticker, target))
    alias_kind, alias_answer = _outcome(lambda: _read(ticker, alias))

    assert alias_kind == target_kind, (
        f"{alias} answered {alias_kind}, {target} answered {target_kind}"
    )
    if target_kind == "raised":
        assert alias_answer is target_answer
    else:
        _assert_same(alias_answer, target_answer)
        if target in _FEED_TARGETS:
            # The stub's feed must give the target a real row: two empty answers compare equal
            # for the wrong reason, and would pass even if the stub returned nothing at all.
            assert not target_answer.empty, (
                f"the stubbed feed must give {target} a real row, or equality proves nothing"
            )

    assert len(feed.calls) <= 1, (
        f"{alias} and {target} cost {len(feed.calls)} corporate-actions requests; the first "
        "of them pays one and the other reads the memo, so a forwarder costs at most one for "
        "the pair"
    )


def test_targets_that_memoise_return_the_same_object() -> None:
    """Where the target holds one instance, the alias must hand back that instance, not a copy."""
    ticker, _ = _ticker()

    info = ticker.info  # populates the memo on the first access, as `info` always did
    assert ticker.get_info() is info

    fast = ticker.fast_info
    assert ticker.get_fast_info() is fast

    # `analyst_price_targets` is None without `trendlyne_sections=True`; the alias must not
    # turn that absence into an empty dict.
    assert ticker.analyst_price_targets is None
    assert ticker.get_analyst_price_targets() is None


def test_get_news_honours_count_as_a_cap() -> None:
    """yfinance's ``count`` is the request's ``snippetCount``; here it caps the same list."""
    ticker, _ = _ticker(announcements=12)

    assert len(ticker.news) == 12
    assert ticker.get_news() == ticker.news[:10], "the default is yfinance's page size"
    assert ticker.get_news(2) == ticker.news[:2]
    assert ticker.get_news(99) == ticker.news
    assert ticker.get_news(0) == []


def test_as_dict_is_honoured_where_yfinance_has_it() -> None:
    ticker, recorder = _ticker()

    major = ticker.major_holders
    assert not major.empty, "the fixture must produce a real table or to_dict() proves nothing"
    _assert_same(ticker.get_major_holders(), major)
    # `NaN == NaN` is False, so a plain dict comparison would fail on the count cell alone;
    # frame equality is the same comparison the value itself is asserted with above.
    pd.testing.assert_frame_equal(
        pd.DataFrame(ticker.get_major_holders(as_dict=True)), pd.DataFrame(major.to_dict())
    )

    for alias, target in (
        ("get_institutional_holders", "institutional_holders"),
        ("get_mutualfund_holders", "mutualfund_holders"),
    ):
        frame = getattr(ticker, target)
        pd.testing.assert_frame_equal(
            pd.DataFrame(getattr(ticker, alias)(as_dict=True)), pd.DataFrame(frame.to_dict())
        )

    # `as_dict` is the value's own `.to_dict()`; it must not become a request.
    assert recorder.calls == [], f"the holder aliases reached the vendor: {recorder.calls}"


def test_the_keywords_that_cannot_narrow_are_disclosed_by_their_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``period``/``freq``/``periods`` are accepted for parity and cannot change the answer.

    That is what the docstrings say, and it is asserted here so the disclosure cannot quietly
    become an unstated difference: a caller passing ``period="1y"`` gets the whole action history
    -- more than they asked for, not a different basis -- and ``periods=0`` still gets the one
    snapshot, because there is no period grid to cap.
    """
    ticker, recorder = _ticker()
    feed = _actions_feed(monkeypatch)

    for alias, target in (
        ("get_dividends", "dividends"),
        ("get_splits", "splits"),
        ("get_actions", "actions"),
        ("get_capital_gains", "capital_gains"),
    ):
        _assert_same(getattr(ticker, alias)(period="1y"), getattr(ticker, target))

    snapshot = ticker.valuation_measures
    for freq, periods in (("yearly", 0), ("trailing", None)):
        _assert_same(ticker.get_valuation_measures(freq=freq, periods=periods), snapshot)

    assert recorder.calls.count("history:TEST") == 1, recorder.calls
    assert len(feed.calls) == 1, (
        f"the feed was read {len(feed.calls)} time(s): get_splits must pay the one request and "
        "get_actions must read the memo it filled, or the alias is refetching"
    )


# --------------------------------------------------------------- requests: zero extra


@pytest.mark.parametrize("alias,target", ALIASES)
def test_each_alias_costs_no_extra_request(
    alias: str, target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticker, recorder = _ticker()
    feed = _actions_feed(monkeypatch)

    # Warm the target first, so the measurement is the alias's own cost and not the target's.
    _outcome(lambda: _read(ticker, target))
    before = len(recorder.calls)
    before_feed = len(feed.calls)
    _outcome(lambda: _read(ticker, alias))
    during = recorder.calls[before:]

    assert during == [], f"Ticker.{alias} made a request its target had already paid for: {during}"
    assert feed.calls[before_feed:] == [], (
        f"Ticker.{alias} re-read the corporate-actions feed its target had already paid for: "
        f"{feed.calls[before_feed:]}"
    )


def test_a_reimplemented_alias_costs_a_request() -> None:
    """Negative control for the recorder: it records a real fetch, so "no extra" is not vacuous."""

    def reimplemented(ticker: Ticker, count: int = 10) -> List[Dict[str, Any]]:
        ticker.history(period="1y")  # the kind of re-fetch a non-delegating alias would make
        return ticker.news[:count]

    ticker, recorder = _ticker()
    before = len(recorder.calls)

    reimplemented(ticker)

    assert len(recorder.calls) - before == 1, (
        f"the recorder saw {len(recorder.calls) - before} request(s); it cannot prove a delta"
    )


# --------------------------------------------------------------- the deliberate refusals


def test_the_ttm_spellings_are_absent_on_purpose() -> None:
    """``ttm_financials``/``ttm_incomestmt`` are not aliases, because their target raises.

    yfinance spells both as ``return self.ttm_income_stmt``. In this package that property raises
    ``NotImplementedError`` -- no quarterly/TTM statement is served -- so an alias would only
    move the failure one name further from the cause. The absence is pinned here so re-adding one
    is a conscious act: this test has to be deleted, and the reason it cites has to be answered.
    """
    ticker, _ = _ticker()

    assert not hasattr(Ticker, "ttm_financials")
    assert not hasattr(Ticker, "ttm_incomestmt")

    # The target they would forward to, and the reason they are absent: it raises today.
    with pytest.raises(NotImplementedError):
        _ = ticker.ttm_income_stmt


# --------------------------------------------------------------- fast_info reuses info's frame


def test_fast_info_after_info_costs_no_history_request() -> None:
    """The measured delta: ``info`` then ``fast_info`` is 1 request, not 2."""
    ticker, recorder = _ticker()

    _ = ticker.info
    assert recorder.calls == ["history:TEST"], f"info should fetch exactly once: {recorder.calls}"

    before = len(recorder.calls)
    fast = ticker.fast_info

    assert recorder.calls[before:] == [], "fast_info refetched a frame info had already memoised"
    assert fast.previous_close is not None
    assert fast.fifty_day_average is not None
    assert fast.last_volume is not None


def test_fast_info_first_still_fetches_its_own_frame() -> None:
    """The reuse is an optimisation on one ordering, not a new dependency on ``info``."""
    ticker, recorder = _ticker()

    fast = ticker.fast_info

    assert recorder.calls == ["history:TEST"], f"fast_info read first must fetch: {recorder.calls}"
    assert fast.previous_close is not None
    assert fast.fifty_day_average is not None
    assert fast.two_hundred_day_average is not None


def test_fast_info_numbers_do_not_depend_on_whether_info_ran_first() -> None:
    """The regression: the fallback fetched an auto-adjusted frame while ``info`` fetched as-traded.

    Both orderings run against a stub that answers the two call shapes with frames on different
    price bases -- a 2:1 split 20 bars before the end, so the 50/200-day means and the year change
    move by a real factor rather than rounding. Before the fix, reading ``fast_info`` first
    computed those three fields from the adjusted frame and disagreed with the ``info``-first
    ordering.
    """
    info_first_ticker, info_first_recorder = _split_basis_ticker()
    _ = info_first_ticker.info
    info_first = info_first_ticker.fast_info

    fast_first_ticker, fast_first_recorder = _split_basis_ticker()
    fast_first = fast_first_ticker.fast_info

    # Both orderings made the same narrowed request `info` makes. `None` here would mean the
    # fallback asked for the full frame, which is the auto-adjusted basis.
    assert info_first_recorder.shapes == [INFO_HISTORY_COLUMNS]
    assert fast_first_recorder.shapes == [INFO_HISTORY_COLUMNS]

    for attribute in ("fifty_day_average", "two_hundred_day_average", "year_change"):
        assert getattr(fast_first, attribute) == pytest.approx(
            getattr(info_first, attribute)
        ), attribute

    # Non-vacuity: the shared number is the as-traded one, and the adjusted frame would have
    # produced a different one for each of the three fields.
    closes = info_first_recorder.as_traded["Close"]
    adjusted = info_first_recorder.auto_adjusted["Close"]
    assert fast_first.fifty_day_average == pytest.approx(float(closes.tail(50).mean()))
    assert fast_first.two_hundred_day_average == pytest.approx(float(closes.tail(200).mean()))
    assert fast_first.year_change == pytest.approx(
        float((closes.iloc[-1] - closes.iloc[0]) / closes.iloc[0])
    )
    assert fast_first.fifty_day_average != pytest.approx(float(adjusted.tail(50).mean()))
    assert fast_first.two_hundred_day_average != pytest.approx(
        float(adjusted.tail(200).mean())
    )
    assert fast_first.year_change != pytest.approx(
        float((adjusted.iloc[-1] - adjusted.iloc[0]) / adjusted.iloc[0])
    )


def test_fast_info_gets_the_memoised_frame_unshrunk_and_unreindexed() -> None:
    """The cross-task contract with ``FastInfo``: daily 1y Close+Volume, passed through as-is."""
    ticker, _ = _ticker()

    _ = ticker.info
    memoised = ticker._history_for_metadata
    fast = ticker.fast_info

    assert list(memoised.columns) == ["Close", "Volume"], (
        "info asks for `requires=INFO_HISTORY_COLUMNS`; the fixture models that narrowing"
    )
    assert fast._history is memoised, (
        "FastInfo must receive the frame itself, not a copy or a reindex"
    )
    assert len(fast._history) == len(_history_frame()) == 300
    assert isinstance(fast._history.index, pd.DatetimeIndex)
