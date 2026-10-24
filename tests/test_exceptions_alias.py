"""
``bfinance.exceptions``: the yfinance names, and the exact limit of what they buy.

The defect this file exists to prevent
--------------------------------------
A module of yfinance-shaped exception names is very easy to write as a promise it cannot keep. The
obvious implementation -- ``class YFException(YFException)`` against the real yfinance, or stubs
deriving straight from ``Exception`` -- makes ``except yfinance.exceptions.X`` *look* like it
would catch a bfinance raise, and it is false: an ``except`` clause binds a class **object**, and
two objects are not one object however alike their names. Worse, the second shape quietly breaks
the package's load-bearing single-root promise, because a stub deriving from ``Exception`` is a
second root a caller's one ``except BFinanceError`` cannot see.

So this file pins the four things that are actually true:

1. the eight names resolve, are classes, and are raisable;
2. the four aliases are the **same object** as the classes bfinance really raises, and the four
   stubs are **distinct** classes that fire for no failure bfinance has;
3. ``import bfinance`` does not load this module (asked in a subprocess, because in-process
   ``sys.modules`` is already polluted -- see ``tests/test_public_surface.py``'s own header);
4. every one of those claims has a negative control, because each is a test that could report
   green while checking nothing.

Offline and hermetic: no network, no clock, no yfinance import. Every verdict is derived from the
shipped module and its ``__module__`` attributes, never from a hand-written list of what should be
in it.
"""

from __future__ import annotations

import importlib
import inspect
import os
import pathlib
import subprocess
import sys
import textwrap
from types import ModuleType

import pytest

import bfinance
from bfinance.exceptions import (
    YFDataException,
    YFException,
    YFInvalidPeriodError,
    YFNotImplementedError,
    YFPricesMissingError,
    YFRateLimitError,
    YFTickerMissingError,
    YFTzMissingError,
)
from bfinance.utils.exceptions import (
    BFinanceError,
    RateLimitExceededError,
    TickerNotFoundError,
    UpstreamServiceError,
)

MODULE = "bfinance.exceptions"

#: The four names that must be **aliases**: ``(alias, the class bfinance already raises)``. Spelled
#: out rather than derived, because a derived expectation is derived from the code under test and
#: proves nothing about it.
ALIASES = (
    ("YFException", BFinanceError),
    ("YFTickerMissingError", TickerNotFoundError),
    ("YFDataException", UpstreamServiceError),
    ("YFRateLimitError", RateLimitExceededError),
)

#: The four names that must be **stubs**: distinct classes, catchable under the root, and raised
#: nowhere. ``__module__`` is part of each expectation -- a stub that claimed to be a re-export
#: would be an alias, and the whole difference between the two shapes lives in that attribute.
STUBS = (
    "YFTzMissingError",
    "YFPricesMissingError",
    "YFInvalidPeriodError",
    "YFNotImplementedError",
)

#: The eight names, in the order the module's own ``__all__`` groups them: four aliases then four
#: stubs. Asserted rather than read from the module, so a silent drop is a failure.
THE_EIGHT = tuple(name for name, _ in ALIASES) + STUBS


def run_probe(program: str, *, search_path: pathlib.Path | None = None) -> str:
    """Run ``program`` in a fresh interpreter and return stdout, failing loudly on a crash.

    Fresh, because ``sys.modules`` cannot answer an import-cost question inside a pytest process:
    ``_every_module_under_src`` in ``tests/test_public_surface.py`` imports every module under
    ``src/``, so by the time any test here runs, in-process ``sys.modules`` is already polluted.

    ``search_path`` prepends a directory to ``PYTHONPATH`` so the negative control can point this
    same probe at a purpose-built package. Nothing else is passed through, because a probe that
    inherits a caller's environment can be answered by it rather than by the code.
    """
    env = None
    if search_path is not None:
        existing = os.environ.get("PYTHONPATH", "")
        entries = [str(search_path)] + ([existing] if existing else [])
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(entries)}
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(program)],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        check=False,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    return result.stdout.strip()


# --------------------------------------------------------------------------- #
# 1. the eight names resolve, are classes, and are raisable
# --------------------------------------------------------------------------- #


