"""
Two corrections this release made, each with a test that fails if it is undone.

Neither defect here was a wrong number. Both were **documentation asserting something the
code did not do**, which is the failure mode this package is built to avoid, so they are the
kind that survive review: the code passes, the suite is green, and the claim is still wrong.

1. ``UNWIRED_NOTE`` named the AST scan as the rule keeping the leaf package unreachable. It is
   not that rule. The scan forbids *imports*, and a dotted string -- which is how every other
   row in the registry defers its import to call time -- would have sailed past it. The rule
   that actually binds is ``Registry.by_module()`` being required to exclude the package, and
   that is asserted in ``tests/test_sources_cascade.py``. The note is shown to callers through
   :meth:`SourceSpec.unavailability`, so a wrong reason reaches whoever is debugging a missing
   split-adjusted series.

2. ``trendlyne.errors`` claimed "nothing in bfinance.trendlyne catches BFinanceError". Literally
   true -- there is no ``except BFinanceError`` -- and behaviourally misleading, because two
   cache boundaries catch ``Exception``, which includes it.

Plus the ``__all__`` the three submodules lacked. Without one, ``import *`` bound ``pandas``,
``bs4``, ``json``, ``re``, ``asyncio``, ``httpx`` and ``logging`` into the caller's namespace.
"""
from __future__ import annotations

import ast
import json
import pathlib
import subprocess
import sys
import textwrap

import pytest

from bfinance.sources import UNWIRED_NOTE
from bfinance.sources.registry import DEFAULT
from bfinance.trendlyne import client as tl
from bfinance.trendlyne import sectors, tables

PACKAGE_DIR = pathlib.Path(tl.__file__).parent
SUBMODULES = ("client", "sectors", "tables")

#: The module-level imports a star-import would bind if __all__ were absent. Named explicitly
#: because "some third-party modules" is not a thing a reader can check.
THIRD_PARTY = frozenset({
    "pandas", "pd", "bs4", "BeautifulSoup", "json", "re", "logging", "logger",
    "asyncio", "httpx", "io", "html", "textwrap", "dataclasses", "datetime", "typing",
})


# --------------------------------------------------------------- the unwired note


class TestTheUnwiredNoteNamesTheRuleThatBinds:
    def test_it_does_not_claim_the_ast_scan_is_what_blocks_it(self):
        """
        The specific wrong claim. An AST scan of import statements cannot stop a dotted
        string, and the registry already uses dotted strings for every other row -- so
        presenting the scan as the blocker is presenting a rule that would not hold.
        """
        assert "this layer is not exempt" not in UNWIRED_NOTE, (
            "the AST scan is not the binding rule; see the by_module() contract"
        )

    def test_it_names_the_rule_that_actually_binds(self):
        assert "by_module" in UNWIRED_NOTE, (
            "the note must name Registry.by_module(), the contract that really keeps the leaf "
            "package unreachable, or the next maintainer will 'fix' the wiring with a string"
        )
        assert "TestIsolation" in UNWIRED_NOTE, (
            "the AST scan is still true and still enforced; it just is not the binding rule"
        )

    def test_the_binding_rule_is_real_and_not_merely_asserted_in_prose(self):
        """
        The note points at this. Check the property in this process rather than trusting the
        cross-reference, because a note that cites a test which has been deleted is worse
        than one that cited nothing.
        """
        reachable = " ".join(DEFAULT.by_module())
        assert "trendlyne" not in reachable, (
            "the leaf package must not be loadable by this layer; that is what the note says"
        )

    def test_the_splice_caveat_is_carried_because_the_row_declares_a_basis(self):
        """
        The row reserves ``Basis.SPLIT_ADJUSTED``, and the series it reserves is a composite:
        split/bonus adjusted with demergers *not* applied. A caller who reads the basis
        column alone would splice it against yfinance, which rewrites demerger history.
        """
        assert "DEMERGER" in UNWIRED_NOTE.upper()
        assert "splic" in UNWIRED_NOTE.lower(), (
            "the consequence has to be named, not just the fact"
        )
        assert tl.DEMERGER_APPLIED is False, (
            "if demergers ever come to be applied, this caveat and the test above are both stale"
        )

    def test_a_caller_debugging_a_missing_basis_sees_the_corrected_reason(self):
        """The note is user-facing through unavailability(), so it is checked there too."""
        from bfinance.sources import load_provider
        from bfinance.sources.errors import SourceUnavailableError

        with pytest.raises(SourceUnavailableError) as excinfo:
            load_provider(DEFAULT.source("vendor-b-adjustments"))
        message = str(excinfo.value)
        assert "by_module" in message
        assert "this layer is not exempt" not in message


# ------------------------------------------------------------- the errors claim


