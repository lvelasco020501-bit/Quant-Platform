# M42 — inverse-volatility allocation: Phase 1, and the end of the research programme

**Research only.** No `BacktestEngine` run. Production Risk, production execution, the paper
sessions, the VPS and both sleeves' parameters are untouched. No strategy was added, no
indicator was added, no stop was moved, Risk V2 was not modified, and there is no leverage and
no short anywhere in this milestone.

**Verdict: Phase 1 FAILS at both breadths. Phase 2 is not opened. The research programme closes,
exactly as M41's closing condition said it would.**

---

## 1. Pre-declaration

Committed in `src/quantplatform/research/m42.py` and `tests/unit/test_m42_predeclaration.py`
before any M42 number existed. Every threshold is inherited by reference, not retyped:

| declared | value | inherited from |
|---|---|---|
| volatility window | 72 bars | `m32.LIQUIDITY_WINDOW` |
| volatility definition | `rvol_72` — population std. dev. of 72 one-bar close-to-close returns | the production `IndicatorFeatures` pipeline |
| window must be contiguous in calendar time | — | M31's FTT finding, via `rotation.contiguous` |
| unusable volatility → holding not funded, share redistributed | — | the project's zero-denominator convention (silence, not a guess) |
| normalisation | match equal weight's deployed exposure bar by bar | M34's allocator; `normalise_to`'s reason |
| per-market cap preserved, surplus water-filled | 1 / breadth | M34's `Allocation.per_asset_cap` |
| CAGR conservation | ≥ 0.50 of baseline | `m37.EDGE_CONSERVATION_FOR_ALTERNATIVE` |
| drawdown cap | ≤ 35% | `m22.SCREEN_MAX_DRAWDOWN` |
| Calmar floor | ≥ 0.50 | `m29.MIN_CALMAR` |
| cost stress | ×2 and ×3 both > 0 | `m29.COST_STRESS_MULTIPLIERS` |
| concentration | asset ≤ 0.60, sleeve ≤ 0.60, single year ≤ 0.50 | M30, M34, M29 |
| profit factor | ≥ 1.00 | `m29.MIN_NEIGHBOUR_PROFIT_FACTOR` |
| out-of-sample | > 0 from 2024-01-01 | `m30.OOS_START` |

### The gate was declared close to self-excluding, in writing, before results

Risk V2 earns **7.53%** a year on the full universe at breadth six. Conserving half of it leaves
**3.77%**, and a Calmar floor of 0.50 then demands a drawdown at or under **7.5%** — a fifth of
the 35% the drawdown gate alone would allow. **Taken together the gates require inverse-vol to
raise the return *and* lower the drawdown, not to trade one for the other.** That is a hard bar
for a pure re-weighting, and it is written down in the module docstring and fixed in
`test_the_declared_gate_is_close_to_self_excluding` so that this NO-GO cannot be waved away as a
badly chosen gate — and so that no threshold could have been lowered to manufacture a pass.
**Nothing was lowered. Nothing moved after results existed.**

### One lemma, proved rather than hoped

Equal weight deploys `active signals / (breadth × sleeves)`; the caps available in that bar total
`wanted markets / breadth`. A market can be wanted by at most every sleeve, so the first never
exceeds the second: **the per-market cap can never force a shortfall**, and water-filling always
has somewhere left to put the budget. The only way a bar can fall short is an unusable
volatility, which goes to cash and is counted. Consequence, and the reason the experiment is
clean: both arms hold the same capital in the same bars, so a drawdown difference is a
re-weighting effect and cannot be a de-risking effect in disguise.

---

## 2. Phase 1 results — equal weight vs inverse volatility

Risk V2 masks from M36/M38 (60 pairs, 0 failures), 30 markets, 19 792 bars, 2017-09-01 →
2026-09-14, fee 10 bps + slippage 5 bps one way.

### Breadth 6

| | CAGR | DD | Calmar | PF | turnover | fees | exposure | OOS | ×2 | ×3 | top asset | top sleeve | best yr | yrs+ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| equal weight | **+7.53%** | 26.38% | **0.29** | 1.15 | 464.7 | 9 510 | 43.5% | **+40.21%** | −0.45% | −7.84% | 41.2% | 78.3% | 38% | 70% |
| inverse vol | +6.44% | **25.04%** | 0.26 | 1.14 | 476.5 | 9 491 | 42.8% | +34.66% | −1.65% | −9.13% | 43.3% | **66.3%** | 44% | 70% |

