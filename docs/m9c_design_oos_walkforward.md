# M9c — DISEÑO: OOS / Walk-Forward / Reproducibility Gate

**Estado:** DISEÑO. Nada implementado. Base: `75a6ab8`, suite 1741 passed.

---

## 0. Hechos medidos

| # | Hecho | Consecuencia |
|---|---|---|
| F1 | **`LedgerEntry` no registra huella de los datos** — solo `experiment_id`, `result_hash`, `code_revision`, `status` | La condición C (mismo id + **mismos datos** + mismo code_revision → distinto hash) **hoy no es comprobable** |
| F2 | `DatasetSpec.source` es texto libre, una **etiqueta, no una huella** | Dos vintages del mismo rango son indistinguibles |
| F3 | **No hay ningún acceso a git en todo `src/`** | `code_revision` necesita un lector nuevo, y no puede vivir en `research` |
| F4 | `MarketBarRepository.get_bars` es **async**; `cli/data.py` usa `asyncio.run` | El entrypoint sigue ese patrón; el runner sigue siendo síncrono |
| F5 | CLI es Typer con sub-apps registradas en `main.py` | `cli/research.py` encaja sin inventar estructura |

**F1+F2 es el hallazgo central de M9c.** La regla de reproducibilidad que pediste no se puede aplicar sin una huella de los datos, y hoy no existe.

---

## 1. Archivos exactos

| Archivo | Cambio |
|---|---|
| `research/definition.py` | `ExperimentRole`, `WindowSpec`, `WalkForwardPlan`, `RegimeSpec`; `role` y `plan` en la definición |
| `research/result.py` | `bars_digest`; `ExperimentStatus.REPRODUCIBILITY_FAILURE` |
| `research/ledger.py` | `bars_digest` y `derived_from` en la entrada; `verify()` |
| `research/compare.py` | **nuevo** — `compare()`, `ComparisonTable` |
| `research/folds.py` | **nuevo** — `plan_folds()`, `FoldResult`, `WalkForwardSummary` |
| `research/sweep.py` | **nuevo** — `expand_sweep()`, `SweepReport` |
| `research/stress.py` | **nuevo** — `expand_stress()` |
| `research/digest.py` | **nuevo** — `bars_digest()` |
| `orchestration/research.py` | `code_revision()`; `load_definition()` |
| `cli/research.py` | **nuevo** — `run`, `walk-forward`, `verify` |
| `cli/main.py` | registra la sub-app |
| tests | 3 archivos nuevos + adiciones |

## 2. Modelos y protocolos nuevos

```
ExperimentRole = IN_SAMPLE | OUT_OF_SAMPLE | WALK_FORWARD_TRAIN | WALK_FORWARD_TEST | BENCHMARK

WindowSpec        start, end                       (media abierta: [start, end))
WalkForwardPlan   folds: tuple[Fold, ...]          declaradas por adelantado
Fold              index, train: WindowSpec, test: WindowSpec
RegimeSpec        labeller_id, lookback_bars       (etiquetado causal, §12)
```

`role` entra en `ExperimentDefinition`, así que **forma parte del `experiment_id`**. Correr la misma configuración como IS y como OOS son dos experimentos distintos con nombres distintos — que es exactamente lo que impide presentar uno como el otro.

## 3. Roles IS / OOS / BENCHMARK

Cada resultado **declara su rol** y el rol está en el hash.

**Regla fundamental, hecha operativa:** un periodo OOS no puede influir en selección de parámetros, reglas, thresholds ni clasificador de régimen. M9c la hace cumplible por dos vías, y conviene ser explícito sobre cuál es cuál:

- **Estructural**: M9c **no selecciona nada**. No hay optimización, así que ningún parámetro puede haber sido elegido mirando el OOS. Mientras eso siga siendo cierto, la regla se cumple por construcción.
- **Contable**: cuando llegue la selección, la única defensa será el ledger — `attempts(experiment_id)` dice cuántas veces se miró. No impide mirar; hace la mirada visible.

**EMA20/50 = `BENCHMARK`, nunca `OUT_OF_SAMPLE`.** Invariante comprobable: ninguna definición cuyo `strategy_id` sea el benchmark puede declararse OOS. Se rechaza al construir, no por convención.

## 4. Walk-forward

```
plan_folds(plan) -> tuple[Fold, ...]
Cada fold produce DOS definiciones derivadas de la base:
  - misma config, ventana = fold.train, role = WALK_FORWARD_TRAIN
  - misma config, ventana = fold.test,  role = WALK_FORWARD_TEST
```

**Qué pasa del train al test: nada.** En M9c walk-forward es un **instrumento de medición, no de ajuste**: como no se selecciona nada, ambas ventanas corren la misma configuración y el leakage es imposible por construcción, no por vigilancia.

Conviene decirlo en voz alta: **hasta que exista selección, "train" es un nombre engañoso.** Es una ventana de observación. Mantengo tu nomenclatura porque la pediste y porque será correcta cuando llegue la selección, pero el docstring debe decir que hoy no se ajusta nada — o alguien leerá "train" y creerá que sí.

