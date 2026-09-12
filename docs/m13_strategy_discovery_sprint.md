# M13 — Strategy Discovery Sprint

**Estado:** COMPLETADO · research/backtesting únicamente · 2026-09-12

Nada de este milestone toca Risk, execution, portfolio, paper ni la sesión PAPER en curso.
`git status` sobre `src/quantplatform/{risk,execution,portfolio,paper,backtesting}` está vacío
en el commit de este documento, y el registro que usa paper sigue siendo exactamente
`['breakout', 'ema_trend']`.

## 1. Pregunta

¿Alguna familia nueva de estrategias — trend/momentum, mean reversion, regime/volatility —
merece pasar a PAPER? Y para cada una, por separado:

1. ¿tiene edge por sí misma?
2. ¿sobrevive al Risk V2 real?
3. ¿o solo parece buena porque se relajó el riesgo?

## 2. Dos escenarios de riesgo, nunca intercambiables

| | Configuración | Para qué sirve |
|---|---|---|
| **A · DEPLOYED RISK V2** | Exactamente la de paper (`var/research/m10c/def_breakout_v2.json`), latches y breakers incluidos. | ¿Sobrevive a la política que paper ejecutaría? |
| **B · RESEARCH VARIANT** | Idéntica a A **excepto** `max_consecutive_losses = None` y `latch_total_drawdown = False`. **No es Risk V2.** | ¿Cuánto tradea la estrategia y tiene edge por sí misma? |

Por qué existe B: bajo A, el breaker de pérdidas consecutivas latchea en las primeras semanas
y rechaza el resto del año (en EMA20/50, 3 986 de 4 008 decisiones). En vivo un humano revisa y
resetea; en un backtest de un año no hay humano, así que el latch trunca la muestra en silencio
y no dice nada de la estrategia. B es un instrumento de investigación, implementado como datos
de la definición del experimento (`research_risk_variant`), sin una línea de código de Risk
cambiada.

Ambos escenarios comparten todo lo demás: sizing al 1 % de riesgo por trade, exposición máx.
50 %, stop inicial 300 bps, break-even 150 bps, trailing 300/200 bps, take-profit 600 bps,
time stop 168 barras, pérdida diaria 3 %, **comisión 10 bps, slippage 5 bps, spread 2 bps**.

## 3. Datos

BTC/USDT spot, 1h, 2025-09-01 → 2026-09-01, 8 760 velas — el dataset de M10c (Binance Vision,
procedencia y checksums en `data/raw/m10c/PROVENANCE.md`). Capital inicial 10 000 USDT.

## 4. Protocolo — fijado antes del primer resultado

Todo lo siguiente vive en código (`src/quantplatform/research/sprint.py`) y lo fijan tests
(`tests/unit/test_strategy_sprint_protocol.py`):

* **Un parámetro canónico por estrategia**, elegido por convención — nunca por un resultado.
* **Dos vecinos** a cada lado, solo para medir fragilidad. Un vecino nunca es candidato.
* **IS / OOS:** IS 2025-09-01 → 2026-05-01 · OOS 2026-05-01 → 2026-09-01.
* **Walk-forward:** las 4 ventanas de M10c (test: oct–nov, ene–feb, abr–may, jul–ago).
  No se ajusta nada en la ventana de train.
* **Stress** (sobre B): fees ×2 · slippage ×3 · fees ×2 + slippage ×3 + spread 5 bps.
* **Veredicto mecánico** (`judge`), umbrales fijos:

| Veredicto | Condición |
|---|---|
| **REJECT** | 0 trades, retorno ≤ 0, o PF < 1 |
| **WEAK** | positivo pero falla alguno: ≥ 30 trades · OOS > 0 · ≥ 50 % ventanas WF positivas y mediana > 0 · todo stress > 0 · ambos vecinos PF ≥ 1 · DD ≤ 15 % |
| **PROMISING** | pasa todo lo anterior |
| **PAPER CANDIDATE** | PROMISING + ≥ 75 % ventanas WF positivas + DD ≤ 10 % + PF ≥ 1.2 + supera a ambos benchmarks + **retorno positivo bajo A (Risk V2 real)** |

