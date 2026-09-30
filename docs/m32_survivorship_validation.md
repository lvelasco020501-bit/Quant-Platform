# M32 fase 1 — el edge no sobrevive el universo corregido: **NO-GO, línea cerrada**

**Estado:** FASE 1 COMPLETA · 30 mercados · 1D y 4H · **CS2 pasa la puerta y falla sensibilidad** · **RF1 falla cinco de siete condiciones** · **FASE 2 NO SE EJECUTA** · 2026-09-30

Pre-declaración commiteada en `0760e9e`, **antes** de ver un solo resultado. Las señales de CS2 y
RF1 vienen de M31 **por referencia** (que a su vez las toma de M30), así que M32 no reescribe ni
un lookback. La puerta tampoco se reescribe: `m32.survives` **es** `m31.survives`, comprobado por
identidad de función, para que este milestone no pueda juzgar el resultado de M31 con otro baremo.
El VPS no se tocó.

---

## 1. Qué estaba bien y qué no, dicho con precisión

El simulador **nunca fabricó historia**: `align()` construye una rejilla de unión y un activo sólo
es rankeable cuando tiene barra *y* histórico propio suficiente, así que SOL está ausente de todos
los rankings de 2019 y nada se rellena. El sesgo que quedaba no era historia inventada, era
**selección**: los seis eran los large caps *de 2026*. Los mercados que lideraron el ciclo de
2017-18 y se apagaron, y los dos que fueron top-diez y se fueron a cero, simplemente faltaban.

**La corrección:**

| | |
|---|---|
| pool declarado | **30 mercados**: los 6 de M30 + 15 que se apagaron + **2 que fueron a cero** + 7 posteriores |
| universo point-in-time | sólo los **6 más negociados en cada barra**, por mediana móvil de `volumen × cierre` sobre 72 barras |
| causalidad | la mediana no lee ninguna barra posterior a la que rankea; un mercado sin 72 barras propias no es rankeable |
| mediana, no media | un solo día frenético en un mercado fino no compra plaza |
| 6 y no otro número | es la amplitud de M30, así que esto prueba **cuáles** seis, no cuántos |

**Por qué el pool es una prueba conservadora y no amable:** el momentum rankea por retorno
reciente, así que es exactamente la regla que habría rotado a LUNA en abril de 2022 y a FTT antes
de noviembre de 2022. Y lo hizo: **LUNA se mantuvo en los dos timeframes y perdió dinero en los
dos** (−1 353 a 1D, −3 051 a 4H). La prueba muerde de verdad.

---

## 2. Dos trampas de datos encontradas, ambas corregidas

**LUNAUSDT es dos activos distintos bajo el mismo ticker.** Cae de 82 a 0.00005 (2022-05-13), se
detiene **437 horas**, y reaparece en **8.87** — el token post-fork reutilizando el símbolo.
Empalmado, eso es una barra ganando **+17 739 900%**: habría sido el mayor beneficio inventado del
dataset, dentro del milestone cuyo propósito es impedir que los datos halaguen a la estrategia. La
detección es genérica, no un parche: cualquier hueco largo que el precio salte por un factor ≥5 es
otro activo con el mismo nombre, y la serie se corta ahí. LUNAUSDT ahora termina 2022-05-13,
deslistado, que es lo que le pasó.

**El score de momentum no exigía contigüidad de calendario.** Pedía 72 *índices* de histórico, no
72 barras adyacentes en el tiempo. FTT estuvo **suspendido 311 días** tras la caída de FTX
(2022-11-15 → 2023-09-22) y sus barras a ambos lados del agujero son contiguas en el fichero, así
que un retorno de "72 barras" lo cruzaba. Ahora se exige contigüidad real para todos los mercados.

**Eso corrige cifras publicadas de M31 a 4H** (1D no cambia en absoluto, verificado byte a byte):

