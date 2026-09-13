# M14 — Rediseño del latch de riesgo + estudio de timeframe con costes

**Estado:** COMPLETADO · research/backtesting únicamente · 2026-09-13

**Risk V2 de producción no cambia.** Ninguna línea de `src/quantplatform/risk/`, `execution/`,
`portfolio/`, `paper/` ni `backtesting/` se modificó. Las políticas alternativas se simulan con
una subclase de research de `StandardRiskEngine`; el registro de paper sigue siendo exactamente
`['breakout', 'ema_trend']`.

## 1. Preguntas

* **Parte A.** El latch permanente de 5 pérdidas bloquea casi toda la actividad (M13). ¿Qué
  política protege igual o mejor sin dejar de operar? Medido por seguridad, actividad y drawdown —
  **nunca por retorno**.
* **Parte B.** Fees + slippage se comen 5–15 % del capital al año (M13). ¿Mejora un timeframe
  más lento la relación **edge / coste**?

## 2. Parte A — cómo se simula una política sin tocar Risk

El motor de backtest guarda los breakers en su propio estado y se los pasa al motor de riesgo en
`RiskContext.breakers`; el motor de riesgo solo los **lee**. `LatchPolicyRiskEngine`
(`src/quantplatform/research/latch_policy.py`) es una subclase de research que sobreescribe un
único método público, `assess`, y cambia una sola cosa: qué breaker de pérdidas consecutivas ve
el chequeo. Quita el del motor y pone el suyo, que la política dispara y libera. Todos los demás
breakers (drawdown, pérdida diaria) pasan intactos, y todo lo demás se hereda sin cambios.

* **Fail-closed:** el wrapper solo puede liberar la pausa que él mismo creó. Nunca quita un
  latch de drawdown ni de pérdida diaria, nunca aprueba lo que el motor rechaza, y una salida
  forzada nunca la bloquea ningún breaker (igual que antes). Test dedicado.
* **La misma pérdida que cuenta el motor:** el resultado de un trade es el `realized_pnl` del
  ciclo de la posición al pasar de abierta a plana — el mismo número que registra el motor.
* **Validación central:** con la política permanente, el wrapper **reproduce bit a bit** el motor
  con el Risk V2 desplegado (rendimiento, trades y curva de equity idénticos).

| Política | Streak | Tras disparar | Drawdown | Qué es |
|---|---|---|---|---|
| **A** | 5 pérdidas | latch permanente | latch 20 % | **Risk V2 de producción**, exacto |
| **B** | 5 pérdidas | pausa 24 h y reactiva; el streak vuelve a 0 | latch 20 % | cooldown |
| **C** | — | — | latch 10 % | protección por drawdown, no por racha |
| **D** | 5 pérdidas | pausa 24 h | latch 10 % | híbrido |
| **REF** | — | — | — | research variant de M13 (**no es Risk V2**): referencia para separar "reduce pérdidas" de "deja de operar" |

La pausa se mide en tiempo, no en barras, para que signifique lo mismo en todo timeframe: 24
barras a 1h, 6 a 4h, 1 a 1d. C usa 10 % (más estricto que el 20 % de producción) porque pasa a ser
la única protección estructural. El breaker de pérdida diaria (3 %, resetea a medianoche UTC) es
igual en las cinco.

**Qué hizo cada política, frente a REF** (`classify_protection`, umbrales fijados antes de correr):

* **stops trading** — menos de la mitad de los trades de REF.
* **reduces losses** — al menos la mitad de los trades, expectancy por trade mejor en más de un
  10 % y drawdown al menos un 10 % menor.
* **hurts** — al menos la mitad de los trades y expectancy peor.
* **no effect** — lo demás.

**Regla de recomendación** (`recommend_policy`, fijada antes de correr): (1) *segura* — su peor
drawdown en todas las combinaciones ≤ 20 %; (2) *operable* — "stops trading" en como mucho la
mitad de las combinaciones donde REF hizo ≥ 30 trades; (3) entre las que quedan, la de menor
tiempo bloqueado mediano; empate → menor drawdown. El retorno no entra.

## 3. Parte B — timeframes

* **1h, 4h y 1d**, construidos **exactamente** a partir de las velas horarias de M10c
  (`src/quantplatform/research/resample.py`): open del primero, close del último, máximo, mínimo,
  suma de volumen. Solo se descartan cubos incompletos en los bordes del dataset; un hueco
  interior se rechaza.
* **30m no se estudia:** el dataset es horario, no se puede construir 30m a partir de él, y la
  pregunta es si operar *menos* ayuda — 30m solo podría empeorar los costes.
* **Mismos costes en todos:** comisión 10 bps, slippage 5 bps, spread 2 bps. Todas las filas de la
  Parte B usan el mismo escenario, **REF** (research variant), para que el timeframe sea lo único
  que cambia.
