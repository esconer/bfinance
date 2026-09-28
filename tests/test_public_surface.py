"""
The public surface of ``bfinance``, and the eight things it refuses to pay for.

The state being guarded
-----------------------
Seven capabilities are opt-in and deliberately absent from ``src/bfinance/__init__.py`` and from
``__all__``: :mod:`bfinance.statements`, :mod:`bfinance.sources`, :mod:`bfinance.costs`,
:mod:`bfinance.flows`, :mod:`bfinance.universe`, :mod:`bfinance.ownership` and
:mod:`bfinance.nse.macro`. :mod:`bfinance.trendlyne` is a third-party-source client and is opt-in
by a different owner's rule, but the effect on an importing caller is identical, so it is guarded
here too.

``from bfinance import costs`` already works with nothing in ``__init__.py``, because
``IMPORT_FROM`` falls through to the submodule when the attribute is absent. A top-level re-export
would *require executing* the module, which is the same thing with the isolation removed -- so the
ergonomic path and the cheap import are not in tension, and the checks below hold both at once.

Three things make these assertions hard to write honestly
---------------------------------------------------------

**Subprocesses pollute themselves.** Importing ``bfinance.costs`` sets the attribute on the parent
package as a side effect, so *this very file* makes an in-process ``sys.modules`` or ``hasattr``
check meaningless the moment it runs next to its own imports -- and pytest runs every module in one
process, so ``tests/test_costs.py`` has already done it. Every claim about what an import costs is
therefore asked in a fresh interpreter.

**A subprocess probe is exactly the kind of test that can silently stop testing anything.** If the
printed word stopped matching, or the probe crashed and the comparison happened to be satisfied,
every isolation test in this file would report green while checking nothing -- which is the
failure mode that has already been shipped three times in this repo. So every probe below is
paired with a **negative control**: a package built in ``tmp_path`` that breaks the rule on
purpose, run through the *same* probe function, which must return the opposite answer. A control
that cannot turn its probe red is itself decoration, so each one asserts the specific field that
differs rather than merely that the two strings are unequal.

**Text scans read prose as code.** ``src/bfinance/__init__.py`` names all eight modules *in its own
docstring* to explain why they are absent, ``tests/test_nse_macro.py`` quotes its own probe output
in prose, ``tests/test_no_lying_docs.py`` quotes the unfailable assertion it forbids, and
``tests/test_ratios_honesty.py:12`` contains ``if "Cash Equivalents" in bs.index`` inside a
docstring quoting the defect it removed. So **no check in this file reads source text at all** --
every structural one reads the AST, which discards comments and treats a docstring as a statement
rather than a name. The one place a name is *resolved* rather than parsed, the exception walk, does
it by import, so ``class X(BFinanceError):`` and a comment quoting that line cannot be confused.

Nothing here is allowed to be a constant disjunction either -- ``tests/test_no_lying_docs.py``
enforces that across the whole suite and has caught four agents so far.
"""

from __future__ import annotations

import ast
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

#: The repo's source root, for reading ``bfinance/__init__.py`` by AST. The exception walk below
#: does not use it: it starts from the *imported* package directory, so it cannot drift from the
#: install the tests are actually exercising.
SRC = pathlib.Path(__file__).resolve().parents[1] / "src"

#: The eight opt-in modules, as dotted names. Seven capabilities plus the third-party client.
OPT_IN = (
    "bfinance.statements",
    "bfinance.sources",
    "bfinance.costs",
    "bfinance.flows",
    "bfinance.universe",
    "bfinance.ownership",
    "bfinance.nse.macro",
    "bfinance.trendlyne",
)

#: ``(dotted module, the import a caller writes, the package that must not hold it as an attribute,
#: the ``__all__`` that must not list it)``.  The import is spelled out per case rather than
#: synthesised, because ``bfinance.nse.macro`` is a grandchild: its ergonomic spelling is
#: ``from bfinance.nse import macro``, and the package that must stay clean is ``bfinance.nse``.
REACH = (
    ("bfinance.statements", "from bfinance import statements", "bfinance", "bfinance"),
    ("bfinance.sources", "from bfinance import sources", "bfinance", "bfinance"),
    ("bfinance.costs", "from bfinance import costs", "bfinance", "bfinance"),
    ("bfinance.flows", "from bfinance import flows", "bfinance", "bfinance"),
    ("bfinance.universe", "from bfinance import universe", "bfinance", "bfinance"),
    ("bfinance.ownership", "from bfinance import ownership", "bfinance", "bfinance"),
    ("bfinance.trendlyne", "from bfinance import trendlyne", "bfinance", "bfinance"),
    ("bfinance.nse.macro", "from bfinance.nse import macro", "bfinance.nse", "bfinance.nse"),
)


# ======================================================================== probes


def run_probe(program: str, *, search_path: pathlib.Path | None = None) -> str:
    """Run a program in a fresh interpreter and return its stdout, or fail loudly.

    ``search_path`` prepends a directory to ``PYTHONPATH`` so a control can point the *same* probe
    at a purpose-built package instead of at ``bfinance``. Nothing else is passed through, because
    a probe that inherits a caller's environment can be answered by it rather than by the code.
    """
    env = None
    if search_path is not None:
        existing = os.environ.get("PYTHONPATH", "")
        entries = [str(search_path)] + ([existing] if existing else [])
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(entries)}
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(program)],
        capture_output=True, text=True, timeout=180, env=env, check=False,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    return result.stdout.strip()


def nse_leaf_report(
    package: str, dotted_leaf: str, *, search_path: pathlib.Path | None = None
) -> str:
    """The two-token pair ``tests/test_statements_cascade.py`` already prints for a grandchild.

    First field: was the dotted leaf executed? Second field: does the parent package hold an
    attribute under the leaf's own name? Both must be ``False`` for a real package, and the control
    below shows both can be ``True`` -- a probe that could only ever print ``False False`` would
    leave the real test green no matter what the package did.
    """
    leaf = dotted_leaf.rsplit(".", 1)[-1]
    return run_probe(
        f"""
        import sys
        import {package}
        print({dotted_leaf!r} in sys.modules, {leaf!r} in dir(sys.modules[{package!r}]))
        """,
        search_path=search_path,
    )


