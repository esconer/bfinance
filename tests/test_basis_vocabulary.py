"""The price-basis vocabulary: one basis, one name, and every stamped basis is in it.

Two defects, both silent, both found by asserting on the *strings that actually travel*.

**One basis, two names.** Every assertion in the first block fails against the tree P0 was written
on: the enum's ``SPLIT_ADJUSTED`` was ``"split_adjusted"`` while the price frames and the provider
published ``"as_traded_split_bonus_adjusted"``. That is not cosmetic here, because the entire
comparability argument is :meth:`Basis.comparable` doing an identity test
(``sources/model.py``): a source that declared the old spelling could never match a frame carrying
the new one, and nothing raised — the failover would simply skip it, or worse, splice it.

**A stamped basis nobody can declare.** The second block walks every ``PRICE_BASIS_*`` constant the
price layer can write into ``attrs["price_basis"]`` and requires each value to be a registered
``Basis`` unless it is named in ``BASIS_VALUES_DELIBERATELY_OUTSIDE_THE_VOCABULARY``. It caught
``as_traded_share_count_adjusted``: bfinance stamped it and ``basis_from`` refused it, so a source
declaring its own library's basis was told that basis does not exist. The one exception is the
synthetic screener value, which is a fabrication warning and not an adjustment state.
"""

from __future__ import annotations

import pytest

from bfinance.market import ohlcv as ohlcv_module
from bfinance.market.ohlcv import PRICE_BASIS_VENDOR_SPLIT_ADJUSTED
from bfinance.sources import DEFAULT, TRENDLYNE_OHLC_PROVIDER, Basis, price_provider
from bfinance.sources.model import BASIS_AXIS, BasisAxis, basis_from


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


#: Every module-level constant the price layer can stamp into ``attrs["price_basis"]``. Found by
#: prefix rather than listed, so a new ``PRICE_BASIS_*`` constant is covered the day it is added.
#: ``_ATTR`` / ``_KEY`` suffixes are excluded because the sibling ``price_provider`` module already
#: names its provenance key ``PRICE_BASIS_ATTR``; a key is not a basis and must not be walked here.
_STAMPABLE = {
    name: value
    for name, value in vars(ohlcv_module).items()
    if name.startswith("PRICE_BASIS_")
    and isinstance(value, str)
    and not name.endswith(("_ATTR", "_KEY"))
}


def test_the_stampable_constants_were_actually_found():
    """Guard against the introspection silently matching nothing after a rename.

    The tests below are only teeth if they are looking at real constants; an empty or partial
    ``_STAMPABLE`` would make them vacuously pass.
    """
    assert len(_STAMPABLE) >= 4, sorted(_STAMPABLE)
    assert {
        "PRICE_BASIS_AS_TRADED",
        "PRICE_BASIS_SHARE_COUNT_ADJUSTED",
        "PRICE_BASIS_VENDOR_SPLIT_ADJUSTED",
        "PRICE_BASIS_SYNTHETIC",
    } <= set(_STAMPABLE)


def test_every_basis_the_price_layer_stamps_is_in_the_vocabulary():
    """A basis bfinance stamps must be a basis a source can declare.

    This is P0's thesis applied to the layer that produces the frames. The point of
    :class:`Basis` is that a source author declares *the basis a frame will carry*; if bfinance
    itself stamps a value that ``basis_from`` refuses, that source's declaration is rejected by
    its own library and the source is silently unreachable.

    It found a real one: ``PRICE_BASIS_SHARE_COUNT_ADJUSTED`` was stamped by
    ``_resolve_adjustment`` while ``as_traded_share_count_adjusted`` was absent from the enum, so
    ``basis_from(frame.attrs["price_basis"])`` raised on a frame bfinance had just built.

    The single deliberate exception is named in the tuple, not omitted: ``PRICE_BASIS_SYNTHETIC``
    records that O/H/L were *derived* from Close, which is a fabrication warning rather than an
    adjustment state, and registering it as a basis would let a source declare synthetic prices
    as a legitimate answer.
    """
    outside = set(ohlcv_module.BASIS_VALUES_DELIBERATELY_OUTSIDE_THE_VOCABULARY)

    for name, value in sorted(_STAMPABLE.items()):
        if value in outside:
            continue
        assert value in {member.value for member in Basis}, (
            f"{name} stamps {value!r}, which is not a Basis value and is not named in "
            f"BASIS_VALUES_DELIBERATELY_OUTSIDE_THE_VOCABULARY -- either register it or say why"
        )
        assert basis_from(value).value == value


def test_the_only_basis_outside_the_vocabulary_is_the_synthetic_one():
    """The exception tuple stays a written decision, not a dumping ground.

    Without this, the previous test is satisfied by adding any inconvenient value to the tuple,
    which would restore exactly the hole it exists to close.
    """
    outside = ohlcv_module.BASIS_VALUES_DELIBERATELY_OUTSIDE_THE_VOCABULARY

    assert outside == (ohlcv_module.PRICE_BASIS_SYNTHETIC,)
    assert ohlcv_module.PRICE_BASIS_SYNTHETIC not in {member.value for member in Basis}, (
        "the synthetic value must stay unregisterable: it is a fabrication warning, not a basis"
    )


def test_the_share_count_basis_is_declarable():
    """The value ``_resolve_adjustment`` stamps for a real share-count factor is a real basis.

    Stated separately from the walk above so the failure names the member rather than a loop
    variable, and so the axis mapping is asserted alongside it: a basis without an axis fails
    ``len(BASIS_AXIS) == len(Basis)`` in ``test_sources_cascade.py``.
    """
    assert Basis.SHARE_COUNT_ADJUSTED.value == ohlcv_module.PRICE_BASIS_SHARE_COUNT_ADJUSTED
    assert basis_from(ohlcv_module.PRICE_BASIS_SHARE_COUNT_ADJUSTED) is Basis.SHARE_COUNT_ADJUSTED
    assert BASIS_AXIS[Basis.SHARE_COUNT_ADJUSTED] is BasisAxis.PRICE
    assert not Basis.SHARE_COUNT_ADJUSTED.comparable(Basis.AS_TRADED), (
        "a share-count-adjusted frame is not the tape; a failover must not splice the two"
    )