class TestTheEightNamesAreRealClasses:
    def test_every_name_in_all_resolves_to_a_class(self) -> None:
        module = importlib.import_module(MODULE)
        resolved = {name: getattr(module, name) for name in THE_EIGHT}
        assert all(inspect.isclass(value) for value in resolved.values()), {
            name: type(value).__name__
            for name, value in resolved.items()
            if not inspect.isclass(value)
        }

    def test_every_name_is_catchable_as_bfinance_error(self) -> None:
        """The claim the single-root rule exists to support, asked through a real ``try``.

        Spelled as a raise-and-catch rather than as ``issubclass`` so a name that resolved to
        something with a clever ``__mro__`` but could not actually be caught would fail here.
        """
        module = importlib.import_module(MODULE)
        for name in THE_EIGHT:
            cls = getattr(module, name)
            try:
                raise cls(f"probe: {name}")
            except BFinanceError as caught:
                assert type(caught) is cls, f"{name} was not caught as itself"
            except BaseException as other:  # pragma: no cover - only on a regression
                raise AssertionError(
                    f"{name} escaped `except BFinanceError` and was caught as "
                    f"{type(other).__name__}"
                ) from None

    def test_all_is_exactly_the_eight_names_with_no_duplicates(self) -> None:
        """``__all__`` is what ``import *`` reads, so a padded one hands a caller the internals.

        The four ``bfinance`` classes are imported plumbing; listing them would make a star import
        bind two vocabularies for the same class.
        """
        module = importlib.import_module(MODULE)
        assert isinstance(module.__all__, list)
        assert sorted(module.__all__) == sorted(THE_EIGHT), (
            f"{MODULE}.__all__ is {sorted(module.__all__)}, expected {sorted(THE_EIGHT)}"
        )
        assert len(module.__all__) == len(set(module.__all__)), (
            f"{MODULE}.__all__ repeats a name: {module.__all__}"
        )
        assert [name for name in module.__all__ if not hasattr(module, name)] == []

    def test_all_binds_no_module_object(self) -> None:
        """Checked by resolution, not by name: a rename of an import defeats a deny-list."""
        module = importlib.import_module(MODULE)
        leaked = sorted(
            name for name in module.__all__ if isinstance(getattr(module, name), ModuleType)
        )
        assert leaked == [], f"{MODULE}.__all__ exports module objects: {leaked}"

    def test_the_two_predicates_can_see_a_name_that_is_neither(self) -> None:
        """Negative control for the two tests above, in memory and decidable.

        ``test_every_name_in_all_resolves_to_a_class`` walks eight hand-picked names, and
        ``test_all_binds_no_module_object`` walks the module's own ``__all__``. If ``getattr``,
        ``inspect.isclass`` or the ``ModuleType`` comparison had stopped working, both would
        report green while checking nothing. So a value that is **neither a class nor a real
        module of this package** -- a plain ``int`` -- is bound to the module and listed in
        ``__all__``, and the same two predicates must report it. Each assertion names the field
        that differs rather than merely that the two answers are unequal.
        """
        module = importlib.import_module(MODULE)
        original_all = list(module.__all__)
        injected = "NOT_A_CLASS_CONTROL"
        original_attr = vars(module).get(injected, inspect.Parameter.empty)
        try:
            setattr(module, injected, 42)
            module.__all__ = original_all + [injected]
            assert not inspect.isclass(getattr(module, injected)), (
                "the injected control resolved to a class, so it would not exercise the isclass "
                "check it exists to exercise"
            )
            not_classes = sorted(
                name for name in module.__all__ if not inspect.isclass(getattr(module, name))
            )
            assert not_classes == [injected], (
                "the isclass check cannot see an injected non-class, so the eight-name test "
                f"would pass against any value at all; it reported {not_classes}"
            )
            assert injected in module.__all__, "the injected control is not in __all__, so the "\
                "checks above were not reading the list the control padded"
        finally:
            module.__all__ = original_all
            if original_attr is inspect.Parameter.empty:
                delattr(module, injected)
            else:
                setattr(module, injected, original_attr)
        assert module.__all__ == original_all, "the control contaminated __all__"
        assert not hasattr(module, injected), "the control contaminated the module namespace"