def pull_report(
    package: str, opt_in: tuple[str, ...], *, search_path: pathlib.Path | None = None
) -> str:
    """What executing ``import <package>`` costs, in the two-token form already in use.

    ``tests/test_sources_cascade.py`` prints ``"no-sources []"`` for exactly this question over
    six modules and ``tests/test_statements_cascade.py`` prints ``"False False"`` for a pair. Both
    are re-used rather than a third grammar invented here: the first token says whether anything
    opted-in was executed, the second names what. The token reads ``sources`` whenever anything was
    pulled, so the control's answer is the same two-token line with the other token.
    """
    return run_probe(
        f"""
        import sys
        import {package}
        pulled = [m for m in {opt_in!r} if m in sys.modules]
        print("sources" if pulled else "no-sources", pulled)
        """,
        search_path=search_path,
    )


def reach_report(
    cases: tuple[tuple[str, str, str, str], ...],
    package: str,
    *,
    search_path: pathlib.Path | None = None,
) -> dict[str, dict]:
    """One probe answering four questions per module, in a fresh interpreter.

    Per ``(dotted, statement, holder, all_owner)`` it records whether the module was already
    executed, whether a bare ``getattr`` on its package raises :class:`AttributeError`, whether the
    package's ``__all__`` lists it, and -- only then -- whether the caller's ``from`` import
    actually executes it.

    The order matters and is the reason one subprocess answers all of it: the ``AttributeError``
    and the ``__all__`` facts are read *before* the ``from`` import, because performing that import
    sets the attribute as a side effect. A probe that ran them afterwards would be reading its own
    side effect and reporting ``resolved`` for every module, which is the exact defect under test.
    """
    program = f"""
    import sys
    import {package}
    report = {{}}
    for dotted, statement, holder_name, all_owner in {cases!r}:
        holder = sys.modules[holder_name]
        all_owner_module = sys.modules[all_owner]
        before = dotted in sys.modules
        try:
            getattr(holder, dotted.rsplit('.', 1)[-1])
            outcome = 'resolved'
        except AttributeError:
            outcome = 'AttributeError'
        listed = dotted.rsplit('.', 1)[-1] in all_owner_module.__all__
        exec(statement)
        report[dotted] = (before, dotted in sys.modules, outcome, listed)
    for dotted in sorted(report):
        print(dotted, *report[dotted])
    """
    report: dict[str, dict] = {}
    for line in run_probe(program, search_path=search_path).splitlines():
        dotted, before, after, outcome, listed = line.split()
        report[dotted] = {
            "already_loaded": before == "True",
            "loaded_by_the_from_import": after == "True",
            "bare_getattr": outcome,
            "listed_in_all": listed == "True",
        }
    return report


#: :data:`REACH` again, against :func:`eager_package`. Kept as a second literal rather than derived
#: from ``REACH`` so the control cannot silently follow the real cases into agreeing with them.
EAGER_CASES = (
    ("eagerpkg.statements", "from eagerpkg import statements", "eagerpkg", "eagerpkg"),
    ("eagerpkg.sources", "from eagerpkg import sources", "eagerpkg", "eagerpkg"),
    ("eagerpkg.costs", "from eagerpkg import costs", "eagerpkg", "eagerpkg"),
    ("eagerpkg.flows", "from eagerpkg import flows", "eagerpkg", "eagerpkg"),
    ("eagerpkg.universe", "from eagerpkg import universe", "eagerpkg", "eagerpkg"),
    ("eagerpkg.ownership", "from eagerpkg import ownership", "eagerpkg", "eagerpkg"),
    ("eagerpkg.trendlyne", "from eagerpkg import trendlyne", "eagerpkg", "eagerpkg"),
    ("eagerpkg.nse.macro", "from eagerpkg.nse import macro", "eagerpkg.nse", "eagerpkg.nse"),
)


@pytest.fixture(scope="module")
def eager_package(tmp_path_factory) -> pathlib.Path:
    """A package that breaks every rule in this file, on purpose, on disk.

    Built as real files rather than as ``types.ModuleType`` fakes so the controls exercise the
    import system's own behaviour -- ``__init__`` ordering, the parent-attribute side effect, the
    ``__all__`` a star import would read -- instead of a simulation of it. It is the *shape* of the
    violation that matters: a parent that imports its opt-in children eagerly and names them in
    ``__all__``, and a child package that imports its own leaf.
    """
    root = tmp_path_factory.mktemp("eager-root")
    package = root / "eagerpkg"
    (package / "nse").mkdir(parents=True)

    children = ("statements", "sources", "costs", "flows", "universe", "ownership", "trendlyne")
    leaves = ("macro", "marketlens")
    for child in children:
        (package / f"{child}.py").write_text(
            f'"""Deliberately empty. Only its existence and its import time are under test."""\n'
            f'__all__ = [{child!r}]\n',
            encoding="utf-8",
        )
    for leaf in leaves:
        (package / "nse" / f"{leaf}.py").write_text(
            f'"""Deliberately empty. Only its existence and its import time are under test."""\n'
            f'__all__ = [{leaf!r}]\n',
            encoding="utf-8",
        )

    # Mirrors the real `bfinance/__init__.py` exactly where it matters: eager imports of its own
    # children, one of them a module object re-exported out of a child package, and every opt-in
    # child named in `__all__`.
    (package / "__init__.py").write_text(
        '"""The violation: every opt-in child is executed at import and named in ``__all__``."""\n'
        f"from . import {', '.join(children)}\n"
        "from .nse import marketlens\n"
        f"__all__ = [{', '.join(repr(c) for c in (*children, 'marketlens'))}]\n",
        encoding="utf-8",
    )
    # The violation one level down: the child package imports the leaf it must leave alone. The
    # real `bfinance.nse` executes nothing it must not, and this one executes exactly that.
    (package / "nse" / "__init__.py").write_text(
        '"""The violation, one level down: the package imports the leaf it must leave alone."""\n'
        "from . import macro, marketlens\n"
        '__all__ = ["marketlens"]\n',
        encoding="utf-8",
    )
    return root


# ============================================ 1. importing bfinance costs nothing


