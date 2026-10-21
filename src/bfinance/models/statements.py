"""
Financial statement models and DataFrame serializers.

Native shape (downstream finengine reads this â€” do not change):
  orientation: metric rows x period-string columns (e.g. "Mar 2024").
  row labels: TitleCase Indian labels ("Sales", "Net Profit", "Borrowings").
  units: values in Rs Cr (1 Cr = 1e7 Rs).

YFinance shape via ``FinancialStatement.to_yfinance()``:
  same orientation (metric rows x period columns), columns parsed to
  DatetimeIndex (month-end dates) and ordered NEWEST FIRST as yfinance orders them,
  values scaled x1e7 to absolute Rs, Indian labels renamed to generic yfinance labels
  where a mapping exists (unmapped rows kept as-is). The scale applies to the monetary
  rows only: percentages, per-share quantities and ratios pass through unchanged --
  see ``_rows_to_scale``.

``to_dataframe()`` is the native shape and stays the contract finengine reads; it imposes
no order on the caller's headers. ``to_yfinance()`` is the yfinance shape and imposes the
yfinance period order. The two are deliberately not the same thing.
"""

from typing import Dict, Iterable, List, Optional, Set
from pydantic import BaseModel, Field
import pandas as pd

from bfinance.utils.exceptions import ParsingError


#: Indian (Screener.in) row label -> generic yfinance-style row label.
INDIAN_TO_YFINANCE_LABELS: Dict[str, str] = {
    "Sales": "Total Revenue",
    "Expenses": "Total Expenses",
    "Operating Profit": "Operating Income",
    "Other Income": "Other Income Non Operating",
    "Interest": "Interest Expense",
    "Depreciation": "Depreciation And Amortization",
    "Profit before tax": "Pretax Income",
    "Tax %": "Tax Rate",
    "Net Profit": "Net Income",
    "EPS in Rs": "Diluted EPS",
    "EPS": "Diluted EPS",
    "Equity Capital": "Common Stock",
    "Reserves": "Retained Earnings",
    "Borrowings": "Total Debt",
    "Other Liabilities": "Other Liabilities",
    "Total Liabilities": "Total Liabilities",
    "Fixed Assets": "Net PPE",
    "CWIP": "Capital Work In Progress",
    "Investments": "Investments And Advances",
    "Other Assets": "Other Assets",
    "Total Assets": "Total Assets",
    "Cash from Operating Activity": "Operating Cash Flow",
    "Cash from Investing Activity": "Investing Cash Flow",
    "Cash from Financing Activity": "Financing Cash Flow",
    "Net Cash Flow": "Net Cash Flow",
}

#: Rs Cr -> absolute Rs. Applied only to the rows that are monetary amounts; see
#: :func:`_rows_to_scale` for the rule and for the recorded evidence behind it.
CR_TO_RS = 1e7

#: yfinance row labels that are **per-share quantities**, not Rs Cr, and so are exempt from
#: the :data:`CR_TO_RS` scale. yfinance's ``Diluted EPS`` is rupees per share, so multiplying
#: it by 1e7 turns ITC's reported 16.51 into 165,100,000 -- a number in no unit at all, and
#: one that also makes a P/E computed from this frame wrong by a factor of 1e7. screener.in
#: prints the row as ``EPS in Rs``, i.e. already rupees. Keyed on the *output* label,
#: because that is the row a caller reads.
PER_SHARE_LABELS = frozenset({"Diluted EPS"})

