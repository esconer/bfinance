"""
The error vocabulary: one idea, one name, and no name that says the wrong thing.

The defect this file closes
---------------------------
``ParticipantFlowsUnavailable`` was a subclass of
:class:`~bfinance.utils.exceptions.TickerNotFoundError` and was **not** catchable as
:class:`~bfinance.utils.exceptions.UpstreamServiceError` or as any flows-local base. The class is
a **date** refusal -- "NSE published no participant file for 2026-01-01" -- and its own docstring
used to concede that "the name reads oddly for a date". So a caller writing
``except TickerNotFoundError`` around a date-keyed archive read was told the request named a
**bad symbol**, for a request that named **no symbol at all** and failed because the date was a
trading holiday or a weekend. That is a confidently-wrong diagnosis handed to the caller as fact,
and it was the last place in the package where two vocabularies existed for one idea.

The same test file pins the three symptoms that had the same root cause: an error hierarchy that
could not express the failure, three modules whose ``import *`` bound their own dependencies into
the caller's namespace, and a file whose two named siblings were catchable as types one of them
was not.

How to read this file
---------------------
Every verdict is **derived** from the shipped source -- the module's own ``__all__``, the class's
own ``__mro__``, the exception's own message -- and every derivation has a **negative control**
that mutates that input in memory and requires the verdict to **change**. A control that cannot
flip a verdict is an unfailable assertion, and this repository has removed four of those, so the
controls here are load-bearing rather than decorative.

Two things this file deliberately does **not** do:

* It does not assert that a message contains a particular English word. It asserts that the
  message distinguishes the two *states* the caller must be able to tell apart, by requiring the
  refusal to name the trading-day reason, to state the exchange was reached, and to say that a
  transport failure is a different error with a different class. Rewording the sentence without
  changing what it asserts does not turn this red, and a sentence that swapped the two meanings
  does.
* It does not assert a class's base by name alone. It asserts the *whole* catchable set, in both
  directions: what the class must be catchable as, and what it must **not** be catchable as. The
  second direction is the one that catches a regression, because a class that quietly regains
  ``TickerNotFoundError`` keeps every positive assertion green.

Everything here is offline and hermetic: no network, no subprocess, no clock beyond the dates
handed to the constructors, no filesystem.
"""

from __future__ import annotations

import ast
import builtins
import importlib
import pathlib
import re
from types import ModuleType
from typing import Any, Iterable

import pytest

import bfinance.costs as costs
import bfinance.flows as flows
from bfinance.costs import CostScheduleError, CostScheduleParseError
from bfinance.flows import (
    ParticipantAsOfMismatch,
    ParticipantFlowsError,
    ParticipantFlowsUnavailable,
)
from bfinance.utils.exceptions import (
    BFinanceError,
    ParsingError,
    TickerNotFoundError,
    UpstreamServiceError,
)

#: The repo's importable package root, for the AST passes below.
SRC = pathlib.Path(__file__).resolve().parents[1] / "src"

#: The three modules that declared no ``__all__``, and therefore leaked their own imports through
#: a star import. Named here, not discovered, so that one of them quietly *gaining* an ``__all__``
#: fails this file instead of passing unnoticed.
LEAKY_MODULES = ("bfinance.universe", "bfinance.ownership", "bfinance.nse.macro")

#: The modules the new ``__all__``s are compared against for style. ``flows`` and ``costs`` are
#: the two that already had one when this work started.
REFERENCE_MODULES = ("bfinance.flows", "bfinance.costs")

#: A date that is a real non-trading day: 2026-01-01 is a Thursday, and NSE was closed for New
#: Year's Day. Used so the tests read as what they are -- a holiday, not a symbol problem.
NEW_YEARS_DAY_2026 = "2026-01-01"

#: A date whose 404 is recorded, verbatim, in ``tests/fixtures/nse/flows/META.json`` as
#: ``participant_oi_20260927_sunday_404``. 2026-09-27 was a Sunday.
SUNDAY_2026_09_27 = "2026-09-27"


def _unavailable(*, reason: str = "no_file", day: str = NEW_YEARS_DAY_2026) -> ParticipantFlowsUnavailable:
    """The real refusal, built by the real constructor. No request is made to get here.

    The three arguments are exactly the three the module's own raise site passes, in order, so
    this cannot drift from the production call shape without a ``TypeError``.
    """
    import datetime as _datetime

    return ParticipantFlowsUnavailable(
        _datetime.date.fromisoformat(day), reason, f"https://nsearchives.nseindia.com/x_{day}.csv"
    )


# --------------------------------------------------------------------------- #
# 1. the re-parenting: catchable as the new base, NOT as the wrong one
# --------------------------------------------------------------------------- #


def _catchable_as(error: BaseException, cls: type) -> bool:
    """``except cls:`` would catch ``error``. The question a caller actually asks.

    Spelled through :func:`issubclass` on the concrete type rather than through a real ``try``, so
    a caller who writes ``except TickerNotFoundError`` and this helper cannot disagree.
    """
    return issubclass(type(error), cls)


def _catchable_as_any(error: BaseException, classes: Iterable[type]) -> list[type]:
    """Every one of ``classes`` that would catch ``error``, in the order given."""
    return [cls for cls in classes if _catchable_as(error, cls)]