* **Walk-forward:** las 4 ventanas de M10c a 1h y 4h. A 1d **no es medible** con un año: cada
  ventana de test tiene 45 barras diarias, menos que el warm-up de cualquier estrategia. Se
  reporta como "n/a", no como cero.

**Estrategias** (parámetros canónicos de M13, sin reoptimizar): EMA20/50 y Breakout20/10 (como
copias de research `ema_trend_mtf` / `breakout_mtf`, misma regla, habilitadas en 4h/1d; las
originales de producción siguen solo en 1h), `regime_trend` (el único no rechazado en M13),
`breakout_trend` y `vol_momentum` (las dos trend con mejor resultado bruto) y `rsi_reversal`
(la mean reversion con mejor bruto). Los parámetros son en barras, igual en cada timeframe.

## 4. Veredicto de cada combinación

`judge` de M13, sin cambios: REJECT / WEAK / PROMISING / PAPER CANDIDATE. Para cada fila
(estrategia × timeframe × política): el scorecard anual es el de **su** política; OOS,
walk-forward, stress y vecinos son evidencia de edge y vienen de REF al mismo timeframe; el
retorno que debe ser positivo para PAPER CANDIDATE es el de **la política con la que correría**
(la suya para A–D; A, producción, para REF). Los benchmarks no tienen vecinos declarados: la
ausencia se registra como chequeo fallido, no aprobado.

**Top combinaciones** (`top_combinations`, fijada antes de correr): solo estrategias de research
bajo una política real (A–D), ordenadas por veredicto, luego por la parte positiva del
walk-forward, luego por menor drawdown. Nunca por retorno.

## 5. Resultados

Generado por `scripts/m14_report.py` desde la evidencia en `var/research/m14/` (ignorada por git,
como M10c y M13). Ningún número de esta sección se escribió a mano.

Policies: **REF** = research variant: no latching breaker (not Risk V2) · **A** = permanent streak latch (production Risk V2) · **B** = streak cooldown 24h · **C** = drawdown latch 10%, no streak · **D** = streak cooldown 24h + drawdown latch 10%

##### 1. Every combination

