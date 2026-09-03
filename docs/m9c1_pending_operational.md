# M9c.1 — Pendientes operativos

Registrados al cerrar M9c.1 (`c0e543c`). Ninguno bloqueaba el diseño de M9c.2; el primero sí
condicionaba su CLI.

**Actualizado en la auditoría documental de M9c.3a.1 (2026-09-03): los cuatro están
resueltos.** La tabla original se conserva sin editar por debajo de la línea; el estado se
corrige aquí porque este documento se sigue citando como referencia viva (`m9c2` y `m9c3a` lo
enlazan) y dejarlo en "No corregidos" contradice el código actual.

| # | Pendiente | ¿Bloquea M9c.2? | Estado | Resuelto en |
|---|---|---|---|---|
| P1 | `SymbolRulesStore({})` vacío en el CLI | No bloquea el diseño, pero `walk-forward` heredaría el defecto | **RESUELTO** — `DatasetSpec.symbol_rules` fija las reglas del venue dentro de la propia definición; nada se consulta en vivo al reproducir | M9c.3a (`408ed98`) |
| P2 | `market_type` hardcodeado a `SPOT` | No | **RESUELTO** — `DatasetSpec.market_type: MarketType` es campo obligatorio | M9c.3a (`408ed98`) |
| P3 | `verify()` sin entrypoint | No | **RESUELTO** — `qp research verify`, con salida humana por defecto y `--json` para máquina | M9c.3a (`408ed98`); salida humana en M9c.3a.1 (`554c37e`) |
| P4 | El ledger no verifica al registrar | No | **RESUELTO** — `ExperimentLedger.record()` verifica automáticamente contra el intento comparable antes de devolver la entrada | M9c.3a (`408ed98`) |

---

## Tabla original (M9c.1, sin editar)

| # | Pendiente | Detalle | ¿Bloquea M9c.2? |
|---|---|---|---|
| P1 | **`SymbolRulesStore({})` vacío en el CLI** | `cli/research.py` construye el factory con un store vacío. Una definición sobre barras reales fallaría al no encontrar reglas de venue. Hoy pasa porque el test corre sobre BD migrada y vacía. | **No bloquea el diseño**, pero `qp research walk-forward` heredaría el mismo defecto. Debe arreglarse antes de que cualquiera de los dos comandos corra con datos reales. |
| P2 | **`market_type` hardcodeado a `SPOT`** | `DatasetSpec` no lo declara y el CLI lo asume. | No. |
| P3 | **`verify()` sin entrypoint** | La función existe y nada la invoca en operación. | No. |
| P4 | **El ledger no verifica al registrar** | Detectar una violación exige llamar `verify()` a mano. | No. |

**P1 es el más serio**: es la diferencia entre un CLI que funciona sobre una base vacía y uno
que funciona. Se corrige en M9c.2 sólo si el diseño lo exige; en caso contrario, en su propia
tanda junto a P2, P3 y P4.
