"""
Guards against the two ways a comment can lie without anyone noticing.

The class of defect
-------------------
A docstring is not executed, so it never fails. A prose claim -- "this engine generates
strikes, Greeks and IV metrics", "next 4 expiry dates", "returns zeros" -- survives every
test run in the repo, including the run that deleted the code it describes. That is how
``market/derivatives.py`` ended up advertising deterministic placeholder open interest
for a fabricator that had already been deleted on correctness grounds, and how
``get_upcoming_expiries()`` kept promising 4 dates while returning 3.

Two guards live here:

**Named-tripwire guards** (the first two tests). A claim tied to a specific name, a
specific import or a specific word is exactly checkable, so it is checked exactly.
These fail when the deleted code is pasted back, which is the realistic way it returns.

**The cross-reference lint** (the last test). The general form -- "fail when a docstring
claims a capability the module lacks" -- is not checkable, because "claims" and "lacks"
are both judgements. What *is* mechanically decidable is a strict subset of it: a docstring
that cross-references a symbol the module does not define. That subset is what shipped;
see that test's docstring for why, and for why the broader lint was rejected.

Everything here is offline: real classes, real docstrings, real calls, no network.
"""

import ast
import inspect
import pathlib
import re
from datetime import datetime

import pytest

from bfinance.market.derivatives import YF_CHAIN_COLUMNS, DerivativesEngine

#: The repo's importable package root.
SRC = pathlib.Path(__file__).resolve().parents[1] / "src"

#: The two files this agent owns. The cross-reference lint is scoped to these, not to all
#: of ``src/`` -- see ``test_no_docstring_cross_references_a_symbol_the_module_lacks``.
OWNED_MODULES = (
    SRC / "bfinance" / "ai" / "context.py",
    SRC / "bfinance" / "market" / "derivatives.py",
)

#: Sphinx cross-reference roles. ``~`` hides the module path in rendered output and has
#: no bearing on resolution.
_XREF_ROLE = re.compile(r":(?:meth|attr|func|class|mod|data|exc):`~?([A-Za-z_][A-Za-z0-9_.]*)`")