# --------------------------------------------------------------------------- #
# 2. alias vs stub: the same object, or a distinct one
# --------------------------------------------------------------------------- #


class TestTheFourAliasesAreTheClassesBfinanceActuallyRaises:
    @pytest.mark.parametrize("alias_name,target", ALIASES)
    def test_the_alias_is_the_same_object_not_a_subclass(self, alias_name: str, target: type) -> None:
        """Identity, not ``issubclass``.

        ``issubclass(YFRateLimitError, RateLimitExceededError)`` would be true for a *subclass*
        too, and a subclass that nothing raises is a class that silently catches nothing. ``is``
        is the only assertion that says the ported ``except`` clause binds the live behaviour.
        """
        module = importlib.import_module(MODULE)
        assert getattr(module, alias_name) is target, (
            f"{alias_name} is not the same object as {target.__name__}"
        )

    @pytest.mark.parametrize("alias_name,target", ALIASES)
    def test_the_alias_keeps_the_defining_module(
        self, alias_name: str, target: type
    ) -> None:
        """The alias is a binding, so ``__module__`` still names the definer.

        This is the mechanism that keeps the package at exactly one class deriving from
        ``Exception``: a re-export cannot add a second root because it is not a second class.
        """
        module = importlib.import_module(MODULE)
        alias = getattr(module, alias_name)
        assert alias.__module__ == target.__module__ != MODULE, (
            f"{alias_name}.__module__ is {alias.__module__!r}; a name defined in {MODULE} would "
            "be counted as a new class by the single-root lint"
        )

    def test_a_ported_except_clause_catches_a_real_bfinance_raise(self) -> None:
        """End-to-end: the clause a port writes, against the class bfinance really raises."""
        try:
            raise RateLimitExceededError("HTTP 429 from screener.in")
        except YFRateLimitError as caught:
            assert caught is not None
        else:  # pragma: no cover - only on a regression
            raise AssertionError("`except YFRateLimitError` did not catch a RateLimitExceededError")

    def test_an_alias_of_a_subclass_still_sits_under_the_root(self) -> None:
        """``YFRateLimitError`` aliases a *grandchild* of the root; the umbrella still holds.

        Checked because it is the one alias whose target is not a direct child of
        ``BFinanceError``, so it is the one that could have broken the chain if any of the
        inheritance had been rewritten.
        """
        assert RateLimitExceededError.__bases__ == (UpstreamServiceError,)
        assert issubclass(RateLimitExceededError, BFinanceError)
        assert YFRateLimitError is RateLimitExceededError