La última condición la añadió el usuario durante el sprint y se implementó y testeó **antes de
abrir ningún resultado**. Una estrategia que solo funciona con los breakers relajados puede
llegar a PROMISING, nunca a PAPER CANDIDATE. El retorno solo entra en el veredicto como
"> 0" y, en el nivel superior, "> benchmarks": no se elige ganadora por retorno.

## 5. Estrategias y parámetros exactos

Todas long-only, spot, H1, sin estado entre llamadas. Solo existen en el registro de research
(`build_research_registry`); ninguna es alcanzable desde paper.

| Estrategia | Familia | Regla | Canónico | Vecinos |
|---|---|---|---|---|
| `ema_trend` | benchmark | fast EMA > slow EMA | 20 / 50 | — |
| `breakout` | benchmark | Donchian: high > máx 20, sale low < mín 10 | 20 / 10 | — |
| `momentum_roc` | trend | entra ROC(n) > 0, sale ROC(n) < 0 | n=72 | 48 · 96 |
| `ema_slope` | trend | pendiente EMA > 0 y close > EMA; sale pendiente < 0 | EMA 50, pendiente 10 | 40 · 60 |
| `breakout_trend` | trend | Donchian 20/10 solo con close > SMA(t) | t=200 | 150 · 250 |
| `vol_momentum` | trend | ROC(72) > k·σ·√72; sale ROC < 0 | k=1.0, σ 72 | 0.75 · 1.25 |
| `zscore_revert` | mean reversion | z(n) < −2, sale z ≥ 0 | n=48 | 36 · 60 |
| `bollinger_revert` | mean reversion | close vuelve dentro de banda −2σ; sale en la media | n=20, 2σ | 16 · 24 |
| `rsi_reversal` | mean reversion | RSI(n) < 30, sale > 50 | n=14 | 10 · 18 |
| `regime_trend` | regime | momentum 72 solo si ER(72) ≥ 0.30 | ER 0.30 | 0.25 · 0.35 |
| `regime_revert` | regime | z-score 48 solo si ER(72) ≤ 0.20 | ER 0.20 | 0.15 · 0.25 |
| `regime_switch` | regime | momentum si ER ≥ 0.30, reversión si ER ≤ 0.20 | 0.30 / 0.20 | ±0.05 ambos |
| `vol_filtered_momentum` | regime | momentum 72 solo si σ24/σ168 ≤ 1.5 | 1.5 | 1.25 · 1.75 |

ER = efficiency ratio de Kaufman. Indicadores nuevos en `src/quantplatform/features/indicators.py`,
todos funciones puras sobre una ventana acotada que termina en la barra decidida.

## 6. Resultados

Generado por `scripts/m13_report.py` desde la evidencia en `var/research/m13/` (ignorada por
git, como la de M10c). Ningún número de esta sección se escribió a mano.

**A** = DEPLOYED RISK V2 (exactly what paper runs).  **B** = RESEARCH VARIANT (non-latching breakers, not Risk V2).  Cells read `A / B`.

### 1. Both scenarios