| STRATEGY | TF | RISK POLICY | TRADES | GROSS | COSTS | NET | PF | EXP | DD | BLOCKED % | VERDICT |
|---|---|---|---|---|---|---|---|---|---|---|---|
| ema_trend_mtf | 1h | REF | 162 | +0.37% | 12.05% | -11.69% | 0.70 | -7.18 | 16.46% | — | benchmark · REJECT |
| breakout_mtf | 1h | REF | 207 | -2.30% | 14.68% | -16.98% | 0.57 | -8.14 | 19.87% | — | benchmark · REJECT |
| regime_trend | 1h | REF | 18 LOW | +5.18% | 1.46% | +3.72% | 1.86 | 20.64 | 3.79% | — | WEAK |
| breakout_trend | 1h | REF | 133 | +2.05% | 10.02% | -7.97% | 0.69 | -5.89 | 11.10% | — | REJECT |
| vol_momentum | 1h | REF | 69 | -3.14% | 5.21% | -8.35% | 0.62 | -12.11 | 13.92% | — | REJECT |
| rsi_reversal | 1h | REF | 193 | -3.06% | 13.79% | -16.85% | 0.54 | -8.73 | 18.87% | — | REJECT |
| ema_trend_mtf | 1h | A | 11 LOW | -0.43% | 0.88% | -1.30% | 0.37 | -11.85 | 1.57% | 99.45% | benchmark · REJECT |
| breakout_mtf | 1h | A | 14 LOW | -1.18% | 1.11% | -2.29% | 0.03 | -16.33 | 2.30% | 96.46% | benchmark · REJECT |
| regime_trend | 1h | A | 18 LOW | +5.18% | 1.46% | +3.72% | 1.86 | 20.64 | 3.79% | 0.00% | WEAK |
| breakout_trend | 1h | A | 8 LOW | -0.80% | 0.64% | -1.44% | 0.05 | -17.94 | 1.44% | 97.09% | REJECT |
| vol_momentum | 1h | A | 14 LOW | +0.32% | 1.13% | -0.80% | 0.82 | -5.73 | 3.23% | 97.18% | REJECT |
| rsi_reversal | 1h | A | 115 | -6.01% | 8.57% | -14.59% | 0.45 | -12.68 | 14.62% | 65.53% | REJECT |
| ema_trend_mtf | 1h | B | 154 | -1.95% | 11.36% | -13.31% | 0.64 | -8.61 | 17.02% | 37.92% | benchmark · REJECT |
| breakout_mtf | 1h | B | 196 | -2.78% | 13.96% | -16.74% | 0.55 | -8.48 | 19.54% | 10.68% | benchmark · REJECT |
| regime_trend | 1h | B | 18 LOW | +5.18% | 1.46% | +3.72% | 1.86 | 20.64 | 3.79% | 0.00% | WEAK |
| breakout_trend | 1h | B | 129 | +2.08% | 9.74% | -7.67% | 0.70 | -5.84 | 10.80% | 3.61% | REJECT |
| vol_momentum | 1h | B | 68 | -2.40% | 5.16% | -7.56% | 0.65 | -11.12 | 13.18% | 11.61% | REJECT |
| rsi_reversal | 1h | B | 192 | -2.97% | 13.73% | -16.69% | 0.54 | -8.69 | 18.72% | 4.46% | REJECT |
| ema_trend_mtf | 1h | C | 70 | -2.68% | 5.44% | -8.12% | 0.53 | -11.59 | 10.23% | 94.36% | benchmark · REJECT |
| breakout_mtf | 1h | C | 72 | -4.63% | 5.52% | -10.15% | 0.35 | -14.09 | 10.19% | 78.22% | benchmark · REJECT |
| regime_trend | 1h | C | 18 LOW | +5.18% | 1.46% | +3.72% | 1.86 | 20.64 | 3.79% | 0.00% | WEAK |
| breakout_trend | 1h | C | 106 | -1.10% | 8.02% | -9.12% | 0.58 | -8.60 | 10.24% | 35.01% | REJECT |
| vol_momentum | 1h | C | 35 | -4.46% | 2.74% | -7.19% | 0.47 | -20.56 | 10.27% | 90.04% | REJECT |
| rsi_reversal | 1h | C | 80 | -4.02% | 6.12% | -10.14% | 0.43 | -12.68 | 10.18% | 79.60% | REJECT |
| ema_trend_mtf | 1h | D | 68 | -3.75% | 5.25% | -9.00% | 0.46 | -13.23 | 10.22% | 94.81% | benchmark · REJECT |
| breakout_mtf | 1h | D | 72 | -4.40% | 5.50% | -9.91% | 0.34 | -13.76 | 10.01% | 78.97% | benchmark · REJECT |
| regime_trend | 1h | D | 18 LOW | +5.18% | 1.46% | +3.72% | 1.86 | 20.64 | 3.79% | 0.00% | WEAK |
| breakout_trend | 1h | D | 112 | -1.38% | 8.46% | -9.84% | 0.57 | -8.79 | 10.11% | 26.20% | REJECT |
| vol_momentum | 1h | D | 38 | -5.18% | 2.96% | -8.14% | 0.44 | -21.43 | 10.33% | 88.79% | REJECT |
| rsi_reversal | 1h | D | 80 | -4.02% | 6.12% | -10.14% | 0.43 | -12.68 | 10.18% | 79.60% | REJECT |
| ema_trend_mtf | 4h | REF | 91 | +0.82% | 6.94% | -6.12% | 0.75 | -6.98 | 12.78% | — | benchmark · REJECT |
| breakout_mtf | 4h | REF | 65 | -2.86% | 4.91% | -7.78% | 0.62 | -11.97 | 11.85% | — | benchmark · REJECT |
| regime_trend | 4h | REF | 11 LOW | +1.30% | 0.92% | +0.38% | 1.05 | 1.22 | 1.87% | — | WEAK |
| breakout_trend | 4h | REF | 38 | +0.55% | 2.97% | -2.42% | 0.77 | -6.37 | 5.77% | — | REJECT |
| vol_momentum | 4h | REF | 36 | -2.90% | 2.80% | -5.70% | 0.48 | -16.47 | 8.33% | — | REJECT |
| rsi_reversal | 4h | REF | 76 | -2.82% | 5.71% | -8.52% | 0.65 | -11.22 | 10.00% | — | REJECT |
| ema_trend_mtf | 4h | A | 15 LOW | -0.68% | 1.21% | -1.89% | 0.63 | -12.62 | 4.34% | 96.31% | benchmark · REJECT |
| breakout_mtf | 4h | A | 19 LOW | -1.39% | 1.51% | -2.89% | 0.55 | -15.23 | 3.85% | 78.42% | benchmark · REJECT |
| regime_trend | 4h | A | 11 LOW | +1.30% | 0.92% | +0.38% | 1.05 | 1.22 | 1.87% | 0.00% | WEAK |
| breakout_trend | 4h | A | 38 | +0.55% | 2.97% | -2.42% | 0.77 | -6.37 | 5.77% | 0.00% | REJECT |
| vol_momentum | 4h | A | 26 LOW | -5.04% | 2.01% | -7.05% | 0.27 | -27.11 | 7.36% | 62.76% | REJECT |
| rsi_reversal | 4h | A | 76 | -2.82% | 5.71% | -8.52% | 0.65 | -11.22 | 10.00% | 0.00% | REJECT |
| ema_trend_mtf | 4h | B | 90 | +1.36% | 6.90% | -5.55% | 0.78 | -6.42 | 12.13% | 6.63% | benchmark · REJECT |
| breakout_mtf | 4h | B | 63 | -1.55% | 4.81% | -6.36% | 0.66 | -10.10 | 10.50% | 2.88% | benchmark · REJECT |
| regime_trend | 4h | B | 11 LOW | +1.30% | 0.92% | +0.38% | 1.05 | 1.22 | 1.87% | 0.00% | WEAK |
| breakout_trend | 4h | B | 38 | +0.55% | 2.97% | -2.42% | 0.77 | -6.37 | 5.77% | 0.00% | REJECT |
| vol_momentum | 4h | B | 36 | -2.81% | 2.80% | -5.61% | 0.49 | -16.23 | 8.25% | 7.41% | REJECT |
| rsi_reversal | 4h | B | 76 | -2.82% | 5.71% | -8.52% | 0.65 | -11.22 | 10.00% | 0.00% | REJECT |
| ema_trend_mtf | 4h | C | 68 | -2.70% | 5.21% | -7.92% | 0.62 | -11.64 | 10.22% | 66.75% | benchmark · REJECT |
| breakout_mtf | 4h | C | 48 | -6.16% | 3.67% | -9.83% | 0.43 | -20.48 | 10.72% | 36.26% | benchmark · REJECT |
| regime_trend | 4h | C | 11 LOW | +1.30% | 0.92% | +0.38% | 1.05 | 1.22 | 1.87% | 0.00% | WEAK |
| breakout_trend | 4h | C | 38 | +0.55% | 2.97% | -2.42% | 0.77 | -6.37 | 5.77% | 0.00% | REJECT |
| vol_momentum | 4h | C | 36 | -2.90% | 2.80% | -5.70% | 0.48 | -16.47 | 8.33% | 0.00% | REJECT |
| rsi_reversal | 4h | C | 76 | -2.82% | 5.71% | -8.52% | 0.65 | -11.22 | 10.00% | 0.00% | REJECT |
| ema_trend_mtf | 4h | D | 69 | -2.74% | 5.31% | -8.05% | 0.63 | -11.66 | 10.34% | 65.52% | benchmark · REJECT |
| breakout_mtf | 4h | D | 57 | -5.15% | 4.36% | -9.51% | 0.48 | -16.68 | 10.40% | 20.51% | benchmark · REJECT |
| regime_trend | 4h | D | 11 LOW | +1.30% | 0.92% | +0.38% | 1.05 | 1.22 | 1.87% | 0.00% | WEAK |
| breakout_trend | 4h | D | 38 | +0.55% | 2.97% | -2.42% | 0.77 | -6.37 | 5.77% | 0.00% | REJECT |
| vol_momentum | 4h | D | 36 | -2.81% | 2.80% | -5.61% | 0.49 | -16.23 | 8.25% | 7.41% | REJECT |
| rsi_reversal | 4h | D | 76 | -2.82% | 5.71% | -8.52% | 0.65 | -11.22 | 10.00% | 0.00% | REJECT |
| ema_trend_mtf | 1d | REF | 14 LOW | +1.53% | 1.16% | +0.37% | 1.06 | 2.04 | 2.67% | — | benchmark · WEAK |
| breakout_mtf | 1d | REF | 15 LOW | +0.91% | 1.23% | -0.32% | 0.91 | -3.54 | 6.15% | — | benchmark · REJECT |
| regime_trend | 1d | REF | 0 LOW | +0.00% | 0.00% | +0.00% | — | — | 0.00% | — | REJECT |
| breakout_trend | 1d | REF | 2 LOW | +3.74% | 0.21% | +3.53% | 46.98 | 165.45 | 0.24% | — | WEAK |
| vol_momentum | 1d | REF | 6 LOW | +0.02% | 0.52% | -0.50% | 0.80 | -9.57 | 2.54% | — | REJECT |
| rsi_reversal | 1d | REF | 25 LOW | +5.27% | 2.07% | +3.21% | 1.29 | 12.82 | 3.80% | — | WEAK |
| ema_trend_mtf | 1d | A | 14 LOW | +1.53% | 1.16% | +0.37% | 1.06 | 2.04 | 2.67% | 0.00% | benchmark · WEAK |
| breakout_mtf | 1d | A | 13 LOW | -2.69% | 1.03% | -3.72% | 0.39 | -28.59 | 6.15% | 18.75% | benchmark · REJECT |
| regime_trend | 1d | A | 0 LOW | +0.00% | 0.00% | +0.00% | — | — | 0.00% | — | REJECT |
| breakout_trend | 1d | A | 2 LOW | +3.74% | 0.21% | +3.53% | 46.98 | 165.45 | 0.24% | 0.00% | WEAK |
| vol_momentum | 1d | A | 6 LOW | +0.02% | 0.52% | -0.50% | 0.80 | -9.57 | 2.54% | 0.00% | REJECT |
| rsi_reversal | 1d | A | 25 LOW | +5.27% | 2.07% | +3.21% | 1.29 | 12.82 | 3.80% | 0.00% | WEAK |
| ema_trend_mtf | 1d | B | 14 LOW | +1.53% | 1.16% | +0.37% | 1.06 | 2.04 | 2.67% | 0.00% | benchmark · WEAK |
| breakout_mtf | 1d | B | 15 LOW | -0.56% | 1.23% | -1.79% | 0.68 | -13.30 | 6.15% | 3.03% | benchmark · REJECT |
| regime_trend | 1d | B | 0 LOW | +0.00% | 0.00% | +0.00% | — | — | 0.00% | — | REJECT |
| breakout_trend | 1d | B | 2 LOW | +3.74% | 0.21% | +3.53% | 46.98 | 165.45 | 0.24% | 0.00% | WEAK |
| vol_momentum | 1d | B | 6 LOW | +0.02% | 0.52% | -0.50% | 0.80 | -9.57 | 2.54% | 0.00% | REJECT |
| rsi_reversal | 1d | B | 25 LOW | +5.27% | 2.07% | +3.21% | 1.29 | 12.82 | 3.80% | 0.00% | WEAK |
| ema_trend_mtf | 1d | C | 14 LOW | +1.53% | 1.16% | +0.37% | 1.06 | 2.04 | 2.67% | 0.00% | benchmark · WEAK |
| breakout_mtf | 1d | C | 15 LOW | +0.91% | 1.23% | -0.32% | 0.91 | -3.54 | 6.15% | 0.00% | benchmark · REJECT |
| regime_trend | 1d | C | 0 LOW | +0.00% | 0.00% | +0.00% | — | — | 0.00% | — | REJECT |
| breakout_trend | 1d | C | 2 LOW | +3.74% | 0.21% | +3.53% | 46.98 | 165.45 | 0.24% | 0.00% | WEAK |
| vol_momentum | 1d | C | 6 LOW | +0.02% | 0.52% | -0.50% | 0.80 | -9.57 | 2.54% | 0.00% | REJECT |
| rsi_reversal | 1d | C | 25 LOW | +5.27% | 2.07% | +3.21% | 1.29 | 12.82 | 3.80% | 0.00% | WEAK |
| ema_trend_mtf | 1d | D | 14 LOW | +1.53% | 1.16% | +0.37% | 1.06 | 2.04 | 2.67% | 0.00% | benchmark · WEAK |
| breakout_mtf | 1d | D | 15 LOW | -0.56% | 1.23% | -1.79% | 0.68 | -13.30 | 6.15% | 3.03% | benchmark · REJECT |
| regime_trend | 1d | D | 0 LOW | +0.00% | 0.00% | +0.00% | — | — | 0.00% | — | REJECT |
| breakout_trend | 1d | D | 2 LOW | +3.74% | 0.21% | +3.53% | 46.98 | 165.45 | 0.24% | 0.00% | WEAK |
| vol_momentum | 1d | D | 6 LOW | +0.02% | 0.52% | -0.50% | 0.80 | -9.57 | 2.54% | 0.00% | REJECT |
| rsi_reversal | 1d | D | 25 LOW | +5.27% | 2.07% | +3.21% | 1.29 | 12.82 | 3.80% | 0.00% | WEAK |