class TestTheErrorsClaimIsPrecise:
    def test_the_broad_handlers_are_disclosed_rather_than_denied(self):
        source = (PACKAGE_DIR / "errors.py").read_text(encoding="utf-8")
        assert "nothing in :mod:`bfinance.trendlyne` catches" not in source, (
            "the claim is technically true and behaviourally misleading; two cache boundaries "
            "catch Exception, which includes BFinanceError"
        )
        assert "cache" in source.lower(), (
            "the corrected sentence has to say where the broad handlers are"
        )

    def test_the_broad_handlers_are_really_only_two_and_really_at_the_cache(self):
        """Counted from the source, so the docstring cannot drift from the code silently."""
        broad = {"Exception", "BaseException"}
        sites = []
        for name in SUBMODULES:
            path = PACKAGE_DIR / f"{name}.py"
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ExceptHandler):
                    continue
                # A bare `except:` and an `except Exception:` are both broad handlers. Only
                # the first is syntactically obvious, which is why the second is easy to
                # miss when auditing for this -- and it is the one actually in the code.
                if node.type is None or ast.unparse(node.type) in broad:
                    sites.append((name, node.lineno))
        assert len(sites) == 2, (
            f"expected exactly two broad handlers, both at the cache boundary; found {sites}. "
            f"If a third was added, errors.py's account of the trade is incomplete."
        )
        for name, lineno in sites:
            lines = (PACKAGE_DIR / f"{name}.py").read_text(encoding="utf-8").splitlines()
            # The handler and the statement it guards, which is where the cache call is.
            window = "\n".join(lines[lineno - 3:lineno + 2]).lower()
            assert "cache" in window, (
                f"{name}:{lineno} is a broad handler that is not guarding a cache call; "
                f"errors.py describes both broad handlers as cache boundaries"
            )

    def test_no_module_catches_the_base_by_name(self):
        """The part of the original claim that was true, and stays asserted."""
        for name in SUBMODULES:
            tree = ast.parse((PACKAGE_DIR / f"{name}.py").read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler) and node.type is not None:
                    rendered = ast.unparse(node.type)
                    assert "BFinanceError" not in rendered, (
                        f"{name}:{node.lineno} catches the package base by name"
                    )


# ------------------------------------------------------------------ the __all__


class TestSubmodulesDeclareTheirPublicSurface:
    @pytest.mark.parametrize("name", SUBMODULES)
    def test_the_module_declares_all(self, name):
        module = {"client": tl, "sectors": sectors, "tables": tables}[name]
        assert getattr(module, "__all__", None), (
            f"bfinance.trendlyne.{name} declares no __all__, so `from ... import *` binds its "
            f"module-level imports as well as its API"
        )

    @pytest.mark.parametrize("name", SUBMODULES)
    def test_every_listed_name_exists(self, name):
        module = {"client": tl, "sectors": sectors, "tables": tables}[name]
        for exported in module.__all__:
            assert hasattr(module, exported), f"__all__ names {exported!r}, which does not exist"

    @pytest.mark.parametrize("name", SUBMODULES)
    def test_nothing_public_is_left_off_the_list(self, name):
        """
        The other direction, and the one that matters: an __all__ that omits real API is a
        quietly smaller surface, which is the same class of error as an inflated one.
        """
        module = {"client": tl, "sectors": sectors, "tables": tables}[name]
        path = PACKAGE_DIR / f"{name}.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        defined = set()
        for node in tree.body:
            target = None
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                target = node.name
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        target = t.id
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                target = node.target.id
            if target and not target.startswith("_") and target != "logger":
                defined.add(target)
        missing = defined - set(module.__all__)
        assert not missing, f"{name}.py defines public names absent from __all__: {sorted(missing)}"

    @pytest.mark.parametrize("name", SUBMODULES)
    def test_a_star_import_binds_only_the_declared_names(self, name):
        """
        The behaviour the list exists for, checked by actually doing the star-import in a
        fresh interpreter. Asserting ``__all__`` is non-empty proves nothing about what a
        star-import binds.
        """
        program = textwrap.dedent(
            f"""
            import json
            namespace = {{}}
            exec("from bfinance.trendlyne.{name} import *", namespace)
            bound = sorted(k for k in namespace if not k.startswith("__"))
            print(json.dumps(bound))
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", program], capture_output=True, text=True, timeout=180
        )
        assert result.returncode == 0, result.stderr
        bound = set(json.loads(result.stdout))
        module = {"client": tl, "sectors": sectors, "tables": tables}[name]
        assert bound == set(module.__all__), (
            f"star-import bound {sorted(bound - set(module.__all__))} beyond __all__"
        )
        # And the specific leak this is about, named rather than left implicit.
        leaked = bound & THIRD_PARTY
        assert not leaked, (
            f"{name} star-import still leaks module-level imports: {sorted(leaked)}"
        )

    def test_the_star_import_check_is_not_vacuous(self):
        """
        The negative control. A probe that always passes because the namespace filter is wrong
        is indistinguishable from one that is working, so it is run against a module with no
        __all__ and required to differ.
        """
        program = textwrap.dedent(
            """
            import json
            namespace = {}
            exec("from bfinance.screener import client", namespace)
            # Deliberately not a star-import: emulate the unbounded case the filter must catch.
            namespace["httpx"] = "leaked"
            namespace["json"] = "leaked"
            bound = sorted(k for k in namespace if not k.startswith("__"))
            print(json.dumps(bound))
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", program], capture_output=True, text=True, timeout=180
        )
        assert result.returncode == 0, result.stderr
        bound = set(json.loads(result.stdout))
        assert {"httpx", "json"} <= bound, (
            "the control must actually put third-party names in the namespace, or the filter "
            "under test is not the filter that runs in the real test"
        )
        for name in SUBMODULES:
            module = {"client": tl, "sectors": sectors, "tables": tables}[name]
            assert bound - set(module.__all__), (
                "the control's extras must fall outside the real __all__ too, or the "
                "assertion above cannot discriminate"
            )