class TestTheFourStubsAreDistinctClassesThatFireForNothing:
    @pytest.mark.parametrize("stub_name", STUBS)
    def test_the_stub_is_defined_in_this_module(self, stub_name: str) -> None:
        """A stub whose ``__module__`` points elsewhere is an alias wearing the wrong name."""
        module = importlib.import_module(MODULE)
        assert getattr(module, stub_name).__module__ == MODULE

    @pytest.mark.parametrize("stub_name", STUBS)
    def test_no_stub_derives_directly_from_exception(self, stub_name: str) -> None:
        """The single-root lint, asserted locally on the four classes that could break it.

        ``tests/test_public_surface.py`` counts roots over every module under ``src/``; this asks
        the same question of the four classes this file is responsible for, so a regression is
        reported next to its cause instead of a file's walk away.
        """
        module = importlib.import_module(MODULE)
        cls = getattr(module, stub_name)
        assert cls.__bases__ != (Exception,), (
            f"{stub_name} derives directly from Exception, which would make it a second root and "
            "would leave it outside `except BFinanceError`"
        )
        assert issubclass(cls, BFinanceError)

    @pytest.mark.parametrize("stub_name", STUBS)
    def test_the_stub_is_not_the_alias_of_the_same_idea(self, stub_name: str) -> None:
        """The control for the alias tests: a stub must be a *different* object.

        Without this, ``YFPricesMissingError = RealOHLCUnavailableError`` -- the mistake this
        module is written to avoid -- would satisfy every test above.
        """
        module = importlib.import_module(MODULE)
        stub = getattr(module, stub_name)
        aliased_objects = {target for _, target in ALIASES}
        assert stub not in aliased_objects, (
            f"{stub_name} is one of the aliased objects; it is an alias, not a stub"
        )

    @pytest.mark.parametrize("stub_name", STUBS)
    def test_the_stub_is_raisable_and_carries_its_message(self, stub_name: str) -> None:
        """A stub is a class a caller can catch, not a module-level ``__getattr__`` trap.

        ``except <name>`` in a ported handler is evaluated at exception time, so a name that only
        existed as a raising ``__getattr__`` would turn every handled failure into an
        ``AttributeError``. Raising and catching through a real ``try`` is what proves the shape.
        """
        module = importlib.import_module(MODULE)
        stub = getattr(module, stub_name)
        try:
            raise stub("no prices")
        except stub as caught:
            assert str(caught) == "no prices"

    def test_the_module_has_no_dunder_getattr_trap(self) -> None:
        """Nothing here is faked at lookup time; every name is a real binding.

        If a future edit added a ``__getattr__`` to satisfy the name list, the eight names could
        pass while being uncatchable, so the absence is asserted directly.
        """
        module = importlib.import_module(MODULE)
        assert not hasattr(module, "__getattr__"), (
            f"{MODULE} defines a module-level __getattr__; a stub must be a class, not a lookup trap"
        )

    def test_no_stub_is_raised_anywhere_in_the_package(self) -> None:
        """The stubs are inert, and this is what makes the docstrings honest.

        If bfinance ever grew a real failure matching one of these, the class would stop being a
        courtesy and the docstring saying "bfinance never raises this" would become the lie this
        repo's doc lints exist to catch. Read by AST so a docstring quoting a raise site is not
        mistaken for one.
        """
        import ast

        src = pathlib.Path(bfinance.__file__).resolve().parent
        raise_sites: list[str] = []
        for path in sorted(src.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in ast.walk(tree):
                exc = None
                if isinstance(node, ast.Raise) and node.exc is not None:
                    exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
                elif isinstance(node, ast.ClassDef) and any(
                    isinstance(b, ast.Name) and b.id in STUBS for b in node.bases
                ):
                    exc = node
                if isinstance(exc, ast.Name) and exc.id in STUBS:
                    raise_sites.append(f"{path.relative_to(src.parent)}:{exc.lineno}")

        assert raise_sites == [], (
            f"these are raised in the package, so their docstrings' 'never raised' claim is now "
            f"false: {raise_sites}"
        )


class TestTheNotImplementedStubKeepsTheRootAndTheBuiltin:
    def test_it_is_under_the_root_and_under_the_builtin(self) -> None:
        """Both bases, because the two callers are different people.

        ``except BFinanceError`` is the package umbrella; ``except NotImplementedError`` is what
        the existing bare raise sites promise (``DerivativesEngine.generate_option_option_chain``
        and friends). Dropping either base breaks a caller, and this is the assertion that says
        which one would break.
        """
        assert YFNotImplementedError.__bases__ == (BFinanceError, NotImplementedError), (
            f"bases are {YFNotImplementedError.__bases__}, not (BFinanceError, NotImplementedError)"
        )
        assert issubclass(YFNotImplementedError, BFinanceError)
        assert issubclass(YFNotImplementedError, NotImplementedError)
        assert issubclass(YFNotImplementedError, RuntimeError)

    def test_the_documented_trade_is_the_one_that_ships(self) -> None:
        """The divergence from yfinance's MRO is asserted, not left to the docstring.

        yfinance's ``YFNotImplementedError`` derives from ``NotImplementedError`` alone and is
        therefore *not* catchable as ``YFException``. bfinance's is, because the single-root rule
        leaves it no alternative. This test fails if either side of that choice is quietly
        reversed, which is the only way the docstring could start lying.
        """
        mro = [base.__name__ for base in YFNotImplementedError.__mro__]
        assert "BFinanceError" in mro and "NotImplementedError" in mro
        # The consequence, stated as behaviour rather than as prose: bfinance's is catchable as
        # the package root, which is exactly what yfinance's is not.
        assert issubclass(YFNotImplementedError, YFException), (
            "YFNotImplementedError no longer sits under the root, so the divergence the docstring "
            "documents has silently reversed"
        )

    def test_the_real_raise_site_is_still_the_bare_builtin(self) -> None:
        """bfinance actually raises bare ``NotImplementedError``; this stub does not change that."""
        import ast

        src = pathlib.Path(bfinance.__file__).resolve().parent
        sites = []
        for path in sorted(src.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Raise) and node.exc is not None):
                    continue
                exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
                if isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
                    sites.append(f"{path.relative_to(src.parent)}:{exc.lineno}")
        assert sites, (
            "no bare `raise NotImplementedError` remains in src/, so the claim that bfinance "
            "signals unimplemented paths with the builtin rather than with this stub is stale"
        )


