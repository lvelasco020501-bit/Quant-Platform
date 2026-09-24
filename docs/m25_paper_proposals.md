# M25 — Dos propuestas de paper, y por qué ninguna puede lanzarse hoy

**Estado:** PROPUESTAS LISTAS · **NO-GO para lanzar ambas** · 2026-09-24

No se tocó nada. Este documento es diseño y verificación; no se inició ninguna sesión, no se
modificó configuración y no se alteró la sesión de paper existente.

## 0. El bloqueador, antes que nada

**Ni `breakout_trend` (B2) ni `regime_trend` (RT) pueden ejecutarse en paper hoy.** No es una
cuestión de evidencia ni de configuración: la plataforma lo impide por construcción.

Paper resuelve estrategias por `build_default_registry()`, que contiene exactamente dos:

```
BUILTIN_STRATEGIES = ema_trend, breakout
```

Las once estrategias de research viven solo en `build_research_registry()`, y un test lo fija
con `assert len(paper) == 2` y este comentario:

> *The paper runner resolves strategies through the default registry. A research rule that
> leaked into it would be one mistyped flag away from a live session.*

El módulo `strategies/research.py` lo declara como decisión deliberada: *"Promoting one to
paper is a separate decision with its own review, never a side effect of having been tested."*

**Esa revisión es exactamente lo que este documento pide, y es tuya.** Lanzar cualquiera de las
dos requiere un cambio de código en `BUILTIN_STRATEGIES` más actualizar ese test — pequeño,
revisable, y que debe hacerse a propósito y no como efecto lateral.

## 0b. El segundo hallazgo, que pesa más que el primero

**A la frecuencia que estas reglas operan bajo Risk V2 desplegado, una sesión de paper no puede
producir evidencia de edge en ningún plazo razonable.**

| | trades/año bajo Risk V2 | para 10 trades | para 30 trades (muestra mínima) |
|---|---|---|---|
| B2 / BTC | 7.85 | ~15 meses | **~3.8 años** |
| RT / ETH | 8.63 | ~14 meses | **~3.5 años** |

En 90 días de paper, B2 haría **~1,9 operaciones** y RT **~2,1**. Con dos trades no se valida
una estrategia: se valida que el sistema no se rompe.

Esto no invalida las propuestas, pero **redefine para qué sirve la sesión**: para probar
mecánica operativa (feed, órdenes, riesgo, reinicios, observabilidad), no para confirmar edge.
Cualquier expectativa distinta lleva a leer ruido como señal a los dos meses.

---

## 1. Propuesta A — B2 sobre BTC/USDT 4H

| | |
|---|---|
| **Estrategia** | `breakout_trend`, congelada |
| **Parámetros** | `entry_lookback=40`, `exit_lookback=20`, `trend_period=400` |
| **Símbolo / timeframe** | BTC/USDT spot · 4H |
| **Procedencia** | M13 canónicos → duplicados mecánicamente en M22 → validados en M23 |
| **Veredicto** | PAPER CANDIDATE en BTC (M23, confirmado en M24) |
| **session_id** | `paper-b2-btc-4h-w1` |
| **state-dir** | `var/state/b2-btc/` — **directorio propio, ver §3** |
| **Capital inicial** | 10.000 USDT |
| **Warm-start** | **Requerido: 401 barras** (`sma_400` necesita 400 + la barra actual) = **67 días** de 4H |

**Reglas de entrada/salida.** Entra cuando el máximo de la barra supera el máximo de las 40
barras previas **y** el cierre está por encima de la SMA de 400. Sale cuando el mínimo perfora
el mínimo de las 20 barras previas, **sin** esperar al filtro de tendencia. Largo-only, una
posición a la vez.

**Riesgo (conversión 4H de M15, NO el Risk V2 desplegado a 1H):**

| | 1H desplegado | 4H que usaría |
|---|---|---|
| stop inicial | 300 bps | **600 bps** |
| take profit | 600 bps | **1200 bps** |
| break-even | 150 bps | **300 bps** |
| holding máximo | 168 barras | **42 barras (7 días)** |
| max_volatility | 0.15 | **0.30** |
| drawdown total (latch) | 20% | 20% |
| pérdida diaria | 3% | 3% |
| pérdidas consecutivas (latch) | 5 | 5 |
| riesgo por trade | 1% | 1% |

**Costes:** fees 10 bps, slippage 5 bps, spread asumido 2 bps. Los mismos con los que se validó.

**Criterios de salud (observables desde el día 1):**
* el feed entrega una barra cada 4h sin stalls más allá del margen configurado;
* `bars_processed` crece monótonamente y el lock nombra este PID;
* cero errores de contrato de estrategia o de reglas de símbolo;
* drawdown observado ≤ 10% (su máximo histórico bajo Risk V2 fue 2.87%);
* ningún breaker con latch disparado.

**Criterios de stop (parar y revisar):** cualquier breaker latcheado; drawdown > 12%; dos
reinicios no planificados; discrepancia entre el estado persistido y el log; o el watchdog
reportando stall dos veces seguidas.

