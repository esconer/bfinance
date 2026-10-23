# Pluggable data sources: what already exists, what is missing, and the order to finish it

**A design assessment, not an implementation.** Written 2026-10-24 against the tree at `f112d22`.
Every `path:line` below was opened in this working tree; §9.2 records the citations that had moved
relative to the brief that commissioned this report, and the claims that could not be verified and
were therefore dropped.

The question this answers is the one a caller asked: *can bfinance take a data source as a plugin,
produce a yfinance-compatible response from it, allow one source to cover only part of the surface,
and let a file in the repository root choose the source with a per-capability fallback?*

---

## 1. Verdict

You can have this, and just over two-thirds of it is already here. **(a) Pluggable?** Yes. A source is
a `SourceSpec` row (`src/bfinance/sources/registry.py:102`) plus, when its entry point is not
`entry(request)`, an adapter in `src/bfinance/sources/bindings.py:127`; the try-order is the
registry's insertion order (`registry.py:311-312`) and the fail-through is
`src/bfinance/sources/cascade.py:512-569`. The mechanism is not theoretical: `tests/test_sources_cascade.py:135-156`
builds a registry, registers rows and resolves through a stub fetch, which is exactly what a plugin
would do. **(b) yfinance-compatible responses?** Yes, and the conversion must stay on bfinance's side
of the boundary — `src/bfinance/models/statements.py:207` (`to_yfinance`) is the existing precedent,
and `plans/yfinance-interchangeability.md:167` already refused a surface-adapter layer for the same
reason. A plugin returns vendor-normalised data; bfinance shapes it. **(c) Partial coverage across
sources?** Yes, and the registry already models it: five `price_history` rows on three bases, with
`provides` refusing a Close-only answer for an OHLC request (`registry.py:204-215`,
`cascade.py:305-320`). What is missing is coverage in the other direction: only `price_history` has
more than one registered row; `statements` has one row that fronts a two-vendor sub-cascade, and
that row is not reachable from `Ticker` (§3.6). **(d) A file in the root, with a fallback per
capability?** This is the part that does not exist. There is no config loader in `src/` — not found,
by search — the default price path bypasses the registry (`src/bfinance/market/ohlcv.py:1765`,
`:1793-1796`), and only `Ticker.history` accepts a source at all
(`src/bfinance/ticker.py:1170-1171`). The honest summary is therefore: the plugin *mechanism* exists
and is tested; the *policy* layer that would let a file choose sources, and the default routing that
would make that choice matter, do not. Saying "let us build a plugin system" would duplicate working
code. The work is to finish the seam, not to invent it.

---

## 2. What exists today

`src/bfinance/sources/` is an opt-in package: `bfinance/__init__.py` does not import it, and
importing it imports none of the modules it describes (`src/bfinance/sources/__init__.py:4-8`,
`registry.py:528-535`). It was built for exactly the question this report is about — one resilient
entry point over several independent scraper modules.

### 2.1 The scorecard

| Piece a plugin system needs | Exists? | Where |
|---|---|---|
| A row type carrying capability, basis, columns and a measured note | yes | `SourceSpec`, `sources/registry.py:102` |
| A registry with a declared try-order and a copy for local overrides | yes | `Registry`, `registry.py:278`; `register`, `:297`; `copy`, `:335` |
| An ordered fail-through cascade | yes | `cascade.py:369`; the loop at `:512`; the terminal raise at `:560` |
| Adapters from a row's entry point to a uniform call | yes | `bindings.py:127`, `:145`, `:181` |
| Basis gating — an ineligible source is never called | yes | `cascade.py:292-304` |
| Column gating — a source that cannot supply the requested columns is never called | yes | `cascade.py:305-320` |
| Health memory that survives across calls | yes | `health.py:52`, `:103`, `:187`, `:247` |
| A cross-basis container that refuses to collapse | yes | `series.py:55`, `:234`, `:307-320` |
| A config file | **no** | not found anywhere under `src/` |
| Default routing through the seam | **no** | `market/ohlcv.py:1765` |
| Entry-point discovery for third-party packages | **no** | no `[project.entry-points]` in `pyproject.toml` |

Eight of the eleven pieces exist. That is the basis for "just over two-thirds": the mechanism is
complete enough to be dangerous to rebuild, and the two policy pieces plus discovery are what a
caller actually notices.

### 2.2 Module map

| Module | Responsibility | Load-bearing symbol |
|---|---|---|
| `sources/registry.py` | The table: capability → ordered rows, each with a basis, a column set and the measurement behind it | `SourceSpec` `:102`, `build_default` `:528`, `DEFAULT` `:889` |
| `sources/cascade.py` | Plan the try-order without calling; resolve with fail-through, recording every attempt | `plan` `:225`, `resolve` `:369`, `is_empty_payload` `:127` |
| `sources/bindings.py` | The one place that knows a source's call shape; a row names an adapter as a string | `ADAPTERS` `:127`, `load_adapter` `:145`, `call_entry` `:181` |
| `sources/model.py` | The closed basis vocabulary, the attempt record, and the single-provider result | `Basis` `:104`, `Attempt` `:309`, `Resolved` `:382` |
| `sources/health.py` | Per-source counters, the reordering they imply, and the process-wide ledger | `HealthLedger.order` `:187`, `default_ledger` `:247` |
| `sources/series.py` | Segments from one source on one basis, hops between them, and a container that will not merge them | `Segment` `:55`, `BasisHop` `:198`, `Series` `:234` |
| `sources/errors.py` | Typed refusals that distinguish "no publisher", "wrong basis", and "everyone failed" | `:49`, `:53`, `:67`, `:78`, `:88` |
| `sources/price_provider.py` | A price reader that lives inside this package, so the leaf vendor package stays unreachable | `price_history` `:914`, `PRICE_BASIS` `:184` |

The isolation rule is worth stating because it constrains every design below: rows are **data, not
imports** (`registry.py:528-535`). `load_provider` is the only place a module string becomes an
import (`cascade.py:161`, `importlib.import_module` at `:177`, `getattr` at `:186`). That is what
keeps `import bfinance.sources` cheap, and it is also why an unwired row fails only when a call
reaches it.

### 2.3 The five registered price rows

| Row | Line | Basis | `provides` | Wired |
|---|---|---|---|---|
| `trendlyne-ohlc` | `registry.py:571` | `SPLIT_ADJUSTED` (`:573`) | Open, High, Low, Close, Volume (`:619`) | yes |
| `marketlens-close` | `:627` | `AS_TRADED` (`:629`) | Close, Volume (`:649`) | yes |
| `nse-bhavcopy` | `:657` | `AS_TRADED` (`:659`) | Open, High, Low, Close, Volume (`:685`) | yes |
| `screener-chart` | `:693` | `DIVIDEND_ADJUSTED` (`:695`) | none (`provides` empty) | yes |
| `vendor-b-adjustments` | `:722` | `SPLIT_ADJUSTED` (`:724`) | Open, High, Low, Close, Volume (`:734`) | **no** — no `module`, so `load_provider` raises at `cascade.py:174-175` |

This table is the strongest evidence that the hard part is already done. The basis column is
load-bearing rather than descriptive: an as-traded request never calls an adjusted row
(`cascade.py:292-304`), and that refusal is a *cost* control as much as a correctness one, because
the bhavcopy row costs one archive file per trading day (`registry.py:677-683`).

### 2.4 What the tests already exercise

`tests/test_sources_cascade.py:135-156` defines `stub_source` and `registry_of`: a test builds a
`SourceSpec`, registers it, and drives the cascade with a `fetch` callable that replays either a
value or an exception. That is the plugin protocol in miniature, with no vendor and no network.
`tests/test_sources_price_provider.py:394-407` reads a real row's module, entry, wiring and basis off
the table rather than restating them. So "can a source be registered" is answered by the suite, not
by this document.

---

## 3. Gap analysis

| # | Gap | Severity |
|---|---|---|
| 3.1 | The default price path bypasses the registry | **blocker** |
| 3.2 | Only `history` has a source seam | **blocker** |
| 3.3 | No configuration layer of any kind | **blocker** |
| 3.4 | Two vocabularies describe one price basis | **blocker** for publishing a contract |
| 3.5 | Failure shapes differ by member, and the empty-payload doc contradicts its own code | significant |
| 3.6 | Two registries, and the `statements` row in this one is dead from `Ticker` | significant |
| 3.7 | A declared-but-unwired row is accepted by name and fails only at call time | significant |
| 3.8 | Six module-level client singletons cannot be reached by a per-config client | significant |
| 3.9 | No entry-point discovery for third-party packages | cosmetic for this ask, significant for distribution |
| 3.10 | "The data directory" is already two different things | significant if the config exposes it |

### 3.1 The default price path bypasses the registry — blocker

