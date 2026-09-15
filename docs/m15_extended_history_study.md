# M15 — Seis años y medio de histórico, riesgo convertido por timeframe

**Estado:** COMPLETADO · research/backtesting únicamente · 2026-09-15

Nada de paper, Risk V2 productivo, execution, portfolio ni las estrategias productivas cambió.
`git status` sobre `src/quantplatform/{risk,execution,portfolio,paper,backtesting}` y sobre las
estrategias productivas (`strategies/{ema_trend,breakout,registry}.py`) está vacío, y el
registro de paper sigue siendo `['breakout', 'ema_trend']`.

## 1. Dataset nuevo — mismo estándar que M10c, más cuatro chequeos

**Fuente:** Binance Vision (`data.binance.vision`), BTCUSDT spot 1h, 2020-01-01 → 2026-09-15 (la
última barra abre 2026-09-14T23:00Z). A diferencia de M10c, el pipeline está **comprometido**
(`scripts/m15_dataset.py`), así que el dataset se reconstruye byte a byte. Los archivos siguen en
`data/raw/m15/`, ignorado por git como el de M10c.

| Chequeo | Resultado |
|---|---|
| Archivos | 80 mensuales (2020-01 → 2026-08) + 14 diarios (2026-09-01 → 14) |
| Checksums SHA-256 | **94 / 94** verificados |
| Unidad del timestamp, detectada por fila | 43 817 en **ms** (≤ 2024) + 14 928 en **µs** (≥ 2025) |
| Barras 1h esperadas / presentes | 58 776 / 58 745 |
| Duplicados · conflictos · desalineadas · OHLCV inválido | **0 · 0 · 0 · 0** |
| Horas ausentes | **31**, en 15 caídas del exchange (2020-02 → 2023-03). Ninguna después de marzo de 2023 |
| ¿Existen esas 31 horas en la API REST en vivo? | **No, ninguna** → son caídas, no datos perdidos |
| vs M10c (año solapado) | **8 760 / 8 760** barras idénticas campo a campo |
| vs REST en vivo (primera barra, crashes, frontera ms→µs, última) | **7 / 7** idénticas |
| 4h vs las velas 4h oficiales de Binance | 14 687 / 14 693 idénticas, **6 explicadas, 0 sin explicar** |
| 1d vs las velas 1d oficiales de Binance | 2 444 / 2 449 idénticas, **5 explicadas, 0 sin explicar** |

**Las 31 horas ausentes.** Ninguno de los dos endpoints de Binance —el archivo y la REST— tiene
barra horaria para ellas: son caídas. No se rellenan. A 1h quedan como huecos (el motor no exige
continuidad). A 4h y 1d la barra se construye con las horas que sí operaron, **solo si todas las
horas ausentes de ese cubo son caídas confirmadas**; cualquier otro hueco se sigue rechazando.
Cuando las cuatro horas de un cubo son caída (2020-02-19 12:00–16:00), no se emite barra: la serie
4h oficial de Binance tampoco la tiene.

**Las 11 discrepancias con las series 4h/1d oficiales.** Nuestras barras lentas son la suma exacta
de la serie 1h de Binance; sus propias series 4h y 1d se construyen aparte y, en 11 de 17 142
barras (0.06 %), no coinciden con ella. Cada una tiene una explicación mecánica:

* **9 — precios idénticos, solo volumen o nº de trades distintos** (p. ej. 2021-01-21: −3 200 BTC
  en la serie oficial). Ninguna estrategia lee volumen.
* **1 — un tick** (2023-03-24 12:00: apertura 28 080.00 frente a 28 079.99).
* **1 — actividad sin barra horaria en ningún endpoint** (2020-12-21 12:00: la vela 4h oficial
  incluye operativa en una hora que no tiene barra 1h ni en el archivo ni en la REST).

**Digests reproducibles** (`bars_digest` de la plataforma): 1h `01cebbd8b757b5be86a24b56202415f6`
(58 745 barras) · 4h `360b9b605f945d85f7707f87736d5add` (14 693) · 1d
`4eb18e680928f27f4ff6d6d45428a0da` (2 449). Dos reconstrucciones independientes dieron los mismos.

## 2. Riesgo dependiente del timeframe — cada conversión, explícita