**Criterios de fracaso (cerrar la línea):** drawdown > 20% (el latch productivo); o
comportamiento de órdenes que no corresponde a la regla — una entrada sin ruptura, una salida
sin perforación — que es un fallo de integración, no de edge.

**Duración mínima:** 90 días para validar **mecánica**. Para evidencia de edge, ver §0b.

**Evidencia mínima antes de considerar promoción:** ≥30 trades cerrados, drawdown dentro de lo
validado, cero incidentes de integridad, y reconciliación limpia tras al menos un reinicio
deliberado. A 7.85 trades/año eso son **años, no meses**.

---

## 2. Propuesta B — regime_trend sobre ETH/USDT 4H

| | |
|---|---|
| **Estrategia** | `regime_trend`, congelada |
| **Parámetros** | `lookback=72`, `er_window=72`, `er_min=0.30` |
| **Símbolo / timeframe** | ETH/USDT spot · 4H |
| **Procedencia** | M13 canónicos, sin cambios desde M14/M15/M16 |
| **Veredicto** | PAPER CANDIDATE en ETH (M24) |
| **session_id** | `paper-rt-eth-4h-w1` |
| **state-dir** | `var/state/rt-eth/` — **directorio propio, ver §3** |
| **Capital inicial** | 10.000 USDT |
| **Warm-start** | **Requerido: 73 barras** (`roc_72` y `er_72` necesitan 72 + 1) = **~12 días** de 4H |

**Reglas de entrada/salida.** Entra cuando el retorno de 72 barras es positivo **y** el
efficiency ratio de 72 barras es ≥ 0.30 — es decir, solo si el mercado se mueve de forma
direccional y no en serrucho. Sale cuando el retorno de 72 barras se vuelve negativo, **sin**
esperar al régimen. Largo-only, una posición a la vez.

**Riesgo:** idéntico al de la Propuesta A — misma tabla, misma conversión 4H, mismos costes.
Esto es deliberado: las dos se validaron bajo la misma configuración y cambiarla en una sola
rompería la comparabilidad que costó M24 construir.

**Configuración adicional que requiere:** `risk.allowed_symbols` incluye hoy solo `BTC/USDT`, y
un validador rechaza un símbolo de mercado que no esté en esa lista. **ETH/USDT hay que añadirlo
explícitamente.**

**Criterios de salud, stop y fracaso:** los mismos que la Propuesta A, con dos umbrales propios
por su perfil medido: drawdown observado ≤ 8% en salud (su máximo bajo Risk V2 fue 4.19%), y
stop a > 10%.

**Duración mínima:** 90 días para mecánica. Evidencia de edge, ver §0b.

**Una advertencia específica de RT.** Su virtud es la selectividad y su riesgo es la misma cosa:
en el walk-forward de BNB tuvo **un año natural con cero operaciones**. Una sesión de paper de
RT puede pasar semanas sin operar sin que eso signifique que algo va mal. Hay que distinguir
"no encuentra régimen" de "está colgada", y el único modo es mirar `bars_processed` y las
decisiones rechazadas, no el contador de trades.

---

## 3. Comparación operativa (no ranking)

| | B2 / BTC | RT / ETH |
|---|---|---|
| **Frecuencia esperada** | 7.85 trades/año (~0.65/mes) | 8.63 trades/año (~0.72/mes) |
| **Exposición** | ~20% del tiempo | ~7% del tiempo |
| **Riesgo medido (RV2)** | DD 2.87%, PF 1.48 | DD 4.19%, PF 1.39 |
| **Estabilidad ante costes** | retiene 34% en el peor escenario | retiene 43% |
| **Estabilidad ante parámetros** | PF vecino mín. 1.30 | PF vecino mín. 1.21 |
| **Warm-start** | **401 barras (67 días)** | **73 barras (12 días)** |
| **Observabilidad** | más trades por unidad de tiempo → señal operativa antes | períodos largos sin operar son normales y hay que saber leerlos |
| **Infraestructura** | idéntica: un feed 4H, un state-dir, un lock, un log, reportes diarios | idéntica |

La diferencia operativa que más pesa es el **warm-start**: B2 necesita 67 días de historia
continua antes de poder emitir su primera señal; RT, 12. Eso cambia el coste de arrancar y el
de recuperarse de una parada larga.

### Aislamiento — lo que exige el código, no lo que sería buena idea

`SessionLock` mantiene **una sesión por directorio de estado**. Su docstring documenta un
incidente real: dos procesos `paper run` vivos 18 horas escribiendo en el mismo `var/`,
entrelazando un log, dejando otro vacío y colisionando en el reporte diario — con el dashboard
mostrando como viva la *más antigua* de las dos.

Por tanto, **`session_id` distintos no bastan**. Cada sesión necesita:

* su propio `--state-dir`,
* su propio `--log-dir`,
* su propio `--reports-dir`.

Con eso no comparten lock, estado financiero, posiciones ni reportes.

---

## 4. Checklist pre-lanzamiento

