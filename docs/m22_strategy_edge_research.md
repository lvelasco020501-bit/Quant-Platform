# M22 — Seis familias de estrategia, buscando edge

**Estado:** COMPLETADO (fase 1 y 2a completas, fase 2b parcial) · research/backtesting únicamente · 2026-09-23

No se tocó paper, Risk V2 productivo, execution, portfolio ni las estrategias productivas.
**Tampoco se escribió ni una línea de estrategia**: las seis familias ya existían implementadas
y registradas desde el sprint M13. M22 es protocolo, runner y reporte.

## 1. Las seis familias y sus doce configuraciones

| # | Familia | Regla | La pregunta que hace |
|---|---|---|---|
| 1 | Trend following | `ema_slope` | ¿el precio está sobre una media que sube? |
| 2 | Breakout | `breakout_trend` | ¿es un máximo nuevo, dentro de una tendencia? |
| 3 | Momentum | `momentum_roc` | ¿el retorno de N barras es positivo? |
| 4 | Mean reversion | `zscore_revert` | ¿el precio está estirado bajo su media? |
| 5 | Regime switching | `regime_switch` | ¿qué regla pide *este* régimen? |
| 6 | Volatility-filtered | `vol_filtered_momentum` | ¿la tendencia corre en calma? |

El canal de Donchian desnudo **no** es candidata: es benchmark. Por eso la familia breakout se
prueba como la regla filtrada que un operador correría de verdad, y el canal pelado sigue
apareciendo en todas las tablas como vara de medir.

## 2. Los parámetros, y por qué ninguno lo elegí yo

| KEY | Base (canónicos de M13) | KEY | Duplicado (calculado) |
|---|---|---|---|
| T1 | period=50, slope=10 | T2 | period=100, slope=20 |
| B1 | 20/10, trend=200 | B2 | 40/20, trend=400 |
| M1 | lookback=72 | M2 | lookback=144 |
| R1 | window=48, z=−2→0 | R2 | window=96, z=−2→0 |
| G1 | 72/48, ER 0.30/0.20 | G2 | 144/96, ER 0.30/0.20 |
| V1 | 72, vol 24/168, ×1.5 | V2 | 144, vol 48/336, ×1.5 |

La primera variante son los parámetros canónicos de M13, sin tocar. La segunda es esa misma con
**cada ventana duplicada y cada umbral intacto**, producida por `doubled()` y no escogida: un
test recalcula las seis y falla si alguna se desvía. Un umbral no es un horizonte — duplicar un
z-score cambiaría la regla, no su plazo.

`Variant` **no tiene campo de símbolo**, así que "mismos parámetros en todo activo" es
estructural, no una promesa: no hay dónde escribir un ajuste por mercado.

Todo esto se commiteó en `3b21cfb` **antes de que existiera un solo resultado**.

## 3. El instrumento, y sus dos límites declarados

Las corridas usan la **variante de research** de M13: Risk V2 desplegado con sus dos breakers
con latch liberados. No es Risk V2 y nunca se reporta como tal. Se eligió sobre la policy C de
M16 por una razón concreta: M17–M21 probaron que C apaga un mercado hasta el 89.86% de su
historia, y cribar edge bajo C confundiría "no hay señal" con "el latch cerró el mercado".

**Lo que se mide es la regla de entrada más la salida de la plataforma**, no la regla de libro.
Dos salidas desplegadas pueden cerrar antes de que la estrategia lo pida: el stop al 6% del
precio y el time stop a las 42 barras (7 días).

**El time stop casi no muerde, y está medido, no supuesto.** La mediana de tenencia va de 3 a 15
barras según la configuración, y la proporción de trades que llegan al tope es del 0% al 13%.
Declaré esta limitación como potencialmente fatal para tendencia antes de correr; los datos
dicen que es menor, y la rebajé en lugar de dejarla sobredimensionada.

## 4. Fase 1 — criba en BTC y ETH (30 corridas, 0 fallos, 1h58)

Puerta de criba, fijada antes de correr y deliberadamente **más floja** que el veredicto:
≥30 trades, retorno > 0, PF ≥ 1, DD ≤ 35%. Decide dónde gastar horas, no qué significa nada.

Donde sí es estricta es en lo irrecuperable: **una familia avanza solo si una misma variante
pasó en BTC y en ETH.** T1 en BTC más T2 en ETH es ajuste por activo llegado por accidente.

Pasaron 15 de 24 configuraciones. Cuatro familias tenían vía; el tope declarado es tres.