#### 2. Part A — latch policies (all timeframes)

| POLICY | WHAT IT DOES | MEDIAN BLOCKED DECISIONS | MEDIAN TIME BLOCKED | WORST DD | MAX LOSS STREAK | EFFECT vs REFERENCE (combinations) |
|---|---|---|---|---|---|---|
| A | permanent streak latch (production Risk V2) | 9.38% | 1.64% | 14.62% | 5 | stops trading 6 · no effect 9 · hurts 3 |
| B | streak cooldown 24h | 1.44% | 0.14% | 19.54% | 12 | reduces losses 1 · no effect 15 · hurts 2 |
| C | drawdown latch 10%, no streak | 0.00% | 0.00% | 10.72% | 12 | stops trading 3 · no effect 11 · hurts 4 |
| D | streak cooldown 24h + drawdown latch 10% | 1.52% | 0.14% | 10.40% | 12 | stops trading 3 · no effect 10 · hurts 5 |

**Recommendation rule** (fixed before the run): safe → operable → least blocked.

* A: excluded — stops trading on 6/10 active combinations
* B: eligible — worst DD 19.54%, median time blocked 0.14%, stops trading on 0/10
* C: eligible — worst DD 10.72%, median time blocked 0.00%, stops trading on 3/10
* D: eligible — worst DD 10.40%, median time blocked 0.14%, stops trading on 3/10