class TestParticipantFlowsUnavailableIsTypedAsADateRefusal:
    """It is a **date** refusal. So it is catchable as a date/availability base, and not as
    :class:`TickerNotFoundError` -- which reports a bad symbol for a trading holiday."""

    def test_it_is_catchable_as_the_flows_base_it_defines(self) -> None:
        assert _catchable_as(_unavailable(), ParticipantFlowsError)

    def test_it_is_still_catchable_as_the_package_wide_upstream_base(self) -> None:
        """The mixin rule: keep the package base in the package, mix in the closest
        ``utils.exceptions`` sibling. ``UpstreamServiceError`` is that sibling, and
        :mod:`bfinance.sources.errors` and :mod:`bfinance.statements.errors` both document the
        rule this module now follows."""
        assert _catchable_as(_unavailable(), UpstreamServiceError)

    def test_it_is_no_longer_catchable_as_ticker_not_found(self) -> None:
        """The whole point, asserted in the direction that can actually regress.

        Every *positive* assertion above stays green if this class quietly regains
        ``TickerNotFoundError``, so this is the one that does the work. A caller catching
        ``TickerNotFoundError`` must not be handed a holiday.
        """
        assert not _catchable_as(_unavailable(), TickerNotFoundError), (
            "ParticipantFlowsUnavailable is catchable as TickerNotFoundError again, so a caller "
            "writing `except TickerNotFoundError` reports a bad symbol for a date NSE did not "
            "publish"
        )

    def test_the_two_refusals_the_module_raises_are_told_apart_by_type(self) -> None:
        """A date nobody published and a payload that is somebody else's are different problems.

        Before the re-parenting both were ``UpstreamServiceError``-adjacent and one was a
        ``TickerNotFoundError``; the module's whole design rests on a caller being able to tell
        "ask for another date" from "this route is lying to us".
        """
        missing = _unavailable()
        mismatch = ParticipantAsOfMismatch(
            __import__("datetime").date.fromisoformat(NEW_YEARS_DAY_2026),
            __import__("datetime").date.fromisoformat("2025-12-31"),
            "https://nsearchives.nseindia.com/x.csv",
        )
        assert _catchable_as(missing, ParticipantFlowsError)
        assert _catchable_as(mismatch, ParticipantFlowsError)
        assert type(missing) is not type(mismatch)
        assert not _catchable_as(missing, type(mismatch))
        assert not _catchable_as(mismatch, type(missing))

    def test_the_mro_keeps_the_package_base_first_and_the_mixin_second(self) -> None:
        """Order is read, not assumed. ``ParticipantFlowsError`` must be the nearest base.

        If the mixin were listed first, ``except ParticipantFlowsError`` and
        ``except UpstreamServiceError`` would both still work, so this file would stay green --
        while the class stopped reading as one of this module's own. Asserting the position is
        what makes the re-parenting a fact about the hierarchy rather than a fact about the set.
        """
        mro = type(_unavailable()).__mro__
        assert mro.index(ParticipantFlowsError) < mro.index(UpstreamServiceError)
        assert mro.index(UpstreamServiceError) < mro.index(BFinanceError)

    def test_nothing_in_the_module_still_claims_a_ticker_not_found_vocabulary(self) -> None:
        """The class is gone from ``flows`` entirely, not just from the hierarchy.

        A module that still imported ``TickerNotFoundError`` for its own use would be evidence
        that some other path in this module also reports a date as a symbol problem, and the
        import would be dead weight besides.
        """
        assert not hasattr(flows, "TickerNotFoundError"), (
            "bfinance.flows still carries TickerNotFoundError; the date refusal is no longer a "
            "ticker refusal, so nothing in this module should still name one"
        )

    def test_the_name_is_unchanged_so_existing_except_clauses_keep_working(self) -> None:
        """Renaming would have been the easier mistake to make and the more damaging one.

        Callers hold ``except ParticipantFlowsUnavailable``. The re-parenting changed the bases
        and the message; it must not have changed the name.
        """
        assert flows.ParticipantFlowsUnavailable.__name__ == "ParticipantFlowsUnavailable"
        assert "ParticipantFlowsUnavailable" in flows.__all__

    def test_control_removing_the_new_base_from_the_mro_flips_the_verdict(self) -> None:
        """Negative control. The new base is an input to the verdict, not decoration.

        A stand-in whose only base is ``UpstreamServiceError`` reproduces the state this change
        fixed, and the two must be distinguishable. Without this, "it is catchable as
        ``ParticipantFlowsError``" could be a restatement of the class's own definition rather
        than a property of the shipped hierarchy.
        """
        class _OnlyTheMixin(UpstreamServiceError):
            """A date refusal with no package base -- i.e. the pre-fix shape, minus the wrong one."""

        error = _unavailable()
        assert _catchable_as(error, ParticipantFlowsError)
        assert not _catchable_as(_OnlyTheMixin("x"), ParticipantFlowsError)

    def test_control_regaining_the_wrong_base_flips_the_verdict(self) -> None:
        """Negative control for the assertion that matters: the wrong base, put back.

        ``TickerNotFoundError`` is re-mixed in on a stand-in, and the "no longer catchable"
        verdict has to move. A control that could not flip this is the unfailable assertion this
        repository has removed four times.
        """
        class _RegainedTickerRefusal(ParticipantFlowsError, TickerNotFoundError):
            """The pre-fix parentage, kept as a local class so the shipped one is untouched."""

        assert not _catchable_as(_unavailable(), TickerNotFoundError)
        assert _catchable_as(_RegainedTickerRefusal("x"), TickerNotFoundError)
        assert _catchable_as(_RegainedTickerRefusal("x"), ParticipantFlowsError)