class TestImportingBfinanceExecutesNoneOfThem:
    """The load-bearing claim, in the only place it can be asked honestly."""

    def test_importing_bfinance_pulls_in_none_of_the_seven_or_trendlyne(self):
        """``import bfinance`` must execute none of the eight.

        Subprocess because in-process ``sys.modules`` is already populated -- by this file's own
        ``import bfinance`` and, in a full run, by whichever test module imported which opt-in
        first. The answer is the empty list, in the two-token form
        ``tests/test_sources_cascade.py`` established.
        """
        assert pull_report("bfinance", OPT_IN) == "no-sources []", (
            "import bfinance executed an opt-in module. A caller who wanted one capability would "
            "now pay for all of them, and a bug in any of them would break `import bfinance`."
        )

    def test_importing_bfinance_nse_pulls_in_no_macro_either(self):
        """``bfinance.nse.macro`` is a grandchild, so the parent package is a second door.

        The top-level probe above would pass with a top-level re-export that only the
        ``bfinance.nse`` package performed. This is the two-token pair
        ``tests/test_statements_cascade.py`` already prints for this shape.
        """
        assert nse_leaf_report("bfinance.nse", "bfinance.nse.macro") == "False False", (
            "bfinance.nse executes or advertises its own leaf; the leaf contract is broken"
        )

    def test_the_pull_probe_goes_red_on_a_package_that_imports_them_eagerly(
        self, eager_package: pathlib.Path
    ) -> None:
        """The negative control, and this one is not optional.

        The probe above reads one line out of a fresh interpreter. If that line stopped varying --
        because the package grew a re-export the comparison missed, because ``sys.modules`` were
        read wrongly, or because the probe crashed and the comparison happened to be satisfied --
        the test would stay green while checking nothing. So the *same* probe function is run
        against :func:`eager_package`, which does the forbidden thing, and must return the other
        answer in the same two-token grammar.
        """
        eager = pull_report(
            "eagerpkg", tuple(case[0] for case in EAGER_CASES), search_path=eager_package
        )
        honest = pull_report("bfinance", OPT_IN)

        assert eager.startswith("sources "), (
            f"the probe cannot detect an eagerly imported module, so it is not testing "
            f"anything: {eager!r}"
        )
        assert sorted(ast.literal_eval(eager[len('sources '):])) == sorted(
            case[0] for case in EAGER_CASES
        ), f"the control did not import everything it claims to: {eager!r}"
        # And the two answers differ, so the probe discriminates rather than printing one word.
        assert honest != eager

    def test_the_nse_leaf_probe_goes_red_on_a_package_whose_child_imports_its_leaf(
        self, eager_package: pathlib.Path
    ) -> None:
        """The control for the grandchild probe, and it is not optional.

        ``eagerpkg.nse`` imports ``macro``, so both fields of the same probe must flip. Both flip
        for a reason worth recording rather than papering over: executing a submodule *always* sets
        the attribute on its parent, so the two fields cannot be broken independently -- a package
        cannot hold ``nse.macro`` without having executed it. The independence of the
        ``__all__``-listing field, which is the one claim a caller can break without executing
        anything, is proved by the *reach* control below instead, where the seven direct children
        flip it and the grandchild deliberately does not.

        What this control does establish is that neither position in the printed pair is stuck at
        ``False``. A probe that could only ever answer ``False False`` would leave the real test
        green no matter what the package did, which is the whole failure mode.
        """
        eager = nse_leaf_report("eagerpkg.nse", "eagerpkg.nse.macro", search_path=eager_package)
        honest = nse_leaf_report("bfinance.nse", "bfinance.nse.macro")

        assert eager == "True True", (
            f"the grandchild probe cannot see a child package that really does import its leaf, "
            f"so it is not testing anything: {eager!r}"
        )
        assert honest != eager


# ==================================== 2. the AST says so too, without a subprocess


def _bfinance_imports(path: pathlib.Path, package_name: str = "bfinance") -> set[str]:
    """Every module path ``path``'s import statements reach, as absolute dotted names.

    Two spellings reach a submodule, and both are reported:

    * ``import bfinance.costs`` and ``from .costs import X`` -- the *base* of the statement;
    * ``from .nse import macro`` -- the leaf, when the base is a package that contains it. This
      second form is the one a base-only scan cannot see, and it is the exact spelling
      ``from bfinance.nse import macro`` reduces to inside the package. It is resolved by joining
      the alias onto the base and letting the caller intersect with the opt-in set, so no name is
      spelled out here and nothing has to be updated when a module is added.

    Relative imports are resolved against the importing module's own package, because the point is
    to catch ``from .costs import ...`` -- and a raw ``node.module`` would read that as the bare
    string ``"costs"`` and match nothing at all. The package name is a parameter so the helper can
    be pointed at a purpose-built file by the control below; a fixed ``SRC`` would make it
    untestable without editing this repo.
    """
    parts = list(package_name.split(".")) if package_name else []
    if parts and path.name == "__init__.py":
        parts = parts[:-1] if len(parts) > 1 else parts
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            remaining = list(parts)
            for _ in range(max(node.level - 1, 0)):
                if remaining:
                    remaining.pop()
            base = ".".join([*remaining, node.module] if node.module else remaining) or "bfinance"
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def test_the_package_init_names_none_of_them_in_an_import_statement():
    """The same claim as the probe above, read from the AST, and it costs no interpreter.

    This is where the text-scan trap would bite. ``src/bfinance/__init__.py`` *names all eight
    modules by name* in its own module docstring, to explain why they are absent -- so a
    substring scan for ``bfinance.costs`` fires eight times on a file that is entirely correct.
    ``ast.parse`` discards comments, and a docstring is a statement rather than a name, so an
    ``Import``/``ImportFrom`` node is the only thing that can violate this rule.
    """
    offenders = sorted(_bfinance_imports(SRC / "bfinance" / "__init__.py") & set(OPT_IN))

    assert offenders == [], (
        f"bfinance/__init__.py imports {offenders} at import time, which is exactly the cost "
        f"the isolation removes. `from bfinance import <name>` reaches them without this."
    )