**Recommended latch policy: C**

#### 3. Part B — timeframe vs cost (research reference, not Risk V2)

| STRATEGY | TF | TRADES/YR | GROSS | FEES | SLIPPAGE | NET | COST / GROSS | GROSS / COST | PF | EXP | DD | WALK-FORWARD |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ema_trend_mtf | 1h | 162 | +0.37% | 8.03% | 4.02% | -11.69% | 3291% | +0.03 | 0.70 | -7.18 | 16.46% | 1/4 |
| ema_trend_mtf | 4h | 91 | +0.82% | 4.63% | 2.31% | -6.12% | 845% | +0.12 | 0.75 | -6.98 | 12.78% | 1/4 |
| ema_trend_mtf | 1d | 14 | +1.53% | 0.77% | 0.39% | +0.37% | 76% | +1.31 | 1.06 | 2.04 | 2.67% | n/a (1d) |
| breakout_mtf | 1h | 207 | -2.30% | 9.79% | 4.89% | -16.98% | gross ≤ 0 | -0.16 | 0.57 | -8.14 | 19.87% | 1/4 |
| breakout_mtf | 4h | 65 | -2.86% | 3.28% | 1.64% | -7.78% | gross ≤ 0 | -0.58 | 0.62 | -11.97 | 11.85% | 1/4 |
| breakout_mtf | 1d | 15 | +0.91% | 0.82% | 0.41% | -0.32% | 135% | +0.74 | 0.91 | -3.54 | 6.15% | n/a (1d) |
| regime_trend | 1h | 18 | +5.18% | 0.97% | 0.49% | +3.72% | 28% | +3.54 | 1.86 | 20.64 | 3.79% | 2/4 |
| regime_trend | 4h | 11 | +1.30% | 0.62% | 0.31% | +0.38% | 71% | +1.41 | 1.05 | 1.22 | 1.87% | 1/4 |
| regime_trend | 1d | 0 | +0.00% | 0.00% | 0.00% | +0.00% | — | — | — | — | 0.00% | n/a (1d) |
| breakout_trend | 1h | 133 | +2.05% | 6.68% | 3.34% | -7.97% | 490% | +0.20 | 0.69 | -5.89 | 11.10% | 1/4 |
| breakout_trend | 4h | 38 | +0.55% | 1.98% | 0.99% | -2.42% | 539% | +0.19 | 0.77 | -6.37 | 5.77% | 1/4 |
| breakout_trend | 1d | 2 | +3.74% | 0.14% | 0.07% | +3.53% | 6% | +17.85 | 46.98 | 165.45 | 0.24% | n/a (1d) |
| vol_momentum | 1h | 69 | -3.14% | 3.48% | 1.74% | -8.35% | gross ≤ 0 | -0.60 | 0.62 | -12.11 | 13.92% | 1/4 |
| vol_momentum | 4h | 36 | -2.90% | 1.87% | 0.93% | -5.70% | gross ≤ 0 | -1.04 | 0.48 | -16.47 | 8.33% | 1/4 |
| vol_momentum | 1d | 6 | +0.02% | 0.34% | 0.17% | -0.50% | 2509% | +0.04 | 0.80 | -9.57 | 2.54% | n/a (1d) |
| rsi_reversal | 1h | 193 | -3.06% | 9.19% | 4.60% | -16.85% | gross ≤ 0 | -0.22 | 0.54 | -8.73 | 18.87% | 1/4 |
| rsi_reversal | 4h | 76 | -2.82% | 3.81% | 1.90% | -8.52% | gross ≤ 0 | -0.49 | 0.65 | -11.22 | 10.00% | 1/4 |
| rsi_reversal | 1d | 25 | +5.27% | 1.38% | 0.69% | +3.21% | 39% | +2.55 | 1.29 | 12.82 | 3.80% | n/a (1d) |