`OHLCVEngine.fetch_history` computes `cascade_on = bool(price_names) or self.price_sources is not None`
(`src/bfinance/market/ohlcv.py:1765`). When it is false — which is every call that does not name a
source — the engine calls `self._fetch_bhavcopy_bars(...)` directly (`:1793-1796`), and that path
raises `RealOHLCUnavailableError` on a transport failure (`:3229-3233`) or an empty archive
(`:3238-3242`). There is no fallback on that branch. The comment above `cascade_on` says explicitly
why `requires` is not a third trigger (`:1755-1764`), so this is a decision, not an omission.

Consequence: "plug in a source" today works only if the caller passes `source=`/`sources=` per call
(`ticker.py:1170-1171`), validated before any request (`ohlcv.py:840-889`) and forwarded by
`download.py:492`. A config file that changes a default cannot affect a path that never reads the
registry. This is the first thing to fix after the basis vocabulary.

### 3.2 Only `history` has a source seam — blocker

`Ticker.history` and `history_async` are the only members with `source`/`sources` parameters
(`ticker.py:1170-1171`, `:1328-1330`); `download` forwards the same pair (`download.py:88-89`,
`:492`). Every other member is a direct call to one vendor. `info` is the sharpest case: the module
documents the two cheap as-traded sources and then explains why it does not pass them
(`ticker.py:485-493`), because engaging the cascade would break the offline callers that stub the
direct archive reader.

So "a plugin type system" currently means "a plugin for one member of the public surface". That is
a defensible first increment, but it is not what the question asked for.

### 3.3 No configuration layer of any kind — blocker

A search of `src/` for `tomllib`, `tomli`, `yaml`, `configparser`, `dotenv`, `.ini`, `.cfg` and
`settings.` returns one hit, and it is the word "settings" in a docstring (`ticker.py:1001`); there
is no `config.py` anywhere under `src/bfinance/`. The complete configuration surface is six
environment variables:

| Variable | Read at | What it controls |
|---|---|---|
| `BFINANCE_HOME` | `mirror.py:104`, `cache/frame_store.py:160`, `universe.py:764`, `:1298` | where local stores live |
| `BFINANCE_MIRROR` | `market/ohlcv.py:74`, read at `:2988` | whether `history` consults the bhavcopy mirror |
| `HTTPS_PROXY` / `HTTP_PROXY` | nine client constructors (`nse/bhavcopy.py:1052`, `nse/corporate_actions.py:1175`, `nse/macro.py:993`, `nse/marketlens.py:197`, `ownership.py:1555`, `screener/client.py:211`, `sources/price_provider.py:643`, `trendlyne/client.py:3277`, `trendlyne/smartoptions.py:686`) | transport proxy |
| `BFINANCE_COSTS_STT_TEXT` | `costs.py:1724` | an override fixture for the STT schedule |
| `BFINANCE_COSTS_EXCHANGE_TEXT` | `costs.py:1726` | the same for exchange charges |

Nothing selects a data source. `BFINANCE_MIRROR` is the nearest precedent — an env var that changes
where `history` reads from — and it is a single boolean-ish path, not an order.

### 3.4 Two vocabularies describe one price basis — blocker for publishing a contract

This is the defect that must be fixed before any plugin contract is published, because a contract
that cannot name a basis unambiguously cannot be obeyed.

* `Basis.SPLIT_ADJUSTED` is the string `"split_adjusted"` (`src/bfinance/sources/model.py:122`).
* The frame attribute constant `PRICE_BASIS_VENDOR_SPLIT_ADJUSTED` is the string
  `"as_traded_split_bonus_adjusted"` (`src/bfinance/market/ohlcv.py:158`), copied verbatim from the
  reader's own `PRICE_BASIS` (`src/bfinance/sources/price_provider.py:184`).
* `tests/test_ohlcv_price_sources.py:989` asserts the frame constant equals the reader constant.
* `tests/test_sources_price_provider.py:402` asserts the registry row's basis is
  `Basis.SPLIT_ADJUSTED`, and the message on the next line says the route "is
  as_traded_split_bonus_adjusted" (`:404`). The test names both strings in one assertion block and
  never compares them.

So the row declares one name, the frame carries another, and only the first is in the enum a plugin
author is told to use. `Basis.comparable` is an identity test (`model.py:152-175`), which means the
two names are, correctly, not comparable — but that also means the enum member the row declares is
not the string the frame publishes. The next section's proposed fix adds a member rather than
renaming either, because both names are load-bearing: the frame's name encodes "demergers not
applied" (`price_provider.py:186-193`), and `Basis.SPLIT_ADJUSTED` is the generic member a future
source may legitimately need.

### 3.5 Failure shapes differ by member, and one docstring contradicts its code — significant

A uniform fallback contract needs one answer to "did this source answer?". Today there are four:

1. **Raise.** Empty bars with `auto_adjust=True` raise `AdjustmentNotAvailableError`
   (`ohlcv.py:1806-1823`); a quarterly balance sheet raises `NotImplementedError`
   (`ticker.py:1423-1424`); a Trendlyne section member with the flag off raises `NotImplementedError`
   (`ticker.py:903-906`).
2. **Empty + provenance attrs.** `splits` returns an empty series carrying
   `bfinance_actions_source == "none"` and a detail string (`ticker.py:1577-1586`); `actions` returns
   an empty frame with the same three attrs (`:1655-1659`).
3. **Empty frame.** `recommendations` returns `pd.DataFrame(columns=columns)` (`ticker.py:1974`) —
   columns, no rows. `insider_transactions` returns a bare `pd.DataFrame()` (`:2210`) with **no**
   columns at all, which is a different shape inside the same family.
4. **`None` / omitted keys.** `isin` returns `str` or `None` (`ticker.py:1619`); `info` omits keys
   whose source is absent rather than setting them to `None` (`src/bfinance/market/quotes.py:271-273`).

There is also a prose defect in the function a fallback would rely on. `is_empty_payload` lists "a
frame with zero rows" as a non-answer (`cascade.py:135-139`), then says a frame with columns and no
rows is *not* the same as `None` and "only the second is a failure here" (`:141-143`), while the
implementation returns `True` for any object with `.empty` true (`:151`). The code is what every
caller has; the paragraph is wrong. It should be corrected with the P0 doc pass, because a plugin
author reading the docstring would conclude that returning an empty frame is an answer, which is the
one thing `Resolved` exists to prevent.

### 3.6 Two registries, and one dead row — significant

`src/bfinance/statements/registry.py:139` defines its own `VENDORS` table, `providers_for` at `:574`,
provider rows at `:378-410`, and its own fail-through cascade at
`src/bfinance/statements/cascade.py:238-264`. Meanwhile `sources/registry.py:548-566` registers a
single `statements` capability row pointing at `bfinance.statements:resolve_from_payloads`, and no
`Ticker` statement accessor consults it: `get_income_stmt` reads the Screener profile and, if opted
in, `_fallback_statement_frame` (`ticker.py:1374-1409`), which drives the statements package
directly. The row is not wrong — its adapter requires a `payloads` mapping the caller must already
hold (`bindings.py:268-284`) — but it is a demonstration row, not a routing path. A plugin system
that claimed to cover statements would be advertising a seam the public API does not cross.

### 3.7 A declared-but-unwired row is accepted by name — significant

`vendor-b-adjustments` is registered with no `module` (`registry.py:720-735`), so `wired` is false
(`:187-198`). `resolve_price_source_names` validates a name against `names_for("price_history")`
(`ohlcv.py:884-885`), which includes it. So `history(sources=["vendor-b-adjustments"])` passes
validation, spends no request, and then fails inside `load_provider` (`cascade.py:174-175`) with the
row's `unavailability()` message (`registry.py:217-230`). If it is the only named source the call
raises; if it is second, the cascade falls through and marks the result degraded. That is a
deliberate design — the row reserves a basis — but it is a trap for a config author, and the config
loader must reject a disabled or unwired row in `order` rather than let it fail at call time.

### 3.8 Six module-level client singletons — significant

Each of these caches one client at module scope: `nse/bhavcopy.py:1508`,
`nse/corporate_actions.py:1393`, `nse/marketlens.py:451`, `ownership.py:1860`,
`trendlyne/client.py:5370`, `nse/macro.py:1514`. The registry rows point at module-level functions,
and those functions call `_default_client()` (`nse/bhavcopy.py:1569`, defined at `:1511-1515`), so a
per-config timeout, proxy or cache cannot be injected through a row without changing the entry
point or the call site. The last of the six is dead: `nse/macro.py:1517-1520` defines
`_default_client`, and no call site in `src/` invokes it — the module shortcuts construct
`MacroClient(host=...)` directly (`:1534`, `:1542`, `:1553`, `:1558`).