def test_the_init_import_scan_sees_a_relative_import_and_ignores_the_docstring_that_names_them(
    tmp_path: pathlib.Path,
) -> None:
    """The control for the AST scan above, and it is the one that matters most here.

    The scan's failure modes are opposite. Too loose -- scanning file text -- fires on the eleven
    lines of prose in the real ``__init__.py`` that name all eight modules to explain their absence,
    and the test would have to be deleted rather than fixed. Too tight -- reading ``node.module``
    raw -- would read ``from .costs import X`` as the bare string ``"costs"`` and match nothing, and
    it would miss ``from .nse import macro`` entirely, because the base of that statement is
    ``bfinance.nse`` and the thing that must not be imported is its child. A synthetic file is
    built that commits the real violation in all three spellings *and* names every module in
    prose, in a comment and in a string, and every half must come out right.
    """
    synthetic = tmp_path / "bfinance" / "__init__.py"
    synthetic.parent.mkdir(parents=True)
    synthetic.write_text(
        '"""Names every module in prose, exactly as the real __init__ does.\n'
        "bfinance.costs bfinance.flows bfinance.universe bfinance.ownership\n"
        "bfinance.statements bfinance.sources bfinance.nse.macro bfinance.trendlyne\n"
        '"""\n'
        "# bfinance.costs, bfinance.flows and a commented-out `from .statements import X`\n"
        "NAMES_IN_A_STRING = 'bfinance.sources bfinance.trendlyne bfinance.nse.macro'\n"
        "from .costs import CostScheduleError\n"
        "import bfinance.universe\n"
        "from .nse import macro\n"
        "from .nse import marketlens\n"
        "from .ticker import Ticker\n",
        encoding="utf-8",
    )

    found = _bfinance_imports(synthetic, "bfinance")

    assert found & set(OPT_IN) == {
        "bfinance.costs", "bfinance.universe", "bfinance.nse.macro",
    }, f"the scan resolved relative imports wrongly: {sorted(found)}"
    assert "bfinance.ticker" in found, "an ordinary relative import must still be seen"
    # `from .nse import marketlens` is what the real `__init__.py` does, and joining the alias onto
    # the base must not drag it into the opt-in set. If this ever fires, the scan has stopped
    # distinguishing "imports the leaf" from "imports a symbol that happens to share its name".
    assert "bfinance.nse.marketlens" in found
    assert "bfinance.nse.marketlens" not in OPT_IN
    # The prose, the comment and the string are the trap; none may register.
    assert not any(name in found for name in ("bfinance.flows", "bfinance.ownership",
                                              "bfinance.statements", "bfinance.sources",
                                              "bfinance.trendlyne")), (
        f"the scan is reading comments, docstrings or strings as imports: {sorted(found)}"
    )


# ================== 3+4. the ergonomic path exists, and the attributes do not


class TestTheFromImportReachesThemAndTheAttributesDoNotExist:
    """Both halves of the same deal, asked in one subprocess so the ordering is provable."""

    def test_the_from_import_reaches_every_opt_in_module_and_the_attribute_does_not_exist(self):
        """``from bfinance import costs`` works, and ``bfinance.costs`` raises ``AttributeError``.

        The four fields, per module: not already loaded; a bare ``getattr`` raises
        :class:`AttributeError` rather than resolving; the owning ``__all__`` does not list it; and
        the caller's own ``from`` import *does* then execute it. Any one of the first three going
        wrong means the top level is paying for the module; the fourth going wrong means the
        ergonomic path was removed instead of merely never being needed.

        The exception is caught rather than reported as ``hasattr(...) is False`` because
        ``hasattr`` answers ``False`` for *any* :class:`AttributeError` -- including one raised
        inside a module-level ``__getattr__`` or a property. Only the exception itself separates
        "there is nothing there" from "something is there and computing it failed", and the second
        is exactly the silent typo this check exists to catch.
        """
        report = reach_report(REACH, "bfinance")

        assert sorted(report) == sorted(OPT_IN), f"the probe answered for {sorted(report)}"
        for dotted, facts in sorted(report.items()):
            assert facts["already_loaded"] is False, (
                f"{dotted} was already executed by `import bfinance`; a caller who wanted one "
                f"capability would pay for all of them"
            )
            assert facts["bare_getattr"] == "AttributeError", (
                f"bfinance exposes {dotted.rsplit('.', 1)[-1]!r} as an attribute, so a typo in a "
                f"caller's module name resolves instead of failing loudly"
            )
            assert facts["listed_in_all"] is False, (
                f"{dotted.rsplit('.', 1)[-1]!r} is in the owning __all__, so `from bfinance import "
                f"*` would execute it for every caller"
            )
            assert facts["loaded_by_the_from_import"] is True, (
                f"`from bfinance import {dotted.rsplit('.', 1)[-1]}` did not execute {dotted}; "
                f"the ergonomic path this file is protecting has been removed"
            )

    def test_the_reach_probe_names_a_package_whose_init_resolves_them(
        self, eager_package: pathlib.Path
    ) -> None:
        """The negative control for the probe above.

        :func:`reach_report` is handed :func:`eager_package`, whose ``__init__`` imports every
        child and lists them, and whose ``nse`` package imports its own leaf. The seven children
        must flip all three of the real test's failure readings, and the grandchild flips two of
        them -- which is what turns ``False``/``False``/``AttributeError`` into evidence rather
        than a constant.
        """
        report = reach_report(EAGER_CASES, "eagerpkg", search_path=eager_package)

        assert sorted(report) == sorted(case[0] for case in EAGER_CASES)

        # The seven direct children break all three failure fields, so each of the three
        # assertions in the real test is separately unproved-and-therefore-proved.
        for dotted, facts in sorted(report.items()):
            if dotted.count(".") != 1:
                continue
            assert facts["bare_getattr"] == "resolved", (
                f"{dotted}: the probe cannot see an attribute the parent really does hold, so it "
                f"cannot be trusted to report the AttributeError of a real package"
            )
            assert facts["listed_in_all"] is True, (
                f"{dotted}: the probe cannot see a name the package really does list, so its "
                f"reading of `__all__` is not evidence"
            )
            assert facts["already_loaded"] is True, (
                f"{dotted}: the probe cannot see a module the parent really did execute, so its "
                f"reading of sys.modules is not evidence"
            )

        # The grandchild is the narrower control: `eagerpkg.nse` executes its leaf but -- exactly
        # like the real `bfinance.nse` -- never names it, so only two of the three failure fields
        # flip. The `listed_in_all` field therefore has no control here, which is why the seven
        # children above are the ones that carry it.
        grandchild = report["eagerpkg.nse.macro"]
        assert grandchild["bare_getattr"] == "resolved"
        assert grandchild["already_loaded"] is True
        assert grandchild["listed_in_all"] is False

        # The last field is the one the real package is *allowed* to share, and that is the point:
        # both packages reach the module on request, so only the other three discriminate.
        assert all(f["loaded_by_the_from_import"] for f in report.values())