| STRATEGY | DEPLOYED RISK RESULT (A) | RESEARCH VARIANT RESULT (B) | TRADES | PF | EXPECTANCY | DD | RETURN | VERDICT |
|---|---|---|---|---|---|---|---|---|
| ema_trend | -1.30% · 99% blocked · latch 2025-09-28 | -11.69% · WF 1/4 · med -1.73% | 11 LOW / 162 | 0.37 / 0.70 | -11.85 / -7.18 | 1.57% / 16.46% | -1.30% / -11.69% | benchmark (REJECT) · no edge |
| breakout | -2.29% · 96% blocked · latch 2025-09-17 | -16.98% · WF 1/4 · med -2.19% | 14 LOW / 207 | 0.03 / 0.57 | -16.33 / -8.14 | 2.30% / 19.87% | -2.29% / -16.98% | benchmark (REJECT) · no edge |
| momentum_roc | -0.97% · 100% blocked · latch 2025-09-07 | -19.34% · WF 1/4 · med -2.39% | 5 LOW / 203 | 0.00 / 0.51 | -19.35 / -9.53 | 1.20% / 20.61% | -0.97% / -19.34% | REJECT · no edge |
| ema_slope | -1.48% · 100% blocked · latch 2025-09-28 | -14.73% · WF 1/4 · med -2.49% | 7 LOW / 166 | 0.07 / 0.63 | -21.14 / -8.85 | 1.85% / 19.30% | -1.48% / -14.73% | REJECT · no edge |
| breakout_trend | -1.44% · 97% blocked · latch 2025-09-17 | -7.97% · WF 1/4 · med -1.72% | 8 LOW / 133 | 0.05 / 0.69 | -17.94 / -5.89 | 1.44% / 11.10% | -1.44% / -7.97% | REJECT · no edge |
| vol_momentum | -0.80% · 97% blocked · latch 2025-11-27 | -8.35% · WF 1/4 · med -2.44% | 14 LOW / 69 | 0.82 / 0.62 | -5.73 / -12.11 | 3.23% / 13.92% | -0.80% / -8.35% | REJECT · no edge |
| zscore_revert | -8.01% · 86% blocked · latch 2025-12-14 | -15.44% · WF 1/4 · med -1.78% | 36 / 123 | 0.36 / 0.53 | -22.24 / -12.55 | 8.93% / 18.05% | -8.01% / -15.44% | REJECT · no edge |
| bollinger_revert | -15.98% · 17% blocked · latch 2026-06-03 | -19.28% · WF 0/4 · med -1.79% | 144 / 189 | 0.42 / 0.43 | -11.09 / -10.20 | 16.21% / 19.85% | -15.98% / -19.28% | REJECT · no edge |
| rsi_reversal | -14.59% · 66% blocked · latch 2026-02-05 | -16.85% · WF 1/4 · med -2.88% | 115 / 193 | 0.45 / 0.54 | -12.68 / -8.73 | 14.62% / 18.87% | -14.59% / -16.85% | REJECT · no edge |
| regime_trend | +3.72% · 0% blocked · latch never | +3.72% · WF 2/4 · med +0.18% | 18 LOW / 18 LOW | 1.86 / 1.86 | 20.64 / 20.64 | 3.79% / 3.79% | +3.72% / +3.72% | WEAK · positive, unproven (LOW SAMPLE), survives deployed |
| regime_revert | -4.97% · 91% blocked · latch 2025-11-07 | -15.42% · WF 1/4 · med -2.07% | 16 LOW / 88 | 0.17 / 0.41 | -31.07 / -17.53 | 5.71% / 16.69% | -4.97% / -15.42% | REJECT · no edge |
| regime_switch | -2.19% · 91% blocked · latch 2025-11-07 | -11.42% · WF 1/4 · med -2.63% | 21 LOW / 111 | 0.59 / 0.58 | -10.44 / -10.28 | 4.58% / 17.00% | -2.19% / -11.42% | REJECT · no edge |
| vol_filtered_momentum | -0.34% · 100% blocked · latch 2025-09-17 | -18.26% · WF 0/4 · med -1.86% | 7 LOW / 194 | 0.62 / 0.53 | -4.81 / -9.41 | 1.16% / 20.31% | -0.34% / -18.26% | REJECT · no edge |

### 2. A · deployed Risk V2 — what the risk engine allowed

| STRATEGY | DECISIONS | APPROVED | LATCHED REJECTIONS | BLOCKED | FIRST LATCH | TOP REFUSAL |
|---|---|---|---|---|---|---|
| ema_trend | 4008 | 22 | 3986 | 99.5% | 2025-09-28 | the consecutive_losses circuit breaker is latched |
| breakout | 820 | 28 | 791 | 96.5% | 2025-09-17 | the consecutive_losses circuit breaker is latched |
| momentum_roc | 4256 | 10 | 4246 | 99.8% | 2025-09-07 | the consecutive_losses circuit breaker is latched |
| ema_slope | 3491 | 14 | 3477 | 99.6% | 2025-09-28 | the consecutive_losses circuit breaker is latched |
| breakout_trend | 550 | 16 | 534 | 97.1% | 2025-09-17 | the consecutive_losses circuit breaker is latched |
| vol_momentum | 993 | 28 | 965 | 97.2% | 2025-11-27 | the consecutive_losses circuit breaker is latched |
| zscore_revert | 506 | 72 | 434 | 85.8% | 2025-12-14 | the consecutive_losses circuit breaker is latched |
| bollinger_revert | 345 | 288 | 57 | 16.5% | 2026-06-03 | the consecutive_losses circuit breaker is latched |
| rsi_reversal | 673 | 230 | 441 | 65.5% | 2026-02-05 | the consecutive_losses circuit breaker is latched |
| regime_trend | 36 | 36 | 0 | 0.0% | never | — |
| regime_revert | 371 | 32 | 339 | 91.4% | 2025-11-07 | the consecutive_losses circuit breaker is latched |
| regime_switch | 517 | 42 | 472 | 91.3% | 2025-11-07 | the consecutive_losses circuit breaker is latched |
| vol_filtered_momentum | 3892 | 14 | 3878 | 99.6% | 2025-09-17 | the consecutive_losses circuit breaker is latched |