| | antes | después |
|---|---|---|
| RF1 fijo 25% · DD | 22.79% | **21.67%** |
| RF1 fijo 25% · Calmar | 1.35 | **1.40** |
| RF1 sin control · DD | 70.43% | 67.95% |

El veredicto de M31 se mantiene —ninguna celda cambia de lado y RF1 sigue pasando, mejor— pero
**una afirmación de sensibilidad de M31 era optimista**: la vecina `lookback 144` a 4H pasaba con
DD 34.50% y ahora falla con 36.62%. El vecindario de RF1 a 4H es **3 de 4**, no 4 de 4. Corregido
en `docs/m31_rotation_exposure_control_1d_4h.md`.

---

## 3. El dataset

24 mercados descargados de Binance Vision con SHA-256 verificado, 1h → 4h y 1d con el mismo
resampler de M16/M29/M30. **Los seis incumbentes NO se re-descargaron**: conservan la serie que
M16 validó, porque re-adquirirlas crearía una segunda linaje justo para los activos cuyas cifras
hay que reproducir.

| hallazgo | mercados |
|---|---|
| deslistados o suspendidos dentro de la muestra | **EOS** (2025-05), **XMR** (2024-02), **OMG** (2024-06), **MATIC** (2024-09), **LUNA** (2022-05) |
| reutilización de ticker detectada y cortada | **LUNA** |
| suspensión larga conservada como hueco real | **FTT** (311 días) |
| reparación de barras fuera de rejilla | LTC +42 barras, NEO +42 (el caso 2018-02-09 que M16 documentó) |

Estándar aplicado: el de M16 **salvo** que las horas ausentes no se confirman contra la API REST.
Se dice en el propio informe de datos. Los huecos largos se marcan como anomalías en vez de
tolerarse en silencio.

---

## 4. Resultado — CS2 fijo 25% a 1D

Tres corridas, para separar el efecto de la corrección del efecto de tener más mercados:

| | incumbentes (M31) | pool sin tope | **corregido top-6** |
|---|---|---|---|
| CAGR | +19.53% | +11.17% | **+15.42%** |
| max DD | 29.34% | 48.79% | **28.77%** |
| Calmar | 0.67 | 0.23 | **0.54** |
| profit factor | 2.20 | — | 1.73 |
| ×2 coste | +17.69% | — | +13.50% |
| ×3 coste | +15.88% | — | +11.61% |
| OOS total | +30.83% | — | **+54.63%** |
| OOS Calmar | 1.06 | — | **1.21** |
| cuota mejor activo | 0.40 | 0.47 | **0.28** |
| cuota mejor año | 0.38 | — | **0.32** |
| años positivos | 0.70 | — | **0.80** |
| mercados tocados | 6 | 27 | 20 |
| **puerta** | ✅ PASA | ❌ dd, calmar, año | ✅ **PASA** |

**La corrida de incumbentes reproduce M31 exactamente** — el script aborta si no, así que las rutas
y el cargador están verificados.

Dos cosas que merecen decirse sin adornos:

**El pool sin tope destruye la estrategia** (DD 48.79%, Calmar 0.23). Rankear 30 mercados a la vez
no es una corrección de supervivencia, es una estrategia distinta y peor. El tope point-in-time no
es un detalle técnico: es lo que hace que la corrección sea una corrección.

**En casi todas las dimensiones de robustez el universo corregido es MEJOR.** Concentración por
activo 0.40 → **0.28**. Años positivos 0.70 → **0.80**. Cuota del mejor año 0.38 → **0.32**. OOS
+30.83% → **+54.63%**. Sólo empeora en retorno (−4.11 pp de CAGR) y en Calmar (0.67 → 0.54). El
edge no era un artefacto de haber elegido seis ganadores.

### Contribución por activo (corregido, 20 mercados tocados, 13 positivos)

