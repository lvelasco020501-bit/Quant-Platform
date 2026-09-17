# M16 — ¿regime_trend 4H funciona fuera de BTC?

**Estado:** COMPLETADO · research/backtesting únicamente · 2026-09-16

Nada de paper, Risk V2 productivo, execution, portfolio ni las estrategias productivas cambió.
El registro de paper sigue siendo `['breakout', 'ema_trend']`.

## 1. La pregunta

M15 dejó una sola combinación en pie: `regime_trend` a 4H bajo la política de research C, positiva
sobre seis años y medio de BTC, con profit factor 2.62 y drawdown 2.71% — **sobre 79 trades**. Dos
explicaciones encajan igual de bien con esa evidencia:

1. hay un edge real de seguimiento de tendencia filtrado por régimen, o
2. hay una estrategia que se ajustó, a lo largo de M13–M15, a la historia de **un solo activo**.

Solo otros mercados distinguen una de otra. Por eso M16 **no cambia nada más que el mercado**.

## 2. Qué se mantiene idéntico (y está fijado por tests)

| Elemento | Valor | Cómo se garantiza |
|---|---|---|
| Estrategia | `regime_trend`, parámetros canónicos de M13 (`lookback=72`, `er_window=72`, `er_min=0.30`) | Un test compara con `CANDIDATES` de M13 y exige que las seis definiciones tengan el **mismo** `StrategySpec` |
| Timeframe | 4H, resampleado determinista desde 1H | Igual que M15; el digest se recalcula dos veces por activo |
| Riesgo | Conversión 4H de M15 sobre Risk V2 productivo, política **C** (latch de drawdown 10%, sin racha) | Un test compara campo a campo con `risk_for_timeframe(...)` y exige la **misma** configuración de riesgo en los seis |
| Costes | Comisión 10 bps, slippage 5 bps, spread 2 bps | Un test exige el mismo `execution_policy` en los seis, idéntico al de producción |
| Protocolo | Walk-forward (entrenar un año, testear el siguiente), 3 escenarios de stress, 2 vecinos declarados | Los mismos que M13–M15 |

**Política C es una variante de research, no Risk V2.** Risk V2 productivo (política A) se corre
aparte, porque el veredicto lo exige (§4), no como candidata.

Lo único que cambia legítimamente por activo: el mercado, sus reglas de venue (tick, lote, mínimo
nocional — capturadas una vez y clavadas en la definición) y cuánta historia tiene el venue.

**No se optimizó nada.** No hay grid search, no hay selección de parámetros por activo, y los dos
vecinos son un chequeo de robustez declarado en M13, no una búsqueda.

## 3. Activos y ventanas

Seis mercados spot contra USDT: BTC, ETH, BNB, SOL, XRP, ADA.

Cada activo arranca en su **primer mes completo** de historia en Binance Vision (el primer archivo
mensual es el mes parcial del listado, y se descarta). Todos terminan en la misma barra que M15
(2026-09-14T20:00Z), para que ningún mercado tenga una ventana reciente más larga que otro.

Se reportan **dos ventanas**, y la diferencia importa:

* **La historia propia de cada activo** responde "¿existe el edge aquí?";
* **La ventana común** (desde el arranque de SOL) responde "¿existe al mismo tiempo?" — la única
  comparación en la que el mercado alcista de un activo no puede hacerse pasar por el de otro.

## 4. Veredicto: la misma regla de siempre

`judge` de M13, y encima `cap_by_sample` de M15: **PAPER CANDIDATE exige ≥ 100 trades en la corrida
del propio mercado y ≥ 5 en cada año de test del walk-forward.** Ese mismo `judge` niega PAPER
CANDIDATE a cualquier cosa que no supere a **los dos benchmarks** y no quede positiva bajo **Risk V2
desplegado**. Por eso cada activo corre también los dos benchmarks (EMA 20/50 y Breakout 20/10, las
copias de research a 4H) y una corrida bajo política A: sin esas corridas el milestone no podría
responder si esto es candidato a paper. No son exploración.