The precedent to follow already exists: `Ticker` builds its own `CorporateActionsClient` per instance
with its own timeout and proxy (`ticker.py:1023-1025`). But it is only half a precedent, because
`Ticker` constructs `OHLCVEngine(self.screener_client)` with one argument (`:585`) and never passes
that client down, so `history` still reaches the module default. A per-config plugin needs an entry
point that takes its client, not a module-level shortcut.

### 3.9 No entry-point discovery — cosmetic for this ask

`pyproject.toml` is 65 lines, has no `[project.entry-points]`, and the only optional table is
`[project.optional-dependencies]` at `:41-48`. So a third-party package cannot announce a source;
the only route is a config row naming `my_pkg.module:entry`. For the question asked — a file in the
project that selects sources — that is sufficient. It becomes significant only if plugins are meant
to be installable distributions discovered without configuration.

### 3.10 "The data directory" is already two different things — significant if exposed

`sqlite_cache.py:109-114` hard-codes `~/.bfinance/cache.db` and does not consult `BFINANCE_HOME`;
`flows.py:1091-1095` hard-codes `~/.bfinance/flow_archive.db` for the same reason. Both facts are
documented at `cache/frame_store.py:155-158`, which follows `mirror.py:104` instead. So a config key
called `data_home` would inherit an inconsistency rather than fix one. Either leave it out of the
schema (recommended for P1) or change both stores in the same commit that adds the key.

---

## 4. The proposed plugin contract

### 4.1 What a row must declare

The config schema and the dataclass should be the same vocabulary, spelled once. A plugin row is a
`SourceSpec` (`registry.py:147-155`):

| Field | What the plugin must say | Refused if |
|---|---|---|
| `name` | A stable identifier, unique in the registry | empty, or already registered (`registry.py:304-309`) |
| `capabilities` | Which capability names it can serve | empty (`:160-164`) |
| `bases` | Every basis it publishes, from the closed vocabulary | empty (`:165-170`); a value not in `Basis` is refused by `basis_from` |
| `provides` | The exact price column names it returns; empty for a non-price row | never refused, but a wrong declaration is a silent column loss (`cascade.py:305-320`) |
| `note` | Why the row says what it says — the measurement, the date, the symbol set | empty (`:171-175`) |
| `module` + `entry` | Where the callable lives, as strings | one without the other (`:176-180`) |
| `adapter` | A registered dotted path, or empty for `entry(request)` | non-empty with no module (`:181-185`); not in `ADAPTERS` (`bindings.py:151-158`) |
| `upstream_only` | `true` only when no other publisher exists anywhere | never refused; it is a claim, and `is_upstream_exclusive` reads it (`:381-388`) |

The contract for the callable itself:

* It receives one mapping (`cascade.py:525`), built by the adapter. It must not require a bespoke
  argument list; that is what the adapter is for (`bindings.py:181-197`).
* It returns the vendor's **normalised** data: a frame indexed by session date with exactly the
  columns declared in `provides`, in rupees and shares; or a scalar/mapping for a non-price
  capability. It may be a coroutine; `_resolved` drives it (`bindings.py:69-77`).
* It signals "no answer" by raising, or by returning `None`, an empty mapping, or a zero-row frame.
  All four fall through (`cascade.py:526-546`), and all four are recorded as `FAILED` with the reason
  (`:534`, `:544`).
* It must not blend: one call, one basis, one source. A series that spans two bases is a `Series`
  of `Segment`s (`series.py:55`, `:234`), and `Series.frame` refuses to collapse it (`:307-320`).
* It must not keep a module-level client that ignores the caller's transport settings. Build per
  call, as `price_provider.py:914-931` does, or accept the client as an argument.

### 4.2 What a plugin must **not** do: emit yfinance-shaped frames

This is a rule, not a preference.

1. **The semantics live in bfinance.** `Basis` and `Basis.comparable` (`model.py:104`, `:152-175`),
   the frame's `price_basis` (`ohlcv.py:2228-2229`) and the adjustment rules in
   `OHLCVEngine` are the package's own claims. `models/statements.py:207` (`to_yfinance`) is the one
   place shape is converted, and its docstring lists three rules that are easy to get wrong —
   alias de-duplication (`:215-222`), newest-first columns (`:223-225`), and the Cr→Rs scale that
   applies only to money rows (`:226-229`). A plugin that shaped its own yfinance frame would
   re-implement all three, differently.
2. **The repository has already refused a surface adapter.** `plans/yfinance-interchangeability.md:167`
   says "do not build a benchmark harness, a surface-adapter abstraction, or a plugin registry" in
   the context of yfinance surface work. This proposal is a *data-source* contract, not a surface
   adapter; keeping the shaping on bfinance's side is what keeps that distinction true.
3. **The target moves.** The measured surface is 44 present / 14 present-but-empty / 40 absent
   against yfinance 1.7.0 (`docs/YFINANCE_PARITY.md:5-7`). A plugin pinned to that shape would rot on
   the next yfinance release, and bfinance's own frame contract is the thing tests already pin.
4. **A frame's `attrs` are passed through untouched.** `Resolved.value` is not rewritten
   (`model.py:393-395`), so a plugin can stamp any basis it likes on its own frame. The registry
   row's `bases` is the claim bfinance checks; a competing attr is not. A plugin must therefore not
   publish a `price_basis` of its own — let the cascade stamp it.

The short form: **vendor data in, yfinance shape out, and the shaping is bfinance's job.**

### 4.3 Worked example: the smallest real row

The `marketlens-close` row is the best template because it is minimal: one capability, one basis,
two columns, a per-symbol entry point, and an adapter that exists only because the entry's signature
is `price_history(symbol, period=...)` rather than `entry(request)`.

```python
# src/bfinance/sources/registry.py:625-653, abridged.
# The note is not optional and is where the measurement goes.
SourceSpec(
    name="marketlens-close",
    capabilities=("price_history",),
    bases=(Basis.AS_TRADED,),
    note="Raw close and volume, unadjusted, one symbol per request. ...",
    provides=("Close", "Volume"),
    module="bfinance.nse.marketlens",
    entry="price_history",
    adapter="bfinance.sources.bindings.marketlens_history",
)
```

```python
# src/bfinance/sources/bindings.py:301-310.
# The adapter's whole job: name the required keys and call the entry.
@_register("bfinance.sources.bindings.marketlens_history")
def marketlens_history(entry, request):
    given = _require(request, "symbol")
    return _resolved(entry(**given))
```

A plugin whose entry point already takes the request mapping needs no adapter at all: leave
`adapter` empty and `call_entry` will call `entry(dict(request))`, falling back to the zero-argument
form only on a signature complaint (`bindings.py:181-197`). That is the smallest possible plugin:
one row, no code in this package.

---

## 5. Config file design

### 5.1 Location and discovery — do not use the current working directory

The user asked for "a file in the root". That is achievable, but the *default* must not be
cwd-relative. There is no `getcwd`, `os.chdir` or `Path.cwd` anywhere in `src/` — not found — so
introducing cwd discovery would be a new concept, not an extension of one, and a dangerous one for a
library: `Ticker("TCS")` would return a different vendor depending on the directory the process was
started in. The test suite runs from the repository root, so a checked-in `bfinance.toml` would
silently re-route the suite as well.

Proposed resolution order, first match wins:

1. `BFINANCE_CONFIG` — an explicit path. Relative paths are resolved against the cwd *because the
   caller named them*, which makes the dependency explicit and opt-in.
2. `<data_home>/bfinance.toml`, where `<data_home>` is `BFINANCE_HOME` or `~/.bfinance`
   (`mirror.py:104`). This is a per-machine setting, not a per-checkout one.
3. No file — today's behaviour, byte for byte.

For a project-root file, the documented spelling is `BFINANCE_CONFIG=./bfinance.toml`. That satisfies
the request without making behaviour depend on where Python was launched.

### 5.2 Format: TOML, with one conditional dependency

`pyproject.toml:14` sets `requires-python = ">=3.10"`, and the runtime dependencies at `:31-39` do
not include `tomli`. `tomllib` entered the standard library in 3.11, so on the declared floor a TOML
loader means adding `tomli>=2.0; python_version < "3.11"` — one conditional line, no import on 3.11
or later. (That is a stdlib fact, not a claim about this tree; the floor is the cited line.)

| | TOML | JSON |
|---|---|---|
| Dependency on 3.10 | needs `tomli` (one line, conditional) | none |
| Comments | yes | no |
| Array of tables for `[[plugins]]` | native | a nested list |
| Already in this repository | yes, `pyproject.toml` | no config precedent |
| Hand-editing risk | low | trailing commas and quoting are easy to get wrong |

TOML is the recommendation because a config an operator edits during an incident needs comments, and
`[[plugins]]` is exactly the shape of "a list of rows". JSON is the fallback if adding a dependency
is refused; the loader should accept both and dispatch on the suffix, which is three lines, so the
decision is reversible.

### 5.3 Schema sketch — proposed, not implemented