| activo | neto | cuota | | activo | neto | cuota |
|---|---|---|---|---|---|---|
| BTC | +7 368 | +27.7% | | LTC | +390 | +1.5% |
| BNB | +6 569 | +24.7% | | BCH | +75 | +0.3% |
| SOL | +5 804 | +21.9% | | TRX | −206 | −0.8% |
| ETH | +5 153 | +19.4% | | MATIC | −228 | −0.9% |
| DOGE | +2 025 | +7.6% | | NEO | −783 | −3.0% |
| ZEC | +1 854 | +7.0% | | **LUNA** | **−1 353** | **−5.1%** |
| LINK | +1 802 | +6.8% | | ETC | −1 379 | −5.2% |
| ADA | +1 754 | +6.6% | | AVAX | −1 472 | −5.5% |
| DOT | +1 053 | +4.0% | | XRP | −3 181 | −12.0% |
| EOS | +739 | +2.8% | | VET | +570 | +2.1% |

Año a año: +21.9%, −6.1%, +34.8%, +4.2%, +40.2%, −11.7%, +16.3%, +19.3%, +31.0%, +1.1%.

### Y aquí es donde se rompe: la sensibilidad

| sonda | CAGR | DD | Calmar | veredicto |
|---|---|---|---|---|
| **declarado: lookback 72 / 25%** | +15.42% | 28.77% | **0.54** | ✅ PASA |
| lookback 36 | +13.37% | **38.31%** | **0.35** | ❌ drawdown, low_calmar |
| lookback 144 | +7.27% | **36.86%** | **0.20** | ❌ drawdown, low_calmar, año |
| exposición 20% | +12.50% | 23.28% | 0.54 | ✅ PASA |
| exposición 30% | +18.23% | 34.07% | 0.53 | ✅ PASA |

**Las dos vecinas del lookback fallan.** Sobre el universo de seis incumbentes las cuatro pasaban.
El Calmar cae 0.54 → 0.35 → 0.20 al mover el parámetro que más importa: eso es un pico estrecho,
no una meseta. La dimensión de exposición sigue sana; la de señal no.

Una explicación parcial, que no uso para rescatar nada: la exigencia de contigüidad penaliza más a
los lookbacks largos sobre series con huecos, y el pool tiene muchas. Es un mecanismo legítimo, no
un bug, y no cambia el hecho medido.

---

## 5. Resultado — RF1 fijo 25% a 4H

| | incumbentes (M31) | pool sin tope | **corregido top-6** |
|---|---|---|---|
| CAGR | +30.38% | +33.08% | +17.44% |
| max DD | **21.67%** | 41.00% | **39.50%** |
| Calmar | **1.40** | 0.81 | **0.44** |
| ×2 coste | +18.74% | +19.81% | +7.16% |
| ×3 coste | +8.13% | +7.86% | **−2.22%** |
| OOS total | +6.23% | +3.83% | **−0.29%** |
| cuota mejor año | 0.40 | 0.59 | **0.66** |
| años positivos | 0.70 | 0.70 | 0.60 |
| **puerta** | ✅ PASA | ❌ dd, año | ❌ **drawdown, low_calmar, año, cost_fragile, oos_negative** |

**Falla cinco de las siete condiciones.** No es un fallo marginal: el drawdown se va a 39.50%, el
Calmar a 0.44, el ×3 se vuelve negativo y el OOS también. Las cuatro sondas de sensibilidad fallan
igualmente.

Peores contribuyentes: ZEC −9 913, TRX −4 367, **LUNA −3 051**, LTC −2 256, NEO −1 482.

Año a año: +63.4%, −11.7%, +16.0%, +59.6%, +80.8%, −19.7%, +10.2%, +35.1%, **−22.6%**, −4.4%.
Los dos últimos años son negativos, consistente con el OOS negativo.

---

## 6. Veredicto

Tu regla de fase 1: *"Repetir: CAGR, DD, Calmar, OOS, stress, yearly consistency, concentration,
**sensitivity**, contribution by asset. Si cualquiera deja de pasar: STOP y reportar NO-GO."*