#### 4. Walk-forward test windows (research reference)

| STRATEGY | TF | F0 Oct-Nov | F1 Jan-Feb | F2 Apr-May | F3 Jul-Aug | POSITIVE |
|---|---|---|---|---|---|---|
| ema_trend_mtf | 1h | -0.84% (17) | -3.17% (12) | -2.63% (16) | +1.85% (17) | 1/4 |
| ema_trend_mtf | 4h | -1.34% (5) | +0.00% (0) | -2.24% (7) | +2.94% (13) | 1/4 |
| breakout_mtf | 1h | -3.75% (24) | -4.97% (20) | -0.63% (21) | +1.49% (26) | 1/4 |
| breakout_mtf | 4h | -0.53% (5) | -4.22% (5) | -1.73% (7) | +2.30% (9) | 1/4 |
| regime_trend | 1h | -1.30% (2) | +0.00% (0) | +0.37% (1) | +4.30% (5) | 2/4 |
| regime_trend | 4h | +0.00% (0) | +0.00% (0) | +0.00% (0) | +1.97% (7) | 1/4 |
| breakout_trend | 1h | -1.47% (10) | -1.97% (8) | -1.96% (13) | +2.41% (17) | 1/4 |
| breakout_trend | 4h | +0.00% (0) | +0.00% (0) | +0.00% (0) | +2.69% (5) | 1/4 |
| vol_momentum | 1h | -2.96% (10) | -4.40% (7) | -1.92% (7) | +3.31% (10) | 1/4 |
| vol_momentum | 4h | -0.77% (1) | +0.00% (0) | -1.04% (2) | +2.28% (8) | 1/4 |
| rsi_reversal | 1h | -3.55% (28) | -7.39% (35) | -2.20% (21) | +0.45% (22) | 1/4 |
| rsi_reversal | 4h | -1.39% (13) | -4.51% (20) | +0.93% (7) | -0.16% (4) | 1/4 |