```toml
# bfinance.toml
version = 1

[capabilities.price_history]
# Replaces the registry's cold-start order for this capability.
# A name that is not registered fails at load, before any request.
order = ["marketlens-close", "nse-bhavcopy"]
# Passed over without being called. A per-call `sources=` that names one wins.
disable = ["screener-chart"]
# Per-source ceiling, same rule as cascade.plan(max_ms=...). Unknown is not slow.
max_ms = 4000

[capabilities.corporate_actions]
order = ["nse-corporate-actions"]

[[plugins]]
name = "acme-prices"
capabilities = ["price_history"]
bases = ["as_traded"]
provides = ["Open", "High", "Low", "Close", "Volume"]
module = "acme_bfinance.prices"
entry = "price_history"
adapter = ""                    # empty means entry(request)
upstream_only = false
note = "Measured against the bhavcopy on 2026-10-20 over 40 symbols; as_traded because ..."
```

Validation rules, all of which must fail at load rather than at call:

1. Unknown top-level keys and unknown keys inside a capability table are errors. A typo that is
   ignored is an operator believing a policy is in force when it is not.
2. `order` names must be registered for that capability — checked with `names_for`
   (`registry.py:355`), the same rule `resolve_price_source_names` applies before a request
   (`ohlcv.py:884-895`).
3. A plugin may not reuse a registered name: `Registry.register` already raises on a duplicate
   (`registry.py:304-309`), and that message names both rows.
4. `bases` values must parse through `basis_from`; an invented basis is refused rather than
   compared against (`model.py:107-116`).
5. `note` is mandatory, for the reason the dataclass gives (`registry.py:171-175`).
6. A row in `order` that is registered but unwired (`wired` false, `:187-198`) is an error, not a
   skip. Otherwise a config author gets a call-time failure they cannot see until an outage (§3.7).
7. The module in a plugin row is imported only when a call reaches it (`cascade.py:176-177`), never
   at load. A config file must not turn into an import of arbitrary code at startup; it does become
   arbitrary code at first use, and that is worth saying in the file's own documentation.

### 5.4 Precedence

| Rank | Layer | Mechanism today | New? |
|---|---|---|---|
| 1 | Per-call argument | `source=`/`sources=` on `history` (`ticker.py:1170-1171`), forwarded to `plan(only=...)` (`cascade.py:377`); `disable=` (`:378`) | exists |
| 2 | Environment | `BFINANCE_SOURCES_<CAPABILITY>`, comma-separated names | **new** |
| 3 | Config file | `[capabilities.<name>].order` | **new** |
| 4 | Built-in default | `DEFAULT` registry order (`registry.py:889`) | exists |

The rules:

* A higher layer **replaces** the order; it does not merge. Merging makes "why did my call go to
  vendor X" unanswerable from the artefact the operator wrote, which is the whole reason to have a
  file.
* A lower-layer `disable` applies only when no higher layer supplies an order or names that source.
  Naming a source explicitly is a request to try it; honouring it is the same rule `only=` already
  follows (`cascade.py:260-279`).
* Environment parsing uses the same name validation as the file. An unknown name in
  `BFINANCE_SOURCES_PRICE_HISTORY` is an error, not a silent fall-through to the default — the
  failure mode that made `resolve_price_source_names` validate up front (`ohlcv.py:845-852`).
* The config never mutates `DEFAULT`. It builds a `Registry.copy()` (`registry.py:335-341`) and passes
  it down. `default_registry` returns the shared instance on purpose (`:892-899`), and mutating it
  would let one caller's file re-order every other caller in the process.

### 5.5 Loading and isolation

`tests/test_public_surface.py:71-80` lists `bfinance.sources` among the eight opt-in modules, and
`:291` asserts that `import bfinance` executes none of them; `:401` scans `bfinance/__init__.py` for
an import statement naming any of them. The config layer must therefore obey three rules:

1. `bfinance/__init__.py` does not import it.
2. The config module imports only the standard library and `tomli` (on 3.10). It produces plain
   dictionaries. It does **not** import `bfinance.sources`.
3. The mapping from dictionaries to `SourceSpec` lives in `bfinance.sources` and runs only when the
   cascade is engaged — the same laziness as `resolve_price_source_names` (`ohlcv.py:881`) and
   `_fetch_cascade_bars` (`:2311`).

Parse once per process, keyed on `(path, mtime)`. A 5,000-symbol `download` constructs 5,000
`Ticker`s (`download.py:99`), one per symbol from the list normalised at `:480-492`; reading and
parsing a file per construction would be a new cost on the hot path for a setting that cannot change
mid-run.

### 5.6 The data-directory key

Do not add one yet. `sqlite_cache.py:109-114` and `flows.py:1091-1095` ignore `BFINANCE_HOME`, while
`mirror.py:104`, `frame_store.py:160` and `universe.py:764` honour it; `frame_store.py:155-158`
records the inconsistency in prose. A `data_home` key would either inherit it (and mean two things)
or require changing two stores in the same commit. If the key is wanted, it should be the same value
as `BFINANCE_HOME`, and that change belongs in its own commit with its own test, not in the plugin
work.

---

## 6. Per-capability fallback design

### 6.1 The capability list

`price_history` and `statements` are registered today, and they are the two capabilities this report
proposes to route. The registry also carries single-source rows for `india_vix`, `index_valuation`,
`participant_flows`, `amfi_aum`, `index_membership`, `membership_history`, `transaction_costs`,
`shareholding_pattern`, `bulk_deals`, `block_deals` and `short_deals` (`registry.py:742-880`); they
are outside this table. The rest of the table is the proposal.

| Capability | Registered? | Source today | Members it feeds | Swappable? |
|---|---|---|---|---|
| `price_history` | yes, 5 rows (4 wired) | bhavcopy (default), Trendlyne, MarketLens, Screener chart | `history`, `download`, `Tickers.history`, `info`/`fast_info` (narrow) | yes — the only capability with real redundancy today |
| `corporate_actions` | no | NSE filings | `splits`, `actions`, `isin`, `history`'s Dividends/Stock Splits columns | one publisher |
| `profile` | no | Screener company page | `info`, `fast_info`, and the fundamentals behind the statements | one publisher |
| `statements` | yes, one row, dead from `Ticker` | the statements sub-cascade | `get_income_stmt`, `get_balance_sheet`, `get_cash_flow` and aliases | yes, but inside `statements/`, not here |
| `consensus` | no | one Trendlyne stock page | `recommendations`, `recommendations_summary`, `earnings_estimate`, `revenue_estimate`, `earnings_history`, `growth_estimates` | one document, six consumers |
| `insider` | no | Trendlyne insider page | `insider_transactions` | one route |
| `symbol_search` | no | three implementations, none registered | no public member today | yes, but there is no surface to route |
| `isin` | no — a property only | NSE feed, then the local universe archive | `isin` | partial: an internal two-step fallback |

Two of the eight are already rows, and both of those are the ones with genuine second sources. That
is not a coincidence: the registry was built where failover was possible. The other six are where the
work is, and most of them have one publisher (`registry.py:371-388` already has `is_solo` and
`is_upstream_exclusive` to say so).

### 6.2 Try-order semantics

`resolve` is already the specification; a fallback implementation should not invent a second one.

1. Refuse an unregistered or measured-unavailable capability before anything is called
   (`cascade.py:434-439`).
2. Plan the order without calling (`:441-450`): basis default (`:344-361`), caller order via `only=`
   (`:260-279`), then health reordering (`health.py:187-203`).
3. Skip, never call, a source on the wrong basis (`cascade.py:292-304`) or missing a requested
   column (`:305-320`), or one known slower than `max_ms` (`:321-333`).
4. Call in order. An exception is a failure and falls through (`:526-539`); an empty payload is a
   failure and falls through (`:541-546`).
5. The first non-empty answer wins and is returned with every attempt recorded (`:547-558`).
6. If none answers, raise `SourceUnavailableError` naming the capability and every provider
   considered (`:560-569`).

The ordering point that surprises people: the declared order is the **cold-start** order, not a
promise. A source with one recorded consecutive failure moves behind a healthy one
(`health.py:187-203`), and `degraded` is set only when a source was *asked* and failed — a basis skip
is the system working (`cascade.py:605-609`). A fallback policy that treats "degraded" as "the
answer is suspect" will therefore be wrong on every healthy basis skip.

### 6.3 When empty counts as failure

`is_empty_payload` (`cascade.py:127-153`) treats `None`, an empty mapping and any zero-row frame as
"did not answer". That is the right rule for a fallback: a vendor serving an HTML shell at HTTP 200
would otherwise read as a real answer, and "this has no history" is a claim about the market that the
source did not make (`:572-585`).

Two corrections are needed before this is published as a contract:

* The docstring at `:141-143` says a zero-row frame with columns is not a failure; `:151` returns
  `True` for it. The code is the behaviour; the sentence is wrong (§3.5).