**La muestra agrupada (pooled) se reporta pero no otorga veredicto.** Sumar los trades de los seis
mercados responde "¿ya hay muestra suficiente?", que es una pregunta sobre la estrategia; el
veredicto sigue siendo por mercado, porque un promedio esconde al activo que cargó con todo.

## 5. Qué se descarta — regla fijada antes de ver resultados

Un activo se descarta si, sobre su propia historia bajo política C:

1. no operó nunca, o
2. perdió dinero, o
3. tuvo profit factor por debajo de 1, o
4. quedó negativo bajo el stress de costes (fees ×2 · slippage ×3 · ambos + spread 5 bps).

Implementada en `discard_reason`, con tests.

## 6. Dataset

### 6.1 Resultado

Cero anomalías en los seis activos. Todos terminan en la misma barra (2026-09-14T20:00Z).

| Activo | Desde | Barras 1H | Horas ausentes | Archivos | Barras 4H | vs 4H oficial de Binance |
|---|---|---|---|---|---|---|
| BTC/USDT | 2017-09 | 79 097 | 127 | 108m + 14d, **122 checksums** | 19 790 | 19 782/19 789, **0 sin explicar** |
| ETH/USDT | 2017-09 | 79 097 | 127 | 108m + 14d, 122 checksums | 19 790 | 19 782/19 789, 0 sin explicar |
| BNB/USDT | 2017-12 | 76 919 | 121 | 105m + 14d, 119 checksums | 19 244 | 19 238/19 244, 0 sin explicar |
| SOL/USDT | 2020-09 | 52 901 | 19 | 72m + 14d, 86 checksums | 13 230 | 13 226/13 230, 0 sin explicar |
| XRP/USDT | 2018-06 | 72 585 | 87 | 99m + 14d, 113 checksums | 18 159 | 18 154/18 159, 0 sin explicar |
| ADA/USDT | 2018-05 | 73 329 | 87 | 100m + 14d, 114 checksums | 18 345 | 18 340/18 345, 0 sin explicar |

**Digests 4H:** BTC `1663dd686f3f224187600777d1618390` · ETH `17172810f92774ea3083214bd735cf1c` ·
BNB `2598f4549f930add65999f83af7aa196` · SOL `ad8e40b983bda8b557e23cadbd993a93` ·
XRP `e750292225f2176355f6fc87c4c1828a` · ADA `75b1e6cb70926af10e4e9a66164d32c0`

**Prueba de que el pipeline generalizado es el de M15.** Las barras 4H de BTC sobre la ventana
exacta de M15 reproducen su digest registrado: `360b9b605f945d85f7707f87736d5add`, 14 693 barras.
Bit por bit.

### 6.2 Tres defectos reales encontrados en los archivos

1. **Filas fuera de rejilla (2018-02-09, los tres activos que ya existían).** Tras una caída del
   exchange, el archivo **mensual** reinicia la rejilla horaria 28 minutos tarde (43 filas). No se
   ajustan al alza ni a la baja: se **rechazan**, y esos días se vuelven a pedir al archivo
   **diario**, que sí está alineado y trae precios distintos. Ajustarlas habría inventado barras
   que el exchange nunca publicó.
2. **Horas que el mensual no tiene.** Se rellenan desde el archivo diario, también con checksum.
   La cobertura viene siempre de archivos verificados; la API REST se usa como **evidencia sobre**
   cada hueco, nunca como parche: en 2017-09-06 sirve barras que ni el archivo ni la propia serie
   4H de Binance tienen, así que rellenar desde ahí habría fabricado barras lentas inexistentes.
   Tras la reparación, **las 568 horas ausentes de los seis activos están ausentes también en la
   API REST**: son caídas del exchange.
3. **Horas sin ningún trade.** Binance excluye las horas sin operativa al construir sus barras
   lentas; nuestro resampler conserva la barra de volumen cero que el venue publicó. De ahí las
   pocas diferencias con su serie 4H oficial, todas clasificadas mecánicamente y **ninguna sin
   explicar**. No se cambió el resampler: sus digests están comprometidos y fijados desde M15.
   Entre 1 y 3 barras 4H por activo abren en una hora de volumen cero (se reporta el número).