### 3. B · research variant — full KPIs (not Risk V2)

| STRATEGY | WIN RATE | AVG WIN | AVG LOSS | TIME IN MKT | SLIPPAGE | MAX CONSEC L | OVERLAY EXITS |
|---|---|---|---|---|---|---|---|
| ema_trend | 34.57% | 49.16 | 36.94 | 48.26% | 401.73 | 9 | 88/162 |
| breakout | 32.85% | 33.08 | 28.31 | 33.64% | 489.33 | 14 | 29/207 |
| momentum_roc | 23.15% | 42.22 | 25.12 | 35.59% | 491.03 | 12 | 48/203 |
| ema_slope | 30.72% | 49.03 | 34.52 | 45.54% | 401.21 | 9 | 74/166 |
| breakout_trend | 35.34% | 37.94 | 29.84 | 24.27% | 333.90 | 10 | 18/133 |
| vol_momentum | 36.23% | 54.59 | 50.00 | 27.99% | 173.80 | 10 | 47/69 |
| zscore_revert | 49.59% | 28.00 | 52.44 | 25.82% | 292.72 | 5 | 58/123 |
| bollinger_revert | 50.79% | 15.19 | 36.41 | 21.86% | 458.03 | 6 | 44/189 |
| rsi_reversal | 50.26% | 20.43 | 38.20 | 23.84% | 459.52 | 6 | 48/193 |
| regime_trend | 55.56% | 80.12 | 53.71 | 8.95% | 48.67 | 3 | 15/18 |
| regime_revert | 46.59% | 26.09 | 55.57 | 20.27% | 212.49 | 8 | 38/88 |
| regime_switch | 41.44% | 33.58 | 41.33 | 21.64% | 273.83 | 9 | 24/111 |
| vol_filtered_momentum | 22.68% | 46.24 | 25.74 | 34.43% | 475.21 | 17 | 60/194 |

### 4. B · research variant — in-sample vs out-of-sample

| STRATEGY | IS RETURN | IS PF | IS TRADES | OOS RETURN | OOS PF | OOS TRADES |
|---|---|---|---|---|---|---|
| ema_trend | -10.33% | 0.64 | 115 | -1.93% | 0.84 | 47 |
| breakout | -15.87% | 0.49 | 143 | -2.07% | 0.81 | 64 |
| momentum_roc | -16.50% | 0.53 | 178 | -7.65% | 0.55 | 91 |
| ema_slope | -13.89% | 0.54 | 115 | -2.01% | 0.82 | 46 |
| breakout_trend | -7.95% | 0.58 | 88 | +0.19% | 1.05 | 40 |
| vol_momentum | -8.12% | 0.49 | 47 | -0.19% | 0.97 | 22 LOW |
| zscore_revert | -14.07% | 0.43 | 81 | -1.59% | 0.83 | 42 |
| bollinger_revert | -12.39% | 0.47 | 126 | -7.86% | 0.35 | 63 |
| rsi_reversal | -15.02% | 0.46 | 126 | -2.15% | 0.80 | 67 |
| regime_trend | -0.17% | 0.94 | 10 LOW | +3.90% | 4.43 | 8 LOW |
| regime_revert | -12.73% | 0.36 | 60 | -3.08% | 0.57 | 28 LOW |
| regime_switch | -11.01% | 0.44 | 72 | -0.46% | 0.94 | 39 |
| vol_filtered_momentum | -14.85% | 0.56 | 167 | -8.76% | 0.45 | 83 |