| FAMILIA | LLEVADA POR | AÑOS+ | WF SHARE | TRADES | |
|---|---|---|---|---|---|
| breakout | B2 | 16 | 0.72 | 522 | avanza |
| trend | T2 | 14 | 0.67 | 1203 | avanza |
| momentum | M1 | 12 | 0.50 | 2040 | avanza |
| vol_filtered | V1 | 8 | 0.50 | 1856 | **cae** |

El desempate lee años positivos, luego walk-forward, luego muestra. **Nunca retorno** — y si lo
hubiera leído, T2 (+57.04% en ETH) habría desplazado a breakout, que es justamente la familia
con mejor consistencia anual.

### Dos familias rechazadas, y el rechazo es informativo

* **Mean reversion: 0 de 4 cruces.** Ni una corrida positiva; pérdidas del 13% al 19% con
  drawdowns clavados en ~20%. Comprar caídas contra nueve años de tendencia alcista estructural
  no funciona, y el resultado es demasiado uniforme para admitir matices.
* **Regime switching: eliminada por la regla que existe para esto.** G1 dio **+9.99% en BTC y
  −16.10% en ETH**. Sin la exigencia de "misma variante en ambos", habría entrado en la
  shortlist apoyada en un solo mercado.

### Un patrón transversal

Duplicar el horizonte **hunde** momentum (+17.96% → −5.39%), régimen (+9.99% → −15.75%) y
vol-filtered (+8.84% → −13.23%) en BTC, y **no mueve** tendencia (+28.97% → +26.82%) ni breakout
(+36.51% → +31.31%). Una familia cuyo resultado depende de acertar el horizonte no tiene edge en
el horizonte: tiene suerte en uno.

## 5. Fase 2a — cross-asset en los cuatro mercados (12 corridas, 0 fallos, 45 min)

| | BTC | ETH | BNB | SOL |
|---|---|---|---|---|
| **B1** | +36.51% · 5.50% | +36.85% · 9.81% | +58.47% · 5.43% | **+1.28%** · 14.59% |
| **B2** | +31.31% · 6.34% | +27.17% · 14.99% | +62.97% · 4.44% | +6.06% · 13.63% |
| T1 | +28.97% · 14.86% | +34.34% · 13.47% | **+5.54%** · 19.13% | +29.29% · 16.81% |
| T2 | +26.82% · 13.93% | +57.04% · 11.54% | +9.07% · 17.99% | **−4.09%** · 20.11% |
| M1 | +17.96% · 17.88% | +33.91% · 16.85% | +5.44% · 20.19% | +11.64% · 20.44% |

Walk-forward (positivas / probadas):

| | BTC | ETH | BNB | SOL |
|---|---|---|---|---|
| B1 | 6/9 | 5/9 | **8/9** | 3/6 |
| B2 | **8/9** | 5/9 | **9/9** | 2/6 |
| T1 | 5/9 | 5/9 | 3/9 | 2/6 |
| M1 | 4/9 | 5/9 | 5/9 | 2/6 |

**T2 queda eliminada** (−4.09%, PF 0.96 en SOL). Era la variante que llevó a trend a la
shortlist. Sobreviven los cuatro mercados solo B1, B2, T1 y M1.

### Lo que el cross-asset cambió

Con BTC y ETH, breakout parecía una respuesta. Con los cuatro, **ninguna familia reproduce en
SOL lo que hace en los otros tres**: breakout cae de +62.97% a +6.06%, trend de +34% a −4%.

El filtro SMA200 sigue haciendo algo demostrable: convierte el **−10.92%** del breakout desnudo
en SOL en positivo. Pero ahí **evita el daño sin producir edge** — PF 1.01 y expectancy de 0.36
USDT por trade sobre 359 trades. Hay que describirlo así, no como "funciona en los cuatro".

### El hallazgo incómodo: el incumbente gana

| regime_trend (M15/M16) | BTC | ETH | BNB | SOL |
|---|---|---|---|---|
| net | +34.32% | +18.59% | +24.84% | +23.01% |
| **DD** | **4.41%** | **5.49%** | **7.10%** | **3.94%** |
| trades | 124 | 146 | 129 | 106 |

Cuatro mercados positivos, drawdown entre 3.9% y 7.1%, exposición del 7–10% del tiempo. Ninguna
de las doce configuraciones nuevas iguala esa consistencia.

Los benchmarks, en cambio, son violentamente dependientes del activo: el breakout desnudo va de
**+68.59% en BNB a −10.92% en SOL**; el EMA de **+42.74% en SOL a −5.01% en BNB**. Cada uno
tiene su mercado y se hunde en el del otro.

