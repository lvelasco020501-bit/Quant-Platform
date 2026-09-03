# M9c.2 — DISEÑO: Roles + compare() + Walk-Forward

**Estado:** DISEÑO. Nada implementado. Base: `c0e543c`, suite 1767 passed.

---

## 0. Hechos medidos

| # | Hecho | Consecuencia |
|---|---|---|
| F1 | Añadir un campo a `ExperimentDefinition` **cambia todos los `experiment_id`** — el hash es sobre `model_dump_json()` completo | Meter `role` reescribe la identidad de todo experimento pasado |
| F2 | **No existe ningún ledger en disco** (`find -name "*.jsonl"` sin resultados) | Ese coste es **cero hoy y distinto de cero para siempre después** |
| F3 | Campos actuales: `name, dataset, strategy, risk, backtest, regime_label` | `role` se añade sin colisionar |
| F4 | P1 (store de símbolos vacío) afecta a cualquier comando que corra sobre datos reales | `walk-forward` heredará el defecto |

**F1+F2 es el argumento de calendario**: introducir `role` en la identidad es gratis ahora
porque no hay nada registrado que invalidar. En cuanto exista un solo experimento real, deja
de serlo.

---

## 1. Archivos exactos

| Archivo | Cambio |
|---|---|
| `research/definition.py` | `ExperimentRole`; campo `role`; invariante del benchmark |
| `research/folds.py` | **nuevo** — `WindowSpec`, `Fold`, `WalkForwardPlan`, `plan_id()`, `derive_fold_definitions()` |
| `research/compare.py` | **nuevo** — `compare()`, `ComparisonRow`, `ComparisonTable`, `COMPARISON_METRICS` |
| `research/aggregate.py` | **nuevo** — `WalkForwardSummary`, `aggregate_walk_forward()` |
| `research/ledger.py` | `plan_id` y `fold_index` en la entrada (linaje) |
| `cli/research.py` | `compare`, `walk-forward` |
| `tests/unit/test_experiment_roles.py` · `test_compare.py` · `test_walk_forward.py` | nuevos |

## 2. Enums y modelos

```
ExperimentRole = BENCHMARK | IN_SAMPLE | OUT_OF_SAMPLE
               | WALK_FORWARD_TRAIN | WALK_FORWARD_TEST

WindowSpec         start, end                     # [start, end)
Fold               index, plan_id, train, test
WalkForwardPlan    base_experiment_id, folds
ComparisonRow      experiment_id, name, role, status, métricas…
ComparisonTable    rows
WalkForwardSummary agregados + conteos
```

## 3. Cómo entra el rol en `experiment_id`

`role: ExperimentRole` es un campo más de la definición, así que entra en
`model_dump_json()` y por tanto en el hash **sin código adicional**. La misma configuración
declarada IS y declarada OOS son dos experimentos con nombres distintos — que es justamente
lo que impide presentar uno como el otro.

**Invariante del benchmark**: una definición cuyo `strategy_id` sea el benchmark congelado
**no construye** si declara `OUT_OF_SAMPLE`. Constante de módulo `BENCHMARK_STRATEGY_ID`,
con el motivo escrito: EMA20/50 fue observada durante week5 y no es evidencia fuera de
muestra de nada.

Conviene ser honesto sobre su alcance: **impide etiquetar, no impide creer.** Nadie queda
protegido de compararse contra EMA y tratar la comparación como limpia. Eso no lo arregla el
código.

## 4. Contrato de `compare()`

```
compare(results: Sequence[ExperimentResult]) -> ComparisonTable
```

Función pura, sin I/O y sin estado.

- **Filas en el orden de entrada.** Sin ordenación, ni parámetro para pedirla.
- **17 columnas fijas**, declaradas una vez en `COMPARISON_METRICS`, iguales para toda fila.
- Una métrica que un resultado no puede aportar es `None`, **nunca 0**.
- **Un resultado FAILED es una fila** con todas las métricas en `None`. Aparece; no se cae de
  la tabla. Una comparación que omite lo que no funcionó ya está eligiendo.
- Sin `best`, `rank`, `top`, `sort_by` ni `order_by`. Test que verifica que **no existen**.

Métricas: total return · max drawdown · Sharpe · Sortino · expectancy · average R ·
expectancy R · profit factor · win rate · avg win · avg loss · max consecutive losses ·
turnover · fees · slippage · closed trades · time in market.

## 5. `WalkForwardPlan` y `Fold`

`plan_id = sha256(base_experiment_id ‖ folds canónicos)`. El plan es un objeto **aparte** de
la definición base: meterlo dentro obligaría a cada definición de fold a arrastrar el plan
entero, y dos folds del mismo plan dejarían de parecerse.

Cada `Fold` declara `index`, `plan_id`, `train` y `test`, como pediste.

## 6. Invariantes temporales

Comprobados **al construir el plan**, no al correr:

1. `train.start < train.end` y `test.start < test.end`
2. `train.end <= test.start` — el test nunca precede al train
3. Las ventanas de test **no se solapan entre folds**
4. `index` contiguo desde 0, y los folds ordenados por él
5. Toda ventana cae dentro del rango del dataset base
6. Ventanas `[start, end)` — con `[start, end]` la barra frontera pertenecería a dos ventanas

El filtrado de barras por ventana ocurre **antes** de entregar nada al motor. El motor no
sabe de ventanas y no debe saberlo.

