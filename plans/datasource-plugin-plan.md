# Data-source policy plan: P0, P1, P2

**Status: proposed, not started.** Written 2026-10-28 against `66ebb90`. This is the executable
plan for the work `docs/PLUGGABLE_DATASOURCES.md` argues for; that document is the *why*, this one
is the *what*, in the order it must happen.

Scope is three phases. **P3 (entry-point discovery) is not planned here** — see the deferral
section at the end, which is a decision and not a placeholder.

---

## 1. Definition of done

The increment is complete when all six hold:

1. One price basis has one name, in code and in the frames, with no second spelling reachable.
2. A `bfinance.toml` exists or does not. **No file reproduces today's behaviour, byte for byte** —
   including the part where a bhavcopy failure raises instead of silently answering from another
   vendor.
3. With a file present, the configured order is what runs, and it is consulted by the *default*
   call, not only by callers who pass `source=`.
4. Every answer produced by a fallback publishes the basis actually used, plus provenance, and the
   basis is the same as the request's unless a human wrote `allow_basis_change = true`.
5. Unknown keys, unknown capability names and unknown source names fail at **load**, naming the
   file — never silently ignored, never deferred to call time.
6. The full suite is green with no new `xfail`, and `import bfinance` still imports none of the
   opt-in modules.

## 2. Phases

| Phase | What | Effort | Risk | Precondition |
|---|---|---|---|---|
| **P0** | One basis, one name | ~1 session | Low — vocabulary only, blast radius measured | none |
| **P1** | `bfinance.toml` + validation | ~1–2 sessions | Low — inert until a file exists | P0 |
| **P2** | Default routing reads the policy | ~2 sessions | **Moderate** — the only phase that changes answers | P1 |
| P3 | Entry-point discovery | — | High | **Deferred, not planned** |

P0 and P1 change no caller-visible answer. P2 does, and only for a caller who wrote a file.

## 3. Bindings for every phase

- **Basis may be reordered within, never changed across.** A single frame is one basis. Cross-basis
  fallback requires `allow_basis_change = true`, and even then publishes the real basis.
- **No behaviour change without a file.** P1 must be inert; P2 must be inert without one.
- **Fail at load, not at call.** A typo in a policy file is an error the operator sees once, not a
  silently disabled rule.
- **Every `Verify:` is run and its output pasted into the commit message.** No projected numbers.
- New tests must fail on the pre-change code. A test that passes both before and after proves
  nothing.

---

## P0 — one basis, one name

### Objective
`Basis` and the price frames name the same basis the same way, so a source author can declare a
basis that a real frame will match.

### Why first
A plugin contract publishes a vocabulary. If the vocabulary has two names for one basis, every
source registered against it is a coin flip, and the failure is silent: the cascade's safety
argument is `Basis.comparable` doing an equality test (`src/bfinance/sources/model.py:107-110`).

### The defect, measured

| | Where | Says |
|---|---|---|
| Enum member | `src/bfinance/sources/model.py:122` | `SPLIT_ADJUSTED = "split_adjusted"` |
| Enum docstring for that member | `src/bfinance/sources/model.py:121` | "Restated for splits **and bonus issues** across the window" |
| Row basis | `src/bfinance/sources/registry.py:573`, `:724` | `bases=(Basis.SPLIT_ADJUSTED,)` |
| Frame value | `src/bfinance/market/ohlcv.py:158` | `PRICE_BASIS_VENDOR_SPLIT_ADJUSTED = "as_traded_split_bonus_adjusted"` |
| Provider constant | `src/bfinance/sources/price_provider.py:184` | `PRICE_BASIS = "as_traded_split_bonus_adjusted"` |
| The same file's prose | `src/bfinance/sources/price_provider.py:92` | "The registry row's basis is `Basis.SPLIT_ADJUSTED`" |

