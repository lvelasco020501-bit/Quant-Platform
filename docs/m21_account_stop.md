# M21 — Account stop: cerrar la exposición abierta cuando se rompe el cap global

**Estado:** COMPLETADO · research/backtesting únicamente · 2026-09-23

No se tocó paper, Risk V2 productivo, execution, portfolio ni las estrategias productivas.
**Esta vez tampoco hizo falta tocar `backtesting`**: el mecanismo ya existía entero.

## 1. Diseño

Cuando el drawdown desde el high-water mark global alcanza el cap, **en la misma barra**:

1. se bloquean las entradas nuevas,
2. se emite una acción `CLOSE` por cada posición abierta, con razón `GLOBAL_DRAWDOWN_FORCED_EXIT`,
3. el engine la autoriza **antes** que cualquier intent de estrategia y con los vetos
   administrativos retirados,
4. la cuenta queda latcheada,
5. no hay recuperación automática — queda explícitamente fuera del alcance de M21.

## 2. Arquitectura — cero cambios en producción

| Pieza necesaria | Ya existía |
|---|---|
| `RiskActionKind.CLOSE` | sí |
| `RiskAction.reason` como texto libre | sí → `GLOBAL_DRAWDOWN_FORCED_EXIT` sin tocar enums |
| Llamada por barra que pregunta qué hacer con lo abierto | `evaluate_open_positions` |
| Autorización sin veto de la estrategia | `_authorise(..., forced_exit=True)`, y **antes** de las entradas |

El orden dentro de la barra es lo que da exactitud: valorar cuenta → breakers → `_offer_bar`
(el fix por barra de M20) → `evaluate_open_positions`. El breach se detecta y se actúa dentro
de la misma barra.

## 3. Invariantes

* **La estrategia no puede vetar**: el cierre no nace de una señal, nace de Risk.
* **Una vez por posición**: sólo se ordena cerrar lo que sigue abierto y no está ya siendo
  cerrado por un stop ordinario → idempotente por construcción.
* **Lo que decidió el engine estándar se preserva y va primero** (stop tocado, protección
  ausente); el account stop sólo añade los símbolos que nadie está cerrando ya.
* **Nada se abre después del stop.** Verificado sobre las corridas reales: **0 trades abiertos
  estrictamente después** en las 8 corridas donde el stop disparó.

## 4. Resultados — 6 mercados, 18 jobs, 72 corridas, 0 fallos

| Métrica | Valor |
|---|---|
| Corridas donde el stop disparó | **8** |
| Cierres forzados emitidos | **2** (XRP·S12·S3 y ADA·S12·S3) |
| Overshoot máximo sobre el cap | **0.49 pp** (BNB·S12·S3, ETH·S15·S3) |
| Trades abiertos estrictamente tras el stop | **0** |
| Bloqueo máximo | S12 **77.89%** · S15 24.59% · S20 1.98% |

### La comparación que importa: M20 (sólo bloquea) vs M21 (bloquea + cierra)

De **24 corridas comparables, sólo 2 difieren**:

| Mercado · cap · escenario | M20 | M21 | delta | forced exits |
|---|---|---|---|---|
| XRP · 12% · S3 | 12.26% | 12.28% | **+0.02 pp** | 1 |
| ADA · 12% · S3 | 12.24% | 12.35% | **+0.11 pp** | 1 |

Las otras 22 son **idénticas**, con cero cierres forzados.

## 5. El hallazgo: mi diagnóstico de M20 estaba equivocado

M20 concluyó que el cap se cruzaba **porque una posición abierta seguía perdiendo**. Con el
mecanismo construido y medido, esa **no es la causa operativa** a 4H con esta estrategia:

* **Cuando el cap se rompe, la cuenta ya está plana.** Los fills se aplican antes del snapshot,
  así que el stop ordinario sacó la posición en esa misma barra y el account stop no encuentra
  nada que cerrar. Se ve en los datos: BNB·S12·S3 registra `realized_at_stop = −116.37` con
  **cero** cierres forzados — esa posición se cerró en la barra del breach, por su propio stop.
* **El overshoot residual es intra-barra.** La pérdida ocurre *dentro* de la barra, antes de que
  ningún mecanismo evaluado a cierre de barra pueda actuar. Ningún dispositivo del lado de la
  exposición puede quitarlo.
* **Cuando el cierre forzado sí actúa, cuesta dinero.** Es una orden de mercado con costes
  estresados (slippage ×10 en S3): realiza la pérdida en lugar de dejar que el stop ordinario la
  recoja algo mejor. De ahí los +0.02 y +0.11 pp.

