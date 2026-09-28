# M29 fase 1 — criba de 1D: ninguna familia pasa la puerta

**Estado:** CRIBA 1D COMPLETA · 56 celdas · 31 min · 1 worker · **ninguna configuración avanza** · 2026-09-28

Umbrales pre-declarados y commiteados en `34848fd`, **antes** de ver un solo resultado. Nada se
tocó después. Las dos sesiones paper del VPS quedaron intactas: este trabajo corrió entero en
el Mac.

---

## 1. Los datos

1D **no se descargó**. El pipeline de M16 descarga 1h, lo valida contra dos archivos con
checksum más la API del exchange, y *deriva* 4h por resampleo — nunca bajó una serie de 4h. 1D
sale por esa misma función (`scripts/m29_dataset.py`), de los mismos CSV de 1h ya validados,
así que hereda la validación de M16 en vez de necesitar la suya.

| activo | barras 1D | lineage sha256 | outages recomputados vs M16 | determinista |
|---|---|---|---|---|
| BTCUSDT | 3301 | ✅ | 127 = 127 | ✅ |
| ETHUSDT | 3301 | ✅ | 127 = 127 | ✅ |
| BNBUSDT | 3210 | ✅ | 121 = 121 | ✅ |
| SOLUSDT | 2205 | ✅ | 19 = 19 | ✅ |

**La comprobación que importa:** M15 construyó su propia serie diaria de BTC por otra ruta. El
solape es de **2449 barras y las 2449 tienen OHLC idéntico, cero discrepancias**. Es el único
control capaz de detectar un error en el resampler mismo, y pasa.

---

## 2. Resultado por familia — medianas de los cuatro activos

Cinco pases por celda: base, coste ×2, coste ×3, OOS (2024-01-01, la ventana de M23 sin
retocar) y Risk V2 desplegado.

| familia | horizonte | CAGR | DD | **Calmar** | PF | trades | exposición | OOS | CAGR ×2 coste | ×3 |
|---|---|---|---|---|---|---|---|---|---|---|
| trend | base | 2.39% | 4.70% | **0.47** | 1.49 | 202 | 38.6% | +0.92% | 1.88% | 1.64% |
| trend | doubled | 1.70% | 6.23% | 0.28 | 1.42 | 198 | 37.0% | +0.41% | 1.13% | 0.64% |
| **breakout** | **base** | 1.97% | **2.90%** | **0.58** | **2.09** | 90 | 16.1% | −0.46% | 1.69% | 1.49% |
| breakout | doubled | 1.21% | 3.26% | 0.40 | 1.96 | 68 | 12.2% | −0.32% | 1.07% | 0.93% |
| momentum | base | 2.11% | 8.90% | 0.28 | 1.35 | 278 | 44.9% | +0.74% | 1.41% | 0.92% |
| momentum | doubled | 1.50% | 8.12% | 0.19 | 1.21 | 259 | 44.5% | −1.02% | 0.80% | 0.09% |
| regime_trend | base | 0.85% | 3.28% | 0.24 | 1.88 | 47 | 7.6% | −1.18% | 0.75% | 0.61% |
| regime_trend | doubled | 0.48% | 2.38% | 0.25 | 1.88 | 22 | 2.9% | 0.00% | 0.46% | 0.39% |
| **mean_reversion** | base | **−0.44%** | 6.22% | −0.07 | 0.76 | 46 | 8.0% | −0.44% | −0.55% | −0.65% |
| **mean_reversion** | doubled | **−0.42%** | 4.92% | −0.09 | 0.70 | 42 | 6.7% | −1.62% | −0.55% | −0.63% |
| vol_filtered | base | 1.63% | 8.95% | 0.21 | 1.28 | 248 | 41.9% | +2.58% | 1.07% | 0.74% |
| vol_filtered | doubled | 1.67% | 8.63% | 0.19 | 1.32 | 235 | 40.3% | −1.01% | 1.06% | 0.37% |
| regime_switch | base | 0.53% | 6.60% | 0.08 | 1.22 | 84 | 13.7% | −3.17% | 0.34% | 0.12% |
| regime_switch | doubled | −0.50% | 6.70% | −0.07 | 0.78 | 56 | 8.3% | −2.92% | −0.62% | −0.76% |

---

## 3. La puerta de robustez

**Ninguna de las catorce configuraciones pasa en ≥3 de 4 activos.** Solo dos celdas pasan en
un activo, ambas en BTC:

| | CAGR | DD | Calmar | PF | trades | exposición | OOS | años positivos | mejor año |
|---|---|---|---|---|---|---|---|---|---|
| **T1 trend / BTC** | 1.93% | 3.76% | 0.51 | 1.53 | 197 | 38.8% | +0.94% | 6/10 (60%) | 40% |
| **B1 breakout / BTC** | 1.47% | 2.48% | 0.59 | 2.12 | 91 | 17.5% | +1.57% | 7/10 (70%) | 30% |

Ambas rozan los umbrales, no los superan con holgura: 60% de años positivos es exactamente el
mínimo, y Calmar 0.51 es exactamente el mínimo más 0.01.

### Por qué falla cada familia

