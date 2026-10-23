"""Pin the exact ``fast_info`` key set, and the four ``info`` keys whose source may be absent.

WHY AN EXACT SET
----------------
``FastInfo`` is a drop-in for yfinance's ``t.fast_info`` and is used two ways: attribute
reads, and the mapping protocol (``dict(t.fast_info)``). A key added to ``keys()`` without a
readable value breaks the second silently, so the set is pinned *exactly*: growing the
surface has to be a deliberate edit here, never a side effect of adding a property.

WHY ABSENCE IS ASSERTED FOR THE ``info`` KEYS
---------------------------------------------
``regularMarketChange``, ``lastFiscalYearEnd``, ``nextFiscalYearEnd`` and
``mostRecentQuarter`` are derivable from what ``build_info_dict`` already receives, but only
when the source exists. ``key in info`` is how callers test availability, so a missing
source must **omit** the key; a published ``None`` would be a permanent stand-in that reads
as "known to be unknown" and can never resolve. Both directions are asserted below. The
epoch values are the ones measured against yfinance 1.7.0 (``docs/INFO_KEY_GAP.md``).

No network anywhere in this module: every profile and frame is built by hand.
"""

import pandas as pd
import pytest

from bfinance.market.fast_info import FastInfo
from bfinance.market.quotes import INFO_VALUE_UNITS, QuoteEngine
from bfinance.models.company import CompanyProfile, FinancialStatement, TopRatios

#: The exact key set ``bfinance``'s FastInfo publishes, snake_case as yfinance 1.7.0 names
#: them. The last four are the keys added in F2; the rest predate it.
EXPECTED_FAST_INFO_KEYS = {
    "currency",
    "exchange",
    "timezone",
    "quote_type",
    "last_price",
    "regular_market_price",
    "last_volume",
    "previous_close",
    "open",
    "day_high",
    "day_low",
    "year_high",
    "year_low",
    "market_cap",
    "shares",
    "fifty_day_average",
    "two_hundred_day_average",
    "regular_market_previous_close",
    "ten_day_average_volume",
    "three_month_average_volume",
    "year_change",
}

#: Measured against yfinance 1.7.0 on RELIANCE.NS, epoch seconds at UTC midnight:
#: ``"Mar 2026"`` -> 1774915200, one year on -> 1806451200, ``"Jun 2026"`` -> 1782777600.
EXPECTED_EPOCHS = {
    "lastFiscalYearEnd": 1774915200,
    "nextFiscalYearEnd": 1806451200,
    "mostRecentQuarter": 1782777600,
}

ABSENT_WHEN_UNSOURCED = tuple(EXPECTED_EPOCHS) + ("regularMarketChange",)


def _profile(ratios=None, **kwargs):
    return CompanyProfile(
        symbol="TEST",
        company_id=0,
        name="Test Ltd",
        ratios=ratios if ratios is not None else TopRatios(),
        **kwargs,
    )


def _ohlcv(closes, volumes=None):
    n = len(closes)
    return pd.DataFrame(
        {"Close": list(closes), "Volume": list(volumes) if volumes is not None else [1_000] * n},
        index=pd.date_range("2025-01-01", periods=n, freq="D"),
    )


def _info(ratios=None, history=None, **kwargs):
    """A real ``info`` dict from hand-built inputs -- never fetched."""
    kwargs.setdefault(
        "profit_loss", FinancialStatement(headers=["Mar 2025", "Mar 2026"], rows={})
    )
    kwargs.setdefault(
        "quarters", FinancialStatement(headers=["Mar 2026", "Jun 2026"], rows={})
    )
    return QuoteEngine.build_info_dict(
        _profile(ratios, **kwargs),
        latest_price=(ratios.current_price if ratios is not None else None),
        history=history,
    )


# ================================================================ the pinned fast_info set

def test_fast_info_key_set_is_exactly_this():
    """Adding a key is a test failure, so the surface only grows deliberately."""
    fi = FastInfo(_profile(TopRatios(current_price=110.0, market_cap=1_000.0)))
    assert set(fi.keys()) == EXPECTED_FAST_INFO_KEYS
    assert set(fi.to_dict()) == EXPECTED_FAST_INFO_KEYS
    assert set(iter(fi)) == EXPECTED_FAST_INFO_KEYS
    assert len(fi) == len(EXPECTED_FAST_INFO_KEYS)