# ================================================== 5. bfinance.__all__ is truthful


def test_the_top_level_all_resolves_every_name_and_repeats_none():
    """``__all__`` is a contract, and the top level has more ways to break it than the leaves.

    A name in ``__all__`` that does not resolve makes ``from bfinance import *`` raise, and a
    duplicate is a silent no-op in every one of the three legitimate uses (``import *``, Sphinx,
    a caller's introspection). No subprocess: resolution is monotone -- nothing in the suite
    deletes an attribute -- so an in-process check is not polluted the way ``sys.modules`` is.
    """
    assert isinstance(bfinance.__all__, list), "a tuple or other type changes what `import *` does"
    unresolved = sorted(name for name in bfinance.__all__ if not hasattr(bfinance, name))
    duplicates = sorted(
        {name for name in bfinance.__all__ if bfinance.__all__.count(name) > 1}
    )

    assert unresolved == [], f"bfinance.__all__ names {unresolved}, which do not resolve"
    assert duplicates == [], f"bfinance.__all__ repeats {duplicates}"
    # `__version__` is in there on purpose; a dunder in `__all__` is a choice, not an accident,
    # and this file would rather it be pinned than quietly removed.
    assert "__version__" in bfinance.__all__ and bfinance.__version__

    # Scoped deliberately: the "no module objects" rule that the eight opt-in modules are held to
    # is NOT applied to the top level, because `from .nse import marketlens` genuinely re-exports a
    # module and callers rely on it. Asserted here so that exception is a recorded decision.
    assert "marketlens" in bfinance.__all__ and isinstance(bfinance.marketlens, ModuleType)


# ================================================ 6. the two name collisions


class TestTheTwoSubmoduleAndSymbolCollisions:
    """Both are real, both are deliberate, and neither is documented anywhere a caller looks.

    When a module and a symbol share a name, the import machinery sets the submodule as an
    attribute on the parent *first* and the ``from .x import x`` rebinding happens *after* -- so
    which one wins is decided by statement order in ``__init__.py`` and by nothing else. That makes
    them choices, not accidents, and the third case in this repo is the proof: ``bfinance.nse``
    hits the same collision with ``bhavcopy`` and deliberately resolved it the other way, keeping
    the module so ``bfinance.nse.bhavcopy.parse_bhavcopy`` keeps working. Nothing in the language
    forced either answer, which is exactly why both need pinning.

    These run in-process, and can: the shadowing is a fact about a completed ``__init__``, and
    importing ``bfinance.screens`` does not change which object the attribute holds -- only the
    act of importing a module *for the first time* sets it, and ``__init__`` has already run. The
    control for them is ``test_a_module_without_the_collision_still_resolves_to_its_module``,
    which reads the other answer in the same process.
    """

    def test_download_resolves_to_the_function_and_not_to_the_module(self):
        """``from .download import download`` runs *after* the submodule import sets the parent
        attribute, so the function shadows the module of the same name.

        That ordering is the whole reason ``bfinance.download`` is callable: a caller writing
        ``bfinance.download(...)`` gets the API, not a module object they would have to subscript.
        Asserting only ``callable(bfinance.download)`` would not do -- a *module* is not callable
        but is also not a function, and the pair below is what separates the two objects.
        """
        module = sys.modules["bfinance.download"]

        assert isinstance(module, ModuleType), "bfinance.download is no longer a module at all"
        assert bfinance.download is not module, (
            "bfinance.download resolves to the module, so the documented `bfinance.download(...)` "
            "call is a TypeError"
        )
        assert inspect.isfunction(bfinance.download), (
            f"bfinance.download is {type(bfinance.download).__name__}, not the function"
        )

    def test_screens_resolves_to_the_registry_instance_and_not_to_the_module(self):
        """Same collision, other shape: ``screens`` is an *instance*, so it is indistinguishable
        from a module by ``callable`` or by ``isinstance(x, ModuleType)`` alone.

        The observable consequence is the whole point -- ``import bfinance.screens as s`` resolves
        through the parent attribute, so ``s`` is the :class:`ScreenerRegistry`, not a module, and
        a caller can write ``s.magic_formula`` without importing the submodule by path.
        """
        module = sys.modules["bfinance.screens"]
        registry = bfinance.screens

        assert isinstance(module, ModuleType), "bfinance.screens is no longer a module at all"
        assert registry is not module, "bfinance.screens resolves to the module, not the registry"
        assert type(registry).__name__ == "ScreenerRegistry", (
            f"bfinance.screens is a {type(registry).__name__}, not the registry instance"
        )

        import bfinance.screens as bound_by_import

        assert bound_by_import is registry, (
            "`import bfinance.screens as s` yielded the module rather than the registry, so the "
            "attribute a caller reaches through that spelling is not the one the package exports"
        )
        # The name is still reachable, from the module, for anyone who wants the class itself.
        assert module.ScreenerRegistry is type(registry)

    def test_a_module_without_the_collision_still_resolves_to_its_module(self):
        """The control for the two above, and it is the reason those two are evidence.

        Both collisions are *shadows*: the module object still exists in ``sys.modules`` under the
        same dotted name. So a check that only asked "is ``sys.modules[name]`` a module" would pass
        with the shadowing removed, and a check that only asked "is the attribute a module" would
        pass on a package that had lost the module entirely. ``bfinance.ticker`` is imported by
        ``__init__`` and never rebound, so it is the other answer, in the same process, on the
        same two questions.
        """
        assert sys.modules["bfinance.ticker"] is bfinance.ticker, (
            "bfinance.ticker no longer resolves to its own module, so the two collision tests can "
            "no longer tell a shadow from a plain module"
        )
        assert sys.modules["bfinance.screens"] is not bfinance.screens
        assert sys.modules["bfinance.download"] is not bfinance.download


# ================================== 7. every opt-in module declares an __all__


def _future_import_names(module: ModuleType) -> set[str]:
    """Names bound by ``from __future__ import ...``.

    ``annotations`` lands in the module namespace as a ``__future__._Feature``. It is a compiler
    directive, not surface: a star import carrying it would hand a caller a feature object, and a
    completeness rule that demanded it would be demanding a lie. Found by AST so the exclusion is
    the *rule* and not a growing list of names.
    """
    tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8-sig"))
    return {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "__future__"
        for alias in node.names
    }