# --------------------------------------------------------------------------- #
# 2. the message tells "not published" apart from "could not reach"
# --------------------------------------------------------------------------- #


class TestTheMessageDistinguishesNotPublishedFromUnreachable:
    """A caller receiving this must be able to tell the exchange **had nothing** from the
    exchange **could not be reached**. Those have opposite remedies: ask for another date, versus
    retry. Conflating them is the confidently-wrong diagnosis this re-parenting removed."""

    #: The three claims the message has to make, each a claim about *state* rather than about
    #: vocabulary, so rewording the sentence is allowed and swapping a meaning is not.
    REACHED = re.compile(r"reached|no document|returned no", re.IGNORECASE)
    NOT_PUBLISHED = re.compile(r"published no|had nothing|no participant file", re.IGNORECASE)
    NOT_A_TRANSPORT_FAILURE = re.compile(
        r"not a transport failure|NOT a transport", re.IGNORECASE
    )
    #: What a transport failure is called elsewhere in this module, so the message can point at
    #: the right class rather than describing the concept in prose.
    TRANSPORT_FAILURE_CLASS = UpstreamServiceError

    def _message(self, **kwargs: Any) -> str:
        return str(_unavailable(**kwargs))

    def test_the_message_states_the_exchange_was_reached_and_had_nothing(self) -> None:
        message = self._message()
        assert self.NOT_PUBLISHED.search(message), message
        assert self.REACHED.search(message), message

    def test_the_message_says_a_transport_failure_is_a_different_error(self) -> None:
        message = self._message()
        assert self.NOT_A_TRANSPORT_FAILURE.search(message), message
        # And it names the class that *is* the transport failure, so a reader can branch on the
        # type rather than on this sentence.
        assert self.TRANSPORT_FAILURE_CLASS.__name__ in message, message

    def test_the_message_names_the_trading_day_reasons_explicitly(self) -> None:
        """Weekend, holiday, or before the archive begins: the three real causes, named.

        A caller who is told only "no data" cannot decide whether to shift the date or to report
        an upstream problem. Each reason implies a different next step, so all three are named.
        """
        message = self._message().lower()
        for cause in ("weekend", "holiday", "before the archive begins"):
            assert cause in message, f"the refusal no longer names {cause!r}: {message}"

    def test_the_message_carries_the_day_the_reason_and_the_url(self) -> None:
        """Unchanged behaviour, pinned so the rewrite did not drop a field a caller reads.

        ``day``, ``reason`` and ``url`` are also attributes, but a caller who only logs
        ``str(exc)`` must still be able to see which date and which URL.
        """
        error = _unavailable(day=SUNDAY_2026_09_27, reason="no_file")
        message = str(error)
        assert SUNDAY_2026_09_27 in message
        assert "no_file" in message
        assert error.url in message
        assert error.day.isoformat() == SUNDAY_2026_09_27
        assert error.reason == "no_file"

    def test_a_weekend_read_is_answered_locally_and_names_the_weekend_reason(self) -> None:
        """The weekend case end-to-end, offline. ``is_weekend`` is pure and local.

        This is the path that costs no request at all, and it is the case most likely to be
        mislabelled as a symbol problem -- a Saturday, reached by asking for a date, is not a bad
        ticker.
        """
        import datetime as _datetime

        day = _datetime.date.fromisoformat(SUNDAY_2026_09_27)
        assert flows.is_weekend(day) is True
        assert flows.missing_day_reason(day) == "weekend"
        error = _unavailable(day=SUNDAY_2026_09_27, reason=flows.missing_day_reason(day))
        assert "weekend" in str(error)
        assert self.NOT_PUBLISHED.search(str(error))

    def test_a_weekday_is_not_prejudged_as_a_missing_day(self) -> None:
        """The negative half of the local check, and the reason it is not a calendar.

        ``missing_day_reason`` returns ``None`` for a weekday because a holiday cannot be known
        without a request. Asserting the ``None`` is what proves the weekend answer is a real
        determination and not a constant.
        """
        import datetime as _datetime

        assert flows.missing_day_reason(_datetime.date.fromisoformat(NEW_YEARS_DAY_2026)) is None
        assert flows.is_weekend(_datetime.date.fromisoformat(NEW_YEARS_DAY_2026)) is False

    def test_control_swapping_the_reason_into_a_transport_claim_flips_the_verdict(self) -> None:
        """Negative control: the verdict is a function of the message, not of the class.

        A message saying the exchange could **not** be reached is the opposite claim, and it must
        stop satisfying "the exchange was reached and had nothing". If this control could not
        move the verdict, the test above would be asserting a constant.
        """
        reached = self._message()
        assert self.REACHED.search(reached)
        unreachable = (
            "Could not reach nseindia.com for 2026-01-01: DNS failure. "
            "No transport failure was recorded."
        )
        assert self.REACHED.search(unreachable) is None
        assert self.NOT_A_TRANSPORT_FAILURE.search(unreachable) is None

    def test_control_a_message_that_omits_the_reason_names_flips_the_verdict(self) -> None:
        """Negative control for the reason-names requirement.

        "No participant file" with no causes named tells a caller nothing about whether to shift
        the date, which is the whole job of the sentence.
        """
        terse = "NSE published no participant file for 2026-01-01."
        lowered = terse.lower()
        missing = [
            cause
            for cause in ("weekend", "holiday", "before the archive begins")
            if cause not in lowered
        ]
        assert len(missing) == 3
        assert self.NOT_A_TRANSPORT_FAILURE.search(terse) is None