| # | Comprobación | Estado |
|---|---|---|
| 1 | Risk V2 productivo intacto | ✅ `git status` sobre `risk/` vacío; último commit `f7d586f`, anterior a todo M22–M24 |
| 2 | Sin procesos huérfanos | ✅ 0 procesos python vivos (se limpiaron dos de 8h a petición tuya) |
| 3 | Máquina sana | ⚠️ carga 3.67, **918 MB de swap libre** — justo, aunque una sesión de paper consume mucho menos que un backtest |
| 4 | Mission Control apunta a la sesión correcta | ⚠️ **verificable solo al lanzar**: el lock nombra sesión y PID, y el dashboard lo lee; hay que comprobarlo tras arrancar, no antes |
| 5 | Warm-start solo restaura market context | ✅ **verificado en la fuente**: `warm_start()` escribe `history`, `last_close` y el ancla; no toca balances, posiciones, órdenes ni breakers, y registra `financial_state_restored: False` |
| 6 | Restart = no | ✅ nada reinicia automáticamente; no hay supervisor ni cron |
| 7 | No automatic resume | ✅ `resume: _ResumeOption = False` — el valor por defecto es `--fresh` |
| 8 | PAPER only | ✅ `execution_mode: paper`; ningún componente de esta ruta abre un socket de trading real |
| 9 | **Estrategias registrables en paper** | ❌ **BLOQUEADO** — ver §0 |
| 10 | `allowed_symbols` incluye ETH/USDT | ❌ solo `BTC/USDT` hoy |
| 11 | Fuente de warm-start disponible | ❌ **ver abajo** |

**Sobre la comprobación 11.** La última sesión, `paper-7c-week5`, quedó guardada el 2026-08-28
**con 1 posición abierta** y −106.95 de realized_pnl. `evaluate_warm_start` **rechaza** la
historia de una sesión que carga financial state, y su razón es exacta:

> *starting fresh from its candles would present an unreconciled account as a recovery: those
> books are still open and need closing by a person, not by a restart*

Así que **hay libros abiertos que alguien tiene que cerrar**, y hasta entonces esa sesión no
puede servir de fuente de warm-start. Las dos sesiones nuevas tendrían que arrancar en frío y
esperar su warm-up completo: 67 días para B2, 12 para RT.

---

## 5. Riesgos operativos

1. **Promoción al registro de paper.** Es el cambio que desbloquea todo y también el que el
   proyecto diseñó para que costara: un error aquí pone una regla de research a un flag de
   distancia de una sesión viva. Debe ir en su propio commit, con el test actualizado a
   propósito y revisado.
2. **La configuración de riesgo 4H nunca se ha desplegado.** Es la conversión de M15, usada en
   research desde M15 hasta M24. Desplegarla es en sí mismo un cambio productivo, no un
   detalle de arranque.
3. **Dos sesiones concurrentes triplican la superficie de operación** — dos feeds, dos locks,
   dos árboles de estado, dos juegos de reportes — sobre una máquina que ya se quedó sin swap
   dos veces esta sesión.
4. **Los requisitos que M21 dejó escritos siguen sin implementarse:** persistencia del latch
   entre reinicios, reconciliación al arranque, `clientOrderId` idempotente. En paper el coste
   de no tenerlos es bajo; antes de dinero real, no.
5. **Riesgo de interpretación, y es el más probable.** Con ~2 trades por trimestre, la
   tentación de concluir algo a los 60 días va a ser fuerte. No habrá nada que concluir.

---

## 6. Duración recomendada de observación

* **Mecánica: 90 días.** Suficiente para feed, reinicios, reportes, watchdog y observabilidad.
* **Edge: no medible en paper a este timeframe.** Necesitaría ~3,5–3,8 años por estrategia.

Si lo que quieres es evidencia de edge en un plazo humano, las alternativas honestas son
timeframes más rápidos (con su propio milestone de validación, porque 1H nunca se validó para
estas reglas), o aceptar que la evidencia ya la tienes en backtest y que el paper solo valida
la integración.

---

## 7. GO / NO-GO

| | Veredicto |
|---|---|
| **B2 / BTC 4H** | **NO-GO para lanzar hoy** |
| **RT / ETH 4H** | **NO-GO para lanzar hoy** |

En ambos casos por la **misma razón, y no es la evidencia**: la plataforma no puede resolver
ninguna de las dos estrategias en paper, por una barrera deliberada que existe precisamente
para que esta decisión se tome de forma explícita.

**Las dos propuestas quedan completas y listas.** Lo que hace falta para convertirlas en GO,
en orden:

1. **Tu aprobación para promover** una o ambas estrategias a `BUILTIN_STRATEGIES`, en su propio
   commit y con el test actualizado a propósito.
2. **Cerrar los libros de `paper-7c-week5`** — tiene una posición abierta desde hace 27 días.
3. **Añadir ETH/USDT a `risk.allowed_symbols`** si se lanza la Propuesta B.
4. **Decidir sobre la configuración de riesgo 4H**, que nunca se ha desplegado.
5. **Decidir si dos sesiones concurrentes** son razonables en esta máquina, o si conviene
   lanzarlas secuencialmente.

Ninguno de esos cinco pasos lo doy por hecho, y no voy a ejecutar ninguno sin que lo apruebes.