**Doce configuraciones nuevas, 42 corridas, y la mejor regla sigue siendo la que ya teníamos.**
Resultado negativo para la búsqueda y positivo para el proyecto: M15/M16 no fueron suerte, y
ahora hay cuatro mercados y un instrumento de riesgo distinto que lo respaldan.

## 6. Fase 2b — stress y Risk V2 (parcial, y por qué)

Antes de correr, comprobé dónde el stress **puede** cambiar un veredicto, contra umbrales fijados
en M13. Siete de dieciséis celdas ya estaban decididas:

* **M1 es WEAK en los cuatro mercados** por drawdown (17.9%, 16.9%, 20.2%, 20.4%, todos sobre el
  techo del 15%). Ningún stress cambia eso, y no se gastó cómputo en confirmarlo.
* **T1 es WEAK en BNB y SOL** por lo mismo.
* **Solo B2 en BTC podía llegar a PAPER CANDIDATE**: única celda con DD ≤ 10%, WF ≥ 0.75,
  PF ≥ 1.2 y superioridad sobre ambos benchmarks a la vez.

### Stress de costes en BTC

| BTC | base | fees ×2 | slip ×3 | los tres + spread 5bps |
|---|---|---|---|---|
| B1 | +36.51% | +23.70% | +24.07% | **+7.85%** (PF 1.07) |
| B2 | +31.31% | +21.00% | +21.33% | **+10.68%** (PF 1.16) |

Ambos sobreviven. El escenario combinado se lleva el 65–79% del retorno: el edge es real pero
**fino frente a costes**. B2 lo resiste mejor (retiene 34% contra 21%) porque opera 234 veces en
lugar de 381 — menos trades es menos superficie de coste.

### Bajo Risk V2 desplegado

| BTC | variante research | Risk V2 desplegado |
|---|---|---|
| B1 | +36.51% · DD 5.50% · 381 trades | +8.84% · DD 4.88% · **54 trades** |
| B2 | +31.31% · DD 6.34% · 234 trades | +12.36% · DD 2.87% · **71 trades** |

Ambas positivas. Los breakers con latch recortan el 86% y el 70% de los trades.

### La corrida que se abortó

Los dos primeros jobs de stress tardaron **3.504s** contra los 1.290s estimados, lo que ponía los
16 jobs en 7.8 horas. Se abortó. El diagnóstico: con 575 MB de swap libre, la carga estaba en
8.53 mientras el consumo real de CPU era de ~3.1 núcleos de 8 — la diferencia es I/O bloqueado,
es decir paginación. **Se corrigió un diagnóstico anterior mío**, que atribuía la lentitud a pura
contención de CPU y descartaba el swap.

Quedaron sin correr: el stress de B1/B2 en ETH, BNB y SOL, y el de T1. Sin él, `judge()` no
concede PROMISING a esas celdas — quedan **WEAK por falta de stress, no por haberlo fallado**, y
así se reportan.

## 7. Veredicto: **WEAK. Ningún PAPER CANDIDATE.**

El veredicto mecánico de `judge()` sobre B2 en BTC —la mejor celda del milestone— es **WEAK**, y
la razón es un hueco en mi propia pre-declaración:

**M22 no declaró una ventana out-of-sample.** M13 a M16 designaban una; yo fijé la puerta de
criba y el desempate pero no una ventana OOS, porque el `judge()` solo entraba en escena en la
fase 2. `Evidence.out_of_sample_return` queda en `None`, y `None` falla su comprobación.

Si se tomara **el último fold del walk-forward** como ventana out-of-sample, B2 en BTC daría
**PAPER CANDIDATE**. Esa regla no se adopta, por tres razones:

1. **Es post-hoc.** La inventaría después de ver los números, y resulta ser la única regla que
   produce el único PAPER CANDIDATE del milestone. Esa es la forma exacta de un criterio elegido
   por su resultado.
2. **Es frágil.** Ese fold da **+0.87%**, casi ruido; el mismo fold en B1 da −0.07%. Si los datos
   acabaran unos meses antes o después, el veredicto se invertiría.
3. **La instrucción era explícita:** no rebajar criterios para conseguir un PAPER CANDIDATE.

Queda registrado como lectura alternativa marcada post-hoc, no como veredicto.

### Tabla final