def public_names_the_module_defines(module: ModuleType) -> set[str]:
    """The public names ``module`` **defines**, in the two forms that are observable.

    **Callables and classes, for every module.** A function or class records where it was defined
    in ``__module__``, so ownership is a fact rather than a guess: a name whose ``__module__`` is
    this module was written here, and one whose ``__module__`` is a sibling is a re-export the
    module does not owe an ``__all__`` entry for.

    **Every own name, for a package.** A package's namespace *is* its curated facade -- it holds no
    working set, only what it chose to expose -- so every public attribute that is not a submodule
    and not a ``__future__`` directive belongs in its ``__all__``. A plain module is different: its
    namespace also holds ``logger``, ``httpx`` and the tunables, and a bare constant carries no
    ``__module__`` at all, so "public" is not something this rule can read off one. That limit is
    stated rather than papered over; the AST-level completeness rule with its two named
    exceptions lives in ``tests/test_error_vocabulary.py``.

    Derived from the module, never from a hardcoded list, so a new class is counted the moment it
    is written and a deleted one stops being counted without a line being edited here.
    """
    defined = {
        name
        for name, value in vars(module).items()
        if not name.startswith("_")
        and getattr(value, "__module__", None) == module.__name__
    }
    if getattr(module, "__path__", None) is not None:
        future = _future_import_names(module)
        defined |= {
            name
            for name, value in vars(module).items()
            if not name.startswith("_")
            and not isinstance(value, ModuleType)
            and name not in future
        }
    return defined


class TestEveryOptInModuleDeclaresAnHonestAll:
    """``__all__`` is the declaration of public surface, and it lies in both directions."""

    @pytest.mark.parametrize("module_name", OPT_IN)
    def test_it_declares_all_and_no_entry_is_a_module_object(self, module_name: str) -> None:
        """Both the existence and the first half of the truthfulness.

        A name in ``__all__`` that resolves to a module means a star import handed a caller
        another library's module object -- ``pd``, ``httpx``, ``datetime``, whatever it is called
        here. Checked by resolution rather than by name, because a rename of the import defeats a
        list and this rule should not need updating when that happens. This was a real defect:
        three of these modules declared no ``__all__`` at all, which is what
        ``tests/test_error_vocabulary.py`` documents.
        """
        module = importlib.import_module(module_name)

        assert hasattr(module, "__all__"), (
            f"{module_name} declares no __all__, so `from {module_name} import *` binds its "
            f"entire namespace -- every dependency it imports included"
        )
        assert isinstance(module.__all__, (list, tuple)), (
            f"{module_name}.__all__ is {type(module.__all__).__name__}, which `import *` reads "
            f"differently from a list"
        )
        assert all(isinstance(name, str) and name for name in module.__all__), (
            f"{module_name}.__all__ carries a non-string or empty name"
        )
        assert len(module.__all__) == len(set(module.__all__)), (
            f"{module_name}.__all__ repeats a name"
        )

        leaked = sorted(
            name for name in module.__all__
            if isinstance(getattr(module, name, None), ModuleType)
        )
        assert leaked == [], f"{module_name}.__all__ exports module objects: {leaked}"

    @pytest.mark.parametrize("module_name", OPT_IN)
    def test_no_public_name_the_module_defines_is_missing_from_all(self, module_name: str) -> None:
        """The other direction, and the one that is easier to forget.

        An over-complete ``__all__`` is caught above. An under-complete one is the same class of
        quiet lie: a class the module wrote and a caller cannot reach by name, a star import that
        silently omits the one function the docs pointed at.
        """
        module = importlib.import_module(module_name)

        missing = sorted(public_names_the_module_defines(module) - set(module.__all__))

        assert missing == [], (
            f"{module_name} defines {missing} and does not list them in __all__. The declaration "
            f"is supposed to describe the public surface, not a subset of it."
        )

    def test_both_directions_of_the_all_check_fire_on_a_module_that_lies(
        self, tmp_path: pathlib.Path
    ) -> None:
        """The control for the two tests above, in memory and decidable.

        The derived set is the only part of this lint that could quietly stop working -- a
        ``getattr`` default, an ``isinstance`` that stopped matching, a branch on ``__path__`` that
        stopped firing -- and a lint that reports "nothing missing" from a broken helper is the most
        expensive kind of green. So a plain module and a *package* are built here, each wrong in a
        different direction, and the same helper is run over both.
        """
        # A plain module: one class declared, one written and forgotten, one dependency listed.
        probe = ModuleType("bfinance.probe_surface")
        probe.Declared = type("Declared", (), {"__module__": probe.__name__})
        probe.Undeclared = type("Undeclared", (), {"__module__": probe.__name__})
        probe.logging = __import__("logging")
        probe.__all__ = ["Declared", "logging"]

        defined = public_names_the_module_defines(probe)
        assert {"Declared", "Undeclared"} <= defined, (
            f"the helper stopped recognising names the module defines: {sorted(defined)}"
        )
        assert sorted(defined - set(probe.__all__)) == ["Undeclared"], (
            "the completeness direction cannot see a name the module wrote and forgot to declare"
        )
        assert sorted(
            name for name in probe.__all__
            if isinstance(getattr(probe, name, None), ModuleType)
        ) == ["logging"], "the module-object direction cannot see a dependency in __all__"

        # A package: the second arm, which is the only thing that sees a bare constant. A constant
        # carries no `__module__`, so without the package arm it is invisible to the lint -- and
        # `bfinance.statements`/`sources`/`trendlyne` are exactly the case that arm exists for, so
        # it has to be proved rather than assumed. The `from __future__ import annotations` is the
        # other half: `bfinance.sources` really does bind one, and a rule that demanded it in
        # `__all__` would be demanding a lie.
        source = tmp_path / "probe_package.py"
        source.write_text(
            "from __future__ import annotations\nTUNABLE = 2000.0\n", encoding="utf-8"
        )
        package = ModuleType("bfinance.probe_package")
        package.__file__ = str(source)
        package.__path__ = []  # what makes a module a package to the helper
        package.TUNABLE = 2000.0
        package.annotations = __import__("__future__").annotations
        package.submodule = ModuleType("bfinance.probe_package.submodule")
        package.__all__ = []

        assert _future_import_names(package) == {"annotations"}, (
            "the `__future__` exclusion is not reading the AST any more, so `bfinance.sources` "
            "would be reported as incomplete for binding a compiler directive"
        )
        assert public_names_the_module_defines(package) == {"TUNABLE"}, (
            "the package arm cannot see a bare constant, or cannot exclude a submodule and a "
            "`__future__` directive, so the completeness rule is blind on the three packages it "
            f"is aimed at: {sorted(public_names_the_module_defines(package))}"
        )
        assert sorted(public_names_the_module_defines(package) - set(package.__all__)) == [
            "TUNABLE"
        ]

        # And a declared name that does not resolve must be visible to the resolution check too,
        # or the `hasattr` assertion above is the only thing standing between a broken `__all__`
        # and a caller.
        probe.__all__.append("Gone")
        assert not hasattr(probe, "Gone")
        assert [n for n in probe.__all__ if not hasattr(probe, n)] == ["Gone"]