### 5. B · research variant — walk-forward test windows

| STRATEGY | F0 Oct-Nov | F1 Jan-Feb | F2 Apr-May | F3 Jul-Aug | POSITIVE |
|---|---|---|---|---|---|
| ema_trend | -0.84% (17) | -3.17% (12) | -2.63% (16) | +1.85% (17) | 1/4 · med -1.73% |
| breakout | -3.75% (24) | -4.97% (20) | -0.63% (21) | +1.49% (26) | 1/4 · med -2.19% |
| momentum_roc | -1.71% (25) | -6.79% (31) | -3.07% (30) | +1.15% (30) | 1/4 · med -2.39% |
| ema_slope | -3.53% (17) | -2.36% (15) | -2.63% (14) | +1.47% (17) | 1/4 · med -2.49% |
| breakout_trend | -1.47% (10) | -1.97% (8) | -1.96% (13) | +2.41% (17) | 1/4 · med -1.72% |
| vol_momentum | -2.96% (10) | -4.40% (7) | -1.92% (7) | +3.31% (10) | 1/4 · med -2.44% |
| zscore_revert | -3.52% (18) | -6.37% (25) | -0.04% (12) | +0.61% (11) | 1/4 · med -1.78% |
| bollinger_revert | -0.69% (24) | -6.16% (31) | -1.93% (21) | -1.66% (22) | 0/4 · med -1.79% |
| rsi_reversal | -3.55% (28) | -7.39% (35) | -2.20% (21) | +0.45% (22) | 1/4 · med -2.88% |
| regime_trend | -1.30% (2) | +0.00% (0) | +0.37% (1) | +4.30% (5) | 2/4 · med +0.18% |
| regime_revert | -3.34% (12) | -4.77% (17) | -0.80% (9) | +0.11% (9) | 1/4 · med -2.07% |
| regime_switch | -3.61% (14) | -5.14% (19) | -1.64% (10) | +3.10% (15) | 1/4 · med -2.63% |
| vol_filtered_momentum | -0.79% (20) | -6.79% (31) | -2.92% (25) | -0.16% (26) | 0/4 · med -1.86% |

### 6. B · research variant — stress

| STRATEGY | BASE | FEES X2 | SLIPPAGE X3 | FEES X2 + SLIPPAGE X3 + SPREAD 5BPS |
|---|---|---|---|---|
| ema_trend | -11.69% | -19.40% | -18.84% | -19.59% |
| breakout | -16.98% | -20.06% | -20.53% | -21.06% |
| momentum_roc | -19.34% | -20.25% | -19.68% | -21.07% |
| ema_slope | -14.73% | -18.81% | -18.76% | -19.94% |
| breakout_trend | -7.97% | -14.19% | -14.54% | -20.02% |
| vol_momentum | -8.35% | -11.01% | -11.28% | -15.15% |
| zscore_revert | -15.44% | -20.27% | -19.94% | -19.81% |
| bollinger_revert | -19.28% | -19.99% | -19.98% | -20.59% |
| rsi_reversal | -16.85% | -20.32% | -20.02% | -20.24% |
| regime_trend | +3.72% | +2.87% | +2.95% | +0.33% |
| regime_revert | -15.42% | -18.52% | -18.96% | -20.20% |
| regime_switch | -11.42% | -15.04% | -19.12% | -19.28% |
| vol_filtered_momentum | -18.26% | -19.20% | -19.24% | -19.31% |

### 7. B · research variant — sensitivity