* The four existing failure shapes (§3.5) should be reduced to two for anything routed through the
  cascade: **raise** with a reason, or **return empty/`None`**. The provenance-attr and
  columns-only shapes are useful to a direct caller, but they are not distinguishable from "the
  vendor answered nothing" by `is_empty_payload`, and a plugin should not be asked to reproduce
  `Ticker`'s attrs.

### 6.4 One frame, one basis — the hard rule

A single frame must never blend bases. This is already structural, not advisory:
`Resolved` is a single-provider object and no code path merges two providers into one
(`model.py:382-391`); a cross-source series is a `Series` of `Segment`s (`series.py:234`), and
`Series.frame` raises `BasisMismatchError` rather than collapsing a series with a hop
(`:307-320`). `Basis.comparable` is an identity test, deliberately (`model.py:152-175`).

Consequences for the design:

* A fallback is a *replacement*, not a splice. If the first source covers half the window, the
  second source is not asked to fill the other half within the same frame; the caller builds a
  `Series` and gets the seam recorded.
* A capability may have rows on several bases (`price_history` has three). The basis column is what
  makes that safe: an as-traded request never reaches an adjusted row (`cascade.py:292-304`).
* A config `order` that mixes bases is not a fallback. `_requested_price_basis` refuses a named list
  whose rows do not share a basis (`ohlcv.py:2455-2478`), and the config loader should apply the same
  check to `order` before the first call.
* A plugin may declare several bases only if it genuinely publishes each one. Declaring a basis "just
  in case" is the failure mode the whole package exists to stop (`registry.py:26-38`).

### 6.5 What is honestly not swappable

* **`statements`.** The real routing table is `statements/registry.py:574` over the nine-cell
  (kind, frequency) matrix (`statements/registry.py:420-424`); each cell needs a payload parser for
  a specific HTML or XHR shape, not a URL. The two vendors already disagree about basis in a way
  that is measured: Screener is
  consolidated-only (`statements/registry.py:383-388`) while the Trendlyne HTML page renders
  standalone and its XHR reports consolidated (`:396-402`). Adding a third vendor means writing a
  parser per cell and a basis claim per cell. `sources/registry.py:81-85` says explicitly that
  wrapping the two vendors as two rows here would duplicate the sub-cascade. Keep the plugin point
  inside `statements/`.
* **`dividends`.** It is not a dividend feed. It is the identity `Dividend Payout % × EPS in Rs`
  applied per fiscal year (`src/bfinance/market/corporate.py:127-138`), dated at fiscal year-end
  (`:121`, `:148-151`), with a guard against a negative payout (`:139-147`). A vendor that does not
  publish both columns in the same period shape cannot serve it, and a vendor that publishes genuine
  ex-dates is a different capability, not a second source for this one.
* **The consensus family.** Six members come from one document (`ticker.py:955-962`), and the
  labels are placed using the vendor's own `is_current`/`is_past`/`period_end` fields
  (`ticker.py:2007-2013`). A second vendor's "consensus" is a different survey of a different
  analyst set; swapping it changes the number, not just the route. A fallback here would need to say
  which survey answered on every frame, and the yfinance shape has nowhere to put that.
* **`splits`.** The factor is the filing's own `SplitRatio`, selected on the ratio rather than the
  event kind (`ticker.py:1538-1549`). There is one publisher of that ratio. A second source would
  have to publish a factor, not a price series to infer one from.
* **`isin`.** NSE's feed first, then `universe.isin_from_archive` (`ticker.py:1614-1619`), whose
  coverage is "archived indices only" and whose `None` means "not found in what is on disk", not "no
  ISIN exists" (`:1600-1605`). A plugin could add a third step, but it cannot make the second step's
  `None` mean something else.

---

## 7. Phased plan

Dependencies are strict: P0 before P1, P1 before P2, P2 before P3. Each phase is shippable on its
own, and each leaves the suite green.

### P0 — one basis, one name

**Why first.** Publishing a plugin contract on top of two names for one basis guarantees misfiled
sources. A plugin author reads `Basis` (`model.py:104-142`), sees `split_adjusted`, declares it, and
the frame that comes back is labelled `as_traded_split_bonus_adjusted` (`ohlcv.py:158`). The
registry's own row is in that position today (`registry.py:573` vs `:734` and
`price_provider.py:184`).

**Change.**

* Add `Basis.SPLIT_BONUS_ADJUSTED = "as_traded_split_bonus_adjusted"` to `sources/model.py`, after
  `SPLIT_ADJUSTED` at `:122`, with a comment saying how it differs: splits and bonuses applied by the
  vendor, demergers not (`price_provider.py:186-193`).
* Add it to `BASIS_AXIS` as `BasisAxis.PRICE` (`model.py:195-203`).
* Re-point `registry.py:573` and `:724` at the new member; update the two prose passages that call
  the row `split_adjusted` (`:255`, `:268`) and the sentence in `price_provider.py:92`.
* Leave `Basis.SPLIT_ADJUSTED`'s value alone, and leave `ohlcv.py:158` a literal: `ohlcv.py` must not
  import `bfinance.sources` at module level (`:788-791`). The test enforces the equality instead.

**Test.** In `tests/test_sources_cascade.py`, beside the existing basis assertions at `:1408`:
`Basis.SPLIT_BONUS_ADJUSTED.value == PRICE_BASIS_VENDOR_SPLIT_ADJUSTED`,
`not Basis.SPLIT_BONUS_ADJUSTED.comparable(Basis.SPLIT_ADJUSTED)`, and
`Basis.SPLIT_BONUS_ADJUSTED.axis is BasisAxis.PRICE`. Re-point
`tests/test_sources_price_provider.py:402-407`. Correct the docstring at
`tests/test_trendlyne_claims.py:86`, which names `Basis.SPLIT_ADJUSTED`.

**Risk.** `basis="split_adjusted"` will afterwards have **no** provider, and a request for it will
raise `SourceBasisError` naming the rows (`cascade.py:451-459`) instead of being served by the
Trendlyne row. That is the truthful state — the vendor's series is not that basis — but it is a
behaviour change and needs a release note. The blast radius is measurable: 62 occurrences of
`SPLIT_ADJUSTED` or `split_adjusted` across eight files, of which the routing assertions are
`tests/test_ohlcv_price_sources.py:490-543`, `:891`, `tests/test_sources_cascade.py:790-792`,
`:1731`, and `tests/test_sources_price_provider.py:402-407`.

**Rejected alternatives.** Changing `PRICE_BASIS_VENDOR_SPLIT_ADJUSTED` to `"split_adjusted"` would
break `tests/test_ohlcv_price_sources.py:989` (the frame label must equal the reader's own
`PRICE_BASIS`) and would drop the "demergers not applied" fact from the label a consumer branches on.
Changing `Basis.SPLIT_ADJUSTED`'s value would silently redefine a vocabulary member that every
existing frame already carries (`model.py:107-116`).

### P1 — a config file, with no behaviour change unless one exists

**Change.**

* New `src/bfinance/config.py`: stdlib only (plus `tomli` on 3.10), parses TOML/JSON to dictionaries,
  validates the schema of §5.3, caches on `(path, mtime)`. No import of `bfinance.sources`.
* New `src/bfinance/sources/policy.py`: dictionaries → `SourceSpec` → `Registry.copy()`; applies
  `order`/`disable`/`max_ms` by capability; returns a `Policy` object with the resolved order per
  capability. This is the only file that constructs specs from config.
* `pyproject.toml`: add `"tomli>=2.0; python_version < '3.11'"` to `:31-39`.

**Test.** New `tests/test_sources_config.py`: (i) with no file, `DEFAULT.names_for("price_history")`
is the pinned tuple and a resolved call behaves exactly as before; (ii) with a `tmp_path` file naming
`["marketlens-close", "nse-bhavcopy"]`, `plan("price_history", registry=policy.registry)` returns that
order and records no exclusions; (iii) an unknown name in `order` raises before any fetch, with the
registered names in the message; (iv) a subprocess probe asserts `import bfinance` still executes
none of the eight opt-in modules, reusing the assertion shape at `tests/test_public_surface.py:291`.

**Risk.** The obvious implementation reads the file at import time, which would (a) put file I/O on
`import bfinance` and (b) break the isolation rule if it imported `bfinance.sources`. Both are
prevented by the lazy rule in §5.5 and by a test that imports `bfinance` in a subprocess with a
config present and asserts the registry was not loaded. The second risk is silence: a malformed file
must raise, never fall back to the default. An operator who wrote an outage plan and got the default
order instead will not find out until the outage.

### P2 — route the default price path through the seam

**Change.**

* `OHLCVEngine` gains a policy-driven branch: `cascade_on` at `ohlcv.py:1765` becomes true when the
  resolved policy names sources for `price_history`, not only when the caller names them.