**Consolidación sin ocultar folds malos**: `WalkForwardSummary` reporta por fold y en agregado — mediana de retorno, **peor fold**, mediana de drawdown, **peor drawdown**, % de folds positivos, dispersión de expectancy, estabilidad de R. Todos descriptivos. **Ningún score único**, y la lista incluye deliberadamente los dos peores casos para que un buen agregado no pueda esconderlos.

## 5. Invariantes anti-leakage

1. `test.start >= train.end` — el test nunca precede al train.
2. Ventanas media abiertas `[start, end)` — una barra pertenece a una sola ventana; con `[start, end]` la barra frontera estaría en ambas.
3. Los folds no se solapan entre sí en la ventana de test.
4. Las barras entregadas a un fold se filtran por su ventana **antes** de llegar al motor. El motor no sabe de ventanas y no debe saberlo.
5. La etiqueta de régimen en T usa solo barras ≤ T (§12).
6. Cada invariante falla al construir el plan, no durante la corrida.

## 6. Semántica de fallo de reproducibilidad

**Requiere `bars_digest`, que hoy no existe (F1/F2).** `bars_digest` = SHA256 sobre la forma canónica de las barras consumidas, guardado en el resultado y en la entrada del ledger. No va en la definición: una definición debe poder declararse *antes* de tener los datos, y meter la huella ahí lo impediría.

```
verify(entry) →
  mismo experiment_id + mismo bars_digest + mismo code_revision + distinto result_hash
    → REPRODUCIBILITY_FAILURE
  mismo experiment_id + distinto bars_digest
    → no es fallo: son datos distintos, y se reporta como tal
  code_revision None en cualquiera de los dos
    → indeterminado, y se dice; no se asume
```

**No se oculta ni se añade otra línea sin más**: la entrada se escribe con `status=REPRODUCIBILITY_FAILURE` y nombrando la entrada anterior con la que discrepa. El ledger sigue siendo append-only — el fallo se **añade**, nunca se corrige hacia atrás.

## 7. `compare()`

```
compare(results) -> ComparisonTable
  rows    = experimentos, en el orden recibido
  columns = las 16 métricas + los agregados walk-forward cuando aplique
```

Función pura. **Orden de inserción, jamás de métrica.** Sin `best`, `rank`, `top`, `sort_by`, ni parámetro de ordenación — igual que en el ledger, con test que verifica que esos atributos **no existen**. Una celda vacía se muestra vacía: una métrica que este experimento no pudo calcular no se rellena con cero.

## 8. Entrypoint mínimo

`cli/research.py`, sub-app Typer registrada en `main.py` como `data` y `paper` (F5):

```
qp research run <definition.json>            una definición, un resultado, una línea de ledger
qp research walk-forward <definition.json>   expande el plan, corre cada fold, resume
qp research verify                           recorre el ledger y reporta discrepancias
```

Carga barras por el repositorio async con `asyncio.run`, siguiendo `cli/data.py` (F4). **Sin scripts ad-hoc**: si no está en el CLI, no se corrió.

## 9. Inyección de `code_revision`

No hay acceso a git en el código (F3), y `research` no hace I/O. Nuevo `orchestration/research.py::code_revision()`:

- ejecuta `git rev-parse HEAD` y `git status --porcelain`
- devuelve `"<sha>"` limpio, `"<sha>-dirty"` con cambios sin commitear, `None` si no hay git
- **`-dirty` es la parte importante**: un resultado producido sobre un árbol sucio no es reproducible y debe decirlo, o `verify()` reportaría fallos de reproducibilidad cuya causa real fue código sin commitear

El CLI lo llama y lo pasa al runner. `research` sigue sin saber que git existe.

## 10. Integration test del adapter

`ExperimentEngineFactory` hoy compila y se tipa, y **ningún test lo ejerce** — es código de composición sin cobertura. M9c lo corre de verdad: registro real de estrategias, barras reales de fixture, a través del runner, y compara contra las cifras literales del benchmark.

## 11. Protocolo de sensibilidad

```
expand_sweep(base, variations) -> tuple[ExperimentDefinition, ...]
```

Cada variación es una definición **completa** con su propio `experiment_id`. Todas se corren, **todas se registran**. `SweepReport` devuelve la distribución — mínimo, cuartiles, mediana, máximo por métrica — y **nunca un ganador**.

**Trazabilidad**: `derived_from` va en la **entrada del ledger**, no en la definición. Así la identidad sigue siendo "qué se corrió" y el linaje "por qué", y la misma configuración corrida suelta o dentro de un barrido conserva el mismo id en vez de partirse en dos.

## 12. Protocolo de régimen

`RegimeLabeller` recibe **solo la historia hasta T**:

```
label(history: Sequence[MarketBar]) -> str   # history termina en la barra T
```

La firma **es** la garantía: no recibe barras futuras, así que no puede mirarlas. `RegimeSpec` entra en la definición, de modo que la etiqueta forma parte de la identidad del experimento — cambiar el clasificador es cambiar el experimento.