| STRATEGY | CANONICAL | NEIGHBOUR A | NEIGHBOUR B |
|---|---|---|---|
| momentum_roc | -19.34% / PF 0.51 | lookback=48: -19.45% / PF 0.55 | lookback=96: -18.68% / PF 0.48 |
| ema_slope | -14.73% / PF 0.63 | period=40: -17.64% / PF 0.53 | period=60: -18.34% / PF 0.51 |
| breakout_trend | -7.97% / PF 0.69 | trend_period=150: -11.26% / PF 0.61 | trend_period=250: -10.32% / PF 0.61 |
| vol_momentum | -8.35% / PF 0.62 | threshold=0.75: -9.17% / PF 0.67 | threshold=1.25: -1.92% / PF 0.86 |
| zscore_revert | -15.44% / PF 0.53 | window=36: -15.42% / PF 0.54 | window=60: -17.41% / PF 0.47 |
| bollinger_revert | -19.28% / PF 0.43 | window=16: -12.70% / PF 0.60 | window=24: -16.21% / PF 0.51 |
| rsi_reversal | -16.85% / PF 0.54 | period=10: -19.47% / PF 0.50 | period=18: -16.75% / PF 0.45 |
| regime_trend | +3.72% / PF 1.86 | er_min=0.25: -0.58% / PF 0.94 | er_min=0.35: +1.57% / PF 1.49 |
| regime_revert | -15.42% / PF 0.41 | er_max=0.15: -12.93% / PF 0.41 | er_max=0.25: -15.38% / PF 0.45 |
| regime_switch | -11.42% / PF 0.58 | er_trend=0.25,er_range=0.15: -10.57% / PF 0.57 | er_trend=0.35,er_range=0.25: -14.31% / PF 0.52 |
| vol_filtered_momentum | -18.26% / PF 0.53 | max_ratio=1.25: -18.59% / PF 0.52 | max_ratio=1.75: -18.57% / PF 0.52 |

### Costes: ¿no hay edge, o el edge se lo comen los costes?

B · research variant, año completo, sobre 10 000 USDT.

| STRATEGY | GROSS (antes de costes) | FEES + SLIPPAGE | NET |
|---|---|---|---|
| regime_trend | +5.18% | 1.46% | +3.72% |
| breakout_trend | +2.05% | 10.02% | -7.97% |
| ema_trend | +0.37% | 12.05% | -11.69% |
| breakout | -2.30% | 14.68% | -16.98% |
| ema_slope | -2.69% | 12.04% | -14.73% |
| rsi_reversal | -3.06% | 13.79% | -16.85% |
| vol_momentum | -3.14% | 5.21% | -8.35% |
| regime_switch | -3.20% | 8.21% | -11.42% |
| vol_filtered_momentum | -4.01% | 14.26% | -18.26% |
| momentum_roc | -4.61% | 14.73% | -19.34% |
| bollinger_revert | -5.53% | 13.74% | -19.28% |
| zscore_revert | -6.66% | 8.78% | -15.44% |
| regime_revert | -9.05% | 6.37% | -15.42% |

## 7. Lectura

**1. ¿Edge por sí misma? Ninguna.** 12 de 13 pierden en B después de costes, benchmarks
incluidos. Y no es solo cuestión de costes: **10 de 13 pierden incluso antes de costes**, y
las tres positivas en bruto lo son por poco (+5.2 %, +2.1 %, +0.4 %). Fees y slippage cuestan
5–15 % del capital al año; con profit factors de 0.41–0.70 no hay margen que proteger.

**2. ¿Sobrevive al Risk V2 real?** Solo `regime_trend` no latchea nunca (máx. 3 pérdidas
seguidas), y por eso su resultado es idéntico en A y en B: +3.72 %, 18 trades. No es edge
demostrado: LOW SAMPLE; in-sample plano (−0.17 %); casi todo el retorno viene de la ventana
jul–ago (5 trades); el vecino `er_min=0.25` pasa a negativo (PF 0.94); y 15 de sus 18 salidas
las hizo el overlay de riesgo, no la estrategia. Veredicto mecánico: **WEAK**.

**3. ¿Alguna solo parece buena porque se relajó el riesgo? No — ocurre lo contrario.**
Ninguna es positiva solo en B. Pero bajo A muchas *pierden menos* (EMA −1.3 % frente a
−11.7 %, `momentum_roc` −1.0 % frente a −19.3 %) únicamente porque el latch las detuvo en
septiembre. Eso es inactividad, no robustez, y no debe leerse como que Risk V2 "mejora" una
estrategia.