* `Ticker` builds the router from the policy and passes it to `OHLCVEngine` (`ticker.py:585` today
  passes one argument; `PriceSourceRouter` already exists at `ohlcv.py:803-837` and is accepted as
  the third optional constructor argument at `:1313-1315`).
* `info`/`fast_info` pass the configured names on their narrow request (`ticker.py:1136`, `:1095`)
  instead of nothing, which is the change the `INFO_PRICE_SOURCES` comment anticipated and refused
  (`:485-493`).

**Test.** With a counting `PriceSourceRouter(fetch=...)` and a policy naming `marketlens-close`
first, a Close-only call spends one per-symbol request and records one attempt; with no config, the
existing signature and direct-path tests are unchanged (`tests/test_ohlcv_price_sources.py:305-311`).
A subprocess test sets `BFINANCE_CONFIG` and asserts the configured vendor answered, so the default
suite cannot silently reach the network.

**Risk.** This is the phase with the largest hidden cost. `ohlcv.py:1760-1764` records that roughly
130 call sites across the suite construct the engine with two arguments and stub `bhavcopy_range` at
module scope; turning the cascade on by default would make those stubs stop being the thing that
answers and let an offline test reach the live NSE. The mitigation is the one already chosen: the
direct path remains the default, and only a config flips it. The default suite sets no config, and a
test asserts that.

### P3 — add capabilities one at a time

**Order and why.** `corporate_actions` first, because it feeds `splits`, `actions`, `isin` and
`history`'s Dividends/Stock Splits columns from one request (`ticker.py:1607-1609`).
`profile` second, because it feeds `info`, `fast_info` and every fundamental. `isin` third, because
it consumes both. `symbol_search` fourth, because there is no public surface to route yet — it is a
new member, not a new route (`trendlyne/client.py:3816`, `marketlens.py:446`,
`screener/client.py:368` are three implementations, none registered). `insider` fifth and
`consensus` last, because each needs a failure-shape decision (§6.3) before it can be a capability,
and the consensus row's granularity is one document with six consumers, not six rows.

**Change, per capability.** A row in `build_default()` with a mandatory note; an adapter in
`bindings.py` only if the entry is not `entry(request)`; a `provides` tuple only if it is a price
row; a failure policy (raise or empty) chosen from §6.3. No new base class, no new registry.

**Test, per capability.** Two stub rows in a local registry, the first raising and the second
serving; assert `Resolved.source` is the second, `degraded` is true, and `attempts` has the failure
then the success (`cascade.py:526-558`). Then one test that the real row's `module` and `entry` are
importable, in the shape of `tests/test_sources_price_provider.py:394-407`.

**Risk.** Each capability added before its failure shape is decided moves the inconsistency into the
registry, where it is harder to see. The other risk is public surface: per-call precedence (rank 1 in
§5.4) needs a `source=` argument on the member, which is a signature change. If that is refused, the
honest position is config-only for these capabilities, and the doc should say so rather than imply a
per-call override exists.

---

## 8. What this does and does not buy

**It does not improve yfinance parity by one member.** The measured surface is 44 present, 14
present-but-empty and 40 absent against yfinance 1.7.0 (`docs/YFINANCE_PARITY.md:5-7`). The absent
members are absent because no route was found — `eps_trend` and `eps_revisions` are refused because
the vendor publishes one current snapshot and no dated history (`:175-187`) — not because the routing
table was hard to edit. A plugin system changes *where* data comes from and *what happens on
failure*. It does not create a survey history, an ESG score, or a second publisher.

**Fallback is not cosmetic: it changes answers.** The registry's own measurement, 14 symbols × 2
windows on 2026-09-30, found 11 byte-identical and 3 that disagreed on 62 of 62 sessions — HDFCBANK
max |ΔClose| 932.60, MOTHERSON 56.27, NESTLEIND 1158.55, with each ratio a constant equal to that
symbol's own corporate-action factor (`registry.py:583-593`). The Trendlyne module records the
demerger case from the other side: ITC 2025-01-03 is 481.60 there and 481.60 on the bhavcopy while
yfinance says 463.74, and the trendlyne ÷ yfinance close ratio is 1.038513 at every session of five
separate years (`trendlyne/client.py:54-60`). A fallback that silently swaps sources inside a window
containing a split does not return a slightly different price; it returns a step that never happened.

Coverage is asymmetric in the other direction too: ETFs are absent from the Trendlyne sitemap, and
the route that resolves them is robots-disallowed, so "ETFs are unreachable through this package"
with the gate off (`trendlyne/client.py:504-509`), and the registry's row says such instruments are
refused rather than answered thin (`registry.py:610-612`). A fallback that placed that row first
would turn an ETF request into a refusal that a bhavcopy answer exists for.

The figures sometimes quoted for GOLDBEES — a 100× disagreement over two sessions and roughly 15% of
ETF sessions missing — were searched for in `docs/`, `src/` and `tests/` and **not found** in this
tree, so they are not used here. The measurements above are stronger because they are traceable to a
file and a line.

**Cost multiplies.** A cold `.info` is about 263 requests by construction: two Screener requests
(`screener/client.py:536`, `:575`) plus 261 bhavcopy archive reads, the count recorded at
`ohlcv.py:3340`. A cold `.history(period="1y")` is about 262: the same 261 plus one corporate-actions
call (`ticker.py:1023-1027`). A fallback that tries two sources on a total failure pays both. The
mitigations already exist and should be part of the policy, not left to the caller: per-source
`max_ms` skips a known-slow source (`cascade.py:321-333`, `health.py:205-220`), and basis gating
means an as-traded request spends nothing on the three adjusted rows (`cascade.py:292-304`). Health
is also the only memory that makes "fallback" more than "try again in the same order": the ledger
lives for the process (`health.py:247-252`) and can be reset per source (`:172-183`).

**What it does buy.** One place to add a vendor; one place to say which vendor is preferred for which
capability; a failure policy that is the same for every member instead of four different shapes; and
provenance on every result — `attempts` (`model.py:356`), the serving source, the basis, and
`degraded` (`cascade.py:605-609`). Those are the things that make a fallback honest when it changes
an answer.

**What not to build.** Do not register a second row for `india_vix`, `participant_flows`, `amfi_aum`,
`transaction_costs` or `ownership` unless a second publisher actually exists: `is_solo` and
`is_upstream_exclusive` already say when a failover is the same wait (`registry.py:371-388`). Do not
build a distribution mechanism for plugins before there is a second publisher for anything. Do not
let a plugin shape a yfinance frame (§4.2).

---

## 9. Citations

### 9.1 Every path:line used above

**`src/bfinance/sources/`**