Desde la configuración 1h de producción (`src/quantplatform/research/m15.py`). **Ninguna regla se
eligió mirando un resultado**, y un test fija que *todos* los campos de `RiskConfiguration` están
clasificados, así que un campo nuevo no puede colarse sin convertir.

| Parámetro | 1h (producción) | Regla | 4h | 1d |
|---|---|---|---|---|
| Stop inicial | 300 bps | **× √(duración de la barra / 1h)** — la excursión típica del precio crece con la raíz del tiempo; el stop queda a la misma distancia "en barras de ruido". Redondeo half-even a bps enteros | 600 | 1 470 |
| Activación break-even | 150 | igual | 300 | 735 |
| Activación trailing | 300 | igual | 600 | 1 470 |
| Distancia trailing | 200 | igual | 400 | 980 |
| Take-profit | 600 | igual | 1 200 | 2 939 |
| Stop mínimo / máximo (risk budget) | 50 / 1 000 | igual | 100 / 2 000 | 245 / 4 899 |
| `max_volatility` (σ por barra) | 0.15 | × √tiempo, como cualquier desviación por barra; 4 decimales | 0.30 | 0.7348 |
| **`max_holding_bars`** | **168** (= 7 días) | **mismo tiempo, no mismas barras** | **42** | **7** |
| Riesgo por trade / exposición máxima | 1 % / 50 % | sin cambio: política sobre el capital | = | = |
| Pérdida diaria 3 % · drawdown diario 5 % | — | sin cambio: por día calendario | = | = |
| Drawdown total y su latch | — | sin cambio: sobre equity; lo fija la política de latch | = | = |
| Racha de pérdidas | — | sin cambio: cuenta trades; lo fija la política | = | = |
| Límites de órdenes por hora / día · staleness | — | sin cambio: tiempo de reloj | = | = |
| Spread, buffers, comisión 10 bps, slippage 5 bps | — | sin cambio: por orden (M15 los mantiene idénticos) | = | = |

**Consecuencia intencionada:** con 1 % de riesgo por trade y un stop más ancho, la posición es más
pequeña — ≈ 33 % del equity a 1h, ≈ 6.8 % a 1d. Es el mismo riesgo, no la misma exposición.
Retorno y costes bajan con ella; por eso PF y expectancy son las medidas comparables entre
timeframes.

## 3. Protocolo

* **Estrategias** (parámetros canónicos de M13, en barras, sin reoptimizar): `regime_trend`,
  `rsi_reversal`, EMA20/50 y Breakout20/10 (copias de research habilitadas en 4h/1d).
* **Políticas:** A (producción: latch permanente de 5 pérdidas + drawdown 20 %), C (latch de
  drawdown 10 %, sin racha), D (racha con cooldown 24 h + drawdown 10 %) y la referencia **REF**
  (sin latch; **no es Risk V2**).
* **Por qué se trabaja por años calendario.** El motor de backtest valida todo su histórico en cada
  barra, así que el coste de una corrida crece con el cuadrado de su longitud: una sola corrida 1h
  de seis años llevaría horas. Cada timeframe se corre año a año (7 ventanas, 2026 hasta la última
  barra), y 4h y 1d además de forma continua sobre todo el periodo.
* **Walk-forward real:** entrenar en un año, testear en el siguiente, 6 folds (test 2021 → 2026).
  No se ajusta nada.
* **Cabecera de cada combinación:** la corrida continua a 4h y 1d; a 1h los siete años combinados
  (retorno compuesto, trades y costes sumados, PF de beneficio y pérdida brutos sumados; el
  drawdown es el peor año, **cota inferior**).
* **Estabilidad por año:** a 4h y 1d se toma de la corrida continua, cortada en cada fin de año —
  nunca de las ventanas anuales, cuyo warm-up se comería los primeros 73 días de cada año diario.
  A 1h, de las ventanas (warm-up de 3 días).
* **Stress** (fees ×2 · slippage ×3 · ambos + spread 5 bps) y **vecinos declarados**, sobre REF, en
  cada año.
* **Veredicto:** `judge` de M13, y encima **`cap_by_sample`**: PAPER CANDIDATE exige ≥ 100 trades
  en todo el periodo **y** ≥ 5 en cada año de test del walk-forward. OOS = el año de test 2025.