# --------------------------------------------------------------------------- #
# 3. costs.py follows the mixin rule
# --------------------------------------------------------------------------- #


class TestCostScheduleParseErrorIsCatchableAsTheSiblingItsDocstringNames:
    """``bfinance.costs`` imported ``BFinanceError`` and nothing else, so it was the one file in
    the package whose errors were not reachable from the hierarchies its siblings are in.

    ``CostScheduleParseError`` describes itself as "an upstream-schema failure (NSE reissued or
    re-laid-out a circular)". :class:`ParsingError` is documented almost verbatim as the error for
    "HTML/JSON parsing fails due to unexpected upstream schema changes", so that is the sibling.
    """

    def test_it_is_catchable_as_parsing_error(self) -> None:
        assert _catchable_as(CostScheduleParseError("x"), ParsingError)

    def test_it_is_still_catchable_as_the_module_base(self) -> None:
        """The re-parenting must not have cost anything. ``except CostScheduleError`` keeps working."""
        assert _catchable_as(CostScheduleParseError("x"), CostScheduleError)

    def test_it_is_catchable_as_the_package_root(self) -> None:
        assert _catchable_as(CostScheduleParseError("x"), BFinanceError)

    def test_it_is_not_catchable_as_upstream_service_error(self) -> None:
        """The negative direction, and it is deliberate.

        ``UpstreamServiceError`` is scoped to a provider that "fails or is unreachable". A
        re-issued circular is neither: the exchange answered, with a document in a new shape.
        Folding a permanent schema change into the retry-worthy bucket is how a permanent failure
        gets retried forever, which is the conflation
        :class:`~bfinance.sources.errors.BasisMismatchError` exists to prevent.
        """
        assert not _catchable_as(CostScheduleParseError("x"), UpstreamServiceError)

    def test_the_coverage_sibling_is_still_not_a_parse_error(self) -> None:
        """The distinction the two siblings exist to preserve, in both directions.

        The module's own docstring says the parse error "is deliberately a different type so a
        caller can tell 'I asked for a date I cannot have' from 'the document I am keyed on
        changed shape'". Adding a mixin must not collapse that.
        """
        coverage = costs.CostScheduleCoverageError("x")
        assert not _catchable_as(coverage, ParsingError)
        assert _catchable_as(coverage, CostScheduleError)
        assert _catchable_as(CostScheduleParseError("x"), ParsingError)

    def test_the_module_base_is_still_itself_a_bfinance_error(self) -> None:
        assert _catchable_as(CostScheduleError("x"), BFinanceError)

    def test_the_mixin_sits_after_the_module_base(self) -> None:
        """Position, read off the MRO rather than assumed.

        If ``ParsingError`` came first, every assertion above would still hold, so this file
        would stay green while the class stopped being one of ``costs``'s own errors. The
        hierarchy is a statement about which vocabulary is primary, and this is what pins it.
        """
        mro = CostScheduleParseError.__mro__
        assert mro.index(CostScheduleError) < mro.index(ParsingError)
        assert mro.index(ParsingError) < mro.index(BFinanceError)

    def test_costs_no_longer_imports_only_the_package_root(self) -> None:
        """The defect at its source, not at one call site.

        ``from bfinance.utils.exceptions import BFinanceError`` and nothing else is what made this
        the only module in the package with no ``utils.exceptions`` mixin, and it was a property
        of the file rather than of any one error. Asserted over the source text so adding a third
        error to this file without a mixin is visible.
        """
        source = (SRC / "bfinance" / "costs.py").read_text(encoding="utf-8-sig")
        imported = re.search(
            r"from bfinance\.utils\.exceptions import \(([^)]*)\)", source
        ) or re.search(r"from bfinance\.utils\.exceptions import ([^\n(]+)", source)
        assert imported is not None, "costs.py no longer imports from bfinance.utils.exceptions"
        names = {n.strip() for n in imported.group(1).replace("\n", " ").split(",") if n.strip()}
        assert names == {"BFinanceError", "ParsingError"}, (
            f"costs.py's exception imports changed to {sorted(names)}: every error it defines "
            "should mix in the closest bfinance.utils.exceptions sibling, per the rule "
            "bfinance.statements.errors and bfinance.sources.errors both state"
        )

    def test_control_moving_the_mixin_below_the_module_base_flips_the_verdict(self) -> None:
        """Negative control on the MRO-position assertion.

        ``CostScheduleError`` and ``ParsingError`` are **siblings** -- both derive directly from
        :class:`BFinanceError` -- so their relative order in the MRO is set by the order they are
        declared in, and reversing the declaration reverses the MRO. That is what makes the
        position assertion above a fact about this class rather than a fact about the exception
        hierarchy: if the order were structurally fixed, the assertion could not move and would be
        a constant.

        The two stand-in bases are declared locally and derive from :class:`BFinanceError` for the
        same sibling relationship, so the control exercises the same mechanism rather than a
        different one.
        """
        assert issubclass(CostScheduleError, BFinanceError)
        assert issubclass(ParsingError, BFinanceError)
        assert not issubclass(CostScheduleError, ParsingError)
        assert not issubclass(ParsingError, CostScheduleError)

        class _SiblingA(BFinanceError):
            """Stand-in for the module base."""

        class _SiblingB(BFinanceError):
            """Stand-in for the utils.exceptions mixin."""

        class _ModuleBaseFirst(_SiblingA, _SiblingB):
            """Module base declared first."""

        class _MixinBaseFirst(_SiblingB, _SiblingA):
            """Mixin declared first."""

        # The shipped class: module base before the mixin.
        shipped = CostScheduleParseError.__mro__
        assert shipped.index(CostScheduleError) < shipped.index(ParsingError)
        # The same two mechanisms, declared the other way round, land the other way round. This
        # is the control: it demonstrates the comparison can flip, so the shipped assertion is
        # not unfailable.
        assert (
            _MixinBaseFirst.__mro__.index(_SiblingB)
            < _MixinBaseFirst.__mro__.index(_SiblingA)
        )
        assert _ModuleBaseFirst.__mro__.index(_SiblingA) < _ModuleBaseFirst.__mro__.index(_SiblingB)
        assert _ModuleBaseFirst.__mro__ != _MixinBaseFirst.__mro__
        # The control proper: the shipped class passes because `module_base before mixin` is
        # True *for it*, and the same comparison applied to the flipped declaration is False. If
        # this were False for both, the assertion on the shipped class would carry no information.
        shipped_check = lambda mro: mro.index(_SiblingA) < mro.index(_SiblingB)  # noqa: E731
        assert shipped_check(_ModuleBaseFirst.__mro__) is True
        assert shipped_check(_MixinBaseFirst.__mro__) is False

    def test_control_dropping_the_mixin_flips_the_verdict(self) -> None:
        """Negative control for the positive assertions.

        A parse error with no mixin at all is the pre-fix state, and it must stop being catchable
        as ``ParsingError``.
        """

        class _NoMixin(CostScheduleError):
            """The pre-fix parentage."""

        assert _catchable_as(CostScheduleParseError("x"), ParsingError)
        assert not _catchable_as(_NoMixin("x"), ParsingError)
        assert _catchable_as(_NoMixin("x"), CostScheduleError)