| Citation | What is at that line |
|---|---|
| `registry.py:26-38` | Why the basis is a column and what the cascade enforces |
| `registry.py:81-85` | Why `statements` is one row, not two |
| `registry.py:102` | `class SourceSpec` |
| `registry.py:147-155` | The nine `SourceSpec` fields |
| `registry.py:160-164` | Refusal: a row with no capability |
| `registry.py:165-170` | Refusal: a row with no basis |
| `registry.py:171-175` | Refusal: a row with no note |
| `registry.py:176-180` | Refusal: module and entry must both be set or both empty |
| `registry.py:181-185` | Refusal: adapter with no module |
| `registry.py:187-198` | `wired` property |
| `registry.py:204-215` | `missing_columns` |
| `registry.py:217-230` | `unavailability` text |
| `registry.py:254-275` | `UNWIRED_NOTE` |
| `registry.py:278` | `class Registry` |
| `registry.py:297` | `register` |
| `registry.py:304-309` | Duplicate-name refusal |
| `registry.py:311-312` | Insertion order is the try-order |
| `registry.py:335-341` | `copy` |
| `registry.py:345` | `specs_for` |
| `registry.py:355` | `names_for` |
| `registry.py:371-388` | `is_solo` and `is_upstream_exclusive` |
| `registry.py:528-535` | `build_default`, pure data |
| `registry.py:548-566` | The `statements-cascade` row |
| `registry.py:571` | `name=TRENDLYNE_OHLC_PROVIDER` |
| `registry.py:573` | Its `bases=(Basis.SPLIT_ADJUSTED,)` |
| `registry.py:583-593` | The 14-symbol measurement, incl. 932.60 / 56.27 / 1158.55 |
| `registry.py:610-612` | ETFs refused rather than answered thin |
| `registry.py:619` | Its `provides` |
| `registry.py:625-653` | The `marketlens-close` row |
| `registry.py:627` | Its `name` |
| `registry.py:629` | Its `bases` |
| `registry.py:649` | Its `provides` ("Close", "Volume") |
| `registry.py:657` | `name="nse-bhavcopy"` |
| `registry.py:659` | Its `bases` |
| `registry.py:677-683` | The bhavcopy's measured cost |
| `registry.py:685` | Its `provides` |
| `registry.py:693` | `name="screener-chart"` |
| `registry.py:695` | Its `bases` |
| `registry.py:720-735` | The unwired `vendor-b-adjustments` row |
| `registry.py:722` | Its `name` |
| `registry.py:724` | Its `bases` |
| `registry.py:734` | Its `provides` |
| `registry.py:889` | `DEFAULT: Registry = build_default()` |
| `registry.py:892-899` | `default_registry` returns the shared instance |
| `registry.py:742-880` | The single-source rows outside the public surface |
| `cascade.py:127` | `is_empty_payload` |
| `cascade.py:135-139` | The three non-answer shapes |
| `cascade.py:141-143` | The paragraph that contradicts `:151` |
| `cascade.py:151` | The `.empty` check that returns `True` |
| `cascade.py:161` | `load_provider` |
| `cascade.py:174-175` | Unwired row raises `SourceUnavailableError` |
| `cascade.py:176-177` | `importlib.import_module` |
| `cascade.py:186` | `getattr(module, spec.entry)` |
| `cascade.py:225` | `plan` |
| `cascade.py:260-279` | `only=` is an order, not a filter |
| `cascade.py:292-304` | Basis skip |
| `cascade.py:305-320` | Column skip |
| `cascade.py:321-333` | `max_ms` skip |
| `cascade.py:344-361` | `_requested_basis` |
| `cascade.py:369` | `resolve` |
| `cascade.py:377-378` | `only` and `disable` arguments to `plan` |
| `cascade.py:525` | The call: `load_provider(spec)(req)` |
| `cascade.py:434-439` | Unregistered / unavailable capability refusal |
| `cascade.py:451-459` | `SourceBasisError` when no row shares the basis |
| `cascade.py:512` | The fail-through loop |
| `cascade.py:526-539` | Exception → failure |
| `cascade.py:541-546` | Empty payload → failure |
| `cascade.py:547-558` | First non-empty answer wins |
| `cascade.py:560-569` | Terminal `SourceUnavailableError` |
| `cascade.py:572-585` | `_empty_reason` |
| `cascade.py:605-609` | The `degraded` rule |
| `bindings.py:69-77` | `_resolved` drives coroutines |
| `bindings.py:127` | `ADAPTERS` |
| `bindings.py:145` | `load_adapter` |
| `bindings.py:151-158` | Unknown-adapter refusal |
| `bindings.py:165` | `PATH` |
| `bindings.py:181-197` | `call_entry` |
| `bindings.py:268-284` | `_payload_fetch` — statements needs caller-held payloads |
| `bindings.py:288` | `nse_bhavcopy_range` |
| `bindings.py:301-310` | `marketlens_history` |
| `bindings.py:314` | `price_provider_history` |
| `bindings.py:337` | `screener_chart_history` |
| `model.py:104` | `class Basis` |
| `model.py:107-116` | Why the vocabulary is closed and `basis_from` refuses an unknown |
| `model.py:120` | `AS_TRADED` |
| `model.py:122` | `SPLIT_ADJUSTED = "split_adjusted"` |
| `model.py:126` | `DIVIDEND_ADJUSTED` |
| `model.py:136` | `REPORTED` |
| `model.py:142` | `STATUTORY` |
| `model.py:152-175` | `comparable` is an identity test |
| `model.py:195-203` | `BASIS_AXIS` |
| `model.py:271` | `SERVED` |
| `model.py:273` | `FAILED` |
| `model.py:277` | `SKIPPED_BASIS` |
| `model.py:290` | `SKIPPED_COLUMNS` |
| `model.py:309` | `class Attempt` |
| `model.py:353` | `PROVENANCE` |
| `model.py:356` | `ATTEMPTS_ATTR` |
| `model.py:362` | `HEALTH_ATTR` |
| `model.py:382-391` | `Resolved` is single-provider by design |
| `model.py:393-395` | The payload is passed through untouched |
| `health.py:52` | `class SourceHealth` |
| `health.py:103` | `class HealthLedger` |
| `health.py:172-183` | `reset` |
| `health.py:187-203` | `order` |
| `health.py:205-220` | `slow` — unknown is not slow |
| `health.py:247-252` | `default_ledger` |
| `series.py:55` | `class Segment` |
| `series.py:198` | `class BasisHop` |
| `series.py:234` | `class Series` |
| `series.py:307-320` | `frame()` raises on a basis hop |
| `errors.py:49` | `SourceError` |
| `errors.py:53` | `SourceCapabilityError` |
| `errors.py:67` | `SourceUnavailableError` |
| `errors.py:78` | `SourceBasisError` |
| `errors.py:88` | `BasisMismatchError` |
| `price_provider.py:86-93` | The "splits and bonuses only, no demerger" prose |
| `price_provider.py:179` | `COLUMNS` |
| `price_provider.py:184` | `PRICE_BASIS = "as_traded_split_bonus_adjusted"` |
| `price_provider.py:186-193` | `DEMERGER_APPLIED = False` and why it rides on the frame |
| `price_provider.py:643` | `HTTPS_PROXY`/`HTTP_PROXY` fallback in the reader |
| `price_provider.py:914-931` | `price_history` builds a reader per call |
| `__init__.py:4-8` | The package is opt-in and imports none of the modules |

**`src/bfinance/` beyond `sources/`**