* **Top 2:** solo estrategias de research bajo A/C/D, por veredicto → walk-forward → drawdown, **una
  por (estrategia, timeframe)** — corrige la limitación de M14, cuya regla devolvía la misma
  combinación dos veces.
* **¿Sigue justificándose C?** La regla de M14 sin cambios: segura (peor DD ≤ 20 %) → operable →
  menos tiempo bloqueado.

## 4. Ejecución

104 trabajos (el walk-forward, las siete ventanas anuales y la corrida continua de cada
estrategia × timeframe), **0 fallidos**, 11 h 38 min en 6 procesos. Los 104 ledgers pasan
`quantplatform research verify` con **0 contradicciones**; cada experimento tiene una sola entrada,
así que la reproducibilidad la establece la re-ejecución desde árbol limpio (§11), no el verify. El
informe completo, con cada combinación bajo cada política, es `var/research/m15/REPORT.md`
(`scripts/m15_report.py`), y los números de máquina, `verdicts.json`.

## 5. Resultados por timeframe — referencia REF (sin latch, no es Risk V2)

Capital inicial 10 000 USDT; porcentajes sobre él. EXP = expectancy en USDT por trade. DD a 1h =
peor año (cota inferior). Stress = fees ×2 + slippage ×3 + spread 5 bps, todos los años combinados.

| Estrategia | TF | Trades | Bruto | Fees | Slippage | **Neto** | PF | EXP | DD | Años + | WF | Stress | Veredicto |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| regime_trend | 1h | 245 | +26.04 % | 13.21 % | 6.61 % | **+6.22 %** | 1.10 | 2.62 | 7.11 % | 5/7 | 4/6 | −14.43 % | WEAK |
| rsi_reversal | 1h | 1 191 | +19.92 % | 58.82 % | 29.41 % | **−68.32 %** | 0.61 | −8.83 | 20.22 % | 0/7 | 0/6 | −78.83 % | REJECT |
| EMA (bench) | 1h | 1 547 | +74.36 % | 80.20 % | 40.10 % | **−45.93 %** | 0.86 | −3.48 | 20.44 % | 1/7 | 0/6 | −74.95 % | REJECT |
| Breakout (bench) | 1h | 1 453 | +57.29 % | 74.32 % | 37.16 % | **−54.18 %** | 0.78 | −4.97 | 20.53 % | 0/7 | 0/6 | −76.82 % | REJECT |
| **regime_trend** | **4h** | 79 | +28.11 % | 2.66 % | 1.33 % | **+24.11 %** | **2.62** | 30.52 | **2.71 %** | **6/7** | **5/6** | **+13.75 %** | WEAK |
| rsi_reversal | 4h | 355 | +5.86 % | 10.32 % | 5.16 % | **−9.62 %** | 0.87 | −2.71 | 16.61 % | 3/7 | 2/6 | −23.97 % | REJECT |
| EMA (bench) | 4h | 448 | +30.88 % | 14.03 % | 7.01 % | **+9.84 %** | 1.08 | 2.19 | 14.86 % | 4/7 | 3/6 | −18.00 % | WEAK |
| Breakout (bench) | 4h | 374 | +32.75 % | 12.51 % | 6.25 % | **+13.99 %** | 1.15 | 3.92 | 8.41 % | 4/7 | 3/6 | −12.36 % | WEAK |
| regime_trend | 1d | 37 | +7.68 % | 0.51 % | 0.25 % | **+6.91 %** | 2.91 | 18.68 | 1.61 % | 4/7 | 1/6 | +3.54 % | WEAK |
| rsi_reversal | 1d | 70 | −0.74 % | 0.89 % | 0.44 % | **−2.07 %** | 0.87 | −2.96 | 4.39 % | 3/7 | 3/6 | −3.95 % | REJECT |
| EMA (bench) | 1d | 179 | +18.17 % | 2.58 % | 1.29 % | **+14.30 %** | 1.48 | 7.98 | 4.19 % | 4/7 | 4/6 | +2.47 % | WEAK |
| **Breakout (bench)** | **1d** | 109 | +14.43 % | 1.54 % | 0.77 % | **+12.12 %** | 1.67 | 11.10 | 4.31 % | **6/7** | **5/6** | **+8.19 %** | WEAK |