#### 5. Stress (research reference, full year)

| STRATEGY | TF | BASE | FEES X2 | SLIPPAGE X3 | FEES X2 + SLIPPAGE X3 + SPREAD 5BPS |
|---|---|---|---|---|---|
| ema_trend_mtf | 1h | -11.69% | -19.40% | -18.84% | -19.59% |
| ema_trend_mtf | 4h | -6.12% | -9.01% | -9.25% | -13.48% |
| ema_trend_mtf | 1d | +0.37% | -0.37% | -0.38% | -1.08% |
| breakout_mtf | 1h | -16.98% | -20.06% | -20.53% | -21.06% |
| breakout_mtf | 4h | -7.78% | -9.53% | -9.77% | -10.03% |
| breakout_mtf | 1d | -0.32% | -0.83% | -0.85% | -2.69% |
| regime_trend | 1h | +3.72% | +2.87% | +2.95% | +0.33% |
| regime_trend | 4h | +0.38% | -0.22% | -0.23% | -1.92% |
| regime_trend | 1d | +0.00% | +0.00% | +0.00% | +0.00% |
| breakout_trend | 1h | -7.97% | -14.19% | -14.54% | -20.02% |
| breakout_trend | 4h | -2.42% | -4.12% | -4.23% | -5.28% |
| breakout_trend | 1d | +3.53% | +3.22% | +3.30% | +3.01% |
| vol_momentum | 1h | -8.35% | -11.01% | -11.28% | -15.15% |
| vol_momentum | 4h | -5.70% | -6.46% | -6.63% | -8.16% |
| vol_momentum | 1d | -0.50% | -0.80% | -0.82% | -0.71% |
| rsi_reversal | 1h | -16.85% | -20.32% | -20.02% | -20.24% |
| rsi_reversal | 4h | -8.52% | -11.34% | -11.62% | -14.26% |
| rsi_reversal | 1d | +3.21% | +1.75% | +1.79% | +0.63% |

#### 6. Top combinations to follow (rule: verdict → walk-forward → drawdown)

* **regime_trend · 1h · policy A** — WEAK, net +3.72%, 18 trades, WF 2/4, DD 3.79%
* **regime_trend · 1h · policy B** — WEAK, net +3.72%, 18 trades, WF 2/4, DD 3.79%

## 6. Lectura — Parte A (latch)

**Ninguna política mejora el edge; lo que cambia es *cuándo* dejan de operar.** Solo 1 de 18
combinaciones sale como "reduces losses". Con estrategias sin edge, un breaker solo puede limitar
la pérdida dejando de operar, así que la pregunta útil no es cuánto retorno salva sino en qué
momento corta.

* **A — producción.** En 1h bloquea el 96–99.5 % de las decisiones de las estrategias trend. Sus
  drawdowns bajos (1.4–2.3 %) son inactividad: latchea tras 5 pérdidas de ruido, con la cuenta a
  ~−1 %. "Stops trading" en 6 de 10 combinaciones activas → excluida por la regla de operabilidad.
* **B — cooldown 24 h.** Casi no bloquea (1.4 % de decisiones, 0.14 % del tiempo) pero tampoco
  protege: peor drawdown 19.5 %, rachas de hasta 12 pérdidas y 15 de 18 combinaciones sin efecto
  frente a REF. Operable, no protectora.
* **C — latch de drawdown 10 %, sin racha.** No bloquea nada mientras la cuenta no pierda un 10 %;
  cuando lo pierde, latchea para siempre. Peor drawdown 10.7 %. Sus 3 "stops trading" son EMA,
  Breakout y RSI en 1h, que sin protección perdían 11–17 %: frena exactamente donde debía.
* **D — híbrido.** Prácticamente C (manda su latch de drawdown): peor DD 10.4 %, 0.14 % de tiempo
  bloqueado, 5 "hurts" frente a 4. Empate práctico; C es más simple, con un breaker menos.

**Recomendación: C** (regla fijada antes de correr: segura → operable → menos bloqueada). **D es
una alternativa equivalente.** Llevarla a producción es un milestone de riesgo aparte, con su propia
revisión y autorización; aquí no se implementó.

## 7. Lectura — Parte B (timeframe)

**¿Un timeframe más lento mejora la relación edge/coste? Sí, de forma consistente — pero un año
de datos no basta para validar ningún edge a 1d.**

* Los costes caen casi en proporción a los trades: EMA 12.1 % → 6.9 % → 1.2 % del capital;
  Breakout 14.7 → 4.9 → 1.2 %; RSI 13.8 → 5.7 → 2.1 %.
* La relación bruto/coste mejora al ralentizar en 5 de 6 estrategias: EMA 0.03 → 0.12 → 1.31;
  Breakout −0.16 → −0.58 → +0.74; RSI −0.22 → −0.49 → +2.55. `regime_trend` es la excepción.