# --------------------------------------------------------------------------- #
# 3. the alias module is opt-in: `import bfinance` must not load it
# --------------------------------------------------------------------------- #


class TestTheAliasModuleIsNotOnEveryCallersImportPath:
    def test_import_bfinance_does_not_load_it(self) -> None:
        """Asked in a fresh interpreter, because in-process ``sys.modules`` is already polluted.

        ``tests/test_public_surface.py::_every_module_under_src`` imports every module under
        ``src/``, so an in-process check would answer True in this very process no matter what the
        package does. That is the failure mode the negative control below rules out.
        """
        assert run_probe(
            """
            import sys
            import bfinance
            print("bfinance.exceptions" in sys.modules)
            """
        ) == "False", "import bfinance loaded bfinance.exceptions; it is meant to be opt-in"

    def test_the_same_probe_detects_a_submodule_that_does_get_loaded(
        self, tmp_path: pathlib.Path
    ) -> None:
        """The negative control, and the brief requires it.

        The probe above answers one token: was ``bfinance.exceptions`` in ``sys.modules`` after
        ``import bfinance``? A probe that could only ever print ``False`` -- because the string
        stopped matching, because the ``in`` test was inverted, because the subprocess died and
        the comparison happened to hold -- would leave that test green while checking nothing.

        So the *same* probe function is pointed at a purpose-built package in ``tmp_path`` whose
        ``__init__`` imports one submodule and leaves another alone, and it must answer ``True``
        for the one that was pulled and ``False`` for the one that was not. Two fields, both
        asserted by name, so a probe that returned one constant cannot satisfy this.
        """
        package = tmp_path / "controlpkg"
        package.mkdir()
        (package / "__init__.py").write_text("from . import pulled\n", encoding="utf-8")
        (package / "pulled.py").write_text("VALUE = 1\n", encoding="utf-8")
        (package / "left_alone.py").write_text("VALUE = 2\n", encoding="utf-8")

        report = run_probe(
            """
            import sys
            import controlpkg
            print("controlpkg.pulled" in sys.modules, "controlpkg.left_alone" in sys.modules)
            """,
            search_path=tmp_path,
        )

        assert report == "True False", (
            "the probe cannot tell a loaded submodule from an unloaded one on a package whose "
            f"__init__ provably imports one and not the other; it reported {report!r}"
        )

    def test_the_probe_reports_true_for_a_module_the_package_really_does_load(self) -> None:
        """The same control against the real package rather than a synthetic one.

        ``bfinance.utils.exceptions`` is loaded by ``import bfinance``, which is the
        ``bfinance.exceptions == False`` answer's own guarantee that the probe is measuring
        ``sys.modules`` and not printing a constant.
        """
        assert run_probe(
            """
            import sys
            import bfinance
            print("bfinance.utils.exceptions" in sys.modules)
            """
        ) == "True", (
            "the probe reports False for every module in the real package, so its False for "
            "bfinance.exceptions is not evidence of anything"
        )

    def test_the_submodule_import_works_with_nothing_in_init(self) -> None:
        """``from bfinance.exceptions import X`` resolves without an ``__init__.py`` re-export.

        Also the reason the two tests above can both hold: the ergonomic path and the cheap import
        are the same thing here, because Python falls through to the submodule when the attribute
        is absent.
        """
        assert run_probe(
            """
            import bfinance.exceptions as exc
            print(all(
                isinstance(getattr(exc, name), type)
                for name in (
                    "YFException", "YFTickerMissingError", "YFDataException", "YFRateLimitError",
                    "YFTzMissingError", "YFPricesMissingError", "YFInvalidPeriodError",
                    "YFNotImplementedError",
                )
            ))
            """
        ) == "True", "the eight names did not all resolve to classes in a fresh interpreter"

    def test_init_does_not_import_the_alias_module(self) -> None:
        """The isolation restated structurally: no ``import`` edge points at it from ``__init__``.

        Complementary to the subprocess probe rather than a repeat of it -- the probe proves the
        *effect*, this proves the *cause*, and either can be broken while the other stays green.
        """
        import ast

        tree = ast.parse(
            (pathlib.Path(bfinance.__file__).resolve().parent / "__init__.py").read_text(
                encoding="utf-8-sig"
            )
        )
        offenders: list[int] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                # Only an absolute dotted path can name bfinance.exceptions. `import exceptions`
                # would name something else entirely, so counting it would be a false positive.
                names = [alias.name for alias in node.names]
                if MODULE in names:
                    offenders.append(node.lineno)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue  # relative: `from .exceptions import X`, spelled without the package
                names = [node.module or ""]
                names += [f"{node.module}.{alias.name}" for alias in node.names if node.module]
                if MODULE in names or MODULE.rsplit(".", 1)[-1] in names:
                    offenders.append(node.lineno)
        assert offenders == [], (
            f"bfinance/__init__.py imports bfinance.exceptions at line(s) {offenders}; the "
            "module is opt-in and putting it on every caller's import path buys nothing"
        )


