# M9 — DISEÑO: Research Harness + deuda D3/D4/D5

**Estado:** DISEÑO. Nada implementado. Base: `f7d586f`, suite 1678 passed.

---

## 0. Hechos medidos antes de diseñar

| # | Hecho | Evidencia |
|---|---|---|
| F1 | Poblar `open_order_symbols` **no rompe ningún test** | sondeo: 1678 passed |
| F2 | **Ningún snapshot real contiene `position_risk`** — la clave está ausente en week3/4/5 | `var/state/*.json` |
| F3 | `DomainModel` usa `extra="forbid"` → un campo renombrado en un snapshot viejo **falla ruidosamente** | verificado: `ValidationError` |
| F4 | `PaperSessionState` **no tiene versión de esquema** | `core/models/paper.py` |
| F5 | `average_r` y `expectancy_r` existen y **no se computan en ningún sitio** | `metrics.py:94,103`, grep sin consumidores |
| F6 | `time_in_market` **no existe** | grep |
| F7 | De las 18 métricas pedidas, **16 ya existen**; faltan `time_in_market` y computar las dos de R | `metrics.py` |
| F8 | `broker.open_orders()` ya devuelve `Order` con `.symbol` | `broker.py:225` |
| F9 | `_update_position_risk` **elimina** el registro al quedar plana la posición, y corre **antes** que `_record_closed_trades` | `engine.py:666`, `:880` |

**F9 es el hallazgo que más condiciona M9a**: cuando se detecta el trade cerrado, el `PositionRiskState` con el denominador de R **ya se borró**. Sin resolverlo, R no se puede computar aunque D3 esté arreglado.

---

## 1. Solución exacta D3

`PositionRiskState.risk_amount` se **divide en dos campos**, sin compatibilidad ambigua:

```
initial_risk_amount: NonNegativeMoney = Field(gt=0)   # fijado al abrir, nunca se reescribe
current_risk_amount: NonNegativeMoney = Field(gt=0)   # riesgo de lo que sigue abierto
```

`risk_amount` **desaparece**. No hay alias, ni propiedad de compatibilidad, ni default: por F3 un snapshot que lo lleve falla al cargar, y por F2 no existe ninguno. La migración es el renombrado y el fallo ruidoso; no hace falta código de migración.

**Cómo se preserva `initial_risk_amount`**: mismo mecanismo con el que `_update_position_risk` ya arrastra `opened_at` y `highest_price_seen` — se toma de `existing` cuando hay registro, y solo se calcula cuando el ciclo de vida es nuevo (`existing is None`).

**Scale-in**: `initial_risk_amount` **no** se actualiza. R queda medido contra lo que la posición arriesgó al abrir. Limitación consciente y documentada; ninguna estrategia embarcada puede escalar, y M5b ya falla ruidosamente ante stops divergentes.

### El registro de trade cerrado (consecuencia de F9)

`RunState.trade_results: list[Decimal]` → `list[ClosedTrade]`:

```
symbol, realized_pnl, initial_risk_amount: Decimal | None, opened_at, closed_at
```

Y para que el denominador sobreviva al borrado: cuando `_update_position_risk` elimina un registro por quedar plana la posición, lo deposita en `state.retired_risk[symbol]`, que `_record_closed_trades` consume y vacía. Explícito, no un efecto lateral.

`_trade_statistics` computa entonces `average_r` y `expectancy_r` sobre los trades que llevan denominador, y devuelve `None` cuando ninguno lo lleva — exactamente lo que su docstring ya promete.

## 2. Solución exacta D4

Un solo núcleo compartido y dos funciones con contratos distintos en la firma:

```
_net_exit_proceeds(price, quantity, policy)
    = quantity·adjust(price, SELL) − fee_for(quantity·adjust(price, SELL))
```

- **Pre-fill (sizing)** — `projected_stop_out_cost` se conserva, pero su parámetro `entry_price` pasa a llamarse **`valuation_price`**. El contrato queda escrito en la firma: es un precio *fee-exclusive*, anterior al fill, y por eso suma la comisión de entrada.
- **Post-fill (restatement)** — nueva `open_risk_amount(*, quantity, avg_entry_price, stop, policy)`, definida como `quantity·avg_entry_price − _net_exit_proceeds(stop.trigger_price, …)`. **No suma comisión de entrada**, porque `avg_entry_price` ya la lleva ([portfolio/engine.py:472,497](src/quantplatform/portfolio/engine.py:472)).

