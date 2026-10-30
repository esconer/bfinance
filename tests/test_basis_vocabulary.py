"""The price-basis vocabulary: one basis, one name.

These are the teeth for P0. Every assertion below fails against the tree this change was written
on: the enum's ``SPLIT_ADJUSTED`` was ``"split_adjusted"`` while the price frames and the provider
published ``"as_traded_split_bonus_adjusted"``. Two spellings of one basis is not cosmetic here,
because the entire comparability argument is :meth:`Basis.comparable` doing an identity test
(``sources/model.py``): a source that declared the old spelling could never match a frame carrying
the new one, and nothing raised — the failover would simply skip it, or worse, splice it.
"""

from __future__ import annotations

import pytest

from bfinance.market.ohlcv import PRICE_BASIS_VENDOR_SPLIT_ADJUSTED
from bfinance.sources import DEFAULT, TRENDLYNE_OHLC_PROVIDER, Basis, price_provider
from bfinance.sources.model import basis_from


def test_the_two_names_are_one_member():
    """An alias, not a second basis: same object, same value, one entry in the enum."""
    assert Basis.SPLIT_ADJUSTED is Basis.SPLIT_BONUS_ADJUSTED
    assert Basis.SPLIT_ADJUSTED.value == "as_traded_split_bonus_adjusted"
    assert Basis.SPLIT_ADJUSTED.axis is Basis.SPLIT_BONUS_ADJUSTED.axis


def test_the_old_spelling_is_no_longer_a_value():
    """Nothing can declare ``split_adjusted`` any more, and nothing silently coerces it.

    The refusal matters as much as the absence: :func:`basis_from` accepts a member or its string
    value, so if the old spelling still resolved, a caller would keep working while holding a basis
    no frame produces.
    """
    assert "split_adjusted" not in {member.value for member in Basis}
    with pytest.raises(ValueError):
        basis_from("split_adjusted")


def test_the_split_bonus_row_serves_the_basis_its_provider_publishes():
    """The registry row and the reader that answers it must name the same basis.

    This is the assertion that was missing. Before P0 the row declared ``"split_adjusted"`` and
    ``price_provider.PRICE_BASIS`` stamped ``"as_traded_split_bonus_adjusted"``, so
    ``spec.serves(PRICE_BASIS)`` was False for the one source that produces the frame.
    """
    spec = DEFAULT.source(TRENDLYNE_OHLC_PROVIDER)

    assert spec is not None
    assert spec.wired is True
    assert price_provider.PRICE_BASIS == Basis.SPLIT_BONUS_ADJUSTED.value
    assert spec.serves(price_provider.PRICE_BASIS), (
        "the row must accept the exact string its own provider stamps on every frame, or the "
        "cascade skips the source before it is ever called"
    )
    assert not spec.serves(Basis.AS_TRADED), (
        "declaring it as_traded is what would let a failover splice an adjusted series onto the tape"
    )


def test_the_frame_constant_is_the_enum_value():
    """The value written into ``attrs["price_basis"]`` is a registered basis, not a free string.

    ``market/ohlcv.py`` cannot import ``bfinance.sources`` (that package is opt-in and this module
    loads with the base import), so the constant is a literal there. This test is the link that
    keeps the literal and the enum from drifting.
    """
    assert PRICE_BASIS_VENDOR_SPLIT_ADJUSTED == Basis.SPLIT_BONUS_ADJUSTED.value
    assert basis_from(PRICE_BASIS_VENDOR_SPLIT_ADJUSTED) is Basis.SPLIT_BONUS_ADJUSTED