#: Row labels that are **dimensionless** -- ratios, day counts and share counts -- and are
#: therefore not Rs Cr amounts either. Every entry is a row that one of the committed
#: screener.in recordings (``tests/fixtures/screener``, captured 2026-09-27) actually emits,
#: so this is a list of measured labels rather than of anticipated ones:
#:
#: * ``section#cash-flow`` -- ``CFO/OP`` 93.0, i.e. cash from operations as a share of
#:   operating profit: a number near 100, not a rupee amount;
#: * ``section#ratios`` -- ``Debtor Days`` 23.0, ``Inventory Days`` 178.0, ``Days Payable``
#:   55.0, ``Cash Conversion Cycle`` 146.0, ``Working Capital Days`` 45.0;
#: * ``section#shareholding`` -- ``Promoters`` 71.77, ``FIIs`` 34.83, ``DIIs`` 49.15,
#:   ``Government`` 0.04 and ``Public`` 15.96 (percent of shares held, which screener.in
#:   prints *without* a ``%``), plus ``No. of Shareholders`` 4041653.0, a plain count.
#:
#: None of these tables is served by ``Ticker.get_*_stmt`` -- those three accessors hand back
#: the P&L, balance sheet and cash flow tables -- but :meth:`FinancialStatement.to_yfinance`
#: is public and a profile's ``ratios_history``/``shareholding`` are ``FinancialStatement``
#: like any other, so the same defect is reachable through them and is closed here rather
#: than left for the first caller who tries.
RATIO_LABELS = frozenset({
    # section#cash-flow
    "CFO/OP",
    # section#ratios
    "Debtor Days", "Inventory Days", "Days Payable",
    "Cash Conversion Cycle", "Working Capital Days",
    # section#shareholding: percent of shares held (no "%" marker) and one count
    "Promoters", "FIIs", "DIIs", "Government", "Public", "No. of Shareholders",
})


def _rows_to_scale(source_labels: Iterable[str]) -> Set[str]:
    """
    The rows :data:`CR_TO_RS` may convert, named by the **output** label a caller reads.

    The rule is one sentence: **the scale converts money, so only a row that screener.in
    prints in Rs Cr may be scaled.** screener.in states each row's unit in the row label, and
    all three non-monetary kinds are present in the committed recordings:

    ===========================  =======================  =====================================
    kind                         how it is recognised     recorded row and its reported value
    ===========================  =======================  =====================================
    percentage                   a ``%`` in the label     ``OPM %`` 35.0, ``Tax %`` 25.0,
                                                           ``Dividend Payout %`` 88.0,
                                                           ``ROCE %`` 39.0
    per share                    :data:`PER_SHARE_LABELS`  ``EPS in Rs`` 16.51 -> ``Diluted EPS``
    ratio / days / count         :data:`RATIO_LABELS`     ``CFO/OP`` 93.0, ``Debtor Days`` 23.0,
                                                           ``No. of Shareholders`` 4041653.0
    ===========================  =======================  =====================================

    Measured against the code as it stood, ``OPM %`` came back as ``350000000.0`` for a
    reported 35.0. That is fabricated magnitude on the primary return path, because
    ``get_income_stmt``, ``get_balance_sheet`` and ``get_cash_flow`` all hand back this frame
    -- and it is the class of error this package exists to prevent, arriving through the
    yfinance-parity work rather than around it.

    Two details are load-bearing:

    * the decision is taken on the label **as screener.in printed it**, because that is the
      only label that carries a unit, and it has to be taken **before** the rename: ``Tax %``
      is a percentage and its yfinance name, ``Tax Rate``, no longer contains a ``%``.
      Deciding after the rename scales it to 250,000,000 and reports nothing;
    * :data:`PER_SHARE_LABELS` and :data:`RATIO_LABELS` are keyed on the *output* label,
      because that is the row a caller reads.
    """
    named = PER_SHARE_LABELS | RATIO_LABELS
    return {
        INDIAN_TO_YFINANCE_LABELS.get(label, label)
        for label in source_labels
        if "%" not in label and INDIAN_TO_YFINANCE_LABELS.get(label, label) not in named
    }