def test_dict_round_trips_every_key():
    """``dict(fast_info)`` is the yfinance-swap path this class was missing."""
    fi = FastInfo(
        _profile(TopRatios(current_price=110.0, market_cap=1_000.0, high_52w=120.0, low_52w=90.0)),
        history=_ohlcv([100.0, 110.0], volumes=[900, 1_100]),
    )
    as_dict = dict(fi)
    assert set(as_dict) == EXPECTED_FAST_INFO_KEYS
    for key in EXPECTED_FAST_INFO_KEYS:
        assert as_dict[key] == fi[key], key
    assert list(fi.values()) == [as_dict[key] for key in fi.keys()]  # noqa: SIM118
    assert dict(fi.items()) == as_dict


def test_contains_and_get_separate_present_from_absent_keys():
    fi = FastInfo(_profile(TopRatios(current_price=110.0)), history=_ohlcv([100.0, 110.0]))
    assert "last_price" in fi
    assert "_profile" not in fi  # an attribute, not a declared mapping key
    assert fi.get("last_price") == fi["last_price"] == 110.0
    assert fi.get("not_a_key") is None
    assert fi.get("not_a_key", 42) == 42
    with pytest.raises(KeyError):
        fi["not_a_key"]


def test_every_enumerated_key_is_readable_through_getitem():
    """Negative control: ``keys()`` may not name a key ``[]`` cannot read.

    Built on the emptiest input the constructor accepts -- no history and an empty profile,
    which is where ``ten_day_average_volume``, ``year_change`` and friends return their
    honest None. If a key were added to the mapping without a property behind it, this loop
    is what fails; if ``__len__`` drifted from ``keys()``, the length assertion fails.
    """
    fi = FastInfo(_profile(), latest_price=None, history=None)
    for key in fi.keys():  # noqa: SIM118 - keys() itself is the subject of this test
        fi[key]  # raises AttributeError/KeyError if keys() names an unreadable key
    assert len(fi) == len(fi.keys())
    assert dict(fi)  # the very call that used to raise TypeError


def test_getitem_refuses_an_attribute_that_is_not_a_mapping_key():
    """The other half of the negative control: an existing attribute is not a promise."""
    fi = FastInfo(_profile())
    assert hasattr(fi, "_history")
    with pytest.raises(KeyError):
        fi["_history"]


# =========================================================== the arithmetic on the frame

def test_regular_market_previous_close_is_the_previous_close_alias():
    fi = FastInfo(_profile(), history=_ohlcv([100.0, 110.0]))
    assert fi.regular_market_previous_close == fi.previous_close == 100.0


def test_ten_day_average_volume_uses_the_last_ten_bars():
    volumes = list(range(1, 16))  # 15 bars
    fi = FastInfo(_profile(), history=_ohlcv([100.0] * 15, volumes=volumes))
    assert fi.ten_day_average_volume == int(sum(volumes[-10:]) / 10)
    assert fi.three_month_average_volume is None  # 15 bars cannot be an average of 63


def test_three_month_average_volume_uses_the_last_sixty_three_bars():
    volumes = list(range(1, 71))  # 70 bars
    fi = FastInfo(_profile(), history=_ohlcv([100.0] * 70, volumes=volumes))
    assert fi.three_month_average_volume == int(sum(volumes[-63:]) / 63)


def test_volume_averages_are_none_without_a_volume_column():
    """A close-only frame (or a bare close list) says nothing about volume.

    ``test_parity_info.py`` passes a plain list of closes to this class, so reading those
    numbers as volumes would publish a price average under a volume name -- fabricated data,
    not a degraded value.
    """
    close_only = pd.DataFrame({"Close": [100.0, 101.0] * 10})
    for history in (close_only, [100.0] * 10):
        fi = FastInfo(_profile(), history=history)
        assert fi.ten_day_average_volume is None
        assert fi.three_month_average_volume is None