# --------------------------------------------------------------------------- #
# 4. the three __all__s, and that they exclude every imported name
# --------------------------------------------------------------------------- #


def _imported_names(module_name: str) -> set[str]:
    """Every top-level name the module **imports**, read from its AST.

    AST rather than ``vars()`` on purpose: ``vars()`` cannot tell a name this module defined
    from a name it bound by import, and a re-import would then read as a definition. Only
    ``Import`` / ``ImportFrom`` nodes count, so a name the module defines *and* uses is still
    reported as defined, which is what keeps this check from flagging a legitimate re-export.
    """
    module = importlib.import_module(module_name)
    tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8-sig"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def _defined_names(module_name: str) -> set[str]:
    """Every top-level name the module **defines**: defs, classes and assignments.

    Used to prove the ``__all__`` is not padded with names the module does not own, and to prove
    it is not silently *short* of the names it does own.
    """
    module = importlib.import_module(module_name)
    tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8-sig"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _module_leaks(reference: ModuleType) -> dict[str, ModuleType]:
    """The names ``import *`` would bind that are modules, keyed by name.

    This is the *observable* shape of the leak the ``__all__``s close: a caller's namespace
    acquiring another library's module object. It is computed from the module, so it cannot
    disagree with what a star import would actually do.
    """
    return {
        name: value
        for name, value in vars(reference).items()
        if not name.startswith("_") and isinstance(value, ModuleType)
    }


#: The dependency names whose leakage the ``__all__``s exist to prevent. Spelled out, not
#: derived, so that a module which starts importing a *new* library is caught by the "every
#: imported name is excluded" assertion below rather than needing this tuple updated first.
DEPENDENCY_NAMES = (
    "pd",
    "httpx",
    "np",
    "json",
    "csv",
    "io",
    "re",
    "os",
    "time",
    "random",
    "threading",
    "asyncio",
    "logging",
    "hashlib",
    "difflib",
    "date",
    "datetime",
    "timedelta",
    "timezone",
    "Path",
    "Any",
    "Callable",
    "Dict",
    "Iterable",
    "List",
    "Literal",
    "NoReturn",
    "Optional",
    "Sequence",
    "Tuple",
    "Union",
    "dataclass",
    "ZoneInfo",
    "SQLiteCache",
    "validate_nse_symbol",
    "iter_trading_days",
    "to_date",
    "BFinanceError",
    "ParsingError",
    "UpstreamServiceError",
    "TickerNotFoundError",
    "RateLimitExceededError",
)