| CONFIG | MERCADO | VEREDICTO | POR QUÉ |
|---|---|---|---|
| B2 | BTC | **WEAK** | sin ventana OOS declarada; todo lo demás pasa |
| B1 | BTC | WEAK | ídem, y WF 0.67 < 0.75 |
| B1, B2 | ETH, BNB, SOL | WEAK | sin stress corrido |
| T1 | BTC, ETH | WEAK | sin stress corrido |
| T1 | BNB, SOL | WEAK | DD 19.13% y 16.81% > 15% |
| M1 | los cuatro | WEAK | DD 16.9–20.4% > 15% |
| T2 | SOL | REJECT | −4.09%, PF 0.96 |
| M2, R1, R2, G1, G2, V1, V2 | — | REJECT / no avanzan | criba fase 1 |

## 8. Limitaciones

* **Sin ventana out-of-sample declarada.** El defecto de protocolo de arriba. Cualquier
  milestone que quiera un PAPER CANDIDATE tiene que declararla **antes** de correr.
* **Stress incompleto** en ETH, BNB y SOL.
* **Largo-only, un solo mercado abierto a la vez, 4H.** Nada aquí dice nada de carteras,
  cortos ni otros timeframes. El 1D no llegó a correrse.
* **Salidas de la plataforma, no de la regla.** Ver §3.
* **Nueve años de un solo régimen macro.** Los cuatro mercados comparten ciclo; su correlación
  hace que "cuatro mercados" sea menos independiente de lo que el número sugiere.
* **SOL tiene 6 años, no 9**, así que sus 6 folds walk-forward pesan menos que los 9 del resto.

## 9. Lo que este milestone deja establecido

1. **El filtro de tendencia sobre un canal de Donchian hace algo real y medible.** Mismo canal
   20/10, lo único distinto es exigir cierre sobre la SMA200: en ETH pasa de +9.90%/DD 20.20% a
   +36.85%/DD 9.81%; en SOL, de −10.92% a positivo. Es el hallazgo nuevo más limpio.
2. **Mean reversion y regime switching quedan descartadas** en 4H para estos mercados.
3. **Las familias que dependen del horizonte no tienen edge**, y la regla de duplicar lo detecta.
4. **El incumbente `regime_trend` sigue siendo la regla más consistente del proyecto**, ahora
   verificado en cuatro mercados y bajo un instrumento de riesgo distinto al de M16.
5. **Ninguna familia nueva merece paper hoy.**

## 10. Qué haría falta para un PAPER CANDIDATE honesto

En este orden, y ninguno es un umbral nuevo:

1. **Declarar una ventana out-of-sample antes de correr.** Es el único bloqueo formal.
2. **Completar el stress** en ETH, BNB y SOL.
3. **Vecinos de B2** (40/20/400 ± un paso), que `judge()` usa como prueba de fragilidad y que M22
   no corrió para las configuraciones nuevas.
4. Si todo eso pasa: B2 es la candidata, no B1 — mejor walk-forward (8/9 y 9/9), mejor retención
   ante costes y un tercio menos de trades.

## 11. Tests, gate y reproducibilidad

* **43 tests del protocolo de M22.** Los que llevan el peso: cada variante duplicada se
  **recalcula** desde su base; duplicar mueve ventanas y no umbrales; un parámetro sin regla de
  duplicación es rechazado en vez de ignorado; cada variante base reusa los parámetros de M13
  sin cambios; una familia no avanza con una variante por mercado; y el desempate no puede leer
  un retorno.
* **Un bug propio, encontrado y corregido.** El agregado walk-forward filtraba por el rol
  `OUT_OF_SAMPLE`, que los folds de un plan nunca llevan — llevan `WALK_FORWARD_TEST`. Las 30
  corridas reportaron `0/0` teniendo 18 folds exitosos cada una. **Cero se lee como "falló todas
  las ventanas", no como "no medí nada"**, que es la forma peligrosa de estar mal. La regla vive
  ahora en el módulo de protocolo, cuatro tests la fijan (incluido que la ausencia reporte `None`
  y no `0`), y `--recompute` la re-deriva desde los folds en disco en lugar de repetir dos horas
  de backtests. **La shortlist no cambió con el arreglo**, verificado antes de reportarla.
* **Suite completa: 2 519 tests.** `ruff format`, `ruff check` y `mypy src` limpios; `mypy .` en
  su línea base de 108 errores / 10 archivos.
* **44 corridas completadas, 0 fallos** (30 criba + 12 cross-asset + 2 deployed), más 2 jobs de
  stress con 3 escenarios cada uno. Ledgers escritos por job, un directorio por corrida.
* **Producción intacta:** `git status` sobre `risk`, `execution`, `portfolio`, `paper`,
  `strategies` y `backtesting` está vacío. M22 no tocó nada fuera de `research/`, `scripts/`,
  `tests/` y `docs/`.