**1h — los costes se comen todo.** Tres de cuatro estrategias pagan en costes más que su bruto
entero (EMA: 120 % del capital en fees + slippage frente a +74 % bruto). La única positiva,
regime_trend, tiene PF 1.10 y **se vuelve negativa con solo duplicar las comisiones** (−4.11 %).
Confirma M14 con seis años y medio en vez de uno.

**4h — regime_trend es lo mejor del estudio.** PF 2.62, DD 2.71 %, positiva en 6 de 7 años, 5 de 6
en walk-forward y **sigue en +13.75 % bajo el stress combinado**. Pero son **79 trades en 6.7 años**
(≈ 12/año, 2 en 2022, 5 en 2025, 4 en 2026) y, a 1 % de riesgo por trade, +24 % en 6.7 años es
≈ 3.3 %/año. Los benchmarks a 4h son positivos en base pero **negativos bajo stress** (−12 % y −18 %):
no aguantan costes realistas.

**1d — los benchmarks mejoran; regime_trend se queda sin muestra.** Breakout20/10 diario: 109 trades,
PF 1.67, 6/7 años, 5/6 WF y +8.19 % bajo stress — lo más robusto a costes con muestra > 100. EMA
diaria: 179 trades, PF 1.48, pero solo 4/7 años. regime_trend diario hace 37 trades y ninguno en
2022, 2025 ni 2026.

**Veredicto: ninguna combinación llega a PAPER CANDIDATE, ni siquiera a PROMISING.** `judge` se
detiene en WEAK antes de que `cap_by_sample` tenga que actuar; y regime_trend 4h tampoco pasaría el
guardia de muestra (79 < 100 trades; 2 trades en el año de test 2022).

## 6. A vs C vs D

| Política | Mediana decisiones bloqueadas | Mediana tiempo bloqueado | Peor DD | Racha de pérdidas más larga | Efecto vs REF (12 estrategia × TF) |
|---|---|---|---|---|---|
| A — producción (racha 5, permanente + DD 20 %) | 94.11 % | **81.03 %** | 17.50 % | 5 | deja de operar 8 · sin efecto 2 · empeora 2 |
| **C — latch de drawdown 10 %, sin racha** | **0.00 %** | **0.00 %** | **10.92 %** | 20 | deja de operar 2 · sin efecto 8 · empeora 2 |
| D — racha con cooldown 24 h + DD 10 % | 29.15 % | 18.63 % | 10.91 % | 17 | deja de operar 3 · sin efecto 5 · empeora 4 |

* **A bloquea justo lo que funciona.** Breakout diario pasa de +12.12 % a +4.67 % (67 % del tiempo
  bloqueado); Breakout 4h de +13.99 % a +5.38 % (96 %). En EMA 4h/1d y rsi 4h deja 10–47 trades en
  6.7 años. Con regime_trend 4h cuesta poco (30.6 % bloqueado, −1.2 pp), porque rara vez encadena
  5 pérdidas.
* **C es idéntica a REF en cada combinación con edge** (regime_trend 4h/1d, Breakout 4h/1d, EMA
  1d): el latch nunca se dispara. Donde se dispara, corta estrategias perdedoras: rsi_reversal y EMA
  a 4h quedan **bloqueadas para siempre tras el drawdown de 2022** — es un kill switch, y así se
  comporta. Las dos que "empeora" (rsi y EMA a 1h) son REJECT en cualquier política.
* **D no aporta nada sobre C**: mismo peor DD (10.91 % frente a 10.92 %) con un 18.6 % de tiempo
  bloqueado y empeora 4 combinaciones, entre ellas Breakout 4h (+13.99 % → +6.00 %).

**¿Sigue justificándose C con más historia? Sí.** La regla de M14, sin cambiar: A queda excluida
(no operable: 81 % del tiempo bloqueada, deja de operar 8 de 12); C y D son seguras (peor DD ≤ 11 %);
C gana por tiempo bloqueado (0 % frente a 18.6 %). **Lo que C cuesta:** sin freno de racha, deja
pasar rachas de hasta 20 pérdidas (Breakout 1h) y descansa entero en el latch del 10 %; y ese latch
es permanente, así que exigiría un procedimiento de reset manual. Es una **recomendación de
research**; Risk V2 productivo no cambia.

