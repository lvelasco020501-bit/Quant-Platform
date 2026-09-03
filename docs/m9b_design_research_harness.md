# M9b — DISEÑO: Research Harness

**Estado:** DISEÑO. Nada implementado. Base: `5d36452`, suite 1699 passed.

---

## 0. Hechos medidos

| # | Hecho | Evidencia |
|---|---|---|
| F1 | **`research/` ya existe** como paquete vacío y **ya está en `DOMAINS` y `ALLOWED_DEPENDENCIES`** | `src/quantplatform/research/__init__.py`; `test_dependency_boundaries.py:33,84` |
| F2 | Su presupuesto es `{core, config, backtesting, data, features, storage, strategies}` — **sin `execution`, `portfolio` ni `risk`** | `test_dependency_boundaries.py:84` |
| F3 | **Ningún archivo fuera de orchestration/api/cli puede importar `strategies`+`risk`+`execution` juntos** | `test_backtesting_is_the_only_package_that_composes_the_whole_chain:350` |
| F4 | El único composition root que arma un backtest es `orchestration/paper.py:245` | grep `BacktestEngine(` |
| F5 | `turnover` se computa **solo si se le pasa `traded_notional`**, y el motor **no se lo pasa** | `metrics.py:202`; grep sin resultados en `engine.py` |
| F6 | `max_consecutive_losses` / `max_consecutive_wins` existen y **no se computan** | `_trade_statistics` |
| F7 | El hash de la definición es **estable entre procesos**: las colecciones son `tuple`, no `frozenset` | 3 corridas con `PYTHONHASHSEED` distinto → mismo SHA256 |
| F8 | El motor no lee reloj ni saca números aleatorios | docstring `engine.py`, verificado en toda la suite |

**F3 es el hecho que decide la arquitectura**, y **F5 es un pendiente que no estaba en tu lista**: `turnover` lleva desde M4 declarado y silenciosamente `None`, porque nadie le pasa el notional. Mismo patrón latente que ya cerramos tres veces.

---

## 1. Archivos exactos

| Archivo | Contenido |
|---|---|
| `research/definition.py` | `ExperimentDefinition`, `canonical_json()`, `experiment_id()` |
| `research/result.py` | `ExperimentResult`, `ExperimentStatus` |
| `research/runner.py` | `ExperimentRunner`, protocolo `BacktestFactory` |
| `research/ledger.py` | `ExperimentLedger` — append-only |
| `research/store.py` | único adaptador de I/O (JSON) |
| `orchestration/research.py` | **implementa** `BacktestFactory` (aquí sí se puede componer) |
| `backtesting/engine.py` | pasa `traded_notional` (F5); computa rachas (F6); `time_in_market` |
| `backtesting/metrics.py` | campo `time_in_market` |
| `tests/architecture/test_dependency_boundaries.py` | añadir `risk` al presupuesto de `research` |

## 2. `ExperimentDefinition`

Modelo frozen. Un solo nivel de anidamiento, todo serializable:

```
dataset:    symbol, timeframe, start, end, source
strategy:   strategy_id, strategy_version, params: tuple[tuple[str, str], ...]
risk:       RiskConfiguration          (incluye position management de M8a/M8b)
execution:  ExecutionPolicy
backtest:   BacktestConfig             (incluye initial_capital)
regime:     regime_label: str | None
```

`params` como tupla de pares ordenada, no `dict`: un mapa serializa en orden de inserción, y dos definiciones idénticas construidas en distinto orden hashearían distinto.

**No hay campo `seed`.** Por F8 no existe ninguna fuente de aleatoriedad, y añadir un campo que nada consume es exactamente el patrón latente que este proyecto ya tuvo que cerrar cuatro veces (`highest_price_seen`, breakers, `average_r`, `turnover`). Si algún día se introduce aleatoriedad, la definición gana el campo **entonces**, y el cambio de hash lo hará visible. Lo que sí entra ahora es un test que demuestra el determinismo.

**Position management no es un bloque aparte**: trailing, break-even, take-profit y time stop viven ya en `RiskConfiguration`. Duplicarlos daría dos fuentes de verdad.

## 3. Serialización canónica y hashing

```
canonical_json(definition) = definition.model_dump_json()   # orden de declaración, estable
experiment_id              = sha256(canonical_json)[:32]
```

Verificado (F7) que es estable entre procesos con `PYTHONHASHSEED` distinto, porque ninguna config usa `frozenset`. **Eso hay que fijarlo con test**, no confiarlo: un `frozenset` añadido mañana a cualquier config rompería la reproducibilidad en silencio. El test corre el hash bajo varias semillas y sobre la definición **completa**, no solo sobre `RiskConfiguration`, que es lo único que medí hoy.

## 4. `ExperimentResult`

```
experiment_id, definition (embebida), code_revision: str | None,
status: ExperimentStatus, error: str | None,
performance: PerformanceSummary, trades: tuple[ClosedTrade, ...],
equity_curve: tuple[EquityPoint, ...], max_drawdown_at: UtcDatetime | None,
started_at, finished_at, result_hash
```

La definición va **embebida**, no referenciada: un resultado que no puede reconstruir su propia entrada no es evidencia.

`code_revision` se **inyecta**, no se lee. Consultar git es I/O, y `research` no hace I/O; el composition root lo pasa. `None` es una respuesta válida y honesta.

`status` incluye `FAILED` con su `error`. **Un experimento que revienta se registra igual** — ocultarlo sería exactamente el sesgo de supervivencia que el ledger existe para impedir.

## 5. Rachas consecutivas

Un recorrido sobre `trade_results` en orden, contando signo y quedándose con el máximo. `realized_pnl == 0` **rompe ambas rachas**: no es una victoria ni una derrota, y contarlo como continuación de cualquiera de las dos inventaría una racha.

