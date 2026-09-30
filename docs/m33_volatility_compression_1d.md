# M33 fase 1 — compresión/expansión de volatilidad: **ninguna regla avanza, M33 cerrado**

**Estado:** CRIBA 1D COMPLETA · 3 reglas × 2 umbrales × 6 mercados · 252 corridas del **motor de producción** · 29 min · **ninguna variante pasa en ≥3 activos** · **4H NO SE CORRE** · 2026-09-30

Pre-declaración commiteada en `732b984`, **antes** de ver un solo resultado. Nada se tocó después.
El VPS no se tocó y ningún fichero de producción se modificó.

---

## 1. Lo que esta familia tiene y las anteriores no

Son reglas **mono-activo**, así que corren por `BacktestEngine` con **Risk V2, sizing por stop,
rechazo de órdenes y mínimos de venue** — la cadena certificada que usa toda estrategia
promovida. M30–M32 necesitaron un segundo backtester porque una regla cross-sectional no se
puede expresar mono-símbolo; aquí no hizo falta, y por tanto estos números no arrastran esa
incertidumbre. Es la primera línea de research desde M29 que se mide con el motor de verdad.

**Cero cambios en producción.** Las tres reglas se expresan con features que ya existían en la
gramática y que `warm_up` ya conocía. Las estrategias viven **sólo** en el registry de research:
un test afirma que el registry visible para paper sigue siendo exactamente `breakout`,
`breakout_trend` y `ema_trend`.

---

## 2. Parámetros pre-declarados — ninguno nuevo

| parámetro | valor | heredado de |
|---|---|---|
| ventana de medición | **24** | `vol_filtered_momentum.short_vol` |
| ventana de referencia | **168** | `vol_filtered_momentum.long_vol` |
| entrada (ruptura) | **20** | `breakout.entry_lookback` — el mismo leg que B2 corre en paper |
| salida | **10** | `breakout.exit_lookback` |
| umbral nulo | **1.0** | "tan quieto como ruido sin deriva" |
| umbral estricto | **0.6667** | **derivado** de `vol_filtered_momentum.max_ratio` = 1.5, invertido |
| sondas de sensibilidad | **12 / 48** | mitad y doble, la regla mecánica de M22 |

**Dentro de una regla sólo se mueve el umbral. Entre reglas sólo cambia qué se mide** — mismas
ventanas, mismo leg de ruptura, misma salida, mismos umbrales. Eso hace de esto una comparación
de tres definiciones de quietud, no seis estrategias sueltas.

**La decisión de diseño que hace el trabajo:** cada estadístico se divide por lo que daría un
paseo aleatorio en las mismas ventanas, así que **1.0 significa lo mismo en las tres reglas**. La
dispersión de precio y el rango realizado crecen como √ventana y se escalan por `√(24/168)`; la
volatilidad de retornos de una barra no crece con la ventana y no se escala. Esa asimetría es
aritmética, no criterio. El multiplicador de banda de Bollinger se cancela en un cociente de
anchos, que es precisamente por qué la forma de cociente es la correcta: un squeeze es una
afirmación sobre anchura, no sobre sigmas.

---

## 3. Resultados por regla

`nPF` = peor profit factor entre las dos vecinas (12 y 48 barras) · `año` = cuota del mejor año

### B — Bollinger squeeze breakout