Prohibido: etiquetar con terciles calculados sobre la muestra completa. Es la fuga más habitual y la más invisible, porque el clasificador parece independiente de la estrategia.

## 13. Protocolo de estrés

```
expand_stress(base, ladder) -> tuple[ExperimentDefinition, ...]
```

Escalera **declarada por adelantado y monótona** sobre `ExecutionPolicy`: multiplicadores de comisión y de slippage. Cada peldaño es una definición completa con su id.

Se reporta **la escalera entera**. Elegir el peldaño más favorable es exactamente el sesgo que esto existe para impedir, así que `expand_stress` no devuelve nada seleccionable y el reporte no ordena.

Sobre huecos de datos: **no se simulan en M9c.** Fabricar barras ausentes es inventar datos, y la plataforma ya trata un hueco como un incidente. Queda fuera hasta que haya un modelo de huecos defendible.

## 14. Tests exactos (38)

**Roles (5)** — el rol entra en el id · misma config con dos roles ⇒ dos ids · el benchmark no puede declararse OOS · un resultado declara su rol · el rol sobrevive el round-trip

**Walk-forward (8)** — el plan produce dos definiciones por fold · test nunca precede a train · ventanas media abiertas: la barra frontera cae en una sola · folds de test no se solapan · las barras se filtran antes del motor · el resumen reporta el peor fold · reporta el % positivo · un fold que falla aparece en el resumen

**Reproducibilidad (7)** — mismo id+datos+revisión ⇒ mismo hash · distinto hash ⇒ `REPRODUCIBILITY_FAILURE` · distinto `bars_digest` no es fallo · `code_revision=None` ⇒ indeterminado, no fallo · el fallo nombra la entrada anterior · el ledger sigue append-only tras un fallo · `-dirty` se registra

**compare (5)** — orden de inserción · sin `best`/`rank`/`top`/`sort_by` · celda no calculable queda vacía · pura · las 16 métricas presentes

**Sensibilidad y estrés (5)** — cada variación tiene id propio · todas quedan registradas · el reporte da distribución, no ganador · la escalera de estrés se reporta entera · `derived_from` va en el ledger, no en la definición

**Régimen (3)** — el labeller solo ve historia hasta T · el spec entra en el id · terciles sobre la muestra completa no son expresables por la firma

**Entrypoint y adapter (5)** — `qp research run` produce resultado y línea de ledger · `walk-forward` corre todos los folds · `verify` detecta la discrepancia · **el adapter real corre de verdad** · `code_revision` llega desde el CLI hasta el resultado

## 15. Riesgos de overfitting

1. **M9c hace el sobreajuste barato y fácil.** Barridos y walk-forward existen para explorar; la única defensa es el conteo, y solo funciona si nadie corre nada fuera del CLI.
2. **La regla OOS es hoy estructural** (no hay selección) y **pasará a ser contable** cuando la haya. Ese cambio es el momento de mayor riesgo del proyecto y merece su propia autorización.
3. **"Train" invita a creer que se ajustó algo.** Riesgo de vocabulario, mitigado por docstring, no por código.
4. **`-dirty` frecuente destruye la reproducibilidad** sin que nada parezca roto.
5. **Selección de escalón de estrés** — mitigado porque nada devuelve un escalón.
6. **EMA20/50 sigue siendo un benchmark observado.** El invariante impide etiquetarla OOS; no impide compararse contra ella y creer que la comparación es limpia.

## 16. Partición

**Sí conviene partir. Tres subfases:**

- **M9c.1 — Cerrar M9b y la huella.** `bars_digest`, `code_revision`, `verify()`, entrypoint CLI, integration test real del adapter. Sin esto, nada de lo demás es auditable.
- **M9c.2 — Roles y walk-forward.** `ExperimentRole`, `WindowSpec`, `WalkForwardPlan`, folds, invariantes anti-leakage, `WalkForwardSummary`, `compare()`.
- **M9c.3 — Sensibilidad, régimen, estrés.** Los tres protocolos de expansión.

Motivo: M9c.1 es infraestructura de auditoría y **debe ir primero** — sin huella de datos no hay detección de fallo de reproducibilidad, y sin entrypoint los experimentos siguen fuera del registro. M9c.3 es lo que más facilita sobreajustar y debería llegar cuando el conteo ya funciona.

## 17. GO / NO-GO

**GO — M9c.1.** Cierra gaps reales y medidos. Máxima prioridad: hoy la condición C que pediste **no es comprobable**.

**GO — M9c.2**, después de .1.

**GO — M9c.3**, después de .2, y **solo** con la mecánica de expansión: sin argmax, sin optimización, sin promoción.

**NO-GO — simular huecos de datos.** Sin modelo defendible, es inventar barras.

**NO-GO — cualquier selección automática**, incluido "el fold mediano" o "el escalón razonable".

**NO-GO — score único agregado**, como pediste. Un número que resume dieciséis es el primer paso hacia optimizarlo.