## 6. `time_in_market`

- **Denominador**: `bars_processed` — las barras que la corrida realmente vio.
- **Numerador**: barras al cierre de las cuales existía alguna posición abierta.
- **Se mide en barras, no en tiempo real.** Un hueco en el feed no es tiempo en mercado; y en H1 las dos coinciden salvo por los huecos, que es justo donde la distinción importa.
- **Varias posiciones**: hoy cuenta *cualquier* exposición, no la suma. Cuando existan varias, una versión ponderada por tamaño será una **métrica distinta con otro nombre**, no una redefinición de esta — o las series históricas dejarían de ser comparables.

Fuente: `state.snapshots`, que ya guarda un snapshot por barra.

## 7. Cómo ejecuta el motor sin duplicar lógica — y sin componerlo

**F3 lo decide, no una preferencia:** ningún archivo de `research` puede importar `strategies`+`risk`+`execution` juntos, y construir un `BacktestEngine` requiere exactamente eso. El harness **no puede** y **no debe** armar el motor.

```
research/runner.py          protocolo BacktestFactory: (definition) -> BacktestEngine
                            ExperimentRunner.run(definition, factory) -> ExperimentResult
orchestration/research.py   implementa la factory; importa todo, y ya está exento por F3
```

`research` importa `backtesting` (para el tipo del motor y `BacktestConfig`), `core` y `risk`. **Presupuesto a enmendar**: añadir `risk`, que hoy falta y hace falta para embeber `RiskConfiguration`. Sigue sin importar `execution`, así que no dispara la prueba de composición.

Es el mismo patrón de puerto que usa todo el resto del código: `research` declara qué necesita, el composition root lo satisface.

## 8. Almacenamiento y ledger

- `ExperimentLedger`: **append-only**, una línea JSON por experimento (`experiment_id`, `result_hash`, `code_revision`, `status`, `created_at`, ruta del resultado).
- **Nunca borra, nunca sobrescribe.** Reejecutar la misma definición añade una línea más; que el mismo `experiment_id` aparezca N veces **es el dato**, no ruido.
- El store escribe cada resultado como `<experiment_id>-<n>.json`.

## 9. Determinismo

Tres propiedades, cada una con su test:

1. misma definición ⇒ mismo `experiment_id`, entre procesos y semillas de hash
2. misma definición + mismos datos ⇒ mismo `result_hash`
3. definición distinta en un solo campo ⇒ `experiment_id` distinto

`result_hash` se computa sobre una forma canónica que **excluye `started_at`/`finished_at`**: dos corridas idénticas ocurren en instantes distintos, y meter el reloj en el hash haría la reproducibilidad imposible de comprobar por construcción. Los timestamps se registran, pero no se hashean.

## 10. Golden de reproducibilidad

Correr EMA20/50 dos veces sobre las mismas barras a través del harness y exigir `result_hash` idéntico, más las cifras literales del benchmark. Es el test que convierte "el harness es reproducible" en una afirmación verificada en vez de una aspiración.

**EMA20/50 no se etiqueta como OOS.** Fue observada durante week5; el harness debe registrarla como benchmark y el ledger no debe presentarla como evidencia fuera de muestra.

## 11. Tests exactos (26)

**Definición y hash (6)** — misma definición ⇒ mismo id · orden de params no altera el id · un campo distinto ⇒ id distinto · id estable entre procesos y `PYTHONHASHSEED` · la definición completa round-trips a JSON · una definición sin rango temporal no construye

**Resultado (5)** — embebe su definición · `code_revision=None` es válido · un fallo se registra con `status=FAILED` y su error · `result_hash` ignora los timestamps · round-trip JSON

**Métricas (7)** — rachas de victorias · rachas de derrotas · un trade a cero rompe ambas · `time_in_market` con exposición continua = 1 · sin exposición = 0 · parcial correcto · `turnover` deja de ser `None` (F5)

**Runner (4)** — usa la factory inyectada y no compone motor · dos corridas ⇒ `result_hash` idéntico (golden) · EMA20/50 reproduce sus cifras literales · una definición que revienta produce resultado, no excepción

**Ledger (4)** — es append-only · reejecutar añade línea, no sustituye · un fallido queda registrado · el conteo por `experiment_id` es legible

**Arquitectura (2)** — `research` no importa `execution` · nadie importa `research`

## 12. Riesgos de overfitting

1. **El harness abarata correr variantes**, que es cómo se sobreajusta. Mitigación: el ledger cuenta.
2. **Reutilización del OOS** — pertenece a M9c, pero el ledger de M9b es su sustrato: sin conteo no hay protocolo.
3. **Selección silenciosa.** M9b **no** ordena por PnL ni expone un `best()`. La comparación devuelve una tabla; ordenarla es decisión de quien la lee, y queda fuera del código.
4. **Borrado selectivo.** Append-only y `status=FAILED` registrado hacen el sesgo de supervivencia visible.
5. **EMA20/50 ya observada.** Benchmark, no OOS.
6. **Sesgo de métrica.** Reportar las 20 juntas evita elegir la que favorece; ninguna se marca como "la principal".

## 13. Partición

**No conviene partir M9b.** Las piezas no son independientes: sin métricas no hay resultado que hashear, sin hash no hay ledger, sin ledger no hay anti-overfit. Partirlo dejaría un harness que corre y no registra, que es peor que no tenerlo.

Sí conviene **ordenar dentro**: (a) métricas pendientes — F5, F6, `time_in_market` — con sus goldens intactos; (b) definición y hash; (c) resultado y runner; (d) ledger. Un solo commit, un solo gate.
