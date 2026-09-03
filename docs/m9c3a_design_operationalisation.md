# M9c.3a — DISEÑO: Operacionalización del Research Framework

**Estado:** DISEÑO. Nada implementado. Base: `b850c6a`, suite 1800 passed.

---

## 0. Hechos medidos, y dos que cambian el diseño

| # | Hecho | Consecuencia |
|---|---|---|
| **F1** | **El `ExperimentResult` no se persiste en ninguna parte.** `ExperimentLedger.record()` escribe solo la `LedgerEntry`; `research/store.py` se diseñó en M9b y nunca se implementó | `compare` **no tiene nada que leer**, y la agregación no tiene cifras que agregar. Es un pendiente que no estaba en tu lista: **P8** |
| **F2** | La fuente productiva de reglas es `BinanceSpotSymbolRulesProvider`: **una llamada de red que devuelve las reglas de hoy**. No hay ninguna persistida | Dos corridas del mismo backtest histórico separadas por meses recibirían reglas distintas → resultado distinto → **`REPRODUCIBILITY_FAILURE` falso**, con `bars_digest` y `code_revision` idénticos |
| F3 | `cli/paper.py::_symbol_rules` ya falla ruidosamente ante reglas ausentes: *"a default tick size is a wrong tick size"* | El precedente de fail-loudly existe y se reutiliza |
| F4 | Sigue sin haber ningún ledger en disco | El cambio de hash de P2 sigue siendo gratis |
| F5 | `LedgerEntry` no tiene identificador propio | `compared_with` necesita algo a lo que apuntar |

**F2 es el hallazgo central.** P1 no es "el CLI pasa un store vacío": es que la fuente
productiva de reglas **varía en el tiempo**, y eso es incompatible con investigación
reproducible. Arreglar P1 copiando lo que hace paper produciría un framework que se acusa a sí
mismo de irreproducible cada vez que el venue ajusta un tick.

---

## 1. Archivos exactos

| Archivo | Cambio |
|---|---|
| `research/definition.py` | `DatasetSpec.market_type`; `DatasetSpec.symbol_rules` |
| `research/store.py` | **nuevo** — persistencia de resultados (**P8**) |
| `research/ledger.py` | `entry_id`, `verdict`, `result_path`; auto-verificación en `record()` |
| `research/aggregate.py` | `FoldRun`; `aggregate_walk_forward` sobre linaje verificado |
| `research/plan_runner.py` | **nuevo** — `WalkForwardRunner`, protocolo `BarLoader` |
| `orchestration/research.py` | `RepositoryBarLoader`; `symbol_rules_for()` |
| `cli/research.py` | `verify`, `compare`, `walk-forward`, `capture-rules` |
| tests | 4 archivos nuevos + adiciones |

## 2. Solución P1 — reglas de venue

**Rechazado: buscarlas en vivo al correr** (lo que hace paper). Correcto para operar, ruinoso
para investigar: por F2 haría irreproducible todo experimento y llenaría el ledger de
violaciones falsas.

**Propuesta: las reglas se fijan en la definición.**

```
DatasetSpec.symbol_rules: SymbolRules
```

- Mismo **contrato** que paper y backtest productivo — el modelo `SymbolRules` sin cambios.
- Mismo **proveedor** para obtenerlas: `BinanceSpotSymbolRulesProvider`, invocado **una vez**
  al escribir la definición, no en cada corrida.
- Sin hardcodear, sin duplicar lógica, sin defaults inventados, sin saltar validaciones: el
  Risk Engine recibe exactamente el mismo objeto que recibiría en paper.
- **Sin reglas → la definición no construye.** Es fail-loudly *antes* de ejecutar, y más
  temprano que el precedente de F3: en el fichero, no en el arranque.

Comando de apoyo, para que nadie escriba un tick a mano:

```bash
qp research capture-rules --symbol BTC/USDT --out rules.json
```

**Efecto secundario deseable:** las reglas pasan a formar parte del `experiment_id`. Dos
corridas bajo reglas distintas son dos experimentos, que es la verdad.

## 3. Solución P2 — market_type, y el efecto en los hashes

`DatasetSpec.market_type: MarketType`, sin default. El CLI deja de asumir `SPOT`.

Junto con `symbol_rules`, **cambia todos los `experiment_id`**. Por F4 no hay evidencia que
invalidar. Es la **segunda y última** vez que esto sale gratis: los dos cambios van juntos en
una sola migración, y después la definición queda cerrada para siempre salvo motivo grave.

