"""
A lint for the sixth instance of a defect this repo has already removed five times.

The pattern
-----------
    **A branch over data that cannot occur, written so it reads as defensive, silently reducing to
    a constant.**

The instance this file exists for is quoted at the top of ``tests/test_ratios_honesty.py``::

        cash_hold = 0.0
        if not bs.empty:
            if "Cash Equivalents" in bs.index:            # dead
                ...
            elif "Cash and Bank Balance" in bs.index:      # dead
                ...
            # cash stays zero when neither cash row is present

Both branches were dead, and the comment two lines below the ``elif`` said the upstream balance
sheet has no cash row. So the guard read as though cash were handled while the value was a constant
— and ``enterprise_value_cr`` was overstated by the whole cash balance for every net-cash company,
on every call, silently, on three public surfaces at once.

The rule
--------
A guard of the form ``"<literal>" [not] in <expr>.index`` or ``.columns`` is **dead** when the
probed label is

1. absent from the text of **every committed fixture**, and
2. not a column label this package **manufactures** itself.

Both halves are required and each kills a different false positive.

Why AST, and why docstrings and comments are handled
----------------------------------------------------
A substring scan over ``src/`` would flag the sentence
``if "Cash Equivalents" in bs.index`` inside the module docstring of
``tests/test_ratios_honesty.py`` — a file that is *about* the defect and quotes it on purpose. It
would equally flag any comment a future contributor writes while removing a guard, which is the
exact moment a lint must stay quiet. So the scan parses, and the two things a text scan cannot tell
apart from code are removed or cannot appear:

* a **comment** is not in the AST at all, so it cannot be flagged, and
* a **docstring** is an expression-statement string, which is stripped explicitly.

:meth:`TestTheDetectorIsNotFooledByProse` proves both, on source that quotes the pattern three
times outside executable code.

Why the "manufactures" half exists
----------------------------------
A first version of this lint - the literal absent from every fixture and nothing else - flagged
exactly one guard on the current tree::

    src/bfinance/flows.py:1813  "HasWorkbook" not in index.columns

and that flag was **wrong**. ``HasWorkbook`` is a column ``bfinance.flows`` invents at
``flows.py:1619`` (``DataFrame(rows, columns=[..., "HasWorkbook"])``); the guard at 1813 is a shape
precondition on a frame this package built, not a branch over upstream data. A module that
manufactures a label can obviously ask about it, so labels this package declares as column names
are excluded. That is the whole of the escape hatch and it fires once on the current tree, for the
stated reason.

Scope, and what this lint does not claim
----------------------------------------
* It scans ``src/`` only. ``tests/`` contains the pattern quoted deliberately
  (``tests/test_ratios_honesty.py:12``), which is the demonstrated false positive for a wider scan.
* It matches **string literals** only. ``if row in df.index`` where ``row`` is a name is not a
  guard this can judge, and the current cash/debt code has moved to exactly that shape
  (``_CASH_ROW_LABELS`` in ``ticker.py:47`` and ``market/ratios.py:34``), so this lint does **not**
  cover the current EV path. It covers the literal form, which is where all five removals were.
* Fixture presence is checked against file **text**, case-insensitively. That is deliberately
  biased towards silence: a false negative is a missed guard, a false positive is a lint somebody
  deletes.

Why it ships despite finding nothing today
-------------------------------------------
Sixteen literal guards exist in ``src/`` and all sixteen are justified - fifteen because a
committed fixture carries the label, one because the package manufactures it. A lint with no
current findings is still worth shipping when the finding it would produce is *this* finding, and
when the detector is proved non-vacuous. What would be noise is a lint with no current findings
*and* no proof it can fire, so :class:`TestTheDetectorIsNotVacuous` and
:meth:`TestTheDetectorIsNotFooledByProse` are not optional decoration - they are the reason the
zero-finding assertion below is a measurement rather than a tautology.
"""

from __future__ import annotations

import ast
import pathlib
import re
from typing import Iterator

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "bfinance"
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"

#: Extensions whose bytes are searched for a column label. ``.xls``/``.xlsx`` are binary and are
#: not read; their filenames are folded into the corpus instead, because a fixture *named* after a
#: column is weak evidence that the column exists, and missing it can only cause silence.
TEXT_FIXTURES = frozenset({".html", ".htm", ".json", ".csv", ".txt", ".md", ".xml"})
BINARY_FIXTURES = frozenset({".xls", ".xlsx"})


# --------------------------------------------------------------------------- #
# the detector
# --------------------------------------------------------------------------- #