# ================================================= 8. exactly one exception root


def _every_module_under_src() -> list[tuple[str, ModuleType]]:
    """``(dotted name, module)`` for every non-``__init__`` file under ``src/bfinance``.

    ``__init__.py`` files are skipped on purpose: a re-export's ``__module__`` points at the module
    that *defined* the class, so walking a package's ``__init__`` would count one class once per
    package that re-exports it and the count below would be a count of aliases, not of classes.

    Collected by import rather than by string match, because a lint about class *inheritance*
    cannot be answered by reading text: ``class X(BFinanceError):`` and a comment quoting that line
    are indistinguishable to a substring scan, and ``ast`` would still have to resolve the base
    name. Importing gives the real ``__bases__``.
    """
    root = pathlib.Path(bfinance.__file__).resolve().parent
    names: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        parts = list(path.relative_to(root).with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts.pop()
        names.append(".".join(["bfinance", *parts]))
    # Importing, not guessing: a wrong derived name raises here rather than quietly contributing
    # zero classes, which is the only way this collector could under-count.
    return [(name, importlib.import_module(name)) for name in names]


def exception_classes_defined_by(module_name: str, module: ModuleType) -> dict[str, type]:
    """The exception classes this module *defines*, keyed by name.

    The ``__module__`` comparison is the ownership test: an imported name is reported by the module
    that defined it, so one class cannot be counted twice and a re-export cannot invent one.
    """
    return {
        name: value
        for name, value in vars(module).items()
        if isinstance(value, type)
        and issubclass(value, BaseException)
        and value.__module__ == module_name
    }


def exception_roots(modules: list[tuple[str, ModuleType]]) -> dict[str, str]:
    """Class name -> defining module, for every class deriving *directly* from ``Exception``.

    ``__bases__ == (Exception,)`` rather than ``Exception in __bases__``: a class that adds a
    second base has chosen a shape this rule does not describe, and counting it here would make the
    number depend on a decision the rule never made.
    """
    return {
        name: module_name
        for module_name, module in modules
        for name, cls in exception_classes_defined_by(module_name, module).items()
        if cls.__bases__ == (Exception,)
    }


def is_an_argument_shape_refusal(cls: type) -> bool:
    """Does this class refuse an argument rather than report a failure to answer?

    "You passed the wrong thing" and "bfinance could not answer" are different ideas, and a
    package that wants one ``except`` for everything cannot afford to conflate them. The one
    permitted exception to the single-root rule is the first kind, and it is recognised by
    *shape* -- a single base that is a builtin other than ``Exception`` -- not by name, because a
    name list goes stale the moment nobody adds a row and a rule does not.

    ``len(cls.__bases__) == 1`` is load-bearing: ``BasisMismatchError(SourceError, ValueError)``
    mixes a package base with a builtin one and is *not* an argument-shape refusal, because the
    caller can catch it with the package root. Recognising it by "has a ``ValueError`` somewhere in
    its MRO" would have got that backwards.
    """
    bases = cls.__bases__
    return (
        len(bases) == 1
        and isinstance(bases[0], type)
        and bases[0].__module__ == "builtins"
        and bases[0] is not Exception
    )


class TestThereIsExactlyOneExceptionRoot:
    def test_exactly_one_class_derives_directly_from_exception_and_it_is_bfinance_error(self):
        """One root, so ``except BFinanceError`` catches the package.

        The **count** is the assertion, not a list of approved class names: a list is a checklist
        that goes stale the moment nobody adds a row, and a count cannot. A second root is not a
        style question -- it is a caller who has to learn a second ``except`` clause, and a
        handler that silently stops catching.

        The name is asserted as well, because a root that is not ``BFinanceError`` would satisfy
        the count while failing every ``from bfinance import BFinanceError`` in the world.
        """
        modules = _every_module_under_src()
        roots = exception_roots(modules)

        assert len(roots) == 1, (
            f"{len(roots)} classes derive directly from Exception ({sorted(roots)}). One root "
            f"means `except BFinanceError` catches the package; two means it does not."
        )
        assert next(iter(roots)) == "BFinanceError", (
            f"the one root is {next(iter(roots))}, so `except BFinanceError` no longer catches it"
        )
        # A walk that matched nothing would also report one root, so the tree size is derived from
        # the filesystem in a second, independent pass: every ``.py`` under the package is either
        # an ``__init__`` (skipped by design) or one of the walked modules. No threshold and no
        # hand-written module list, both of which would rot the first time a file is added.
        every_file = sorted(pathlib.Path(bfinance.__file__).resolve().parent.rglob("*.py"))
        assert len(modules) == len([p for p in every_file if p.name != "__init__.py"]), (
            f"the walk saw {len(modules)} modules for "
            f"{len([p for p in every_file if p.name != '__init__.py'])} non-__init__ files; the "
            f"count of roots above is being read off an incomplete tree"
        )
        assert len({module_name for module_name, _ in modules}) == len(modules), (
            "the walk derived the same module name twice, so a class could be counted twice"
        )

    def test_every_other_exception_is_under_the_root_or_refuses_an_argument(self):
        """The partition, which is the claim the single root exists to support.

        Three buckets, all decidable: the root itself; a class under it, catchable with one
        ``except``; and an argument-shape refusal, which is not, and says so by shape. A class
        landing in none of them is the failure worth catching -- a base class defined in a
        dependency, or a second root discovered from a different angle.
        """
        unclassified = {
            f"{module_name}.{name}": [b.__name__ for b in cls.__bases__]
            for module_name, module in _every_module_under_src()
            for name, cls in exception_classes_defined_by(module_name, module).items()
            if cls.__bases__ != (Exception,)
            and not issubclass(cls, bfinance.BFinanceError)
            and not is_an_argument_shape_refusal(cls)
        }

        assert unclassified == {}, (
            f"these exceptions are catchable by neither BFinanceError nor an argument-shape "
            f"rule: {unclassified}"
        )

    def test_the_root_lint_goes_red_on_a_second_root_defined_in_memory(self) -> None:
        """The control, and the brief requires it: a count cannot be trusted unproved.

        A lint that reports "exactly one" because it found nothing would be indistinguishable from
        a correct one, and a lint that reports "exactly one" because it found one out of two would
        be worse. So a second root is constructed in memory, with a ``__module__`` that names a
        real scanned module so it passes the ownership test, and the same collector must then
        report two.

        The three companions are there so the second root cannot be absorbed by another rule on the
        way: one is under the root, one is a pure argument-shape refusal, and one mixes the two --
        the same three shapes the real tree contains, so the count is proved to move only because a
        genuine second root appeared.
        """
        name = "bfinance.utils.exceptions"
        synthetic = ModuleType(name)
        synthetic.SecondRootError = type(
            "SecondRootError", (Exception,), {"__module__": name}
        )
        synthetic.NotARoot = type("NotARoot", (bfinance.BFinanceError,), {"__module__": name})
        synthetic.DealsWithValueErrors = type(
            "DealsWithValueErrors", (ValueError,), {"__module__": name}
        )
        synthetic.Mixed = type(
            "Mixed", (bfinance.BFinanceError, ValueError), {"__module__": name}
        )

        assert list(exception_roots([(name, synthetic)])) == ["SecondRootError"], (
            "the collector cannot see a root it was handed, so the count of 1 above is not "
            "evidence of anything"
        )
        assert is_an_argument_shape_refusal(synthetic.DealsWithValueErrors), (
            "the argument-shape rule no longer accepts a single builtin base, so the second root "
            "could have been excused by that rule instead of counted"
        )
        assert not is_an_argument_shape_refusal(synthetic.Mixed), (
            "a class mixing a package base with a builtin one is being excused, which is the case "
            "the rule exists to reject"
        )
        assert not is_an_argument_shape_refusal(synthetic.NotARoot)
        assert len(exception_roots([*_every_module_under_src(), (name, synthetic)])) == 2, (
            "the count does not move when a second root is added, so it is not a count"
        )

    def test_the_argument_shape_rule_reads_shape_and_not_the_builtin_it_happens_to_be(self):
        """Both sides of the rule, on the real classes and on the two shapes it must separate.

        ``DealsSnapshotMismatch(ValueError)`` is the permitted one. ``BasisMismatchError`` mixes a
        package base with ``ValueError`` and is *not* -- its caller can still catch it with the
        package root, so treating it as an argument-shape refusal would hide it from exactly the
        ``except`` the single root exists to enable. A rule written as "some builtin in the MRO"
        cannot tell those apart, which is why the rule is about the single direct base.
        """
        everything = self._every_exception()
        single_builtin_base = [
            cls for cls in everything.values() if is_an_argument_shape_refusal(cls)
        ]
        mixed = [
            (name, cls)
            for name, cls in sorted(everything.items())
            if ValueError in cls.__mro__[1:-1] and not is_an_argument_shape_refusal(cls)
        ]

        assert single_builtin_base, (
            "no exception in src/ has a single builtin base any more, so the argument-shape rule "
            "has stopped being exercised and this whole rule is untested"
        )
        assert mixed, (
            "no exception mixes a package base with a builtin one any more; the case the rule "
            "exists to reject has disappeared and the rule above may have simplified itself into "
            "something wrong"
        )
        for cls in single_builtin_base:
            assert not issubclass(cls, bfinance.BFinanceError), (
                f"{cls.__name__} is under the root, so the argument-shape rule is redundant for it"
            )
        for name, cls in mixed:
            assert any(b.__module__ != "builtins" for b in cls.__bases__), (
                f"{name} reaches ValueError with no package base among its direct bases, so it is "
                f"an argument-shape refusal and the rule is rejecting it for the wrong reason"
            )

    @staticmethod
    def _every_exception() -> dict[str, type]:
        return {
            name: cls
            for module_name, module in _every_module_under_src()
            for name, cls in exception_classes_defined_by(module_name, module).items()
        }


# ============================ 9. every exported exception is under the exported root


@pytest.mark.parametrize("module_name", (*OPT_IN, "bfinance"))
def test_every_exported_exception_is_under_the_exported_root(module_name: str) -> None:
    """What the count in the class above buys a caller, asserted on the actual exports.

    A caller writes ``except bfinance.BFinanceError`` and expects the package's refusals to land
    there. That is only true if the *exported* surface says so: a class that exists, is raised, and
    is not in any ``__all__`` is invisible to this check, and a class that is in an ``__all__`` and
    off the root is a second ``except`` clause a caller has to learn about by being bitten.

    ``DealsSnapshotMismatch`` is the one permitted departure and it earns its place by refusing an
    argument, which the same shape rule as above recognises -- not because its name is on a list.

    That rule is a filter, and a filter that accepts everything turns the assertion below into a
    tautology. Its control lives beside it:
    ``TestThereIsExactlyOneExceptionRoot`` proves the rule accepts a single builtin base and rejects
    a mixed one, both in memory and on the real tree, so ``off_root`` can only ever be empty
    because every exported exception really is catchable.
    """
    module = importlib.import_module(module_name)
    exported = {
        name: value
        for name, value in ((n, getattr(module, n, None)) for n in module.__all__)
        if isinstance(value, type) and issubclass(value, BaseException)
    }

    assert exported, f"{module_name} exports no exception at all; this check is now vacuous"
    off_root = {
        name: value
        for name, value in exported.items()
        if not issubclass(value, bfinance.BFinanceError)
        and not is_an_argument_shape_refusal(value)
    }

    assert off_root == {}, (
        f"{module_name} exports exceptions a caller cannot catch with `except "
        f"BFinanceError`: {sorted(off_root)}. Either bring them under the root or accept that the "
        f"package has no single root."
    )
    # `BFinanceError` must itself be reachable from the top level, or the clause above is advice
    # nobody can follow. Checked on the top level only, because the opt-ins inherit it.
    if module_name == "bfinance":
        assert issubclass(bfinance.BFinanceError, Exception)