| configuración | razones (× nº de activos) |
|---|---|
| T1 trend base | low_calmar×2, single_year×2, inconsistent_years×2, oos_negative×1 |
| T2 trend doubled | low_calmar×4, inconsistent_years×2, oos_negative×2, single_year×2 |
| B1 breakout base | **oos_negative×3**, inconsistent_years×2, single_year×2, low_calmar×1 |
| B2 breakout doubled | inconsistent_years×4, low_calmar×3, single_year×3, oos_negative×3 |
| M1 momentum base | **low_calmar×4**, single_year×3, inconsistent_years×1 |
| M2 momentum doubled | single_year×4, oos_negative×4, low_calmar×3, cost_fragile×1 |
| G1 regime_trend base | **oos_negative×4**, low_calmar×3, inconsistent_years×3 |
| G2 regime_trend doubled | **thin_sample×4**, inconsistent_years×4, oos_negative×4 |
| R1/R2 mean_reversion | **negative_after_costs×4, cost_fragile×4** en ambas variantes |
| V1/V2 vol_filtered | **low_calmar×4** en ambas |
| S1 regime_switch base | low_calmar×4, oos_negative×4, cost_fragile×2 |
| S2 regime_switch doubled | negative_after_costs×3, cost_fragile×3, low_calmar×4 |

---

## 4. Lo que dice la criba

**Mean reversion vuelve a quedar rechazada, ahora en 1D.** Negativa después de costes en los
cuatro activos y en las dos variantes, PF 0.70–0.76. M22 la rechazó en los cuatro cruces a 4h;
1D no la rescata. **Familia cerrada** por la stop rule declarada.

**Regime switching vuelve a caer.** S2 pierde dinero en tres de cuatro. Coherente con M22.

**Doblar el horizonte empeora casi todo.** En seis de las siete familias el Calmar de la
variante doubled es igual o peor. Solo `regime_trend` sube (0.24 → 0.25), y a costa de quedarse
en 22 trades — thin_sample en los cuatro activos.

**El drawdown a 1D es notablemente mejor que a 4h.** Breakout base tiene 2.90% de DD mediano
contra el 6.34% que M23 midió en 4h. Pero el CAGR también baja: 1.97% frente al 3.2%
in-sample de B2. Menos riesgo y menos retorno; el Calmar no mejora lo suficiente.

**Los costes no son el cuello de botella a 1D.** Doblarlos cuesta entre 0.2 y 0.7 pp de CAGR
en las familias que ganan. A esta frecuencia el problema no son los costes: es que no hay
suficiente edge bruto que proteger.

**`regime_trend`, el incumbente, es de lo peor a 1D.** 0.85% de CAGR y OOS negativo en los
cuatro activos. Medido como candidato y no como benchmark, no sostiene su reputación en este
timeframe.

---

## 5. Qué merece pasar a 4H

Por el ranking de profit declarado (Calmar primero, CAGR después) **entre lo que pasó la
puerta** — y como no pasó nada en ≥3 activos, esto es un orden de las *menos malas*, no una
lista de supervivientes:

1. **breakout base (B1)** — mejor Calmar mediano (0.58), mejor PF (2.09), menor DD (2.90%) y
   menor exposición (16.1%). Es además la familia de B2, ya en paper a 4h.
2. **trend base (T1)** — mejor CAGR mediano (2.39%) y la única que pasa todas las puertas en
   BTC junto a B1, con OOS positivo en la mediana.

**Cerradas por la stop rule:** mean_reversion y regime_switch.

**No recomiendo llevar a 4H**: momentum y vol_filtered (low_calmar en los cuatro activos, y
exposición del 40–45% para un Calmar de 0.2), regime_trend (OOS negativo en los cuatro), y
todas las variantes doubled.

---

## 6. Limitaciones, dichas antes de que alguien las encuentre

1. **La pre-declaración no fijó cómo agregar los cuatro activos.** `survives()` recibe un
   `Measured` con un campo `assets_positive`, pero no dice si los demás campos son la mediana,
   el peor o el activo. Aquí se aplicó **la puerta por activo y luego se contaron los activos
   que pasan**, que es la lectura coherente con `MIN_ASSETS_POSITIVE` y con la regla de M22
   ("una sola variante que pase en ambos mercados"). Es una elección hecha después de correr,
   aunque no cambie ningún umbral, y debería haber estado declarada.
2. **La sensibilidad a parámetros no se corrió.** El campo `neighbour_min_profit_factor` se
   neutralizó, así que ninguna de estas celdas ha demostrado no ser un pico estrecho. Ninguna
   podría ser PAPER CANDIDATE aunque pasara el resto.
3. **No hay walk-forward en esta criba.** Solo OOS por corte de fecha.
4. **Los costes siguen siendo modelados**, no medidos del venue. Lo declarado fue supervivencia
   a ×2 y ×3, no una medición.
5. **1D reduce la muestra.** Con 3301 barras y ~90 trades en breakout, los ratios descansan en
   pocas observaciones pese a cubrir nueve años.

---

## 7. Clasificación

| familia | 1D |
|---|---|
| mean_reversion | **REJECT** (cerrada) |
| regime_switch | **REJECT** (cerrada) |
| momentum | **REJECT** a 1D |
| vol_filtered | **REJECT** a 1D |
| regime_trend | **REJECT** a 1D |
| trend | **WEAK** — pasa en 1/4 activos |
| breakout | **WEAK** — pasa en 1/4 activos, mejor perfil riesgo/retorno |

Ningún PROMISING. Ningún PAPER CANDIDATE. **No se rebajó ningún criterio.**

---

## 8. Deuda técnica registrada, no atendida

El motor de backtest es **O(n²) en longitud de histórico** — medido a 1k/2k/4k/8k barras, el
coste se multiplica por 3.88, 3.91 y 3.99 al doblar. Eso pone 1H en ~82.7 h para 56 corridas
frente a ~5 min de 1D. Es la deuda técnica de mayor impacto del proyecto y **queda anotada sin
tocar**, por instrucción: el objetivo ahora es encontrar edge, no optimizar infraestructura.

## 9. Reproducir

```bash
uv run python scripts/m29_dataset.py    # deriva y verifica 1D desde los 1h de M16
uv run python scripts/m29_screen.py     # 56 celdas, 1 worker, ~31 min
```