Consecuencia elegante: `break_even_price` de M8a es **la raíz de `_net_exit_proceeds − quantity·avg_entry_price`**, es decir el precio donde `open_risk_amount` vale cero. Las tres funciones quedan sobre un mismo núcleo en vez de tres aritméticas que deben coincidir.

`_update_position_risk` pasa a llamar `open_risk_amount`. Efecto: `current_risk_amount` deja de estar sobrestimado en una comisión de entrada.

## 3. Solución exacta D5

Una línea en `_risk_context`, con `open_orders()` invocado una sola vez:

```
working = self._broker.open_orders()
open_order_count=len(working)
open_order_symbols=frozenset(o.symbol for o in working)
```

Medido: **0 tests rotos** (F1). Pero 0 rotos también significa que **nada ejercita hoy ese camino**, así que M9a debe añadir las pruebas que demuestren que ahora sí dispara:

- una salida estratégica que coincide con una forzada se rechaza por `CONFLICTING_ORDER`, **no** por `QUANTITY_PRECISION`
- se sigue vendiendo **una sola vez**
- la regla de M6 sigue: `CONFLICTING_ORDER` es *advisory* para una salida forzada
- paper hereda lo mismo, porque comparte `_risk_context`

## 4. Cambios de modelos

| Modelo | Cambio |
|---|---|
| `PositionRiskState` | `risk_amount` → `initial_risk_amount` + `current_risk_amount` |
| `ClosedTrade` | **nuevo**, en `core/models/` |
| `RunState` | `trade_results: list[ClosedTrade]`; `retired_risk: dict[str, PositionRiskState]` |
| `TradeStatistics` | sin campos nuevos — `average_r` y `expectancy_r` ya existen (F5) |
| `PerformanceSummary` | `time_in_market: Money | None` (F6) |
| `PaperSessionState` | `schema_version: int = 1` |

## 5. Migración y compatibilidad de snapshots

Hoy no hay versión (F4). Los tres snapshots reales cargan porque el default de cada campo añadido resultó ser el valor históricamente correcto — `position_risk=()` y `breakers=()` significan "nada protegido / nada enganchado", que era cierto. **Eso fue suerte, no diseño**: un campo futuro cuyo default no coincida con la verdad histórica leería mal un snapshot viejo en silencio.

Propuesta: `schema_version: int = 1`, ausente ⇒ 1. El renombrado de D3 lo sube a **2**. Un snapshot v1 **con** `position_risk` se rechaza explícitamente; por F2 no existe ninguno, así que no se pierde nada real y la regla queda escrita para la próxima vez.

Nada de alias ni de lectura tolerante: `extra="forbid"` ya convierte un renombrado en fallo ruidoso, que es la compatibilidad no ambigua que pediste.

## 6. Arquitectura del Research Harness

Paquete nuevo `research/`, hoja del grafo: puede importar `backtesting`, `risk`, `core`; **nadie lo importa**. Se añade al presupuesto de imports de los tests de arquitectura.

```
research/
  definition.py   ExperimentDefinition  — qué correr, congelado y hasheable
  runner.py       ExperimentRunner      — definición → resultado, sin I/O
  result.py       ExperimentResult      — qué salió, con hash de contenido
  compare.py      compare(results)      — tabla, función pura
  store.py        único adaptador de I/O (JSON)
```

La propiedad central es la **reproducibilidad**: el id del experimento es el SHA256 de su definición serializada, y dos corridas de la misma definición deben producir resultados byte-idénticos. Es testeable y es lo que hace del harness algo distinto de un script.

## 7. Formato de definición

Modelo frozen serializable a JSON. El archivo **es** la definición; su hash es el id.

```
dataset:   symbol, timeframe, start, end
strategy:  strategy_id, strategy_version, params
risk:      RiskConfiguration completa
execution: ExecutionPolicy
backtest:  BacktestConfig
split:     in_sample / out_of_sample / walk_forward  (declarado ANTES de correr)
```

## 8. Formato de resultados

```
definition_id (hash), definition (embebida), performance: PerformanceSummary,
trades: tuple[ClosedTrade, ...], equity_curve, result_hash, created_at
```

La definición va embebida, no referenciada: un resultado que no puede reconstruir su propia entrada no es evidencia.

## 9. Protocolo OOS / walk-forward (M9c)