## 7. Resultados

### 7.1 La tabla

| ASSET | TRADES | NET | PF | DD | WF | STRESS | EMA BENCHMARK | VERDICT |
|---|---|---|---|---|---|---|---|---|
| BTC/USDT | 124 | +34.32% | 2.13 | 4.41% | 8/9 | +17.02% | +7.76% ✅ | WEAK |
| ETH/USDT | 146 | +18.59% | 1.39 | 5.49% | 7/9 | +7.87% | −4.89% ✅ | **PROMISING** |
| BNB/USDT | 129 | +24.84% | 1.58 | 7.10% | 4/9 | +15.18% | +6.83% ✅ | WEAK |
| SOL/USDT | 106 | +23.01% | 1.60 | 3.94% | 5/6 | +17.25% | +44.57% ❌ | **PROMISING** |
| XRP/USDT | 27 · LOW SAMPLE | −2.95% | 0.79 | 10.14% | 6/8 | −6.01% | −9.10% ✅ | REJECT |
| ADA/USDT | 139 | +4.40% | 1.08 | 6.02% | 4/8 | −1.35% | +22.75% ❌ | WEAK |

NET, PF, DD y STRESS son política C sobre la historia propia de cada mercado; STRESS es el peor de
los tres escenarios de costes. ✅/❌ es si regime_trend **superó al benchmark EMA en ese mismo
mercado** — pregunta distinta de "¿fue positivo?".

### 7.2 Las respuestas

1. **¿Funciona en varios activos?** Sí: es rentable en **5 de 6**. No es un artefacto de BTC.
2. **PF > 1:** en **5 de 6** (XRP es el único por debajo, 0.79).
3. **Sigue positivo bajo stress de costes:** en **4 de 6** (caen ADA y XRP).
4. **Supera al benchmark EMA en el mismo mercado:** en **4 de 6**. Pierde contra EMA justo donde
   el activo tuvo una tendencia enorme (SOL +44.57%, ADA +22.75%): un filtro de régimen se pierde
   parte del movimiento que un cruce de medias simplemente cabalga.
5. **Muestra total:** **671 trades** en 6 mercados (5 de 6 superan 100 trades por sí solos).
   Pooled PF 1.45 sobre historias propias, **1.75 sobre la ventana común**.
6. **¿Dónde falla claramente?** En **XRP** — pero por la razón equivocada (§7.3). En **ADA** el
   fallo es real y económico: +4.40% en ocho años, PF 1.08, y **negativo en cuanto suben los
   costes**. El edge de ADA no paga los costes.
7. **¿El agregado justifica PROMISING?** **Sí, como familia.** 5/6 rentables, PF agrupado 1.45,
   4/6 sobreviven al stress, 6/6 positivos en la ventana común, y dos mercados (ETH, SOL) alcanzan
   PROMISING por su cuenta con el contrato de siempre. Lo que M15 no podía distinguir —edge real
   vs. sobreajuste a BTC— queda resuelto a favor del edge.
8. **¿Justifica PAPER CANDIDATE?** **No.** Y no por poco:
   * **ningún mercado** tiene ≥ 5 trades en *cada* año de test del walk-forward (BNB llega a tener
     un año con 0, ADA con 1, BTC con 2): `cap_by_sample` lo impide por diseño;
   * en 2 de 6 **pierde contra el benchmark EMA**, y `judge` exige superar a ambos benchmarks;
   * bajo **Risk V2 desplegado** (política A) BNB da −2.80% y ADA −2.93%;
   * y el riesgo con el que se midió todo esto tiene el problema de §7.3.

### 7.3 Top finding: el latch permanente de C es discontinuo

XRP no reprobó por falta de edge. Su latch de drawdown saltó **una vez** —
`excessive_drawdown`, 2020-07-29T08:00Z— y dejó el mercado sin operar para siempre: 27 trades,
0 trades de 2021 en adelante, **73.93% del tiempo bloqueado**.

Lo revela el propio stress:

| XRP/USDT | TRADES | NET |
|---|---|---|
| base | 27 | −2.95% |
| fees ×2 | **146** | **+41.96%** |
| slippage ×3 | **146** | **+42.67%** |
| fees ×2 + slippage ×3 + spread 5 bps | 22 | −6.01% |

Con **costes más altos** el camino del equity cambia lo justo para que el latch **no** salte, el
mercado sigue operando y el resultado pasa de −3% a +42%. No es que cobrar más comisiones mejore
la estrategia: es que **un latch permanente convierte un resultado continuo en uno discontinuo**,
decidido por qué lado de un umbral pasó el equity un martes de julio de 2020. La misma XRP sobre
la ventana común (§4 del reporte), que empieza después de ese momento, da **+53.75% con 113
trades**.

M14 y M15 recomendaron C con evidencia de BTC, donde **nunca saltó** (aquí tampoco: 0.00% de
tiempo bloqueado en 5 de 6 mercados). M16 encontró el caso en el que sí salta, y es feo.
**Consecuencia:** antes de llevar esto a paper hay que revisar el latch permanente —
probablemente un reset por período o por régimen— y eso es un milestone propio, no un ajuste.

### 7.4 Qué descartar

Por la regla fijada de antemano (§5):

* **ADA/USDT — descartar.** Negativo bajo stress de costes; +4.40% en ocho años no paga el riesgo.
* **XRP/USDT — descartar de la evidencia actual, reabrir con el latch corregido.** Con la regla
  tal como está escrita, perdió dinero sobre su propia historia y se descarta. Pero su número real
  está contaminado por §7.3, así que **no** es evidencia de que XRP no sirva.
* **Conservar:** BTC, ETH, BNB, SOL.

### 7.5 Lo que sigue en pie

* **BTC ya no es el único caso.** Sobre 2017-09 en adelante: 124 trades, +34.32%, PF 2.13, DD
  4.41%, 8/9 años de walk-forward positivos, +17.02% con los costes triplicados.
* **La ventana común es la comparación más limpia** y es la más favorable: 6/6 mercados positivos,
  571 trades, PF agrupado 1.75.
* **Los costes son aceptables a 4H**: entre 1.18% y 7.17% del capital en toda la historia, y cuatro
  mercados siguen positivos con fees ×2, slippage ×3 y 5 bps de spread encima.
* **La muestra por mercado sigue siendo el cuello de botella**: 100-146 trades en 8-10 años son
  ~15 trades al año. El walk-forward anual, con 0-6 trades en varios años de test, no puede
  sostener un veredicto de PAPER CANDIDATE, y `cap_by_sample` lo dice explícitamente.

### 7.6 GO / NO-GO

* **GO** — el edge existe fuera de BTC y sobrevive a costes. La línea de investigación continúa.
* **NO-GO a paper.** Ni PAPER CANDIDATE ni despliegue. Faltan: arreglar el latch permanente
  (§7.3), y muestra por mercado, que solo llega con más activos o más frecuencia — no con más
  ajuste de parámetros.

## 8. Tests, gate y reproducibilidad

* **Tests nuevos: 33.** 18 fijan el protocolo (los seis activos, sus ventanas, y que estrategia,
  riesgo y costes son **idénticos** en los seis); 15 cubren el lector de Binance Vision (unidades
  de timestamp, duplicados, conflictos, huecos agrupados, OHLCV, CSV determinista). Suite completa:
  **2 358 pasan**.
* **Gate completo, verde:** `ruff format --check`, `ruff check`, `mypy src` limpio, `mypy .` en su
  línea base de **108 errores / 10 archivos** (sin errores nuevos), `pytest`, `docker compose
  config`, `git diff --check`.
* **Ledgers:** 42 verificados, **0 fallos de reproducibilidad**.
* **Re-ejecución desde árbol limpio** (commit `4cf48d3`, SOL: política C y walk-forward):
  **13 de 13 resultados idénticos** campo a campo salvo revisión, marcas de tiempo e
  identificadores del intento. 0 sin pareja. Las parejas se forman por `experiment_id`.
* **Nada se desplegó.** No se tocó la sesión PAPER en curso, ni Risk V2 productivo, ni execution,
  ni portfolio, ni las estrategias productivas.