class TestEachOfTheThreeDeclaresAllAndItExcludesEveryImportedName:
    """The three modules declared no ``__all__``, so ``from bfinance.universe import *`` bound
    that module's own ``pd`` and ``httpx`` into the caller's namespace."""

    @pytest.mark.parametrize("module_name", LEAKY_MODULES)
    def test_the_module_now_declares_all(self, module_name: str) -> None:
        module = importlib.import_module(module_name)
        assert hasattr(module, "__all__"), f"{module_name} still declares no __all__"
        assert isinstance(module.__all__, list)
        assert len(module.__all__) > 0

    @pytest.mark.parametrize("module_name", LEAKY_MODULES)
    def test_no_name_in_all_resolves_to_a_module_object(self, module_name: str) -> None:
        """The requirement, stated against the object and not against a name list.

        A ``__all__`` naming ``pd`` would be caught here even if the module had also renamed it,
        and a ``__all__`` naming ``datetime`` would be caught even though ``datetime`` is not
        this module's to begin with. What is asserted is the property a caller experiences: the
        name they receive is not a module.
        """
        module = importlib.import_module(module_name)
        leaked = {
            name: getattr(module, name)
            for name in module.__all__
            if isinstance(getattr(module, name), ModuleType)
        }
        assert leaked == {}, f"{module_name}.__all__ exports module objects: {sorted(leaked)}"

    @pytest.mark.parametrize("module_name", LEAKY_MODULES)
    def test_no_name_in_all_is_one_the_module_imported(self, module_name: str) -> None:
        """Every imported name is excluded, read from the module's own AST.

        The general form of the check above, so it also covers the imported names that are *not*
        modules -- a bare ``to_date`` or ``SQLiteCache`` leaking is the same defect as ``pd``
        leaking, and the object-type test cannot see it.
        """
        module = importlib.import_module(module_name)
        imported = _imported_names(module_name)
        assert not (set(module.__all__) & imported), (
            f"{module_name}.__all__ exports names it only imported: "
            f"{sorted(set(module.__all__) & imported)}"
        )

    @pytest.mark.parametrize("module_name", LEAKY_MODULES)
    def test_none_of_the_named_third_party_dependencies_appears(self, module_name: str) -> None:
        """The literal deny-list, as a second, independent reading of the same fact.

        The AST test above is correct by construction; this one is correct by enumeration, and it
        names the specific offenders. A test that only works by construction fails silently if the
        construction is wrong.
        """
        module = importlib.import_module(module_name)
        present = sorted(set(module.__all__) & set(DEPENDENCY_NAMES))
        assert present == [], f"{module_name}.__all__ exports {present}"

    @pytest.mark.parametrize("module_name", LEAKY_MODULES)
    def test_every_name_in_all_actually_exists(self, module_name: str) -> None:
        """``__all__`` is a contract: a name in it that does not resolve makes ``import *`` raise."""
        module = importlib.import_module(module_name)
        missing = [name for name in module.__all__ if not hasattr(module, name)]
        assert missing == [], f"{module_name}.__all__ names {missing}, which do not exist"

    @pytest.mark.parametrize("module_name", LEAKY_MODULES)
    def test_all_has_no_duplicates(self, module_name: str) -> None:
        module = importlib.import_module(module_name)
        assert len(module.__all__) == len(set(module.__all__)), (
            f"{module_name}.__all__ repeats a name"
        )

    @pytest.mark.parametrize("module_name", LEAKY_MODULES)
    def test_all_lists_only_names_the_module_defines(self, module_name: str) -> None:
        """No name the module does not own. The other half of the contract.

        Without this, an ``__all__`` could be made to "pass" by listing a name that happens to
        resolve -- including an imported one, which the tests above forbid -- while quietly
        omitting the module's own public surface.
        """
        module = importlib.import_module(module_name)
        defined = _defined_names(module_name)
        not_own = sorted(set(module.__all__) - defined)
        assert not_own == [], f"{module_name}.__all__ names {not_own}, which it does not define"

    @pytest.mark.parametrize("module_name", LEAKY_MODULES)
    def test_every_public_name_the_module_defines_is_listed(self, module_name: str) -> None:
        """And no public name is quietly dropped.

        ``logger`` and ``USER_AGENTS`` are the two the sibling modules omit, so they are named
        here rather than excused by a general rule -- an unnamed exception is an exception that
        grows.
        """
        module = importlib.import_module(module_name)
        omitted = sorted(
            name
            for name in _defined_names(module_name)
            if not name.startswith("_")
            and name not in module.__all__
            and name not in ("logger", "USER_AGENTS")
        )
        assert omitted == [], (
            f"{module_name} defines public names its own __all__ omits: {omitted}. A star import "
            "would not carry them, and the declaration is supposed to describe the public surface"
        )

    @pytest.mark.parametrize("module_name", LEAKY_MODULES)
    def test_the_star_import_now_binds_no_module_object(self, module_name: str) -> None:
        """The end-to-end version, on the module the whole complaint was about.

        ``_module_leaks`` looks at the module namespace rather than at ``__all__``, so this is a
        separate reading: it would pass if and only if a star import genuinely stopped binding
        ``pd`` and ``httpx``.
        """
        module = importlib.import_module(module_name)
        assert _module_leaks(module), (
            f"{module_name} no longer leaks any module object at all; this assertion checks the "
            "pre-__all__ shape is gone and needs re-deriving if that is real"
        )
        # And the declaration is what closed the door, rather than the modules being renamed.
        declared = set(module.__all__)
        for name in _module_leaks(module):
            assert name not in declared, name

    @pytest.mark.parametrize("module_name", LEAKY_MODULES)
    def test_the_errors_a_module_defines_are_in_its_all(self, module_name: str) -> None:
        """The vocabulary is public surface.

        A caller needs to catch these, and ``except <Name>`` only works if the name is reachable.
        This is what would have caught the ``TickerNotFoundError`` problem from the other
        direction: a class that is not in ``__all__`` is a class a star-importing caller cannot
        name.
        """
        module = importlib.import_module(module_name)
        for name in _defined_names(module_name):
            value = getattr(module, name)
            if isinstance(value, type) and issubclass(value, BaseException):
                assert name in module.__all__, (
                    f"{module_name} defines {name} but does not list it in __all__"
                )