def test_year_change_is_the_frame_first_to_last_close():
    fi = FastInfo(_profile(), history=_ohlcv([100.0, 150.0, 120.0]))
    assert fi.year_change == pytest.approx((120.0 - 100.0) / 100.0)


def test_year_change_is_none_on_a_one_bar_frame():
    assert FastInfo(_profile(), history=_ohlcv([100.0])).year_change is None
    assert FastInfo(_profile(), history=None).year_change is None


def test_year_change_documents_the_frame_basis():
    """The window is the frame handed in (~1y, ~247 bars), and the docstring names it.

    The property must not be documented as a measured 52-week change; a caller reading the
    doc has to be able to learn that the basis is first-to-last close of the supplied frame.
    """
    doc = FastInfo.year_change.__doc__ or ""
    assert "earliest close" in doc
    assert "supplied frame" in doc


# ======================================= the four info keys: present-and-typed or absent

def test_fiscal_and_change_keys_are_present_with_the_documented_values():
    info = _info(TopRatios(current_price=110.0), history=_ohlcv([100.0, 110.0]))
    for key, expected in EXPECTED_EPOCHS.items():
        assert key in info, key
        assert isinstance(info[key], int) and not isinstance(info[key], bool), key
        assert info[key] == expected, key
        assert INFO_VALUE_UNITS[key] == "epoch", key
    assert info["regularMarketChange"] == pytest.approx(10.0)  # 110 current - 100 previous
    assert INFO_VALUE_UNITS["regularMarketChange"] == "inr"


def test_no_present_key_is_a_none_stand_in():
    """``key in info`` must mean "a value is available", in every source combination."""
    scenarios = [
        _info(TopRatios(current_price=110.0), history=_ohlcv([100.0, 110.0])),
        _info(TopRatios(current_price=110.0), history=None),
        _info(TopRatios(), history=_ohlcv([100.0, 110.0])),
        _info(TopRatios(), history=None),
    ]
    for info in scenarios:
        for key in ABSENT_WHEN_UNSOURCED:
            assert key not in info or info[key] is not None, key


def test_keys_are_omitted_when_there_are_no_statement_headers():
    info = QuoteEngine.build_info_dict(_profile(TopRatios(current_price=110.0)), history=None)
    for key in ABSENT_WHEN_UNSOURCED:
        assert key not in info, key
    assert info.get("lastFiscalYearEnd") is None  # get's default, not a stored None


def test_missing_quarter_headers_omit_only_most_recent_quarter():
    info = _info(
        TopRatios(current_price=110.0),
        history=_ohlcv([100.0, 110.0]),
        quarters=FinancialStatement(),
    )
    assert "mostRecentQuarter" not in info
    assert info["lastFiscalYearEnd"] == EXPECTED_EPOCHS["lastFiscalYearEnd"]
    assert info["nextFiscalYearEnd"] == EXPECTED_EPOCHS["nextFiscalYearEnd"]
    assert info["regularMarketChange"] == pytest.approx(10.0)


def test_a_one_bar_frame_omits_regular_market_change():
    info = _info(TopRatios(current_price=110.0), history=_ohlcv([110.0]))
    assert "regularMarketChange" not in info
    assert info["lastFiscalYearEnd"] == EXPECTED_EPOCHS["lastFiscalYearEnd"]


def test_a_profile_with_no_price_omits_regular_market_change():
    """cmp falls back to 0.0; ``0.0 - previous_close`` would be a fabricated session change."""
    info = _info(TopRatios(), history=_ohlcv([100.0, 110.0]))
    assert "regularMarketChange" not in info


def test_a_trailing_ttm_header_does_not_move_the_fiscal_year_end():
    """``TTM`` is not a period; the anchor stays on the latest dated annual column."""
    info = _info(
        TopRatios(current_price=110.0),
        history=_ohlcv([100.0, 110.0]),
        profit_loss=FinancialStatement(headers=["Mar 2025", "Mar 2026", "TTM"], rows={}),
    )
    assert info["lastFiscalYearEnd"] == EXPECTED_EPOCHS["lastFiscalYearEnd"]