### Breadth 12

| | CAGR | DD | Calmar | PF | turnover | fees | exposure | OOS | ×2 | ×3 | top asset | top sleeve | best yr | yrs+ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| equal weight | **+7.50%** | **22.44%** | **0.33** | 1.14 | 439.8 | 9 528 | 55.0% | **+24.10%** | −0.07% | −7.11% | 23.4% | 72.4% | 31% | 80% |
| inverse vol | +5.65% | 22.76% | 0.25 | 1.12 | 447.2 | 9 229 | 54.2% | +14.83% | −1.91% | −8.93% | 32.8% | **68.4%** | 40% | 70% |

### Gates

| breadth | result | failed |
|---|---|---|
| 6 | **FAILS** | `low_calmar`, `cost_fragile`, `sleeve_concentration`, `exposure_not_matched` |
| 12 | **FAILS** | `low_calmar`, `cost_fragile`, `sleeve_concentration`, `exposure_not_matched` |

**Three of those four are substantive, and the equal-weight baseline fails all three too**
(Calmar 0.29/0.33 against a 0.50 floor; ×2 cost already negative at −0.45%/−0.07%; sleeve share
78.3%/72.4% against a 0.60 cap). They are not inverse-vol's doing — they are Risk V2's, as M38
already reported. The fourth, `exposure_not_matched`, is a construction failure of M42's own:
**136 bars at breadth 6 and 161 at breadth 12** — 0.7% and 0.8% of the sample — went to cash
because no funded holding had a usable volatility that bar. It is reported as a failure because
that is what was declared, and it is immaterial to the verdict: the CAGR gap of 1.09 and 1.85
points is an order of magnitude larger than 0.8% of exposure could account for. **The verdict
would be identical without it.**

CAGR conservation, the one gate written specifically for M42, is the one gate inverse-vol
passes: 6.44% and 5.65% both clear the 3.77% and 3.75% floors. It passes because the floor is
low, not because the re-weighting worked.

---

## 3. Where the weight went, and why it did not help

The mechanism did exactly what it was built to do. It is the premise that was wrong.

| | capital in quietest third | middle | noisiest third | HHI capital | HHI risk |
|---|---|---|---|---|---|
| breadth 6 — equal weight | 52.5% | 30.1% | 17.4% | 0.6196 | 0.6349 |
| breadth 6 — inverse vol | **59.8%** | 27.7% | **12.5%** | 0.6305 | **0.6182** |
| breadth 12 — equal weight | 47.6% | 30.4% | 22.0% | 0.5553 | 0.5720 |
| breadth 12 — inverse vol | **56.3%** | 28.1% | **15.5%** | 0.5675 | **0.5537** |

Inverse-vol moved 7–9 points of capital out of the noisiest third and into the quietest, and it
inverted the relationship between the two concentrations: equal weight is more concentrated in
**risk** than in capital (0.6349 > 0.6196), inverse-vol is more concentrated in **capital** than
in risk (0.6305 > 0.6182). That is the textbook signature of the method working.

**And the drawdown barely moved — in the wrong direction at breadth 12.** 26.38% → 25.04% at
breadth 6, 22.44% → **22.76%** at breadth 12. A pure re-weighting that demonstrably flattened
risk contribution failed to flatten the drawdown.

### The attribution says why

Both arms' deepest drawdown is the *same episode*: 2021-04-14 → 2023-06-28 at breadth 6, and
2021-08-11 → 2023-10-11 at breadth 12. Twenty-six months, to the bar, in both arms. And the
markets that lost the money inside that window are the same markets in both arms:

| | worst five inside the drawdown window |
|---|---|
| breadth 6 — equal weight | DOGE −542, ETH −475, TRX −387, ETC −285, BNB −244 |
| breadth 6 — inverse vol | ETH −608, DOGE −449, TRX −358, BNB −285, BTC −161 |
| breadth 12 — equal weight | ETH −433, AVAX −314, XRP −304, DOGE −269, ADA −265 |
| breadth 12 — inverse vol | ETH −485, ADA −289, XRP −248, DOGE −229, DOT −216 |