| candidata | puerta de siete condiciones | sensibilidad | fase 1 |
|---|---|---|---|
| CS2 fijo 25% · 1D | ✅ PASA | ❌ **2 de 4 vecinas fallan** | ❌ **NO-GO** |
| RF1 fijo 25% · 4H | ❌ falla 5 de 7 | ❌ 4 de 4 fallan | ❌ **NO-GO** |

# NO-GO

**FASE 2 NO SE EJECUTA.** Estaba condicionada a que la fase 1 pasara, y no pasa. El motor de
producción no se toca, no se porta nada, y no se ajusta ningún parámetro — ni el lookback, ni la
exposición, ni un umbral.

**La línea de rotación queda cerrada.**

Ningún PAPER CANDIDATE. Ningún umbral rebajado. Ninguna señal cambiada.

---

## 7. Lo que queda establecido, que no es poco

1. **El edge de rotación no era puro artefacto de selección.** Con el universo corregido, CS2 a 1D
   sigue pasando las siete condiciones y **mejora** en concentración por activo, consistencia
   anual y OOS. Eso es información real y sobrevive a este NO-GO.
2. **Lo que mata a CS2 es fragilidad de parámetro, no supervivencia.** El Calmar cae de 0.54 a
   0.20 moviendo el lookback. Un edge que sólo existe en 72 barras no es un edge del que se pueda
   vivir.
3. **RF1 a 4H era, en buena parte, selección.** Pasaba con seis ganadores elegidos en 2026 y se
   cae a cinco fallos con el universo que existía.
4. **Rankear muchos mercados a la vez es peor, no mejor.** El pool sin tope hunde ambas candidatas.
   La liquidez point-in-time no es un tecnicismo del método: es parte del edge.
5. **Dos defectos de datos que habrían inflado resultados quedan corregidos** en el simulador, no
   sólo en este milestone: el empalme de tickers y la contigüidad de calendario.

---

## 8. Limitaciones

1. **El pool sigue siendo un acto de 2026.** Contiene lo que hoy se recuerda como significativo y
   nada que Binance haya borrado del archivo, ni nada de fuera de Binance. Un universo realmente
   imparcial se reconstruiría de un snapshot de listings point-in-time, que este proyecto no tiene.
   Es una corrección grande en la dirección honesta, no una prueba de su ausencia.
2. **BCHUSDT empieza en 2019-11**, no en 2017: antes cotizaba como `BCHABCUSDT`. BCH fue top-cinco
   en 2017-18 y esa parte de su historia no está en el pool.
3. **Las horas ausentes de los 24 mercados nuevos no se confirmaron contra REST**, a diferencia de
   los seis de M16. Declarado en el informe de datos.
4. **La liquidez es `volumen × cierre`**, no quote volume real: el CSV canónico de este proyecto no
   lleva quote volume desde M10c. Es un proxy correcto y comparable entre mercados, no la cifra del
   exchange.
5. **La regla de contigüidad no se aplica al ranking de liquidez**, sólo a los scores. Una mediana
   de volumen sobre una ventana con huecos sigue siendo un nivel razonable; un retorno sobre una
   ventana con huecos no lo es.
6. **Fase 2 no se ejecutó**, así que sigue sin comprobarse que estos números no dependan del
   simulador nuevo. El NO-GO no depende de esa comprobación, pero tampoco la sustituye.
7. **Sin walk-forward**, igual que en M31: no estaba entre las condiciones declaradas.

---

## 9. Reproducir

```bash
uv run python scripts/m32_dataset.py                             # 24 mercados, ~97 s con caché
uv run python scripts/m32_phase1.py --timeframe 1d --sensitivity  # ~102 s, 229 MB pico
uv run python scripts/m32_phase1.py --timeframe 4h --sensitivity  # ~155 s, 1.16 GB pico
```

Evidencia en `var/research/m32/phase1_1d.json` y `phase1_4h.json`. Ambos scripts abortan si la
corrida de incumbentes deja de reproducir M31 exactamente.