The two are semantically **the same basis** — as-traded, splits and bonuses applied, demergers not.
They differ only in spelling, which is exactly the condition the enum's own docstring says it exists
to prevent. `tests/test_sources_price_provider.py:403-407` shows the split inside one assertion: the
failure message names `as_traded_split_bonus_adjusted` while the assertion is
`spec.serves(sources.Basis.SPLIT_ADJUSTED)`.

Occurrence counts, measured on this tree (exact, not substring):

| Token | Occurrences | Files |
|---|---|---|
| quoted `"split_adjusted"` literal | 8 | 3 |
| `Basis.SPLIT_ADJUSTED` member use | 14 | 6 |
| `"as_traded_split_bonus_adjusted"` literal | 15 | 9 |
| **total code occurrences** | **37** | **12 distinct** |

### Decision to make (P0.0)

Two shapes, and the plan recommends the first:

- **A — recommended: one member, aliased.** Add
  `SPLIT_BONUS_ADJUSTED = "as_traded_split_bonus_adjusted"` and make `SPLIT_ADJUSTED` an alias of
  it (same value, so Python's `Enum` folds it). Old code keeps working, `Basis.SPLIT_ADJUSTED.value`
  becomes the string the frames already carry, and the two names can no longer drift because there
  is one value.
- **B — clean break.** Rename the member outright and delete the old spelling. Smaller enum, but a
  breaking change to an exported name for a spelling fix.

Choose A unless the next release is already a major bump. **Record the choice in the commit**, because
reviewers will ask why the enum lists one basis twice.

> The design document proposed only "add the member and re-point the rows", which leaves
> `split_adjusted` as a second, unproduced basis that a future source can still declare. Aliasing
> removes that possibility instead of documenting it.

### Tasks

| # | File | Change |
|---|---|---|
| P0.1 | `src/bfinance/sources/model.py` | Add the member at `:122` per the chosen shape, with a comment naming the frame that produces it (`src/bfinance/market/ohlcv.py:158`). |
| P0.2 | `src/bfinance/sources/model.py:195-197` | Add the member to `BASIS_AXIS` as `BasisAxis.PRICE`. A member with no axis entry fails `basis.axis` at call time. |
| P0.3 | `src/bfinance/sources/price_provider.py:184` | `PRICE_BASIS = Basis.SPLIT_BONUS_ADJUSTED.value` — one literal, not two. Update `:92` prose to the new member name. |
| P0.4 | `src/bfinance/market/ohlcv.py:158` | Same: derive the constant from the enum. `ohlcv.py` already imports `bfinance.sources.cascade` lazily at `:2311`; import `model` at module level only if that creates no cycle — otherwise alias the value with a comment and a test that asserts equality. |
| P0.5 | `src/bfinance/sources/registry.py:573`, `:724` | Re-point both rows to the new member. |
| P0.6 | `src/bfinance/sources/registry.py:255`, `:268`, and the two prose blocks near `:598`/`:605` | Replace the word `split_adjusted` in prose with the new member name. Prose that names a basis is what a reader copies. |
| P0.7 | `src/bfinance/sources/model.py` `Basis` docstring | One sentence: which member is canonical and which is an alias, so the duplication is deliberate and explained. |

### New tests

| Test | Assertion | Fails on the pre-change code because |
|---|---|---|
| `test_the_vendor_frame_basis_is_a_registered_enum_value` | For every registry row on the price axis, `row.bases` values each equal `Basis(value).value` — i.e. the declared basis is a real enum value, not just a string. | The row declares `"split_adjusted"` while the frame publishes `"as_traded_split_bonus_adjusted"`. |
| `test_every_price_row_serves_the_basis_its_provider_publishes` | For the Trendlyne row, `spec.serves(Basis.SPLIT_BONUS_ADJUSTED)` is true and the constant its provider stamps equals that member's value. | Today the assertion is on `SPLIT_ADJUSTED`, so one of the two must be false. |
| `test_split_adjusted_and_split_bonus_adjusted_are_one_value` | `Basis.SPLIT_ADJUSTED.value == Basis.SPLIT_BONUS_ADJUSTED.value` (shape A only). | The two values differ today. |
| `test_no_second_price_basis_string_exists_in_src` | Walk `src/`, assert the only occurrences of the two spellings are the enum definition and module constants derived from it — no bare literal in a row. | The row and the provider each carry their own literal today. |

### Existing tests to update (the blast radius)

- `tests/test_sources_price_provider.py:407` — `spec.serves(sources.Basis.SPLIT_ADJUSTED)` must
  become the canonical member. Leave `:408-411` untouched: the `not serves(AS_TRADED)` assertion is
  the one that stops a failover splicing an adjusted series onto the tape.
- `tests/test_sources_cascade.py` — 4 quoted literals and 7 member uses.
- `tests/test_ohlcv_price_sources.py` — 2 quoted literals.
- `tests/test_trendlyne_claims.py` — 1 member use.
- `tests/test_frame_store.py`, `tests/test_trendlyne_core.py` — assert the frame literal; they
  should pass unchanged, which is the control that the frame did not move.

### Verify

```bash
# 1. the vocabulary is single-valued
uv run python -c "from bfinance.sources import Basis; print(Basis.SPLIT_BONUS_ADJUSTED.value, Basis.SPLIT_ADJUSTED.value)"

# 2. the affected modules, then the whole offline suite
uv run pytest tests/test_sources_price_provider.py tests/test_sources_cascade.py tests/test_ohlcv_price_sources.py -q
uv run pytest -q -m "not live"
```

Expected: the first prints the same string twice; the suite is green with the same skip/xfail counts
as at `66ebb90` and no new ones.

### Rollback
One commit, vocabulary only: `git revert` restores both spellings and every frame is byte-identical,
because no frame value changed.

---

## P1 — a config file that can only be explicit

### Objective
A `bfinance.toml` can state, per capability, which sources to try in which order — and its mere
existence changes nothing until P2 wires it.

### Why here
Nothing in `src/` parses a config file today. Verified: no `tomllib`, `tomli`, `yaml`,
`configparser` or `dotenv` anywhere under `src/`. The whole configuration surface is five
environment variables — `BFINANCE_HOME`, `BFINANCE_COSTS_EXCHANGE_TEXT`, `BFINANCE_COSTS_STT_TEXT`,
`HTTPS_PROXY`, `HTTP_PROXY` — and **none of them selects a data source**.

### Tasks

| # | File | Change |
|---|---|---|
| P1.1 | `src/bfinance/config.py` (new) | Discovery, then parsing. Order, first match wins: `BFINANCE_CONFIG` (relative resolved against the cwd, because the caller named it) → `<data_home>/bfinance.toml` → nothing. **Never** cwd-relative by default: `Ticker("TCS")` must not answer from a different vendor because of the directory a process started in, and the suite runs from the repo root. |
| P1.2 | `src/bfinance/config.py` | `version = 1` is required and refused if absent or unknown. An unversioned policy file is one the library cannot promise to read the same way twice. |
| P1.3 | `src/bfinance/sources/policy.py` (new) | `RoutingPolicy` — capability → ordered source names, a `disable` list, `allow_basis_change`. Pure data plus validation; no I/O, so it is testable without a file. |
| P1.4 | `src/bfinance/sources/policy.py` | Validate against the live registry: unknown capability, unknown source name, a source that does not declare the capability, an unknown top-level key → raise `ConfigError` naming the file and the key. **Load-time failure is the feature.** |
| P1.5 | `pyproject.toml` | `tomli>=2.0; python_version < "3.11"` — `requires-python` is `>=3.10` (`pyproject.toml:14`) and `tomllib` is stdlib only from 3.11. One conditional line, no code branch: `try: import tomllib except ImportError: import tomli as tomllib`. |
| P1.6 | `src/bfinance/config.py` | Load lazily, once per process, cached. `tests/test_public_surface.py` enforces that `import bfinance` pulls in none of the opt-in modules; config must not become a new import at package load. |

Shape, for review:

```toml
version = 1

[capabilities.price_history]
order = ["nse-bhavcopy", "marketlens-close"]
allow_basis_change = false
```

### New tests

| Test | Assertion |
|---|---|
| `test_no_file_means_no_policy` | With `BFINANCE_CONFIG` unset and no data-home file, loading returns `None` and imports nothing heavy. |
| `test_unknown_source_name_fails_at_load` | A name not in the registry raises `ConfigError` naming the file and the name. |
| `test_unknown_key_fails_at_load` | `orderr` raises rather than being ignored. |
| `test_a_source_that_cannot_serve_the_capability_fails_at_load` | Naming a statements source for `price_history` is refused, not accepted and failed later. |
| `test_relative_config_path_resolves_against_the_cwd` | `BFINANCE_CONFIG=./bfinance.toml` works from the directory named, and only there. |
| `test_config_is_not_read_at_import` | `import bfinance` with a policy file present performs no file read (monkeypatched `open` or a sentinel mtime). |

### Verify

```bash
uv run python -c "import bfinance, sys; print([m for m in sys.modules if 'bfinance.config' in m])"   # expect []
uv run python -c "from bfinance.sources.policy import RoutingPolicy"                                  # imports clean
uv run pytest tests/test_config.py -q
uv run pytest -q -m "not live"
```

Expected: the first prints `[]`; the suite is unchanged from P0, because P1 is inert.

### Rollback
Delete the two new modules and the dependency line. Nothing else imports them until P2.

---

## P2 — routing that reads the policy

### Objective
`Ticker("X").history()` consults the policy when one exists, and behaves exactly as today when one
does not.

### Why here
Without this, P1 is inert: the file validates and `history()` still calls the bhavcopy fetcher
directly. The seam already exists — `OHLCVEngine.__init__` accepts
`price_sources: Optional[PriceSourceRouter]` (`src/bfinance/market/ohlcv.py:1313-1315`),
`PriceSourceRouter` is at `:803`, and `Ticker` constructs the engine with one argument at
`src/bfinance/ticker.py:585`. Today `cascade_on` (`:1765`) is false unless the caller named a
source, and the false branch calls `self._fetch_bhavcopy_bars(...)` at `:1793` and raises with no
fallback.

### Tasks

| # | File | Change |
|---|---|---|
| P2.1 | `src/bfinance/ticker.py:585` | Pass a router built from the policy when one exists; pass nothing when none does. One argument, conditionally. |
| P2.2 | `src/bfinance/market/ohlcv.py:1765` | `cascade_on` also true when a policy-supplied router is present, with the configured order as `price_names`. Do not touch the `requires=` reasoning in the comment above it. |
| P2.3 | `src/bfinance/sources/cascade.py:141-143` | Fix the docstring contradiction: it says a zero-row frame is **not** a failure, while `:151` returns `True` for exactly that. The implementation is the correct behaviour — an empty frame is the shape a failed vendor returns — so the sentence goes, not the code. |
| P2.4 | `src/bfinance/sources/policy.py` | `allow_basis_change = false` is enforced by construction: the ordered list handed to the cascade contains only rows on the requested basis. Cross-basis rows are dropped with a debug log, not tried and refused. |
| P2.5 | `src/bfinance/ticker.py` | When an answer came from a non-first source, stamp `attrs["bfinance_fallback_from"]` and the source that answered, so a degraded answer is identifiable after the fact. |

**Explicitly deferred inside P2:** a call-shape-aware default order. A year of one symbol is ~261
bhavcopy requests but one Trendlyne request; `download()` over 500 symbols is the reverse. Making the
default depend on call shape is the right end state and is a separate, measured change — not a
side effect of this one.

### New tests

| Test | Assertion |
|---|---|
| `test_no_policy_still_raises_when_bhavcopy_fails` | With no file, a stubbed bhavcopy failure raises `RealOHLCUnavailableError` exactly as today. This is the regression test for the whole phase. |
| `test_policy_makes_the_default_history_use_the_ordered_sources` | With a file naming a stub source first, a bare `history()` returns that source's frame. |
| `test_cross_basis_fallback_is_refused_by_default` | A policy naming an adjusted source for an as-traded request does not answer from it. |
| `test_cross_basis_fallback_is_allowed_only_by_the_flag` | With `allow_basis_change = true`, it answers and `price_basis` equals the basis actually used, not the one requested. |
| `test_a_fallback_answer_is_labelled` | `attrs["bfinance_fallback_from"]` is present only when a later source answered. |
| `test_empty_frame_from_the_first_source_falls_through` | An empty frame from source 1 reaches source 2 — the `is_empty_payload` contract, asserted through the cascade rather than in isolation. |

### Verify

```bash
uv run pytest tests/test_sources_cascade.py tests/test_price_path_single_fetch.py -q
uv run pytest -q -m "not live"
# and the no-file control, which must be indistinguishable from the pre-P2 behaviour:
uv run python -c "import os; os.environ.pop('BFINANCE_CONFIG', None); from bfinance import Ticker; print('ok')"
```

Expected: green, with the same request-count assertions in `tests/test_price_path_single_fetch.py`
that exist today — P2 must not add a fetch to the default path.

### Rollback
P2 is the only phase that changes answers. Revert it and the config file becomes inert again; P0 and
P1 stay.

---

## P3 — deferred, and why

Entry-point discovery (`[project.entry-points."bfinance.sources"]`) is **not** in this plan.

- The ask was a file in the repository root that selects sources. A config row already does that;
  discovery adds distribution convenience, not capability.
- It contradicts the cheap-import contract: enumerating entry points at import pulls modules in, and
  `tests/test_public_surface.py` forbids that today. Making it lazy is solvable; the next point is
  not.
- **It is a trust boundary with no verification.** A plugin declares its own basis and nothing can
  check the claim. With a config file a human wrote the name; with discovery any installed package
  can enter every price call and claim `as_traded` while returning adjusted bars, and every `Adj
  Close` downstream is wrong with no error anywhere.
- Order becomes ambiguous when two packages register the same capability, and the tie-break would
  fall out of installation order — different on different machines.

Revisit only with signed/allow-listed packages and an explicit precedence rule.

## What this plan refuses

- **Making statements, `dividends`, `splits`, or the consensus family swappable.** They are
  schema-bound: statements are screener sections with vendor labels and a crore-to-rupee scale,
  `dividends` is `Dividend Payout % × EPS` dated at fiscal year-end rather than an ex-date feed, and
  the consensus members are placed from `is_current`/`is_past` flags. A second vendor there is a
  different frame, not the same member from elsewhere.
- **A `data_home` config key.** Existing code already disagrees about what `BFINANCE_HOME` means;
  exposing it in a policy file would publish a contradiction rather than fix it.
- **Any silent cross-basis fallback**, including inside `allow_basis_change = true`: that flag
  changes what is *tried*, never what is *claimed*.
- **A new plugin framework or a yfinance surface-adapter layer.** `plans/yfinance-interchangeability.md:167`
  refuses the latter by name, and the former would rename working code.

## Corrections to `docs/PLUGGABLE_DATASOURCES.md`

Recorded here so the two documents do not disagree:

1. **§3.4 / §7 P0: "62 occurrences across 8 files" is wrong.** Measured exactly on this tree: the
   quoted `"split_adjusted"` literal appears 8 times in 3 files, `Basis.SPLIT_ADJUSTED` 14 times in
   6 files, and `"as_traded_split_bonus_adjusted"` 15 times in 9 files — **37 occurrences across 12
   distinct code files**. 62 counted overlapping substring matches, and counted `docs/`.
2. **§7 P0 leaves `split_adjusted` as an unproduced second basis** after re-pointing the rows. This
   plan prefers aliasing the two names to one value, so the vocabulary cannot drift again.
3. **§3.5 cites the `is_empty_payload` contradiction as `cascade.py:135-143` vs `:151`.** The
   contradictory sentence is `:141-143` specifically; `:135-139` correctly lists an empty frame as a
   non-answer and matches the code.