def _without_docstrings(tree: ast.Module) -> ast.Module:
    """Drop leading docstrings, so prose cannot be read as code.

    Comments never reach this stage: ``ast.parse`` discards them, which is the whole reason this
    file parses instead of scanning. Docstrings survive it as expression statements, so they are
    removed explicitly rather than relied upon not to parse as a ``Compare``.
    """
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body.pop(0)
    return tree


def _literal_guards(tree: ast.Module) -> Iterator[tuple[str, bool, str, int]]:
    """Yield ``(label, negated, axis, lineno)`` for every literal label-membership guard.

    ``"<lit>" in <expr>.index``, ``"<lit>" not in <expr>.columns`` and the same inside a tuple
    literal, which is how ``bhavcopy.py:537-539`` writes a two-name column probe.

    Anything whose probed side is not a string constant is skipped: ``row`` in ``df.index`` where
    ``row`` is a parameter is not a judgement this detector can make, and guessing at it is how a
    lint becomes noise.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        for op, comparator in zip(node.ops, node.comparators):
            if not isinstance(op, (ast.In, ast.NotIn)):
                continue
            if not isinstance(comparator, ast.Attribute) or comparator.attr not in ("index", "columns"):
                continue
            left = node.left
            probes = left.elts if isinstance(left, (ast.Tuple, ast.List, ast.Set)) else [left]
            for probe in probes:
                if isinstance(probe, ast.Constant) and isinstance(probe.value, str):
                    yield probe.value, isinstance(op, ast.NotIn), comparator.attr, node.lineno


def _manufactured_labels(tree: ast.Module) -> set[str]:
    """String labels this module *declares as column names*, at any AST shape that does so.

    That is: a ``columns=``/``index=``/``names=`` keyword, a positional list argument (the
    ``DataFrame(rows, ["A", "B"])`` spelling), and a ``.columns = [...]`` / ``.index = [...]``
    assignment. A label in any of those positions is one this package invents, so asking whether a
    parsed payload has it is asking about the wrong thing.
    """
    labels: set[str] = set()

    def harvest(node: ast.AST) -> None:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                labels.add(sub.value)

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr in ("columns", "index"):
                    harvest(node.value)
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg in ("columns", "index", "names"):
                    harvest(keyword.value)
            for argument in node.args:
                if isinstance(argument, (ast.List, ast.Tuple)):
                    for element in argument.elts:
                        if isinstance(element, ast.Constant) and isinstance(element.value, str):
                            labels.add(element.value)
    return labels


def _fixture_corpus() -> str:
    """Every committed fixture's text, lowercased, for substring lookup of a column label."""
    parts: list[str] = []
    for path in sorted(FIXTURES.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() in TEXT_FIXTURES:
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
        elif path.suffix.lower() in BINARY_FIXTURES:
            parts.append(path.name)
    return "\n".join(parts).lower()


def dead_guards(source: str, corpus: str) -> list[tuple[str, bool, str, int]]:
    """The lint proper: guards over a label that no payload can carry.

    ``source`` is a module's text and ``corpus`` is the fixture corpus, both passed in rather than
    read from disk, so :class:`TestTheDetectorIsNotVacuous` can run the identical code over a
    synthetic module and a synthetic corpus. A detector that cannot be pointed at a case is a
    detector nobody can test.
    """
    tree = _without_docstrings(ast.parse(source))
    manufactured = _manufactured_labels(tree)
    return [
        (label, negated, axis, lineno)
        for label, negated, axis, lineno in _literal_guards(tree)
        if label.lower() not in corpus and label not in manufactured
    ]


def dead_guards_in_tree() -> tuple[list[str], int]:
    """Run the lint over ``src/``. Returns ``(offenders, guards_seen)``."""
    corpus = _fixture_corpus()
    offenders: list[str] = []
    seen = 0
    for path in sorted(SRC.rglob("*.py")):
        source = path.read_text(encoding="utf-8-sig")
        found = dead_guards(source, corpus)
        seen += len(list(_literal_guards(_without_docstrings(ast.parse(source)))))
        for label, negated, axis, lineno in found:
            sign = "not in" if negated else "in"
            offenders.append(
                f"{path.relative_to(SRC.parents[1])}:{lineno}: {label!r} {sign} <frame>.{axis}"
            )
    return offenders, seen


# --------------------------------------------------------------------------- #
# 1. the lint on the current tree
# --------------------------------------------------------------------------- #


def test_no_label_membership_guard_probes_something_no_fixture_can_carry() -> None:
    """The lint's assertion on the real tree. It is currently clean, and the count is printed in
    the message when it is not so the reader can see how much surface was checked.

    A lint that ships having flagged the whole existing codebase on its first run is deleted by
    the next person. So this ran first, as a measurement, and what it found is recorded in this
    file's docstring: one flag, ``flows.py:1813``'s ``"HasWorkbook"``, which was a false positive
    and produced the "manufactures" half of the rule.
    """
    offenders, seen = dead_guards_in_tree()
    assert offenders == [], (
        f"{len(offenders)} label-membership guard(s) probe a value absent from every committed "
        f"fixture and not manufactured by this package, out of {seen} such guard(s) in src/:\n"
        + "\n".join(offenders)
        + "\nEither the guard is dead - a branch that reads as defensive and reduces to a "
        "constant, which is what test_ratios_honesty.py is about - or the fixture that would "
        "justify it has not been committed."
    )
    # A non-zero surface, so "clean" means "checked 16 things", not "found nothing to look at".
    assert seen >= 10, f"only {seen} literal label guards exist in src/; the scan is too narrow"


# --------------------------------------------------------------------------- #
# 2. non-vacuity
# --------------------------------------------------------------------------- #

#: The historical defect, verbatim from the top of ``tests/test_ratios_honesty.py``. Ten rows of
#: balance sheet and no cash row, measured on three committed screener.in fixtures.
HISTORICAL = '''
import pandas as pd

_CASH_ROW_LABELS = ("Cash Equivalents", "Cash and Bank Balance")

def enterprise_value_cr(mcap, debt, bs):
    cash_hold = 0.0
    if not bs.empty:
        if "Cash Equivalents" in bs.index:
            cash_hold = _f(bs.loc["Cash Equivalents"].iloc[-1])
        elif "Cash and Bank Balance" in bs.index:
            cash_hold = _f(bs.loc["Cash and Bank Balance"].iloc[-1])
    return mcap + debt - cash_hold
'''

#: A corpus that really does carry the label, e.g. once a fixture with a cash row is committed.
CORPUS_WITH_CASH = (
    "company name, equity capital, reserves, cash equivalents, cash and bank balance, total assets"
)


class TestTheDetectorIsNotVacuous:
    """A lint that cannot fire is a comment. Every claim below is the detector *finding* something.

    The assertion in section 1 is that nothing is found, so without these the file's only content
    would be a statement that the detector returned an empty list - which is also what a detector
    with a broken pattern returns.
    """

    def test_it_fires_on_the_historical_defect(self) -> None:
        found = dead_guards(HISTORICAL, "")
        assert [(label, axis) for label, _, axis, _ in found] == [
            ("Cash Equivalents", "index"),
            ("Cash and Bank Balance", "index"),
        ], f"the detector missed the exact defect this file exists for: {found}"
        # And both are recognised as the *positive* form, so the `not in` flag is not swallowed.
        assert all(negated is False for _, negated, _, _ in found)

    def test_it_stays_quiet_once_a_fixture_carries_the_label(self) -> None:
        """The other direction, and the reason the rule is "absent from every fixture" rather than
        "not a known column list": a hand-maintained allow-list of labels would have to be
        extended every time a fixture lands, and would go stale instead."""
        assert dead_guards(HISTORICAL, CORPUS_WITH_CASH) == []

    def test_it_stays_quiet_on_a_label_this_package_manufactures(self) -> None:
        """The one escape hatch, and the false positive that forced it.

        ``flows.py:1619`` builds its frame with ``columns=[..., "HasWorkbook"]``; the guard at
        1813 asks about that frame. A label this package invents cannot be absent from a recording
        of *upstream* data, because it never was upstream data.
        """
        source = '''
import pandas as pd

def parse(rows):
    frame = pd.DataFrame(
        rows, columns=["Month", "Title", "ExcelUrl", "PdfUrl", "HasWorkbook"]
    )
    return frame

def coverage(index):
    if not isinstance(index, pd.DataFrame) or "HasWorkbook" not in index.columns:
        raise ValueError("expected a frame from parse")
    return int(index["HasWorkbook"].sum())
'''
        assert dead_guards(source, "") == [], (
            "a label this package manufactures was flagged; the escape hatch is load-bearing and "
            "removing it reintroduces the flows.py:1813 false positive"
        )

    def test_the_escape_hatch_is_the_thing_standing_between_them_and_a_false_positive(self) -> None:
        """Same source, escape hatch disabled, and the flag comes back.

        Without this the previous test could be passing because the guard went unfound for some
        unrelated reason - a syntax the matcher missed, say - rather than because the rule works.
        """
        source = '''
import pandas as pd

def parse(rows):
    return pd.DataFrame(rows, columns=["Month", "HasWorkbook"])

def coverage(index):
    if "HasWorkbook" not in index.columns:
        raise ValueError("wrong frame")
    return int(index["HasWorkbook"].sum())
'''
        # Reproduce the detector with the manufactured-label set emptied.
        tree = _without_docstrings(ast.parse(source))
        labels = [label for label, _, _, _ in _literal_guards(tree)]
        assert labels == ["HasWorkbook"], f"the guard was not found at all: {labels}"
        assert "HasWorkbook" in _manufactured_labels(tree)
        assert "hasworkbook" not in ""  # the corpus really is empty
        assert dead_guards(source, "") == []
        # Same guard, same corpus, rule removed -> flagged.
        without_rule = [
            (label, negated, axis, lineno)
            for label, negated, axis, lineno in _literal_guards(tree)
            if label.lower() not in ""
        ]
        assert [(label, axis) for label, _, axis, _ in without_rule] == [("HasWorkbook", "columns")]

    def test_the_fixture_corpus_is_real_and_the_scan_reads_it(self) -> None:
        """Guards against a detector pointed at an empty corpus, which would flag all sixteen."""
        corpus = _fixture_corpus()
        assert len(corpus) > 500_000, f"the corpus is only {len(corpus)} characters"
        for label in ("SctySrs", "Volume", "EPS in Rs", "Buy", "Close"):
            assert label.lower() in corpus, f"{label!r} is not in the corpus; the scan is broken"
        assert "hasworkbook" not in corpus, (
            "the corpus now contains the one manufactured label, which would make the lint's "
            "escape hatch look unnecessary"
        )


# --------------------------------------------------------------------------- #
# 3. prose is not code
# --------------------------------------------------------------------------- #


class TestTheDetectorIsNotFooledByProse:
    """Three occurrences of the pattern in one module, none of them a guard.

    This is the whole reason the file parses instead of scanning, and it is not hypothetical: the
    module docstring of ``tests/test_ratios_honesty.py`` quotes ``if "Cash Equivalents" in
    bs.index`` on line 12, on purpose, as the defect being documented. A text scan over the repo
    would flag that sentence, and so would any comment a contributor writes while *removing* one of
    these guards - which is the moment the lint has to stay quiet.
    """

    SOURCE_WITH_PROSE = '''
"""Background.

The old code was:

    if "Cash Equivalents" in bs.index:
        cash_hold = _f(bs)
    elif "Cash and Bank Balance" in bs.index:
        cash_hold = _f(bs)

Both branches were dead, which is the point of recording them here.
"""

import pandas as pd

# A branch over data that cannot occur: if "Cash Equivalents" in bs.index was written
# by someone who knew the source had no cash row, it reduces to a constant.

def has_cash(bs):
    # Also in prose: `"Cash and Bank Balance" in bs.index` is not a guard.
    return "Cash Equivalents" in bs.index
'''

    def test_a_docstring_and_a_comment_carrying_the_pattern_are_not_guards(self) -> None:
        found = dead_guards(self.SOURCE_WITH_PROSE, "")
        # Exactly one real guard survives: the `return` at the bottom. The docstring's two and the
        # comment's two are not code, and a substring scan would have reported five.
        real = [
            (label, axis)
            for label, _, axis, lineno in found
            if lineno == 21  # `return "Cash Equivalents" in bs.index`
        ]
        assert real == [("Cash Equivalents", "index")], (
            f"the detector is reading prose as code: {found}"
        )
        assert len(found) == 1, (
            f"prose leaked into the findings: {found}. A comment and a module docstring that name "
            "the pattern are not guards, and flagging them is how this lint would get deleted"
        )

    def test_a_real_guard_after_the_prose_is_still_found(self) -> None:
        """So the previous test is not passing because the file failed to parse at all."""
        assert len(dead_guards(self.SOURCE_WITH_PROSE, "")) == 1

    def test_the_pattern_quoted_in_a_test_docstring_is_outside_this_lints_scope(self) -> None:
        """``tests/`` is excluded, and the reason is a specific line, not a policy.

        ``tests/test_ratios_honesty.py:12`` is the module docstring quoting the dead guard. If the
        scan were ever widened to the whole repository, that sentence would be its first and only
        finding, and the lint would be deleted the same day.
        """
        source = (FIXTURES.parent / "test_ratios_honesty.py").read_text(encoding="utf-8-sig")
        assert re.search(r'if "Cash Equivalents" in bs\.index', source), (
            "the quoted example moved; this test's justification needs updating"
        )
        # It is inside a docstring, so the detector would already ignore it - but the *scope*
        # exclusion is what stops a future widening from flagging the prose around it.
        module_doc = ast.get_docstring(ast.parse(source)) or ""
        assert 'if "Cash Equivalents" in bs.index' in module_doc