- **El split se declara en la definición**, antes de correr. Cambiarlo cambia el hash y por tanto crea un experimento distinto.
- **Walk-forward**: secuencia de ventanas (train, test), anclada o rodante, también declarada de antemano.
- **Anti-overfit por conteo, no por prohibición**: no se puede impedir mirar el OOS varias veces, pero sí se puede hacer **contable**. Cada mirada al OOS es un `definition_id` nuevo en el ledger, así que el número de intentos queda registrado y es auditable. Esa es la mitigación honesta.
- **Sensibilidad de parámetros**: se corre la rejilla declarada y se reporta la **distribución** de métricas. **Sin argmax.** M9c no selecciona parámetros.
- **Segmentación por régimen**: partición por regla declarada. La etiqueta de régimen debe computarse **solo con datos in-sample**, o el régimen filtra futuro.
- **Stress de fees/slippage**: misma definición con multiplicador sobre `ExecutionPolicy`; se reporta la degradación, no un aprobado/suspenso.

## 10. Tests requeridos

**M9a (18)** — D3: `initial_risk_amount` fijo tras una reducción · `current_risk_amount` baja · un snapshot con `risk_amount` falla al cargar · `schema_version` ausente ⇒ 1 · v1 con `position_risk` se rechaza · el denominador sobrevive al borrado (F9) · `average_r` se computa · `expectancy_r` se computa · `None` cuando ningún trade lleva denominador. D4: `open_risk_amount` no suma comisión de entrada · `projected_stop_out_cost` sí la suma (pre-fill) · las dos difieren exactamente en esa comisión · `break_even_price` es donde `open_risk_amount` vale cero. D5: conflicto detectado como `CONFLICTING_ORDER` · una sola venta · advisory en salida forzada · paper igual · `open_orders()` se llama una vez.

**M9b (12)** — misma definición ⇒ mismo id · misma definición ⇒ resultado byte-idéntico · definición distinta ⇒ id distinto · el resultado embebe su definición · `time_in_market` correcto · las 18 métricas presentes · comparación de dos resultados es pura · round-trip JSON · un resultado sin trades no inventa métricas · EMA20/50 benchmark reproduce sus cifras conocidas · el harness no muta la definición · `research` no es importado por nadie (test de arquitectura).

**M9c (10)** — el split se declara antes de correr · cambiarlo cambia el id · las ventanas walk-forward no se solapan · el test jamás precede al train · la etiqueta de régimen no usa datos futuros · la rejilla reporta distribución y **no** argmax · el stress degrada monótonamente al subir costes · cada mirada OOS es un id nuevo · el ledger cuenta las miradas · una definición sin split no puede reportar métricas OOS.

## 11. Riesgos de overfitting

1. **El propio harness.** Hace barato correr variantes, que es exactamente como se sobreajusta. Mitigación: conteo de miradas OOS en el ledger.
2. **Reutilización del OOS.** Un OOS mirado veinte veces es in-sample. Solo el conteo lo revela.
3. **Multiplicidad.** Veinte configuraciones producen una buena por azar. Reportar distribución, no máximo.
4. **Look-ahead en el régimen.** Etiquetar con datos completos filtra futuro; debe computarse in-sample.
5. **Supervivencia de parámetros.** Un default "razonable" elegido tras ver resultados es una selección no registrada.
6. **EMA20/50 ya fue mirada.** El benchmark se congeló tras week5; **no** es OOS limpio para nada, y el harness no debe presentarlo como tal.

## 12. División de M9

- **M9a — D3/D4/D5.** Deuda acotada, radio medido, sin funcionalidad nueva.
- **M9b — Research Harness.** Definición, runner, resultado, comparación, `time_in_market`, R computada.
- **M9c — Protocolo OOS/walk-forward.** Splits, ventanas, sensibilidad, régimen, stress.

Orden obligatorio: **M9a antes que M9b**. Sin D3 no hay R, y sin R el harness no puede reportar dos de sus métricas — quedarían declaradas y sin computar, que es el patrón que este milestone existe para cerrar.

## 13. GO / NO-GO

**GO — M9a.** D5 medido en 0 roturas; D3 sin migración real por F2/F3; D4 unifica tres aritméticas en un núcleo.

**GO — M9b**, con el alcance acotado por F7: faltan una métrica y computar dos, no dieciocho.

**GO — M9c** para **protocolo y mecánica**. **NO-GO** para cualquier selección de parámetros, incluido elegir el "mejor" de una rejilla.

**NO-GO — estrategias nuevas, ML, optimización, tocar EMA.** Fuera de M9 entero.
