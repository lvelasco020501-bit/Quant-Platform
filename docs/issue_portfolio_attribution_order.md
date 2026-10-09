# Issue — portfolio cost attribution depended on the interpreter's hash seed

**Found:** 2026-10-09, during M42 Phase 1, by the reproduction check that compares M42's
equal-weight arm against M38's own published Risk V2 card.
**Scope:** `quantplatform.research.portfolio._rebalance`. Research only. No production code path,
no live Risk, no execution, no paper session, no VPS. Fixed in the same change that found it.
**Severity:** low on magnitude, high on principle. It moved no verdict in any milestone, and it
made a published number unreproducible.

## What happened

`_rebalance` charged each holding's trading cost against the equity standing at the moment the
charge was levied:

```python
for holding in set(book.actual) | set(target):  # <- order came from tuple hashing
    ...
    charge = book.equity * abs(after - before) * rate
    book.paid += charge
    book.equity -= charge
    book.spent[holding] = book.spent.get(holding, ZERO) + charge
```

Holdings are `(sleeve, market)` tuples of strings, and CPython randomises string hashing per
process, so the iteration order of that set differed between runs. The order decides how the
bar's total cost is split between the holdings that moved: the first one visited is charged
against the larger equity.

**The equity that comes out is not affected.** Closing equity is
`equity * Π(1 - w_i · rate)`, a product of the same factors in a different sequence. So the
return, the drawdown, the Calmar ratio, the exposure, the out-of-sample return and the cost
stress rows are all order-independent, and total turnover and total fees shift only at the far
end of the 40-digit working precision from Decimal summation order.

**What is affected is attribution.** `spent` feeds `SleeveContribution.cost`, which feeds
`by_asset` and `by_sleeve`, which feed `top_asset_share` and `top_sleeve_share`; and
`open_cost` feeds each closed episode's net result, which feeds the portfolio profit factor.
Those are precisely the three fields M42's reproduction check flagged.

## Why nothing noticed for eight milestones

Every consumer of these numbers compares them against a threshold — 0.60 for concentration,
1.00 for profit factor — and the drift is five orders of magnitude smaller than the distance to
any threshold. A single run is also self-consistent, so the defect is invisible unless the same
evidence is measured twice in two processes. M42 is the first milestone to do that: it recomputes
M38's baseline rather than quoting it, which is the only reason this surfaced at all.

## Demonstration

```
$ for seed in 1 2 3 4; do PYTHONHASHSEED=$seed python probe.py; done
AAA=5.025665270069352923044763407 BBB=6.143940440670202689429910615 CCC=5.657420492923652767261421753
AAA=5.025679486037157050330395567 BBB=6.143810660093579200291798282 CCC=5.657536057532472129113901926
AAA=5.025637527073760614615803580 BBB=6.143940440670202689429910615 CCC=5.657448235919245075690381581
AAA=5.025637527073760614615803580 BBB=6.143940440670202689429910615 CCC=5.657448235919245075690381581
```

Same series, same targets, same costs, three different splits of the same total.

## The fix

One word: `for holding in sorted(set(book.actual) | set(target)):`. The regression test is
`test_cost_attribution_does_not_depend_on_the_interpreter_hash_seed`, which runs the simulation
in four subprocesses under four hash seeds and demands one answer. It fails without the sort and
passes with it, which is the only way this class of defect can be caught — an in-process test
sees one process's order and calls it stable.

## Effect on results already published

`scripts/m38_compare.py` was re-run under the fix and its report regenerated. **No verdict
moved.** Breadth 6 still fails on drawdown, breadth 12 still passes, and M38 is still an overall
NO-GO. The measured movements, across all three bases and both breadths:

| field | largest relative movement |
|---|---|
| CAGR, drawdown, Calmar, exposure, OOS, cost stress, yearly shares | none — identical |
| turnover, fees | 1.5e-37 |
| profit factor | 4.2e-08 |
| top asset share | 1.9e-05 |
| top sleeve share | 3.7e-05 |

M36's and M38's cached per-pair extractions are untouched: the defect was in how a portfolio
was measured, not in what the engine did, so no engine run had to be repeated. Milestone
documents M34–M38 quote attribution figures that differ from the regenerated ones in the fifth
decimal; they have not been rewritten, because restating them would imply the conclusions
changed, and none did.