| activo | CAGR | DD | Calmar | PF | trades | expo | ×2 | ×3 | OOS | año | nPF | |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **B1 umbral 1.0** | | | | | | | | | | | | |
| BTC | +0.90% | 3.64% | 0.25 | 1.53 | 87 | 16.7% | +0.71% | +0.52% | +0.14% | 0.50 | 1.31 | ❌ |
| ETH | +1.18% | 4.61% | 0.26 | 1.57 | 80 | 15.3% | +0.97% | +0.82% | −2.47% | 0.56 | 1.14 | ❌ |
| BNB | +1.34% | 4.44% | 0.30 | 1.78 | 85 | 14.9% | +1.09% | +0.90% | −0.54% | 0.69 | 1.46 | ❌ |
| **SOL** | +2.05% | 2.97% | **0.69** | 2.13 | 49 | 11.6% | +1.86% | +1.65% | +1.69% | 0.39 | 1.84 | ✅ |
| ADA | +0.13% | 6.46% | 0.02 | 1.06 | 71 | 11.3% | +0.02% | +0.09% | −4.29% | 3.84 | 1.39 | ❌ |
| XRP | +1.38% | 3.16% | 0.44 | 1.92 | 52 | 8.9% | +1.29% | +1.17% | +4.92% | 0.53 | 1.51 | ❌ |
| **B2 umbral 0.6667** | +0.04…+0.79% | 2.90…4.83% | −0.02…0.20 | 0.95…1.67 | 36–57 | 6–11% | | | | | | **0/6** |

**B1 pasa en 1 de 6. B2 en 0 de 6.**

### V — Volatility compression breakout

| activo | CAGR | DD | Calmar | PF | trades | expo | ×2 | ×3 | OOS | año | nPF | |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **V1 umbral 1.0** | | | | | | | | | | | | |
| BTC | +1.54% | 3.66% | 0.42 | 2.15 | 90 | 17.8% | +1.33% | +1.12% | +2.40% | 0.35 | 1.29 | ❌ calmar |
| ETH | +1.53% | 4.11% | 0.37 | 1.75 | 86 | 16.3% | +1.32% | +1.08% | −3.14% | 0.50 | 1.62 | ❌ |
| BNB | +1.12% | 3.77% | 0.30 | 1.56 | 97 | 17.5% | +0.85% | +0.62% | −0.22% | 0.58 | 1.35 | ❌ |
| SOL | **+3.21%** | 4.38% | **0.73** | 2.05 | 68 | 15.3% | +2.52% | +2.24% | **−0.15%** | 0.58 | 1.92 | ❌ año, OOS |
| ADA | +1.59% | 2.57% | 0.62 | **2.29** | 62 | 10.6% | +1.48% | +1.32% | **−0.12%** | 0.45 | 1.72 | ❌ OOS |
| XRP | +1.51% | 3.96% | 0.38 | 1.88 | 62 | 10.8% | +1.39% | +1.29% | +4.51% | 0.45 | 1.60 | ❌ calmar |
| **V2 umbral 0.6667** | | | | | | | | | | | | |
| **XRP** | +1.22% | **1.37%** | **0.89** | **3.19** | 31 | 5.7% | +1.19% | +1.10% | +3.49% | 0.50 | 2.15 | ✅ |
| otros cinco | +0.28…+0.68% | 1.90…2.92% | 0.12…0.31 | 1.54…2.31 | 21–44 | 3.5–8.4% | | | | | | ❌ |

**V1 pasa en 0 de 6. V2 en 1 de 6.** V1 es la mejor de las tres reglas por CAGR y PF, y **dos de
sus seis fallan sólo por OOS o sólo por Calmar** — lo más cerca que llega esta familia.

### R — Range compression breakout

| activo | CAGR | DD | Calmar | PF | trades | OOS | año | nPF | |
|---|---|---|---|---|---|---|---|---|---|
| **R1** BTC | +0.72% | 3.28% | 0.22 | 1.45 | 84 | +0.10% | 0.52 | 1.29 | ❌ |
| R1 ETH | +0.51% | 4.29% | 0.12 | 1.24 | 78 | −0.89% | 1.23 | 1.20 | ❌ |
| R1 BNB | +0.17% | 4.29% | 0.04 | 1.09 | 87 | −0.93% | 3.14 | 0.98 | ❌ |
| R1 SOL | +0.85% | 3.96% | 0.22 | 1.44 | 42 | +2.09% | 0.72 | 1.63 | ❌ |
| R1 ADA | +0.04% | 5.29% | 0.01 | 1.02 | 68 | −2.40% | 16.11 | 1.23 | ❌ |
| **R1 XRP** | +1.68% | 3.33% | **0.50** | 1.99 | 63 | **+5.64%** | 0.41 | 1.71 | ✅ |
| **R2** (seis) | −0.60…+0.86% | 2.71…6.00% | −0.11…0.32 | 0.53…1.97 | 24–49 | | | | **0/6** |