## 7. Top 2 (regla declarada en §3: research, bajo A/C/D, una por estrategia × TF)

1. **regime_trend · 4h** — WEAK · +22.93 % (A) / +24.11 % (C, = REF) · 76–79 trades · PF 2.56–2.62 ·
   DD 2.71 % · WF 5/6 · 6/7 años · stress +13.75 %. El informe lista A porque A, C y REF empatan en
   veredicto, WF y DD; con C la combinación es exactamente REF.
2. **regime_trend · 1h · A** — WEAK · +1.14 % · 199 trades · PF 1.03 · WF 4/6 · negativa bajo fees ×2.
   Ocupa el puesto porque es la única otra combinación de research que no es REJECT: **es un segundo
   puesto débil, y lo digo así**. Fuera de la regla (es benchmark), Breakout20/10 diario la supera en
   todo.

## 8. Qué descartar

* **rsi_reversal, entera**: REJECT en los 3 timeframes y las 4 políticas; 0/7 años a 1h.
* **Todo 1h** para EMA, Breakout y rsi (costes > bruto), y **regime_trend 1h** como candidata: su
  edge no sobrevive a duplicar comisiones.
* **EMA y Breakout a 4h**: positivos en base, negativos bajo stress.
* **regime_trend 1d**: 37 trades en 6.7 años, tres años sin operar.
* **Política D**: sin ventaja sobre C, más bloqueo, más daño.
* **Política A como política para timeframes lentos**: bloquea 67–99 % del tiempo en los benchmarks.

**Lo que queda vivo:** regime_trend 4h (el mejor edge, muestra corta) y, como referencia,
Breakout20/10 diario (el más robusto con > 100 trades). Ninguno es PAPER CANDIDATE.

## 9. Limitaciones

* **1h en ventanas anuales** (motor cuadrático): el DD 1h es el peor año, cota inferior; el retorno
  1h compone siete corridas que arrancan con capital fresco; y **el latch de C/D se reinicia cada
  1 de enero**, así que a 1h C parece menos permanente de lo que es (a 4h/1d, corridas continuas, no).
* **Walk-forward diario**: cada año de test pierde ~73 días de warm-up, así que el WF 1d
  infravalora la actividad (regime_trend 1d: 0 trades en 4 de 6 folds). La estabilidad por año sale
  de la corrida continua; el WF diario es orientativo.
* **Stress y vecinos se corren por años**: su "base" (p. ej. regime_trend 4h +20.05 %) no es la
  cabecera continua (+24.11 %); se comparan entre sí, no con la cabecera.
* **Walk-forward sin ajuste**: con parámetros fijos, el año de train no se usa; mide estabilidad
  fuera de muestra, no una optimización.
* Un activo, un venue, un periodo 2020–2026 que incluye un ciclo completo; nada de esto prueba el
  futuro.

## 10. Próximo paso sugerido (no autorizado)

Nada va a paper. Para convertir regime_trend 4h en candidato hace falta **muestra, no ajuste**: más
activos con el mismo protocolo, o más historia. Y para correr 1h de verdad, arreglar el motor
cuadrático (histórico incremental) en un milestone propio. Adoptar C en Risk V2 productivo sería
una decisión aparte, con su procedimiento de reset.

## 11. Reproducibilidad desde árbol limpio

Re-ejecución desde el commit `185ca01` (árbol sin cambios; cada entrada nueva lleva la revisión sin
`-dirty`) de **todo el diario** (4 estrategias, 36 trabajos) y de **regime_trend 4h** (la
combinación de cabecera, 9 trabajos: las cuatro políticas continuas, los siete años con stress y
vecinos, y el walk-forward).

* **332 de 332 resultados almacenados idénticos** en todos los campos salvo `code_revision`, marcas
  de tiempo e identificadores del intento. 0 sin pareja.
* `research verify` sobre los 104 ledgers: **0 fallos de reproducibilidad**. Como en M13 y M14, la
  etiqueta `code_changed` solo dice que la revisión cambió (`result_hash` la incluye); la igualdad
  se prueba comparando contenidos.
* Las parejas se forman por `experiment_id` (hash de la definición), no por nombre: en los ledgers
  anuales, stress y vecinos comparten el nombre de su base (167 entradas), y emparejar por nombre
  cruza REF con sus variantes.