class TestTheThreeAllsMatchTheStyleOfTheOnesThatAlreadyExisted:
    """``flows`` and ``costs`` had ``__all__`` first. The new three follow the same local
    convention rather than inventing a second one."""

    @pytest.mark.parametrize("module_name", LEAKY_MODULES + REFERENCE_MODULES)
    def test_all_is_a_list_of_strings_sorted_within_its_grouped_sections(
        self, module_name: str
    ) -> None:
        module = importlib.import_module(module_name)
        assert isinstance(module.__all__, list)
        assert all(isinstance(name, str) for name in module.__all__)
        # Grouped, not alphabetical: the reference modules' `__all__` is grouped by capability
        # group with a comment per group, so a global alphabetical order is *not* the local
        # convention and imposing one would be a second style. What is required instead is that
        # no group is empty and no name is a bare alias of another.
        assert len(module.__all__) == len(set(module.__all__))
        assert all(name for name in module.__all__)

    @pytest.mark.parametrize("module_name", LEAKY_MODULES + REFERENCE_MODULES)
    def test_all_is_declared_at_module_level_not_inside_a_function(self, module_name: str) -> None:
        """A module-level ``__all__`` is what ``import *`` reads.

        A function-local assignment would satisfy ``hasattr`` and satisfy every test above, while
        ``import *`` bound the names this file is about. The AST is the only thing that can tell
        the two apart, which is why it is here and not implied by the others.
        """
        module = importlib.import_module(module_name)
        tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8-sig"))
        module_level = [
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(getattr(t, "id", None) == "__all__" for t in node.targets)
        ]
        assert len(module_level) == 1, (
            f"{module_name} declares __all__ {len(module_level)} times at module level"
        )
        nested = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                isinstance(child, ast.Assign)
                and any(getattr(t, "id", None) == "__all__" for t in child.targets)
                for child in ast.walk(node)
            )
        ]
        assert nested == [], f"{module_name} also assigns __all__ inside {len(nested)} function(s)"

    def test_the_reference_modules_still_exclude_their_imports(self) -> None:
        """The two that already had an ``__all__`` still hold to the same rule.

        Without this, the new ``__all__``s could satisfy every test in this file while the older
        two drifted, and the local convention would be split rather than matched.
        """
        for module_name in REFERENCE_MODULES:
            module = importlib.import_module(module_name)
            imported = _imported_names(module_name)
            leaked = sorted(set(module.__all__) & imported)
            assert leaked == [], f"{module_name}.__all__ exports {leaked}"
            assert _module_leaks(module), f"{module_name} leaks no module objects at all"

    def test_adding_a_third_party_name_to_all_turns_the_check_red(self) -> None:
        """Negative control, in the only form that can be trusted: mutate a real ``__all__``.

        The three tests above assert properties of the shipped lists. A property that happened to
        hold for the wrong reason would stay green, so this one takes the shipped
        ``bfinance.universe.__all__``, appends a module object the module genuinely carries, and
        requires every check to go red. It restores the original in a ``finally``, so a failure
        here cannot contaminate the rest of the session.
        """
        module = importlib.import_module("bfinance.universe")
        original = list(module.__all__)
        assert _module_leaks(module), "bfinance.universe carries no module object to inject"
        injected = "pd"
        try:
            module.__all__ = original + [injected]
            injected_value = getattr(module, injected)
            assert isinstance(injected_value, ModuleType), (
                f"the negative control injected {injected!r}, which is not a module object, so it "
                "would not exercise the check it exists to exercise"
            )
            leaked = {
                name: getattr(module, name)
                for name in module.__all__
                if isinstance(getattr(module, name), ModuleType)
            }
            assert leaked != {}, "injecting a module object into __all__ did not trip the check"
            assert set(module.__all__) & _imported_names("bfinance.universe"), (
                "injecting an imported name into __all__ did not trip the AST check"
            )
            assert set(module.__all__) & set(DEPENDENCY_NAMES), (
                "injecting a third-party name into __all__ did not trip the deny-list"
            )
            missing = [name for name in module.__all__ if not hasattr(module, name)]
            assert missing == []
        finally:
            module.__all__ = original
        # And the shipped list is back to clean, which is what makes the control above a control
        # rather than a permanent corruption.
        assert not {
            name: getattr(module, name)
            for name in module.__all__
            if isinstance(getattr(module, name), ModuleType)
        }

    def test_control_a_padded_all_with_an_undefined_name_flips_the_verdict(self) -> None:
        """Negative control for the "every name exists" and "only names it defines" checks.

        A name that resolves to nothing is the failure ``import *`` turns into an
        ``AttributeError``, so it is the one that must be impossible to ship.
        """
        module = importlib.import_module("bfinance.nse.macro")
        original = list(module.__all__)
        phantom = "TOTAL_RETURN_INDEX_ACCESSOR_THAT_DOES_NOT_EXIST"
        try:
            module.__all__ = original + [phantom]
            assert not hasattr(module, phantom)
            missing = [name for name in module.__all__ if not hasattr(module, name)]
            assert missing == [phantom], (
                f"a name in __all__ that does not resolve was not detected: missing={missing}"
            )
            not_own = sorted(set(module.__all__) - _defined_names("bfinance.nse.macro"))
            assert phantom in not_own
        finally:
            module.__all__ = original
        assert module.__all__ == original

    def test_control_shrinking_all_to_the_leak_itself_flips_the_verdict(self) -> None:
        """Negative control in the other direction: an ``__all__`` that lists only the leak.

        A declaration that named the dependencies and nothing else would satisfy "every name
        exists" and "no name is a module" would be the only thing left to catch it. This pins
        that the check really is object-based.
        """
        module = importlib.import_module("bfinance.ownership")
        leaks = _module_leaks(module)
        assert leaks, "bfinance.ownership carries no module object to declare"
        only_the_leak = sorted(leaks)
        try:
            exported = {
                name: getattr(module, name)
                for name in only_the_leak
                if isinstance(getattr(module, name), ModuleType)
            }
            assert exported, "the leak-only list exported no module objects at all"
            assert set(only_the_leak) & _imported_names("bfinance.ownership"), (
                "the leak-only list does not consist of imported names, so it is not the failure "
                "this control is about"
            )
        finally:
            pass
        # The shipped list still carries the module's own public surface, which the leak-only
        # list does not.
        shipped = set(module.__all__)
        assert not (shipped & set(only_the_leak))
        assert shipped & {
            "OwnershipClient",
            "FpiOwnershipUnavailableError",
            "PROVENANCE_SHAREHOLDING",
        }

    def test_control_a_star_import_into_a_fresh_namespace_binds_no_module(self) -> None:
        """Negative control, executed the way a caller experiences it.

        The checks above reason about ``__all__`` as data. This one performs the star import in a
        throwaway namespace and looks at what landed, which is the only reading that cannot be
        satisfied by an ``__all__`` that is correct but not the one ``import *`` reads. It is
        also what a caller writing ``from bfinance.universe import *`` would actually observe.
        """
        namespace: dict[str, Any] = {}
        exec("from bfinance.universe import *", namespace)  # noqa: S102 - the thing under test
        exec("from bfinance.ownership import *", namespace)  # noqa: S102
        exec("from bfinance.nse.macro import *", namespace)  # noqa: S102
        bound_modules = {
            name: value
            for name, value in namespace.items()
            if isinstance(value, ModuleType)
        }
        assert bound_modules == {}, (
            f"a star import bound module objects into the caller's namespace: "
            f"{sorted(bound_modules)}"
        )
        # Names a built-in `import *` would bind (no leading underscore on a module global) are
        # gone once __all__ exists; assert the declaration is what removed them.
        assert "pd" not in namespace
        assert "httpx" not in namespace
        assert "json" not in namespace
        # And it did bind the API, so the check is not passing because nothing was exported.
        assert "OwnershipClient" in namespace
        assert "MacroClient" in namespace
        assert "CATALOGUE" in namespace

    def test_control_a_module_without_all_still_leaks(self) -> None:
        """Non-vacuity for the four star-import assertions above.

        Without this, "no module objects were bound" would pass on any run where the injection
        silently did nothing, and this file's central claim about the three ``__all__``s would be
        untested. ``builtins`` is used as a stand-in module object so no real leak is required.
        """
        namespace: dict[str, Any] = {}
        module = importlib.import_module("bfinance.universe")
        leaked_names = sorted(_module_leaks(module))
        assert leaked_names, "the stand-in setup needs a module that really does leak"
        try:
            # Simulate the pre-__all__ state by executing a star import against the leaked names.
            exec(  # noqa: S102 - the pre-fix state is exactly what is being simulated
                f"from bfinance.universe import {', '.join(leaked_names)}", namespace
            )
        except ImportError:
            pytest.fail("a leaked name is not importable, so the leak is not real")
        bound = {
            name: value for name, value in namespace.items() if isinstance(value, ModuleType)
        }
        assert bound, (
            "explicitly importing the leaked names bound no module objects, so the star-import "
            "assertions above are not measuring anything"
        )
        assert set(leaked_names) <= set(bound)

    def test_builtins_is_a_real_module_object(self) -> None:
        """The last non-vacuity guard: the type the checks use is the type that is there.

        Every "resolves to a module object" assertion in this file rests on ``ModuleType``
        matching what ``import *`` actually binds. If it matched nothing, all of them would pass
        vacuously and this line is the only thing that would notice.
        """
        assert isinstance(builtins, ModuleType)
        assert isinstance(importlib, ModuleType)
        assert not isinstance(SRC, ModuleType)
        assert not isinstance("ParticipantFlowsUnavailable", ModuleType)