## 7. Derivación de definiciones por fold

```
derive_fold_definitions(base, plan) -> tuple[tuple[Definition, Definition], ...]
```

Cada par es `(train_def, test_def)`: la base con `dataset.start/end` sustituidos por la
ventana y `role` fijado. Ids distintos entre sí y respecto de la base.

**El linaje (`plan_id`, `fold_index`) va en la entrada del ledger, no en la definición** —
mismo criterio que `derived_from` en M9b. Así la identidad sigue siendo "qué se corrió" y el
linaje "por qué", y la misma ventana corrida suelta o dentro de un plan conserva un solo id.

## 8. Agregación

```
aggregate_walk_forward(results) -> WalkForwardSummary
```

Calculada **solo sobre folds TEST**. Los folds train son ventanas de observación; mezclarlos
contaría dos veces el mismo periodo.

| Campo | Nota |
|---|---|
| `median_return`, `worst_return` | |
| `median_max_drawdown`, `worst_max_drawdown` | |
| `positive_share_of_completed` | **el nombre dice el denominador** — ver §9 |
| `expectancy_dispersion` | desviación típica poblacional; `None` con menos de 2 folds |
| `r_stability` | desviación típica de `average_r` por fold; `None` igual |
| `folds_total`, `folds_completed`, `folds_failed` | |

`None` donde no hay base para calcular, nunca 0. **Ningún score único**, como pediste.

## 9. Folds fallidos

- Se **conservan** y se cuentan en `folds_failed`.
- Se **excluyen** de mediana y dispersión: meter un fallo como 0 inventaría un retorno.
- El riesgo de excluirlos es que 8 fallos y 2 folds positivos se lean como "100% positivo".
  Por eso el campo se llama **`positive_share_of_completed`** y `folds_total` y
  `folds_failed` van al lado: el nombre lleva su denominador, y quien lea puede calcular la
  otra fracción. Un campo llamado `positive_share` a secas sería la mentira cómoda.

## 10. CLI propuesta

```bash
qp research compare <result.json>...  [--out <table.json>]
qp research walk-forward <definition.json> --plan <plan.json> --ledger <path>
```

- `compare` **no escribe al ledger**: no corre nada, solo lee resultados ya registrados.
- `walk-forward` escribe **una línea por definición de fold** — dos por fold — cada una con
  `plan_id` y `fold_index`, incluidos los fallidos, y luego imprime el resumen.

**P1 (store de símbolos vacío) afecta a `walk-forward` igual que a `run`.** No bloquea el
diseño; sí impide que ninguno de los dos corra sobre datos reales hasta arreglarlo.

## 11. Tests exactos (30)

**Roles (6)** — el rol entra en el id · misma config, dos roles ⇒ dos ids · el benchmark no
construye como OOS · el benchmark sí construye como BENCHMARK · el rol viaja al resultado ·
round-trip

**compare (7)** — orden de entrada preservado · 17 columnas siempre · métrica ausente ⇒
`None`, no 0 · un FAILED es una fila · pura (no muta la entrada) · sin
`best`/`rank`/`top`/`sort_by` · tabla vacía es tabla, no error

**Plan e invariantes (8)** — `train.end <= test.start` obligatorio · test solapados se
rechazan · índices no contiguos se rechazan · ventana fuera del dataset se rechaza ·
`[start, end)`: la barra frontera cae en una sola ventana · `plan_id` estable · plan distinto
⇒ id distinto · ventana vacía se rechaza

**Derivación (4)** — dos definiciones por fold · roles correctos · ids distintos entre folds
· el linaje va al ledger, no a la definición

**Agregación (5)** — solo folds TEST · peor fold reportado · fallidos excluidos de la
mediana y contados · `positive_share_of_completed` con el denominador correcto · dispersión
`None` con un solo fold

## 12. Riesgos de overfitting

1. **El riesgo mayor es que walk-forward *parezca* validación.** Sin selección, un plan que
   sale bien demuestra **estabilidad entre ventanas**, no edge fuera de muestra: la
   configuración la eligió una persona que ya vio todos los datos. Es el malentendido más
   caro disponible en esta fase y merece decirse en el docstring del módulo, no solo aquí.
2. **"TRAIN" sugiere ajuste.** Hoy no ajusta nada. Riesgo de vocabulario.
3. **Multiplicidad**: un plan de N folds da N oportunidades de que algo salga bien por azar.
   Mitigado reportando peor fold y dispersión, no mediana sola.
4. **Selección de plan**: probar varias particiones hasta que una funcione es sobreajuste
   sobre la partición. El ledger lo cuenta; nada lo impide.
5. **`compare()` no ordena, pero quien lo lee sí.** La neutralidad llega hasta el borde del
   proceso.

## 13. GO / NO-GO

**GO — roles**, y **con prioridad de calendario**: por F1+F2 es gratis ahora y no volverá a
serlo.

**GO — `compare()`**. Superficie pequeña, sin estado, sin I/O.

**GO — walk-forward** como instrumento de medición, con los invariantes comprobados al
construir el plan.

**NO-GO — cualquier selección**, incluidos "el fold mediano", "descartar el peor fold" y
"elegir la partición que mejor se comporta".

**NO-GO — score único agregado.**

**NO-GO — arreglar P1–P4 dentro de M9c.2.** Ninguno bloquea el diseño; mezclarlos ocultaría
qué cambió y por qué. Van en su propia tanda.