Re-weighting shuffles the order and changes nothing about the identity. **The drawdown is one
common factor, not a volatility-concentration effect.** When the whole asset class falls
together, reallocating between its members cannot reduce the fall — it can only decide which
member delivers it. Inverse-vol bought less of the noisiest third, and the noisiest third was
not where the loss came from: ETH and BTC are in the quiet end of this universe, and ETH is the
largest single loser in three of the four rows above.

This also sharpens what M34 and M35 actually achieved, which M41 had read too generously. M34's
0.08 was a correlation **between sleeves**, two different rules on the same markets; M35 widened
the **market count**. Neither was an allocation effect, and M42 is the measurement that
separates them: holding the masks and the deployed capital fixed and changing only the weights
buys nothing.

### Secondary costs

Inverse-vol **raised** turnover at both breadths (464.7 → 476.5 and 439.8 → 447.2) and left cost
fragility worse (×2 −0.45% → −1.65% and −0.07% → −1.91%). That is structural rather than
incidental: volatility moves every bar, so a target proportional to its reciprocal re-weights
every bar, where equal weight only trades when a mask changes. No rebalancing band was
introduced to soften this — a band is a parameter, choosing one after seeing the turnover would
be optimising on results, and it was not declared.

---

## 4. A defect found on the way, fixed, and documented separately

M42's reproduction check — the equal-weight arm must reproduce M38's own published Risk V2 card —
**refused to judge on the first run**, over three attribution fields disagreeing in the sixth
significant digit. The cause was real: `_rebalance` visited holdings in set order, so the split
of each bar's cost between them depended on the interpreter's hash seed. Written up in
[`issue_portfolio_attribution_order.md`](issue_portfolio_attribution_order.md). One-word fix,
regression test across four hash seeds, M38's report regenerated under the fix, **no verdict in
any milestone moved**, largest movement 3.7e-05 relative. The tolerance was not widened.

Worth stating plainly: that check exists because M42 recomputes its baseline instead of quoting
it, and it is the only reason an eight-milestone-old non-determinism surfaced at all.

---

## 5. Reproducibility

- `uv run python scripts/m42_phase1.py` → `var/research/m42/inverse_vol_phase1_4h.json`
- Volatility cached at `var/research/m42/volatility_4h.json`, keyed by feature name and market
  set, recomputed if either changes.
- Inputs: `var/research/m36/positions_4h.json` (Risk V2, 60 pairs), M32's point-in-time
  universe, M30's costs and OOS window. No engine run, no new cache of positions.
- The equal-weight arm is verified against M38's published card to 1e-20 on all fourteen
  measures before any gate is read; a mismatch aborts rather than reports.
- Tests: `tests/unit/test_m42_predeclaration.py` (39), `tests/unit/test_inverse_vol_allocation.py`
  (22, including the hash-seed regression and the no-look-ahead check).

---

## 6. Verdict

**Phase 1 FAILS at breadth 6 and at breadth 12. Phase 2 is not opened. No engine run. No ninth
family. The research programme M13–M42 closes here.**

M41 set the closing condition in advance: *"Si el re-ponderado por volatilidad inversa no baja el
drawdown de cartera por debajo del 35% conservando el CAGR, se para el programa de research y se
cierra, en lugar de abrir una novena familia."* The re-weighting did not clear the gate, and it
failed in the most informative way available — not by being badly tuned, but by moving the thing
it was designed to move and leaving the drawdown where it was.

**What M13–M42 established, and it is a real result even though it is a negative one:** at 4H
these signals carry a genuine 29–30% gross edge at a 26–32% drawdown, and no risk configuration
or capital allocation tested over eight families and thirty milestones keeps that edge at a
drawdown this project would accept. Risk V2 keeps 25% of it. M37's ALT2 keeps 70% and fails on
drawdown at breadth 6. Equal weight and inverse-vol differ by noise. The binding constraint is
not the stop, not the time limit, not the entry rule and not the weights: **it is that this
universe has one factor, and a long-only book in it inherits that factor's drawdown whatever it
holds and however much of each.** Beating it needs something this platform does not have —
leverage to scale a small edge, or the ability to be short — and both are excluded by the
declared constraints.

The one survivor of the whole programme is unchanged and unimpressive: **B2 on BTC at 4H,
+1.7%/year**, the single paper candidate from M29.