def _source_files(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted(p for p in root.rglob("*.py") if p.is_file())


def _module_names(path: pathlib.Path) -> set[str]:
    """Every name a module binds: defs, classes, imports, assignments, arguments."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.alias):
            names.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


# --------------------------------------------------------------------------- #
# 1. The seeded-RNG fabricator must not come back by copy-paste
# --------------------------------------------------------------------------- #

def test_the_seeded_contract_rng_name_appears_nowhere_in_src():
    """
    ``_contract_rng`` was a SHA-256-seeded ``random.Random`` that fabricated open interest
    and volume. It had zero callers -- an AST walk of ``src/`` and ``tests/`` found no
    reference at all -- and it was deleted along with the ``hashlib`` and ``random``
    imports that existed only to serve it.

    The realistic way it returns is a copy-paste from git history, which is why this is a
    name test and not a behaviour test: the moment the identifier is back in the tree the
    fabrication path is back with it, whatever the caller does or does not use.
    """
    hits = [str(p.relative_to(SRC)) for p in _source_files(SRC) if "_contract_rng" in p.read_text(encoding="utf-8-sig")]

    assert hits == [], (
        f"the deleted option-chain fabricator is back in the tree: {hits}. If a real "
        f"chain is being synthesised, that is a data-integrity defect, not an "
        f"optimisation opportunity."
    )
    assert not hasattr(DerivativesEngine, "_contract_rng"), (
        "DerivativesEngine._contract_rng exists again; see the deleted implementation in "
        "git history for why it had to go."
    )


def test_derivatives_no_longer_imports_what_only_the_fabricator_needed():
    """
    ``hashlib`` and ``random`` were imported by ``derivatives.py`` solely to build the
    seeded RNG. Left behind, they are the visible half of a deletion that did not finish:
    a reader cannot tell whether they are load-bearing or leftovers.

    Checked over the AST's import nodes, not over the file text, because this module's
    ``generate_option_chain`` docstring legitimately *names* ``random.Random`` while
    explaining that it was stdlib and has been removed. A text search would fire on that
    and be wrong -- which is the false positive that killed the broader lint.
    """
    tree = ast.parse((SRC / "bfinance" / "market" / "derivatives.py").read_text(encoding="utf-8-sig"))
    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    stale = imported & {"hashlib", "random", "secrets", "uuid"}
    assert stale == set(), (
        f"derivatives.py imports {sorted(stale)} but has no randomness left to justify "
        f"them. Either the fabricator is back, or these are dead imports."
    )


def test_the_derivatives_docstrings_do_not_advertise_a_deleted_feed():
    """
    The old module docstring said, in the present tense, that "all volumes/OI are
    deterministic placeholders seeded by contract symbol" and that ticker-level gating
    "must happen in ticker.py (NEEDS-MAIN)". Every clause was false by the time it was
    read: the gating was done (``ticker.py`` passes no ``fo_symbols``, so
    ``Ticker.options == ()``) and nothing generated a volume or an open interest.

    A stub scan over ``src/`` matched the word "placeholder" here, which is why it is
    pinned. Scoped to this module on purpose: ``nse/bhavcopy.py`` uses the word in a
    comment about a *parser* default, which is a true statement about parser plumbing and
    must not be caught by a tripwire aimed at fabricated market data.
    """
    source = (SRC / "bfinance" / "market" / "derivatives.py").read_text(encoding="utf-8-sig")
    module_doc = ast.get_docstring(ast.parse(source)) or ""
    class_doc = inspect.getdoc(DerivativesEngine) or ""

    for doc, what in ((module_doc, "module docstring"), (class_doc, "class docstring")):
        assert "placeholder" not in doc, (
            f"the {what} advertises placeholder market data again. Nothing in this "
            f"module produces a volume, an open interest, a Greek or an IV."
        )
        assert "NEEDS-MAIN" not in doc, (
            f"the {what} still carries the stale NEEDS-MAIN tracking marker; the "
            f"ticker-level gating it refers to has been done."
        )

    # The class docstring used to read "engine generating strike matrices, Greeks, and
    # IV metrics", which is a capability claim for code that no longer exists.
    for claim in ("generating", "Greeks"):
        assert claim not in class_doc, (
            f"the class docstring claims {claim!r} again; that code was deleted."
        )


# --------------------------------------------------------------------------- #
# 2. Docstring claims that can be checked exactly
# --------------------------------------------------------------------------- #

def test_empty_option_chain_is_a_zero_row_frame_not_a_frame_of_zeros():
    """
    Pinned because the docstring was imprecise in a way that invites a future default.

    "returns zeros" reads as a frame full of ``0.0``, which is a *readable quote*. What
    it actually returns is a frame with **no rows at all**: ``(0, 13)`` on each side, and
    0 non-null cells, carrying ``YF_CHAIN_COLUMNS`` so a caller can index it without a
    KeyError. A zero-row frame cannot be mistaken for data; a zero-filled one can, which
    is exactly the failure this module was cleaned up to avoid.
    """
    chain = DerivativesEngine.empty_option_chain()

    for side, frame in (("calls", chain.calls), ("puts", chain.puts)):
        assert frame.shape == (0, 13), f"{side} is {frame.shape}, expected a zero-row (0, 13) frame"
        assert int(frame.notna().sum().sum()) == 0, f"{side} has non-null cells; it must be empty"
        assert list(frame.columns) == YF_CHAIN_COLUMNS, f"{side} lost its yfinance columns"
    assert len(YF_CHAIN_COLUMNS) == 13, "the (0, 13) shape and the column list disagree"


def test_get_upcoming_expiries_returns_only_future_thursdays_and_the_docstring_agrees():
    """
    The docstring promised "next 4" unconditionally. It returns 3, because the current
    month is dropped once its last Thursday is more than a day behind ``today``.

    Checked against the calendar, not against a golden tuple -- a golden tuple of dates
    would have to be rewritten every month and would rot into a test nobody trusts:

    * every returned date is a Thursday, and none is in the past;
    * the dates strictly ascend and are unique;
    * the count is 3 or 4, never 5 and never fewer than 3;
    * and the docstring's "3 or 4" claim is present, so the prose and the return value
      are pinned to each other rather than one of them drifting.
    """
    expiries = DerivativesEngine.get_upcoming_expiries()
    today = datetime.today().date()
    parsed = [datetime.strptime(e, "%Y-%m-%d") for e in expiries]

    assert expiries, "no expiries returned at all"
    for raw, when in zip(expiries, parsed):
        assert when.strftime("%A") == "Thursday", f"{raw} is a {when.strftime('%A')}"
        assert when.date() >= today, f"{raw} is in the past (today is {today})"
    assert len(set(expiries)) == len(expiries), f"duplicate expiry dates: {expiries}"
    assert parsed == sorted(parsed), f"expiry dates are not ascending: {expiries}"
    assert 3 <= len(expiries) <= 4, f"expected 3 or 4 expiries, got {len(expiries)}: {expiries}"

    doc = inspect.getdoc(DerivativesEngine.get_upcoming_expiries) or ""
    assert "3 or 4" in doc, (
        "the docstring no longer states the 3-or-4 count, but it returns 3 or 4. Either "
        "state the real count or change the function; do not leave the two disagreeing."
    )
    assert "next 4" not in doc.replace('"next 4"', "").replace("``next 4``", ""), (
        "the docstring is promising an unconditional 'next 4' again, which the function "
        "does not deliver for the 1-6 days after the current month's last Thursday."
    )


# --------------------------------------------------------------------------- #
# 3. The lints
# --------------------------------------------------------------------------- #

def test_no_assertion_in_the_suite_is_a_constant_disjunction():
    """
    A test that cannot fail is worse than no test: it occupies a slot in the report, it
    survives every review, and it makes the files around it look guarded.

    Found in this repo, live and offline, in the very file whose job is to make the 0.2.0
    statement-order break impossible to land silently::

        assert df.columns.is_monotonic_increasing is False or True

    ``is`` binds tighter than ``or``, so that parses as ``(x is False) or True`` and is
    True for every possible value of ``x``.

    The rule is exactly the two constant folds, not a general "weak assertion" heuristic:
    ``<anything> or True`` is always True, and ``<anything> and False`` is always False.
    Anything cleverer would have opinions about intent, and those are false-positive
    generators in a codebase where plenty of assertions are legitimately loose.

    Verified against the whole suite: this was the only hit, so shipping it costs nothing.
    """
    offenders: list[str] = []
    tests_dir = pathlib.Path(__file__).resolve().parent
    for path in sorted(tests_dir.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assert):
                continue
            test = node.test
            if not isinstance(test, ast.BoolOp):
                continue
            for value in test.values:
                if isinstance(value, ast.Constant) and (
                    (isinstance(test.op, ast.Or) and value.value is True)
                    or (isinstance(test.op, ast.And) and value.value is False)
                ):
                    offenders.append(
                        f"{path.relative_to(tests_dir.parent)}:{node.lineno} "
                        f"{ast.unparse(test)}"
                    )

    assert offenders == [], (
        "assertion(s) that cannot fail -- `X or True` is always True and `X and False` is "
        f"always False: {offenders}"
    )


def test_no_docstring_cross_references_a_symbol_the_module_lacks():
    """
    The shippable half of "a docstring must not claim a capability the module lacks".

    A general version of that lint is not writable: "claims" and "lacks" are judgements,
    and a keyword scan over capability words ("generates", "fetches", "returns data")
    fires on every honest sentence that describes intent, on every changelog-style comment,
    and on every sentence explaining what was *removed*. There is a demonstrated false
    positive for it in this very repo, in the docstring rewritten alongside this file:
    ``generate_option_chain`` names ``random.Random`` to explain that the old fabricator
    was stdlib and is gone, and ``derivatives.py`` correctly does not import ``random``.
    A "docstrings may only name modules they import" rule fails on that sentence, and
    fails on it for the right reason -- the sentence is accurate. The alternative that
    would be needed is a per-claim allow-list (a registry of claim -> check), which is a
    hand-maintained checklist, not a lint, and it goes stale the moment nobody remembers
    to add a row.

    What is left is mechanically decidable and was a real instance of this bug class: a
    docstring cross-referencing a symbol the module does not define. ``derivatives.py``
    did exactly that, pointing at the deleted seeded RNG, so a reader was told to look up
    a method that was not there.

    Two deliberate restrictions keep it sound:

    * only *unqualified* references are checked. ``:meth:`Ticker.option_chain``` names a
      symbol in another module and is perfectly legitimate; a bare ``:meth:`foo``` cannot
      be anything but local, so requiring it to resolve is correct, not a guess.
    * it is scoped to the two modules this change owns, not to all of ``src/``. Widening
      it is a one-line change to ``OWNED_MODULES`` and is the intended follow-up, but
      only after the three pre-existing unresolved references it would immediately catch
      are fixed -- ``market/corporate.py`` (module docstring -> ``resolve_actions``),
      ``screener/client.py`` (``_parse_peers``) and ``trendlyne/client.py``
      (``SNAPSHOT_ONLY_ENDPOINTS``). Those live in files under active edit by other
      work, so the honest move today is to keep the lint and record the blockers rather
      than ship a red suite or edit files this change does not own.
    """
    dangling: list[str] = []
    for path in OWNED_MODULES:
        source = path.read_text(encoding="utf-8-sig")
        bound = _module_names(path)
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            doc = ast.get_docstring(node)
            if not doc:
                continue
            where = getattr(node, "name", "<module>")
            for match in _XREF_ROLE.finditer(doc):
                target = match.group(1)
                if "." in target or target in bound:
                    continue
                dangling.append(f"{path.relative_to(SRC)}:{where} -> :{target}:")

    assert dangling == [], (
        f"docstring(s) pointing at a symbol the module does not define: {dangling}. A "
        f"cross-reference to deleted code is how a removal goes unnoticed."
    )


# A guard on the guards: the tripwire tests above must not pass vacuously, so the
# detector itself is proved to fire. If `_XREF_ROLE` or the module-name collector ever
# stops matching, every test in this file would report green while checking nothing --
# which is the exact failure this file exists to prevent, reproduced in miniature.
@pytest.mark.parametrize(
    "doc, found, unqualified",
    [
        (":meth:`empty_option_chain`", ["empty_option_chain"], ["empty_option_chain"]),
        # The two halves the lint's soundness rests on: a dotted name is another module's
        # symbol and is skipped, an unqualified one must resolve locally.
        (
            "see :meth:`resolve_options` and :attr:`Ticker.options`",
            ["resolve_options", "Ticker.options"],
            ["resolve_options"],
        ),
        (":class:`bfinance.ticker.Ticker`", ["bfinance.ticker.Ticker"], []),
        (":meth:`~DerivativesEngine.resolve_options`", ["DerivativesEngine.resolve_options"], []),
        ("no roles here at all", [], []),
    ],
)
def test_the_cross_reference_detector_is_not_vacuous(doc, found, unqualified):
    """The roles are found, and the dotted/unqualified split is what the lint assumes."""
    matched = _XREF_ROLE.findall(doc)

    assert matched == found
    assert [m for m in matched if "." not in m] == unqualified