**R1 pasa en 1 de 6. R2 en 0 de 6.** R2 pierde dinero en BNB y ADA.

---

## 4. Veredicto

La regla cross-asset estaba **declarada antes de correr**: una variante avanza sólo con ≥3 de 6
mercados.

| variante | regla | umbral | pasa | |
|---|---|---|---|---|
| B1 | bb_squeeze | 1.0 | **1/6** | cerrada |
| B2 | bb_squeeze | 0.6667 | **0/6** | cerrada |
| V1 | vol_compression | 1.0 | **0/6** | cerrada |
| V2 | vol_compression | 0.6667 | **1/6** | cerrada |
| R1 | range_compression | 1.0 | **1/6** | cerrada |
| R2 | range_compression | 0.6667 | **0/6** | cerrada |

# STOP — M33 cerrado

**4H no se corre.** Ningún parámetro se ajusta. Ningún umbral se rebaja. Ningún PAPER CANDIDATE.

**Los tres pases sueltos son en pares (variante, mercado) distintos** — B1/SOL, V2/XRP, R1/XRP.
Ninguna variante pasa en dos mercados. Eso es la firma del ruido, no de un edge: si la compresión
fuese una causa real, la misma regla funcionaría en varios mercados, no una regla distinta en
cada uno.

---

## 5. Lo que estos números enseñan

**El drawdown no fue nunca la restricción, y eso es nuevo.** Ninguna de las 36 celdas se acerca
al tope del 35%: el peor es **6.46%** y la mediana ronda el 3.7%. En M30, el drawdown era lo
único que fallaba y fallaba por el doble. Aquí la puerta que muerde es **Calmar** (31 de 36
celdas): el retorno es demasiado pequeño incluso para el riesgo pequeño que se toma.

**Esta familia produce un perfil genuinamente de bajo riesgo y bajo retorno.** Exposición del
3.5% al 17.8% — está fuera del mercado el 82–96% del tiempo — con CAGR de ~1% y drawdowns de
~3%. Es un perfil coherente y defendible; simplemente no llega al umbral de retorno/riesgo que
este proyecto declaró.

**Y llega exactamente donde ya está el incumbente.** B2 en paper mide 1.04% de CAGR con 2.03% de
DD y Calmar 0.51 a 1D. Estas reglas de compresión aterrizan en el mismo vecindario: ~1% al año.
Un mecanismo distinto que llega al mismo sitio marginal. **Ese es el hallazgo estructural de
M33**: el problema de esta plataforma a 1D no es la familia elegida, es que el espacio entero
rinde en torno al 1% anual después de costes.

**La sensibilidad es sana, por una vez.** El peor PF de las vecinas (12 y 48 barras) es ≥1.00 en
**33 de 36** celdas; sólo 3 fallan `narrow_peak`. Estas reglas **no** son picos estrechos — a
diferencia de la candidata de M32, cuyo Calmar se desmoronaba al mover el lookback. Aquí el
parámetro no es el problema: la magnitud lo es.

**Los costes son casi irrelevantes a esta frecuencia.** Triplicarlos cuesta entre 0.1 y 1.0 pp de
CAGR. Con 21–97 operaciones en nueve años y exposición de un dígito, no hay mucho coste que
pagar. Sólo 6 celdas de 36 fallan `cost_fragile`, y todas ellas ya eran marginales.