Dicho sin rodeos: **el account stop es una garantía, no una mejora**. Acota lo que puede pasar
cuando hay posición abierta en el breach — un caso que estos datos apenas ejercitan — y no
reduce el drawdown observado.

## 6. Contra los criterios PASS

| Criterio | Resultado |
|---|---|
| DD global respeta el cap salvo movimiento de una barra | ✅ máximo 0.49 pp |
| Una posición abierta no puede seguir perdiendo tras el breach | ✅ por construcción y por test |
| No hay ratchet oculto | ✅ ninguna cadena nueva |
| No hay flapping | ✅ 0% |
| Accounting no se rompe | ✅ cuenta plana, equity = caja |
| Execution auditable | ✅ razón registrada por cierre, con símbolo, instante y cantidad |
| El forced exit no puede ser rechazado por la estrategia | ✅ test a nivel de decisión |
| Risk mantiene autoridad final | ✅ la acción nace de Risk |

**Los ocho criterios se cumplen.** Con una salvedad que no está en la lista y sí importa: **S12
bloquea hasta el 78% de la historia** de un mercado. Ese coste es idéntico al de M20 y es el
argumento para preferir S15 o S20 si algún día se adopta un cap.

## 7. Lo que un backtest no puede probar

Se dice, no se finge:

* **Persistencia del latch entre reinicios** — propiedad del runtime de paper/live.
* **Crash entre la decisión y el fill** — si el proceso cae después de ordenar el cierre y antes
  de que el venue lo confirme, hace falta reconciliación al arrancar.
* **Duplicados contra el venue real** — aquí la idempotencia se prueba en la costura que usa el
  engine; contra un exchange hace falta además `clientOrderId` idempotente.

Los tres son **requisitos del diseño productivo**, no cosas que este milestone haya validado.

## 8. Veredicto: **GO condicionado para diseño productivo, NO-GO para despliegue**

**GO para diseñar**, porque el mecanismo está probado y es barato: no requiere ningún cambio en
producción para existir, la autoridad queda del lado correcto, y la auditoría es completa.

**NO-GO para desplegar**, y por una razón que no existía antes de medirlo: **el account stop no
mejora el drawdown en esta estrategia**. Desplegarlo hoy añadiría un mecanismo de cierre forzado
—con su superficie de fallo en reinicios, crashes y duplicados— a cambio de **+0.02 y +0.11 pp de
drawdown** en las dos únicas corridas donde llegó a actuar.

**Lo que haría que valiera la pena**, en orden:

1. **Estrategias o timeframes donde la posición siga abierta en el breach.** A 4H con stop por
   barra, el stop ordinario llega primero casi siempre. En intradía rápido, o con posiciones sin
   stop propio, el account stop sí sería el que actúa.
2. **Stop intra-barra** si se quiere bajar del 0.49 pp: requiere datos de tick y otro régimen de
   ejecución, y es un milestone en sí mismo.
3. **Reconciliación al arranque** antes de cualquier despliegue, para los tres puntos del §7.

## 9. Tests, gate y reproducibilidad

* **14 tests del account stop**, además de los 36 del motor de recuperación. Los que llevan el
  peso: el cierre se ordena **en la barra del breach**; una sola vez por símbolo; pedir dos
  veces la misma barra devuelve la misma instrucción; nada que cerrar cuando la posición ya no
  está; la estrategia **no puede abrir nada** después del stop (verificado a nivel de decisión);
  el cierre forzado **paga fees y slippage**; y sin cap el motor se comporta exactamente igual
  que antes.
* **Un artefacto propio, corregido:** la métrica `trades_after_stop` contaba con `>=` y marcaba
  como violación una posición abierta **en** la barra del stop por una orden aprobada antes de
  que el stop existiera. Con `>` la invariante se cumple en las 8 corridas donde el stop
  disparó, y hay un test que fija la semántica de la barra del stop.
* **Suite completa: 2 475 tests.** `ruff format`, `ruff check` y `mypy src` limpios; `mypy .` en
  su línea base de 108 errores / 10 archivos.
* **72 corridas, 18 jobs, 0 fallos.** 18 ledgers verificados, **0 fallos de reproducibilidad**.
* **Re-ejecución desde árbol limpio** (commit `fa9aabd`, ADA bajo S12): **4 de 4 resultados
  idénticos** campo a campo salvo revisión, marcas de tiempo e identificadores del intento.
* **Producción intacta:** `git status` sobre `risk`, `execution`, `portfolio`, `paper`,
  `strategies` **y `backtesting`** está vacío. M21 no necesitó ni el hook aditivo de M20.