## 4. Semántica de auto-verificación (P4)

Elegida la opción **A** con un matiz: registrar y verificar en el mismo acto, sin que la
verificación pueda impedir el registro.

Al llamar `record(result)`:

1. Se escribe **siempre** la entrada del resultado. **Primero.** Una verificación no puede
   bloquear la evidencia que la origina.
2. Se buscan entradas previas con el mismo `experiment_id`.
   - **Ninguna** → `verdict = None`. No hay con qué comparar y no se finge que sí.
   - **Alguna** → se compara contra la más reciente que **comparta `bars_digest` y
     `code_revision`**; si no existe ninguna así, contra la más reciente sin más, solo para
     informar. *Motivo:* comparar siempre contra la última confundiría el caso real —
     ejecutar A, luego B, luego A otra vez daría `CODE_CHANGED` y taparía la contradicción
     con la primera A.
3. El `verdict` se graba **en la entrada**, sea cual sea. `DATASET_CHANGED`, `CODE_CHANGED` e
   `INDETERMINATE` quedan visibles sin línea extra: son información, no alarma.
4. Solo `REPRODUCIBILITY_FAILURE` añade **una segunda entrada**, con
   `status=REPRODUCIBILITY_FAILURE` y `compared_with` apuntando al `entry_id` de la que
   contradice.

Nada se sobrescribe. El ledger sigue append-only y una violación se lee como lo que es: dos
hechos y una contradicción entre ellos.

`entry_id` (**F5**) = SHA256 del contenido canónico de la entrada, excluido él mismo.

## 5. `qp research verify`

```bash
qp research verify --ledger <path> [--experiment-id <id>]
```

Recorre el ledger, agrupa por `experiment_id`, compara cada entrada con su predecesora
comparable y **reporta el recuento de los cinco veredictos**. No escribe: el registro ya lo
hizo `record()`. Sale ≠0 si encuentra alguna `REPRODUCIBILITY_FAILURE`, para que valga en CI.

## 6. `qp research compare`

```bash
qp research compare --ledger <path> <experiment_id>... [--json]
```

Lee los resultados por el store (**P8**), en el **orden en que se pidieron los ids** — el
lector trajo un orden y se le devuelve. 17 métricas, FAILED incluidos, sin ordenar por
rendimiento, sin ganador. Salida de tabla legible por defecto y `--json` para máquina, que es
el patrón que ya usa `cli/data.py`.

## 7. `WalkForwardRunner` (P6)

```
research/plan_runner.py
    BarLoader (Protocol):  (symbol, market_type, timeframe, window) -> Sequence[MarketBar]
    WalkForwardRunner.run(base, plan, *, loader, factory, ledger, code_revision) -> WalkForwardRun
```

Vive en `research` y no compone nada: recibe el `BackestFactory` y un `BarLoader`, ambos
puertos. `orchestration` implementa el loader sobre el repositorio async. Respeta el
presupuesto de dependencias sin tocarlo.

Secuencia: deriva las definiciones → por cada fold carga las barras de su ventana `[start,
end)` → ejecuta por `ExperimentRunner` → registra **cada** resultado con `plan_id` y
`fold_index` → **agrega solo al terminar el plan entero**.

`TRAIN` y `TEST` corren la misma configuración. **No hay fitting.**

## 8. Política ante fold FAILED (P6.7)

Un fold que falla **no detiene el plan**: cada ventana es una observación independiente, y
abandonar tras la primera convertiría un plan de diez folds en un plan de uno.

La excepción es la seguridad, y hay que nombrarla: si el fallo es de **integridad**
(`DataIntegrityError`, `PositionRiskUnavailableError`) el plan **se detiene**. Esos errores
dicen que el estado no es describible, y seguir produciría nueve folds más cuyo significado
nadie puede defender. Un fallo de estrategia o de configuración es local; uno de integridad
no lo es.

Todo fold ejecutado queda registrado, incluidos los fallidos y los no ejecutados por parada
temprana — estos con `status=FAILED` y el motivo. **Nada se descarta.**

## 9. Contrato seguro de agregación (P7)

```
FoldRun(entry: LedgerEntry, result: ExperimentResult)
aggregate_walk_forward(runs: Sequence[FoldRun], *, require_complete: bool = True)
```

Ya no acepta una lista desnuda de resultados: la pertenencia se **demuestra** con el linaje
de la entrada. Rechaza, levantando:

- más de un `plan_id`
- `fold_index` duplicado
- linaje ausente (`plan_id` o `fold_index` en `None`)
- rol `WALK_FORWARD_TRAIN` donde se espera `TEST`
- folds faltantes cuando `require_complete` (por defecto **sí**)

**Ningún agregado parcial silencioso.** Un resumen de siete folds de un plan de diez es una
afirmación distinta, y si se quiere hay que pedirla explícitamente.

## 10. Resolución de reglas — resumen

| Cuándo | Quién | Qué pasa si faltan |
|---|---|---|
| Al escribir la definición | `qp research capture-rules` → `BinanceSpotSymbolRulesProvider` | el comando falla; no escribe fichero |
| Al construir la definición | validador de `DatasetSpec` | **no construye** |
| Al correr | nadie — ya están en la definición | no puede faltar |

## 11. Tests exactos (34)

**Reglas y market type (7)** — definición sin reglas no construye · las reglas entran en el id
· `market_type` entra en el id · llega al motor · una definición con reglas corre de verdad
sobre barras reales · `capture-rules` escribe reglas usables · `capture-rules` falla ante
símbolo desconocido

**Store (3)** — un resultado persiste y se relee · el ledger apunta a él · un resultado
ausente se reporta, no se inventa

**Auto-verificación (6)** — primera corrida ⇒ `verdict=None` · repetición idéntica ⇒
`REPRODUCIBLE` · datos distintos ⇒ `DATASET_CHANGED` visible en la entrada · código distinto
⇒ `CODE_CHANGED` visible · contradicción ⇒ **entrada adicional** con `compared_with` · el
resultado original se escribe igualmente

**verify CLI (3)** — cuenta los cinco veredictos · sale ≠0 ante una violación · un ledger
limpio sale 0

**compare CLI (4)** — conserva el orden pedido · conserva FAILED · 17 métricas · `--json`
legible por máquina

**Walk-forward runner (6)** — ejecuta todos los folds · un fold FAILED no elimina los
siguientes · un fallo de integridad **sí** detiene el plan · linaje correcto por fold ·
TRAIN/TEST correctos · agrega solo al final

**Agregación segura (4)** — rechaza dos `plan_id` · rechaza `fold_index` duplicado · rechaza
linaje ausente · rechaza plan incompleto salvo petición explícita

**End-to-end (1)** — definition + plan → datos → motor → store → ledger → verify → aggregate

## 12. Riesgos de regresión

1. **Los `experiment_id` cambian por segunda y última vez.** Gratis hoy por F4; hay que
   hacerlo ahora o no hacerlo.
2. **`DatasetSpec` gana dos campos obligatorios** → toda definición existente en tests deja de
   construir. Es la mayor superficie mecánica del milestone.
3. **`record()` deja de ser una escritura pura**: lee el ledger antes de escribir. Con
   ficheros grandes es O(n) por registro. Aceptable a esta escala; hay que decirlo.
4. **`aggregate_walk_forward` cambia de firma** → los cinco tests de M9c.2 se reescriben.
5. **`capture-rules` toca la red.** Debe quedar fuera de la suite por defecto o usar
   transporte inyectado, como ya hacen los tests del proveedor.

## 13. Partición interna

Un solo commit, con este orden interno: **(a)** store + persistencia de resultados (P8, es lo
que desbloquea todo lo demás) → **(b)** `market_type` + `symbol_rules` y la migración de
fixtures → **(c)** auto-verificación y `entry_id` → **(d)** los tres comandos de CLI →
**(e)** plan runner y agregación segura.

Partirlo en dos commits dejaría un estado intermedio donde la definición ya cambió de hash y
la mitad de las capacidades sigue sin ejecutarse — el peor sitio donde parar.

## 14. GO / NO-GO

**GO — P8 (store).** No estaba en tu lista y es la dependencia de P5 y P7. Sin él, `compare`
no tiene qué leer.

**GO — P1 con reglas fijadas en la definición.** **NO-GO — resolverlas en vivo al correr**:
por F2 haría irreproducible cada experimento y produciría violaciones falsas.

**GO — P2**, ahora, por F4.

**GO — P3, P4, P5, P6, P7** según lo anterior.

**NO-GO — parar el plan ante cualquier fallo.** Solo ante fallos de integridad.

**NO-GO — agregados parciales por defecto.**

**NO-GO — sensibilidad, régimen y estrés.** Son M9c.3b.