# --------------------------------------------------------------------------- #
# 4. the limit, asserted: `except yf.exceptions.X` does NOT catch a bfinance raise
# --------------------------------------------------------------------------- #


class TestTheDocumentedLimitIsTrue:
    def test_a_yfinance_except_clause_does_not_catch_a_bfinance_raise(self) -> None:
        """The sentence the module docstring is required to contain, proved as behaviour.

        Nothing here imports yfinance -- it is a dev-only dependency, and a test that needed it
        would fail in a runtime install. The class object is built in memory with the same name,
        which is all an ``except`` clause binds.

        The second handler exists because the escape is the result: without it the raise leaves the
        test as an error, so the passing state is unobservable and the test cannot pass at all.
        """
        yfinance_side = type("YFException", (Exception,), {"__module__": "yfinance.exceptions"})

        caught = None
        try:
            raise RateLimitExceededError("HTTP 429")
        except yfinance_side as error:  # type: ignore[misc]
            caught = error
        except RateLimitExceededError:
            # The documented limit: the yfinance-shaped clause above did not match, so the raise
            # escaped it. Reaching here is the proof; `caught` stays None.
            pass
        else:  # pragma: no cover - only on a regression
            raise AssertionError(
                "the raise neither matched the yfinance-shaped clause nor escaped it"
            )

        assert caught is None, (
            "a bfinance raise was caught by a yfinance-shaped class object, which would mean the "
            "docstring's 'this does not make `except yf.exceptions.X` catch a bfinance raise' is "
            "false"
        )

    def test_the_same_clause_catches_when_it_is_rewritten_to_this_module(self) -> None:
        """The other half, so the control above is a control and not a proof of hopelessness."""
        try:
            raise RateLimitExceededError("HTTP 429")
        except YFRateLimitError as caught:
            assert type(caught) is RateLimitExceededError
        else:  # pragma: no cover - only on a regression
            raise AssertionError("the rewritten clause did not catch it either")

    def test_the_docstring_states_the_limit_in_plain_words(self) -> None:
        """The claim is asserted as text because the limit is a promise about *reading*.

        A caller who never learns it writes ``except yf.exceptions.X`` and gets an unhandled
        traceback. The substring is the two spellings of the clause, in one sentence, so the
        sentence cannot be quietly reworded into something vaguer.
        """
        doc = importlib.import_module(MODULE).__doc__ or ""
        assert "except" in doc and "yfinance.exceptions." in doc, (
            "the module docstring no longer names the yfinance-spelled `except` clause it exists "
            "to disclaim"
        )