| Citation | What is at that line |
|---|---|
| `market/ohlcv.py:74` | `MIRROR_ENV_VAR = "BFINANCE_MIRROR"` |
| `market/ohlcv.py:158` | `PRICE_BASIS_VENDOR_SPLIT_ADJUSTED = "as_traded_split_bonus_adjusted"` |
| `market/ohlcv.py:777` | `PRICE_CAPABILITY = "price_history"` |
| `market/ohlcv.py:788-791` | Why `ohlcv` must not import `bfinance.sources` at module level |
| `market/ohlcv.py:792` | `DEFAULT_PRICE_BASIS = "as_traded"` |
| `market/ohlcv.py:803-837` | `PriceSourceRouter` and its three fields |
| `market/ohlcv.py:840-889` | `resolve_price_source_names` |
| `market/ohlcv.py:845-852` | Why a name is validated before a request |
| `market/ohlcv.py:881` | The lazy `bfinance.sources.registry` import |
| `market/ohlcv.py:884-895` | Name validation and its refusal message |
| `market/ohlcv.py:1313-1315` | `OHLCVEngine.__init__` accepts `price_sources` |
| `market/ohlcv.py:1755-1764` | Why `requires` is not a cascade trigger; ~130 stubbing call sites |
| `market/ohlcv.py:1765` | `cascade_on = ...` |
| `market/ohlcv.py:1793-1796` | The direct `_fetch_bhavcopy_bars` branch |
| `market/ohlcv.py:1806-1823` | Empty bars + `auto_adjust` raises |
| `market/ohlcv.py:2228-2229` | `price_basis` written on every frame |
| `market/ohlcv.py:2278` | `_fetch_cascade_bars` |
| `market/ohlcv.py:2311` | The lazy cascade import |
| `market/ohlcv.py:2359-2363` | `plan` then `resolve` |
| `market/ohlcv.py:2442-2478` | The basis of a named source list, and the refusal when they differ |
| `market/ohlcv.py:2988` | `BFINANCE_MIRROR` read |
| `market/ohlcv.py:3223-3233` | Direct bhavcopy transport failure raises |
| `market/ohlcv.py:3235-3242` | Direct bhavcopy empty archive raises |
| `market/ohlcv.py:3340` | The measured 261 archive requests |
| `ticker.py:485-493` | `info` explains why it does not engage the cascade; `INFO_PRICE_SOURCES` |
| `ticker.py:501-515` | `Ticker.__init__` signature |
| `ticker.py:585` | `OHLCVEngine(self.screener_client)` — no router passed |
| `ticker.py:656-661` | `_ensure_profile` calls `get_company_profile` |
| `ticker.py:903-906` | Trendlyne sections off → raise |
| `ticker.py:952-983` | `_stock_page_section`; six sections on one document |
| `ticker.py:1001` | The one hit for "settings" (a docstring) |
| `ticker.py:1023-1027` | Per-instance `CorporateActionsClient`, one request |
| `ticker.py:1095` | `fast_info`'s narrow history request |
| `ticker.py:1116` | `info` |
| `ticker.py:1136` | `info` calls `history(period="1y", requires=...)` with no source |
| `ticker.py:1170-1171` | `source` / `sources` on `history` |
| `ticker.py:1288-1308` | Forwarding the pair to the engine |
| `ticker.py:1328-1330` | The same on `history_async` |
| `ticker.py:1374-1409` | `get_income_stmt` |
| `ticker.py:1423-1424` | Non-yearly balance sheet raises |
| `ticker.py:1528-1531` | `dividends` via `CorporateActionsEngine.extract_dividends` |
| `ticker.py:1538-1549` | `splits` selects on `SplitRatio` |
| `ticker.py:1577-1586` | `splits` empty shape + provenance attrs |
| `ticker.py:1589-1605` | `isin`, and what its `None` means |
| `ticker.py:1607-1609` | One corporate-actions request serves three members |
| `ticker.py:1614-1619` | `isin`'s two-step chain |
| `ticker.py:1655-1659` | `actions` empty shape + attrs |
| `ticker.py:1673-1683` | `capital_gains` is a constant empty series |
| `ticker.py:1948-1974` | `recommendations`; empty frame with columns on error |
| `ticker.py:2002-2036` | `earnings_estimate`; `is_current`/`is_past`/`period_end` |
| `ticker.py:2200-2210` | `insider_transactions`; failure returns a bare empty frame |
| `market/quotes.py:260-273` | `build_info_dict`; absent keys are omitted, not `None` |
| `download.py:88-89` | `source` / `sources` on `download` |
| `download.py:99` | One `Ticker` constructed per symbol |
| `download.py:480-492` | The ticker list and `resolve_price_source_names` |
| `market/corporate.py:117-158` | `extract_dividends` |
| `market/corporate.py:121` | "Dated at fiscal year-end" |
| `market/corporate.py:127-128` | The payout × EPS identity |
| `market/corporate.py:139-147` | The negative-dividend guard |
| `market/corporate.py:148-151` | Period → date parsing |
| `market/corporate.py:155-158` | Empty series on no rows |
| `statements/registry.py:139` | `VENDORS` |
| `statements/registry.py:151-154` | `TRENDLYNE_DATA_ROUTES` |
| `statements/registry.py:167` | `SCREENER_DATA_ROUTE` |
| `statements/registry.py:378-410` | `PROVIDERS` |
| `statements/registry.py:420-424` | The nine (kind, frequency) cells; four cannot be served |
| `statements/registry.py:383-388` | Screener is consolidated-only |
| `statements/registry.py:396-402` | Trendlyne HTML renders standalone |
| `statements/registry.py:574` | `providers_for` |
| `statements/cascade.py:238-264` | The statements fail-through |
| `statements/cascade.py:267-274` | Its terminal raise |
| `models/statements.py:207` | `to_yfinance` |
| `models/statements.py:215-229` | Alias de-duplication, newest-first, selective scale |
| `nse/bhavcopy.py:1508` | `_DEFAULT_CLIENT` |
| `nse/bhavcopy.py:1052` | `HTTPS_PROXY`/`HTTP_PROXY` fallback |
| `nse/bhavcopy.py:1511-1515` | `_default_client` |
| `nse/bhavcopy.py:1569` | `bhavcopy_range` calls `_default_client()` |
| `nse/corporate_actions.py:1393` | `_DEFAULT_CLIENT` |
| `nse/corporate_actions.py:1175` | `HTTPS_PROXY`/`HTTP_PROXY` fallback |
| `nse/marketlens.py:451` | `_DEFAULT_CLIENT` |
| `nse/marketlens.py:446` | `search` (a sync search implementation) |
| `nse/marketlens.py:197` | `HTTPS_PROXY`/`HTTP_PROXY` fallback |
| `nse/macro.py:1514` | `_DEFAULT_CLIENT` |
| `nse/macro.py:993` | `HTTPS_PROXY`/`HTTP_PROXY` fallback |
| `nse/macro.py:1517-1520` | `_default_client`, never called in `src/` |
| `nse/macro.py:1534` | A shortcut that builds `MacroClient(host=...)` |
| `nse/macro.py:1542` | The same for `index_daily` |
| `nse/macro.py:1553` | The same for `india_vix` |
| `nse/macro.py:1558` | The same for `valuations` |
| `ownership.py:1860` | `_DEFAULT_CLIENT` |
| `ownership.py:1555` | `HTTPS_PROXY`/`HTTP_PROXY` fallback |
| `trendlyne/client.py:54-60` | ITC 481.60 vs 463.74; ratio 1.038513 vs 0.962915 |
| `trendlyne/client.py:504-509` | ETFs absent from the sitemap; the resolving route is disallowed |
| `trendlyne/client.py:3816` | `search` (a sync search implementation) |
| `trendlyne/client.py:3277` | `HTTPS_PROXY`/`HTTP_PROXY` fallback |
| `trendlyne/smartoptions.py:686` | `HTTPS_PROXY`/`HTTP_PROXY` fallback |
| `trendlyne/client.py:5370` | `_DEFAULT_CLIENT` |
| `cache/sqlite_cache.py:109-114` | `cache.db` hard-coded, ignores `BFINANCE_HOME` |
| `flows.py:1091-1095` | `flow_archive.db` hard-coded, ignores `BFINANCE_HOME` |
| `cache/frame_store.py:155-160` | The inconsistency, documented; `frames.db` honours the env var |
| `mirror.py:104` | `BFINANCE_HOME` or `~/.bfinance` |
| `universe.py:764` | The same for the archive root |
| `universe.py:1258` | `isin_from_archive` |
| `universe.py:1298` | The same env var on the second path |
| `costs.py:1679-1698` | `_fixture_text`, the only `__file__`-relative read |
| `costs.py:1724-1726` | The two cost fixture env vars |
| `screener/client.py:211` | `HTTPS_PROXY`/`HTTP_PROXY` fallback |
| `screener/client.py:368` | A search implementation |
| `screener/client.py:536` | The profile's main-page request |
| `screener/client.py:575` | The peers request (the second profile request) |

**Tests, docs, build**

| Citation | What is at that line |
|---|---|
| `tests/test_sources_cascade.py:135-156` | `stub_source` and `registry_of` — the plugin protocol in miniature |
| `tests/test_sources_cascade.py:790-792` | The Trendlyne row's basis, used in a routing test |
| `tests/test_sources_cascade.py:1408` | `Basis.SPLIT_ADJUSTED.axis is BasisAxis.PRICE` |
| `tests/test_sources_cascade.py:1731` | A `Segment` on `Basis.SPLIT_ADJUSTED` |
| `tests/test_sources_price_provider.py:394-407` | The row's module, entry, wiring and basis |
| `tests/test_ohlcv_price_sources.py:305-311` | The engine's signature contract |
| `tests/test_ohlcv_price_sources.py:490-543` | The split-adjusted request and the as-traded skip |
| `tests/test_ohlcv_price_sources.py:891` | The skip reason text |
| `tests/test_ohlcv_price_sources.py:989-990` | Frame constant == reader constant; != as-traded |
| `tests/test_trendlyne_claims.py:84-96` | The unwired row's basis, in prose |
| `tests/test_public_surface.py:71-80` | The eight opt-in modules |
| `tests/test_public_surface.py:291` | `import bfinance` executes none of them |
| `tests/test_public_surface.py:401-415` | The AST scan of `bfinance/__init__.py` |
| `docs/YFINANCE_PARITY.md:5-7` | 44 present / 14 present-but-empty / 40 absent |
| `docs/YFINANCE_PARITY.md:175-187` | `eps_trend` and `eps_revisions` refused, with the reason |
| `plans/yfinance-interchangeability.md:167` | The non-goal: no surface-adapter abstraction |
| `pyproject.toml:14` | `requires-python = ">=3.10"` |
| `pyproject.toml:31-39` | Runtime dependencies; no `tomli` |
| `pyproject.toml:41-48` | The only extra table, `[project.optional-dependencies] dev` |

### 9.2 Lines that moved, and claims that were dropped

Every citation above was re-read in this tree. These differ from the brief that commissioned the
report:

| Brief said | This tree | Note |
|---|---|---|
| `tests/test_public_surface.py:284-293` asserts the import claim | the docstring is `:284-290`, the assertion is `:291`, the class starts at `:280` | corrected |
| `universe.py:1297` reads `BFINANCE_HOME` | `universe.py:1298` | corrected |
| `statements/registry.py` routes at `:380`/`:392`/`:406` | `PROVIDERS` starts at `:378`; the route constants are `:167` and `:151-154` | the cited lines are the provider entries, not the routes |
| `statements/cascade.py:247` is the fail-through | the `try` is `:247-249`; the `continue` is `:264` | same behaviour, wider citation used |
| `insider_transactions` at `ticker.py:2210` returns "empty frame, columns only" | it returns a bare `pd.DataFrame()` with **no** columns | corrected; this is the inconsistency inside shape 3 |
| Profile cost is "2 requests" | derived from `screener/client.py:536` and `:575` | the 263 total is a sum, not a stopwatch reading |
| Trendlyne and Yahoo disagree by 100× on GOLDBEES for two days; Trendlyne misses ~15% of ETF sessions | **not found** in `docs/`, `src/` or `tests/` | dropped; the verified measurements in §8 are used instead |
| `cascade.py:141-143` describes empty-frame behaviour | it contradicts `:151` | recorded as gap 3.5, not silently repeated |