#: Non-date period headers that :meth:`FinancialStatement.to_yfinance` refuses to guess at.
#: ``screener.in`` appends ``TTM`` to the annual P&L table; a trailing-twelve-months total
#: is not a fiscal period, has no month-end date, and cannot be placed in a
#: ``DatetimeIndex``. Guessing one would fabricate a column date. Dropping it is therefore
#: a decision, not a fallback, and it is recorded in the returned frame's ``attrs`` under
#: ``bfinance_dropped_period_columns`` so the caller can see it rather than infer it from a
#: column that is not there. The TTM values stay reachable on the native shape
#: (:meth:`FinancialStatement.to_dataframe`), which is unchanged.
NON_DATE_PERIOD_HEADERS = frozenset({"TTM"})


class FinancialStatement(BaseModel):
    """
    Generic financial table model (P&L, Balance Sheet, Cash Flow, Quarters, Ratios).
    """
    headers: List[str] = Field(default_factory=list)
    rows: Dict[str, List[Optional[float]]] = Field(default_factory=dict)

    @property
    def empty(self) -> bool:
        """True if the financial statement contains no data."""
        return not self.headers or not self.rows

    def to_dataframe(self, orient: str = "columns") -> pd.DataFrame:
        """
        Convert financial statement to pandas DataFrame.
        orient='columns' (default, matches yfinance): rows are metrics, columns are period dates.
        orient='index': rows are periods, columns are metrics.

        Raises:
            ParsingError: a row's cell count does not match ``len(headers)``. pandas
                would raise a bare ``ValueError("Length of values (2) does not match
                length of index (3)")``, which is not a BFinanceError and therefore
                escapes the typed handlers in ``Ticker`` â€” and which names neither the
                metric nor the statement. The check is here, at the single point where
                rows and headers are joined, because a ragged row can enter from the HTML
                parser or from a cache round-trip alike.
        """
        if not self.headers or not self.rows:
            return pd.DataFrame()

        periods = len(self.headers)
        for metric, values in self.rows.items():
            if len(values) != periods:
                raise ParsingError(
                    f"Malformed statement row {metric!r}: {len(values)} value(s) for "
                    f"{periods} period(s) ({', '.join(self.headers)}). Refusing to build "
                    f"a DataFrame from a ragged row."
                )

        # Build raw table
        df = pd.DataFrame(self.rows, index=self.headers).T
        if orient == "index":
            return df.T
        return df

    def get_metric(self, metric_name: str) -> Dict[str, Optional[float]]:
        """Get metric values mapped by period."""
        if metric_name not in self.rows:
            # Case-insensitive match
            for k, v in self.rows.items():
                if k.lower() == metric_name.lower():
                    return dict(zip(self.headers, v))
            return {}
        return dict(zip(self.headers, self.rows[metric_name]))

    def to_yfinance(self, *, scalable_rows: Optional[Iterable[str]] = None) -> pd.DataFrame:
        """
        Return the yfinance-shaped copy: ``DatetimeIndex`` columns **newest first**, absolute
        rupees, generic row labels.

        This is the shape ``Ticker.get_income_stmt()`` and friends return, so the four
        differences from :meth:`to_dataframe` are load-bearing rather than cosmetic:

        1. **Alias de-duplication.** ``INDIAN_TO_YFINANCE_LABELS`` maps two spellings
           (``"EPS in Rs"`` on the annual P&L, ``"EPS"`` on the ratios table) onto one
           yfinance label. ``rename`` therefore emits the *same* index label twice, and
           ``df.loc["Diluted EPS"]`` returns a DataFrame where every other row returns a
           Series -- silently changing the type of every downstream arithmetic operation.
           Aliased rows are coalesced here, first non-null winning per period so a spelling
           that is blank in one period does not erase what the other carries for it. The
           merge is published in ``attrs["bfinance_merged_row_labels"]``.
        2. **Newest-first columns.** yfinance's statement frames read
           ``df.columns[0]`` as the latest period. Reproducing screener's oldest-first order
           would make every "latest period" lookup silently return the oldest one.
        3. **Non-monetary rows are not scaled.** The Cr -> Rs scale converts money, so it is
           applied only to the rows screener.in prints in Rs Cr; percentages, per-share
           quantities and ratios pass through unchanged. See :func:`_rows_to_scale` for the
           rule and the recorded evidence behind it.
        4. **Non-date periods are dropped and said so.** See
           :data:`NON_DATE_PERIOD_HEADERS`; the dropped header names are in
           ``attrs["bfinance_dropped_period_columns"]``.

        The native statement is not mutated, and ``to_dataframe()`` is untouched by any of
        this: the Indian-label Rs-Cr frame is still the contract finengine reads.

        Args:
            scalable_rows: The exact set of row labels to scale by :data:`CR_TO_RS`, **replacing**
                the label-derived decision. For a vendor whose labels state no unit — which is
                every vendor except screener.in — the caller should pass its own measured set, or
                pass an empty set to scale nothing. ``None`` keeps the screener.in behaviour,
                which is the default every existing caller depends on.

        Raises:
            ParsingError: propagated from :meth:`to_dataframe` for a ragged row.
        """
        if not self.headers or not self.rows:
            return pd.DataFrame()
        df = self.to_dataframe(orient="columns")
        # Decided on the screener.in labels, before the rename below hides the unit they
        # carry ("Tax %" -> "Tax Rate"), and keyed by the output label the caller reads.
        scalable = (
            set(scalable_rows)
            if scalable_rows is not None
            else _rows_to_scale(df.index)
        )
        df = df.rename(index=INDIAN_TO_YFINANCE_LABELS)
        dates, keep, dropped = [], [], []
        for col in df.columns:
            if col in NON_DATE_PERIOD_HEADERS:
                # Named, so the drop is a decision this function makes on purpose. Any other
                # unparseable header is still skipped, but only the recognised non-periods
                # are reported -- see the attrs block below.
                dropped.append(col)
                continue
            try:
                dt = pd.to_datetime(f"01 {col}", format="%d %b %Y") + pd.offsets.MonthEnd(1)
            except Exception:
                continue
            dates.append(dt)
            keep.append(col)
        if not keep:
            return pd.DataFrame()
        df = df[keep].copy()
        df.columns = pd.DatetimeIndex(dates)
        df = df.apply(pd.to_numeric, errors="coerce").dropna(how="all")

        merged = {}
        if df.index.has_duplicates:
            merged = {
                str(label): [str(i) for i in group.index]
                for label, group in df.groupby(level=0, sort=False)
                if len(group) > 1
            }
            # ``first`` on a row group runs *along periods* and skips NaN, so the coalesce is
            # per period rather than a whole-row winner-take-all: a spelling that is blank in
            # one period does not erase what the other spelling carries for that period.
            # ``sort=False`` keeps first-appearance order, so the surviving row sits where the
            # caller put it and the frame does not silently reorder itself.
            df = df.groupby(level=0, sort=False).first()

        # ``scalable`` is the label-derived decision, and a caller who knows better supplies its
        # own row set. This parameter exists because the decision is vendor-specific: it is read
        # from screener.in's labels, which is correct there, and a second vendor's labels say
        # nothing about units. See bfinance.statements.yfinance_bridge for the measured case
        # (trendlyne's per-share and ratio rows have no marker in the label at all).
        scaled = [label for label in df.index if label in scalable]
        if scaled:
            df.loc[scaled] = df.loc[scaled] * CR_TO_RS

        df = df.sort_index(axis=1, ascending=False)
        for alias in ("Net Income Including Noncontrolling Interests", "Net Income Continuous Operations"):
            if alias not in df.index and "Net Income" in df.index:
                df.loc[alias] = df.loc["Net Income"]
        df.attrs["bfinance_dropped_period_columns"] = dropped
        df.attrs["bfinance_merged_row_labels"] = merged
        return df

    def __repr__(self) -> str:
        return f"<FinancialStatement periods={len(self.headers)} metrics={len(self.rows)}>"