**El umbral estricto empeora las tres reglas.** B2 < B1, V2 < V1 (por CAGR y PF medianos), R2 <
R1. Exigir más quietud reduce las operaciones a 21–57 y el retorno con ellas, sin mejorar el
Calmar. Es evidencia contra la hipótesis en su forma fuerte: **más compresión no es más edge.**

**OOS es la segunda causa de muerte** (21 de 36). En la ventana 2024→ la mayoría de las celdas
son planas o negativas, incluidas las mejores de V1 (SOL −0.15%, ADA −0.12%). Lo que había,
había menos de él en los últimos dos años y medio.

---

## 6. Shortlist

| | CAGR | DD | Calmar | PF | OOS | nPF | |
|---|---|---|---|---|---|---|---|
| V2 / XRP | +1.22% | **1.37%** | **0.89** | **3.19** | +3.49% | 2.15 | pasa sola |
| B1 / SOL | +2.05% | 2.97% | 0.69 | 2.13 | +1.69% | 1.84 | pasa sola |
| R1 / XRP | +1.68% | 3.33% | 0.50 | 1.99 | +5.64% | 1.71 | pasa sola |

**Ninguna avanza.** Se listan porque tres celdas limpias en tres mercados distintos es información
—no cero— y porque si alguna vez se retoma esta familia, éstas son las que merecerían mirarse
primero. Pero un solo mercado por variante es exactamente lo que la regla de ≥3 existe para
rechazar, y M24 ya enseñó lo que pasa con un candidato que funciona en un mercado.

Años de V2/XRP: +0.0%, −0.2%, +1.4%, **+5.2%**, −0.5%, +0.9%, +1.7%, +2.2%, −0.4%.
Años de B1/SOL: +0.0%, +4.3%, −0.1%, +2.7%, +4.8%, +0.8%, +0.1%.

---

## 7. Limitaciones

1. **`ATR` no existe en esta plataforma.** La regla 2 se declaró con `volratio` — el cociente de
   volatilidad realizada corta/larga — en vez de ATR verdadero, porque añadir ATR habría exigido
   editar la gramática de indicadores, que es un fichero de producción. Es una sustitución
   honesta, declarada antes de correr, y captura la misma idea (volatilidad baja respecto a su
   propia historia) sobre cierres en vez de sobre rangos verdaderos. No es ATR.
2. **La expansión no se exige explícitamente.** El enunciado pedía "entrada cuando el precio rompe
   con ATR expandiéndose"; aquí **la ruptura ES el evento de expansión**, y la condición de
   compresión mira el régimen previo. Exigir además que el cociente haya subido requeriría estado
   entre barras, y una estrategia con estado es más difícil de razonar y de testear. Declarado,
   no escondido.
3. **Sin walk-forward.** No estaba entre las puertas declaradas. Sólo OOS por corte de fecha.
4. **Sin sensitivity sobre las ventanas de ruptura** (20/10), sólo sobre la de medición (24). Las
   primeras son las heredadas de `breakout`, que M13 y M22 ya midieron.
5. **Muestra pequeña en varias celdas.** V2 opera 21–44 veces en nueve años. No se declaró suelo
   de operaciones y no se aplicó ninguno — rechazar por un suelo que nadie pactó sería la misma
   falta que relajar uno que sí. Pero los ratios de esas celdas descansan en pocas
   observaciones y eso hay que leerlo así.
6. **Seis mercados, los large caps de hoy.** M32 mostró lo que el sesgo de selección le hace a un
   resultado. Esta criba no lo corrige; no hacía falta, porque nada pasó.

---

## 8. Reproducir

```bash
uv run python scripts/m33_screen.py    # 36 celdas, 252 corridas del motor, ~29 min
```

Evidencia en `var/research/m33/screen_1d.json`. Motor de producción con Risk V2; la variante
`deployed` (breakers con latch) se corre y se registra para cada celda aunque ninguna llegue a
necesitarla.