* **A 4h los costes bajan a la mitad, pero el neto sigue negativo en todo** salvo `regime_trend`
  (+0.38 %). No alcanza.
* **A 1d aparecen tres netos positivos**: EMA +0.37 % (14 trades), `rsi_reversal` +3.21 % (25 trades,
  PF 1.29, sobrevive el stress combinado) y `breakout_trend` +3.53 % (**2 trades**: no significa
  nada). Todo a 1d es LOW SAMPLE y sin walk-forward medible → nada puede pasar de WEAK.
* La ventana jul–ago (F3) sigue siendo la única positiva para casi todo, en 1h y en 4h.

**Mejor timeframe por coste/edge: 1d**, con la advertencia de muestra. 4h mejora el coste pero no
el signo.

## 8. ¿Mejora `regime_trend`? No.

1h +3.72 % (18 trades) → 4h +0.38 % (11) → 1d, 0 trades en todo el año. Tampoco depende de la
política: nunca dispara ningún breaker, y por eso su resultado es idéntico en A, B, C y D. Sigue en
WEAK: LOW SAMPLE, con casi todo el retorno en la ventana jul–ago.

## 9. Qué descartar

* **Política A** para operar estrategias trend (bloquea el 96–99 % en 1h) y **política B** como
  protección (peor drawdown 19.5 %).
* **1h y 4h** para todas las estrategias estudiadas salvo `regime_trend`: neto negativo y 1 de 4
  ventanas walk-forward positiva.
* **`vol_momentum`** en todo timeframe (bruto ≈ 0 o negativo en los tres); **`breakout_mtf`** (bruto
  negativo en 1h/4h, neto negativo en 1d); **`breakout_trend`** en 1h y 4h; **`regime_trend`** en 4h y 1d.

## 10. Top 2 combinaciones

**Por la regla fijada** (veredicto → walk-forward → drawdown): `regime_trend · 1h · A` y
`regime_trend · 1h · B`. Son el mismo resultado, porque la estrategia nunca dispara un breaker,
así que la regla devuelve en realidad **una** combinación. Es una limitación de la regla —no
deduplica resultados idénticos— y no se cambió después de ver la salida.

**Juicio (no regla), para seguir en research, no en paper:**

1. **`regime_trend · 1h · C`** — mismo resultado (+3.72 %, WEAK). Necesita ≥ 30 trades; a su ritmo
   (~18/año) eso es más de un año de datos.
2. **`rsi_reversal · 1d · C`** — la única familia que pasa de bruto negativo a positivo al ralentizar
   el timeframe: 25 trades, PF 1.29, positiva también con fees ×2 + slippage ×3 + spread. LOW
   SAMPLE y sin walk-forward medible.

**Ninguna es PAPER CANDIDATE. 0 PROMISING.**

## 11. Validación y reproducibilidad

* **El wrapper en modo permanente reproduce bit a bit** el motor con el Risk V2 desplegado (test).
* **REF a través del wrapper = motor sin wrapper** en 18 de 18 jobs; el ledger las marca *reproducible*.
* **1h REF reproduce M13 dígito a dígito** para las 6 estrategias, copias multi-timeframe incluidas.
* **`research verify`**: 18 ledgers, 0 fallos de reproducibilidad.
* **Re-ejecución desde el commit limpio:** ver el commit siguiente a este documento.

## 12. Limitaciones

* **Un año de datos.** A 1d son 365 barras: ninguna conclusión diaria es validable, ni hay
  walk-forward posible.
* **El overlay de riesgo no se reescaló por timeframe.** Stop, take-profit y trailing están en bps
  calibrados para 1h, y el time stop en barras (168 barras son 7 días a 1h y 168 días a 1d). A 1d
  el overlay se comporta distinto; parte de lo que se ve a 1d puede ser eso.
* La Parte B se midió solo con REF (research variant), no con Risk V2.
* La regla de top-2 no deduplica combinaciones idénticas (ver §10).

## 13. Recomendación y siguiente paso

1. **Latch:** proponer **C** (o D) como diseño para un milestone de riesgo propio. Producción no
   cambia hasta que lo autorices.
2. **Ampliar el historial** — p. ej. BTC/USDT 1h desde 2020 en Binance Vision — para poder medir 4h
   y 1d con muestra suficiente y walk-forward. **Requiere tu permiso para descargar.**
3. **Reescalar el overlay por timeframe** (stop por volatilidad, time stop en tiempo) antes de
   sacar conclusiones a 1d.
4. **Nada pasa a paper.**

## 14. Corrección de M13 incluida

Los tests de protocolo de M13 leían `var/research/m10c/def_breakout_v2.json`, y `var/` está
ignorado por git: pasaban en esta máquina y habrían fallado en un clon limpio o en CI. Ahora leen
una copia comprometida en `tests/fixtures/deployed_risk_v2_definition.json`.