**4. El mercado, no las reglas.** La ventana jul–ago (F3) es positiva para 10 de 13
estrategias, incluidas las que pierden todo lo demás. Es un periodo tendencial del activo, no
evidencia de ninguna regla; y cae dentro del OOS, así que un OOS "positivo" aquí pesa poco.

## 8. Hallazgo separado — política de riesgo, no fallo de estrategia

**El latch permanente de 5 pérdidas consecutivas del Risk V2 desplegado bloquea casi todo.**
En las estrategias trend rechaza el 96–99.8 % de las decisiones y latchea entre 6 y 28 días
después del arranque. Con win rates de 23–36 %, típicos de trend-following, una racha de 5
pérdidas es prácticamente segura en semanas. En paper significa una sesión que se queda muda
tras la primera racha hasta un reset manual; en backtest significa que el Risk V2 real no se
puede evaluar sobre un año.

Esto es un problema de diseño de la política de riesgo y se reporta aparte. **En este sprint el
latch no esconde edge**: B, sin latch, también es negativo. Opciones de diseño para decidir en
un milestone propio (ninguna implementada, requiere autorización): cooldown temporal en lugar
de latch permanente, latch por drawdown en lugar de conteo de pérdidas, o reset simulado en
backtest para poder evaluar la política completa.

## 9. Validación y reproducibilidad

* **El escenario A es exactamente paper:** EMA20/50 y Breakout20/10 bajo A reproducen **bit a
  bit** los benchmarks M11-v2 almacenados (−1.3032966000388245 % y −2.286316262136912 %).
* **Harness y motor coinciden** en el retorno de A para 13 de 13 estrategias.
* **`quantplatform research verify`** sobre los 13 ledgers: **0 fallos de reproducibilidad**.
  En 11 el mismo experimento de año completo quedó registrado dos veces dentro de la corrida
  (baseline de stress y de sensibilidad) y el ledger lo marca *reproducible*. Los dos
  benchmarks no tienen sensibilidad, así que no tienen esa repetición interna.
* **Re-ejecución desde el commit limpio:** ver el commit siguiente a este documento.

## 10. Limitaciones

* Un activo, un año, H1. El OOS es limpio solo respecto a los parámetros, fijados antes de
  correr; el año ya lo había mirado M10c.
* El warm-up (hasta 260 barras) consume parte de cada ventana walk-forward de ~1 080 barras.
* El overlay de riesgo (stop, TP, trailing, break-even, time stop) está en **ambos**
  escenarios: no se midió la señal pura sin overlay. Cambia mucho las salidas (p. ej. 88/162 en
  EMA, 15/18 en `regime_trend`).
* RSI en su forma de media simple; EMA acotada con error de semilla ~e⁻⁸.
* "Overlay exits" es aproximado: ventas aprobadas menos señales de salida de la estrategia.

## 11. Veredicto y recomendación

**Ninguna estrategia pasa a PAPER.** 0 PAPER CANDIDATE, 0 PROMISING, 1 WEAK
(`regime_trend`), 10 REJECT, y los dos benchmarks también REJECT.

Qué descartar: `momentum_roc`, `vol_filtered_momentum`, `bollinger_revert`, `rsi_reversal`,
`zscore_revert`, `regime_revert` y `ema_slope` — negativas en bruto, sin ventanas WF sanas y
con vecindario igual de malo. `regime_switch`, `vol_momentum` y `breakout_trend` también: su
mejor cara es un bruto de ~0 que los costes vuelven claramente negativo.

Siguiente paso recomendado, en este orden:

1. **Decidir la política del latch** (§8) como milestone de riesgo separado. Mientras exista,
   ninguna estrategia trend puede evaluarse ni operar más de unas semanas.
2. **Bajar la fricción antes que buscar señales nuevas.** Con 100–200 trades/año y ~30 bps de
   ida y vuelta, los costes se comen el 5–15 % del capital. Horizontes más largos (4h/1D) o
   menos rotación atacan el problema real que muestra este sprint.
3. **Seguir `regime_trend` solo en observación**, sin paper: necesita al menos 30 trades antes
   de que ninguna cifra suya signifique algo. A su frecuencia actual (~18/año) eso es más de un
   año de datos: más histórico, no más paper.

