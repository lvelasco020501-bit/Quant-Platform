# M8 — DISEÑO: Position Management V2

**Estado:** DISEÑO. Nada implementado. Base: `8fd34cc`, suite 1638 passed.
EMA20/50 congelada. Strategy no se toca.

---

## 0. Tres defectos latentes que M8 debe arreglar antes de añadir nada

| # | Defecto | Evidencia |
|---|---|---|
| **D1** | `_update_position_risk` **reconstruye** `PositionRiskState` y **no arrastra `highest_price_seen`** desde `existing` | [engine.py:660-682](src/quantplatform/backtesting/engine.py:660). Inocuo hoy porque el campo no se escribe nunca; **fatal para trailing**: cada fill reiniciaría el ancla |
| **D2** | `_update_position_risk` recorre **solo símbolos con fills** (`for symbol in {fill.symbol for fill in fills}`) | [engine.py:663](src/quantplatform/backtesting/engine.py:663). Trailing, break-even y TP se actualizan **sin fill**: hoy no existe camino por barra |
| **D3** | `risk_amount` se recalcula desde la cantidad **restante**, pero su docstring dice "at risk **at entry**: the denominator of every R-multiple" | [risk.py:394](src/quantplatform/core/models/risk.py:394) vs [engine.py:672](src/quantplatform/backtesting/engine.py:672). Tras una salida parcial R cambiaría de significado en silencio |

`highest_price_seen` existe en el modelo y **no se lee ni escribe en ningún sitio de `src/`** — verificado por grep. Es vocabulario sin consumidor, igual que lo eran los breakers antes de M7b.

---

## 1. Arquitectura propuesta

Un único añadido conceptual: **el estado de riesgo de una posición avanza por barra, no por fill.**

```
_process_bar:
    ... fills, contabilidad, _update_position_risk (por fill)   ← D1/D2 corregidos
    _snapshot
    _record_closed_trades / _update_equity_anchors / _update_breakers
    _advance_position_risk(bar, state)      ← NUEVO: trailing, break-even, TP armado
    _forced_exit_intents(bar, snapshot, state)   ← ya existe; gana TP y time stop
    _authorise(forced_exit=True)
    _generate → _build_intents → _authorise
```

`_advance_position_risk` pide a Risk el estado **siguiente** de cada posición y lo guarda. Risk sigue sin estado y sin reloj: recibe la barra y el estado actual, devuelve el estado nuevo. La orquestación sigue siendo la única que muta. Respeta la dirección `backtesting → risk`.

Nuevo método en `StandardRiskEngine`:

```
advance_position_risk(*, positions, position_risk, bar) -> Mapping[str, PositionRiskState]
```

Separado de `evaluate_open_positions`, que sigue devolviendo acciones. **Una decide qué nivel rige mañana; la otra decide si el nivel de hoy se rompió.** Mezclarlas haría que un nivel movido por esta barra juzgara esta misma barra, que es justo lo que la §5 prohíbe.

## 2. Contratos y modelos

**Ya existe todo el vocabulario de stops**: `StopKind.HARD/TRAILING/BREAK_EVEN/TIME`, `StopSpecification.activated_at`, `max_holding_seconds`, `PositionRiskState.highest_price_seen`. Nada de eso hay que inventarlo.

**Falta take-profit.** No hay nada en `src/` — verificado por grep.

`PositionRiskState` gana dos campos, porque un stop protector, un objetivo y un plazo son tres cosas que coexisten y `stop` es un único `StopSpecification`:

```
take_profit: Price | None = None      # objetivo absoluto, derivado por Risk al abrir
time_stop_at: UtcDatetime | None = None   # instante límite, derivado de opened_at
```

`time_stop_at` como instante absoluto en lugar de `StopSpecification(kind=TIME)` porque el plazo se resuelve **una vez al abrir** desde `opened_at`, y guardarlo resuelto elimina la aritmética repetida y hace el estado legible en el snapshot.

`RiskCheckCode` nuevos: `TAKE_PROFIT`, `TIME_STOP`. `RiskActionKind.CLOSE` ya cubre las salidas; `REDUCE` queda sin usar hasta M9 (§10).

## 3. Archivos que tocaría

| Archivo | Cambio |
|---|---|
| `core/enums.py` | 2 `RiskCheckCode` |
| `core/models/risk.py` | `PositionRiskState.take_profit`, `.time_stop_at` |
| `core/models/paper.py` | ninguno — `PositionRiskState` viaja entero |
| `risk/config.py` | 5 knobs, todos `None` por defecto |
| `risk/engine.py` | `advance_position_risk`; TP y time stop en `evaluate_open_positions`; derivación de TP y plazo al aprobar |
| `risk/sizing.py` | reutilizar `projected_stop_out_cost` para el nivel de break-even |
| `backtesting/engine.py` | `_advance_position_risk`; D1 y D2 |
| tests | `test_exit_management.py` (nuevo) + adiciones |

## 4. Prioridad entre salidas — tu jerarquía tiene un error de categoría

Propusiste:

```
DATA INTEGRITY → HARD STOP → TRAILING STOP → BREAK-EVEN → TAKE-PROFIT → TIME STOP → STRATEGY EXIT
```

**HARD, TRAILING y BREAK-EVEN no son tres salidas que compitan: son tres formas de fijar el mismo y único nivel protector.** Una posición tiene un stop a la vez; trailing y break-even lo *mueven*, no compiten con él. Ponerlos en tres escalones implicaría que pueden dispararse por separado en la misma barra, y no pueden: solo hay un `trigger_price`.

Jerarquía corregida:

```
1. DATA INTEGRITY        → levanta y para la corrida (no es un check)
2. PROTECTIVE STOP       → el nivel vigente, sea hard, trailed o movido a break-even
3. TAKE-PROFIT
4. TIME STOP
5. STRATEGY EXIT
```

**Y el hallazgo que más simplifica M8:** con salidas totales y ejecución a la apertura de la barra siguiente, los niveles 2–5 producen **exactamente la misma orden** — vender todo al próximo open. La prioridad determina el **motivo registrado**, no la economía.

La jerarquía es una cuestión de auditoría hasta que existan salidas parciales. Es el argumento principal del §10.

## 5. Política intrabar conservadora

Con OHLC no sabemos si el `high` precedió al `low`. Dos casos, y solo uno es real:

**Caso 1 — TP y stop tocados en la misma barra, salida total.** Ambos disparan; ambos producen la misma orden al mismo precio. **No hay resultado favorable que elegir porque no hay dos resultados.** Se registra el stop como motivo, por prioridad. No se necesita política de desempate económico.

**Caso 2 — un nivel movido por esta misma barra.** Este sí es real, y es el que hay que legislar:

> **Un disparo se juzga contra el stop tal como estaba al abrir la barra.**

Un trailing subido por el `high` de esta barra **no estuvo en el mercado** durante la parte de la barra anterior a ese `high`. Aplicarlo hacia atrás inventaría una protección que no existía y produciría un stop-out a un precio mejor del que la cuenta podía haber conseguido. Por eso `advance_position_risk` corre **después** de `evaluate_open_positions` en el orden lógico, y lo que calcula rige **desde la barra siguiente**.

Es la misma disciplina que ya hace honesta la ejecución next-bar, aplicada al nivel en vez de al fill. Es reproducible desde OHLC solo, y no elige nunca el lado favorable.

**Caso 3 — gap a través del stop.** Sin cambios respecto a M6: el disparo es el toque, el fill es el próximo open, y el hueco cuesta lo que cuesta. No hay fill al nivel del stop.

## 6. Semántica de TAKE-PROFIT

- Derivado por Risk al aprobar la entrada, desde `take_profit_distance_bps`, anclado al **`reference_price`** — no al `valuation` — por la misma razón que M7a corrigió para el stop.
- Disparo: `bar.high >= take_profit`. Toque exacto dispara, simétrico con el stop.
- Salida **total** en M8. Parcial diferido (§10).
- Sin configurar → `take_profit is None` → nunca dispara. V1 idéntico.

## 7. Semántica de TRAILING

- **Activación**: se arma cuando `bar.high >= entry_price * (1 + trailing_activation_bps)`. `activated_at` se sella entonces y no se vuelve a sellar.
- **Ancla**: `highest_price_seen = max(highest_price_seen, bar.high)`, actualizado **cada barra** una vez armado. Requiere D1 y D2 arreglados.
- **Nivel**: `highest_price_seen * (1 - trailing_stop_distance_bps)`.
- **Nunca afloja**: `nuevo_trigger = max(nuevo_trigger, trigger_actual)`. Es una invariante del modelo, no del cálculo: un stop que retrocede es un stop que devuelve riesgo que ya se había retirado. Se comprueba y, si el cálculo produjera un retroceso, se conserva el anterior.
- **Rige desde la barra siguiente** (§5).

## 8. Semántica de BREAK-EVEN — no llamar break-even a una pérdida

Mover el stop a `entry_price` **pierde**: paga slippage de entrada, comisión de entrada, slippage de salida y comisión de salida. Llamarlo break-even sería exactamente el tipo de nombre que miente que este proyecto rechaza.

El nivel correcto ya es calculable con maquinaria existente. `projected_stop_out_cost(quantity, entry_price, stop, side, policy)` ([sizing.py](src/quantplatform/risk/sizing.py)) devuelve lo que cuesta ser stopeado en un nivel, **incluyendo slippage y las dos comisiones**. El break-even honesto es la **raíz de esa función**: el `trigger_price` para el que el coste proyectado es cero.

Se resuelve en cerrado, no por búsqueda: la función es afín en `trigger_price` para los tres modelos de comisión (NONE, BASIS_POINTS, FLAT), así que dos evaluaciones determinan la recta y su raíz. Es la misma técnica de sondeo con la que M4 descompuso las comisiones.

- **Activación**: `bar.high >= entry_price * (1 + break_even_activation_bps)`.
- **Efecto**: reemplaza el stop protector por `StopKind.BREAK_EVEN` en el nivel de coste cero, **solo si es superior al stop actual** (nunca afloja).
- Rige desde la barra siguiente.

## 9. Semántica de TIME STOP

- Configurado en **barras**, no en segundos: `max_holding_bars`. El timeframe es un hecho de la corrida, y expresar el plazo en la unidad en que llegan los datos evita que un plazo en segundos caiga a mitad de vela y haya que decidir qué significa.
- `time_stop_at = opened_at + max_holding_bars * timeframe.seconds`, resuelto al abrir.
- Disparo: `bar.close_time >= time_stop_at`. Frontera exacta dispara.
- **Consecuencia que hay que decir en voz alta**: como todo disparo llena al open siguiente, un time stop de 3 barras **sale en la apertura de la cuarta**. La posición se mantiene 3 barras completas y se liquida en la primera impresión posterior. Documentarlo o se leerá mal.
- **Interacción con hard stop**: el stop tiene prioridad, pero bajo salidas totales producen la misma orden — la diferencia es el motivo registrado.
- **Interacción con Strategy EXIT**: una sola salida. La forzada se autoriza primero; la estratégica sobre el mismo símbolo choca con `CONFLICTING_ORDER` — bloqueante, porque no es forzada — y además el balance base ya está reservado. **A verificar con test explícito, no a asumir.**

## 10. Partial exits — **DEFER a M9**

A favor de GO (más soporte del esperado, todo verificado):
- `RiskActionKind.REDUCE` existe y valida que se declare cantidad.
- `build_forced_exit_intent` ya honra `action.quantity` ([intents.py:167](src/quantplatform/backtesting/intents.py:167)).
- El portfolio ya reduce parcialmente (`new_quantity = previous.quantity - fill.quantity`).
- `_update_position_risk` ya reescala el registro en una reducción.

A favor de DEFER, y pesa más:
1. **Las parciales son lo único que vuelve económicamente real la ambigüedad intrabar.** Con salidas totales, TP y stop en la misma barra producen la misma orden (§5). Con un TP parcial y un stop total, las cantidades difieren y hay que elegir — que es precisamente lo que dijiste que no aceptas. Diferir las mantiene inertes.
2. **D3 sin resolver.** `risk_amount` se recalcula desde lo que queda, mientras su contrato dice "at entry". El denominador de R cambiaría en silencio en cada scale-out. Hay que decidir si R se ancla al riesgo original o al vigente — decisión de medición, no de implementación, y merece su propia discusión.
3. **Las trade statistics contarían mal.** `_record_closed_trades` cuenta un ciclo de vida completo ([engine.py:744](src/quantplatform/backtesting/engine.py:744)); una posición parcialmente cerrada no aporta nada hasta cerrarse del todo, así que una estrategia de scale-out tendría estadísticas engañosas mientras la desarrollamos.
4. **No hay evidencia que lo justifique.** EMA20/50 no tiene edge demostrado del que hacer scale-out.

**M8 = salidas totales.** Si un TP parcial resulta valioso, se hace en M9 con D3 resuelto primero.

## 11. Persistencia

`PositionRiskState` viaja entero dentro de `PaperSessionState.position_risk`; los dos campos nuevos van con él sin tocar `paper.py`. `Price | None` y `UtcDatetime | None` son tipos que el modelo ya serializa.

Sin `computed_field` nuevo, así que `_COMPUTED_FIELD_EXCLUSIONS` no interviene — **a verificar con test de round-trip**, no por lectura. El resume sigue fail-closed sobre cualquier `position_risk`, sin cambio.

## 12. Tests exactos (tus 14 + 11)

**Take-profit (3)** — 1 TP no alcanzado, sin acción · 2 TP alcanzado, salida · 3 TP sin configurar nunca dispara

**Trailing (5)** — 4 no armado por debajo de la activación · 5 armado sella `activated_at` una vez · 6 el nivel sigue al máximo · 7 **nunca retrocede** en una barra adversa · 8 el ancla **sobrevive un fill** (regresión de D1)

**Break-even (3)** — 9 activa en el avance configurado · 10 el nivel deja el round trip modelado en cero, no en `entry_price` · 11 nunca afloja un stop ya superior

**Time stop (3)** — 12 no dispara una barra antes · 13 dispara exactamente en la frontera · 14 sale en el open de la barra siguiente al disparo

**Intrabar y prioridad (4)** — 15 stop y TP en la misma barra: una salida, motivo = stop · 16 un trailing subido por esta barra **no** juzga esta barra · 17 trailing + Strategy EXIT: **una sola** salida · 18 gap a través del stop llena al open, no al nivel

**Estado (4)** — 19 trailing mueve el stop y conserva el resto del estado · 20 break-even reemplaza el stop y conserva `opened_at` · 21 cierre total elimina el risk state · 22 round-trip del snapshot con TP y plazo

**Breakers y costes (3)** — 23 breaker activo no bloquea ninguna de las cuatro salidas · 24 breaker no resetea ni mueve stops · 25 fees y slippage pasan por el broker normal

**Golden V1 (2)** — 26 sin configuración, corrida byte-idéntica · 27 señales EMA sin cambios

## 13. Riesgos de regresión

1. **D2 obliga a un camino de actualización por barra**, no por fill. Es el cambio estructural de M8: hoy `position_risk` solo se toca cuando hay fill.
2. **D1 es un arreglo silencioso** — nadie lo nota hoy porque el campo no se usa. Necesita test propio o volverá a romperse.
3. **`_advance_position_risk` corre en cada barra para cada posición abierta**, mientras `_update_position_risk` corría solo tras fills. Más trabajo por barra; irrelevante a escala H1, pero es un cambio de perfil.
4. **La derivación del break-even asume que `projected_stop_out_cost` es afín en el trigger.** Cierto para NONE, BASIS_POINTS y FLAT; un modelo de comisión nuevo lo rompería sin avisar. Necesita test que fije la propiedad, no solo el valor.
5. **`PositionRiskState` gana campos** → todo constructor del modelo en tests debe seguir compilando. Tienen default `None`, así que no debería romper; a medir, como en M7a.
6. **La deduplicación forzada/estratégica descansa en `CONFLICTING_ORDER` y en la reserva de balance**, ninguno de los dos escrito para esto. Test explícito obligatorio.

## 14. Propuesta de división

**M8a — el nivel protector.** D1, D2, `_advance_position_risk`, la regla "el stop de la barra es el de su apertura", trailing y break-even. Un solo concepto: el stop que ya existía ahora se mueve. Sin salidas nuevas, sin `RiskCheckCode` nuevos.

**M8b — salidas nuevas.** Take-profit y time stop: dos campos, dos códigos, la jerarquía de motivos y la deduplicación con Strategy EXIT.

Divide por lo que cambia: M8a mueve un nivel existente y arregla dos defectos latentes; M8b añade razones de salida que antes no existían. Si M8b se atasca, M8a ya deja el trailing y el break-even en verde — y los dos defectos, corregidos.

## 15. GO / NO-GO

**GO — M8a** (trailing, break-even, D1, D2, política intrabar). Vocabulario existente, maquinaria de costes existente, superficie acotada.

**GO — M8b** (take-profit, time stop) con salidas **totales**.

**DEFER — salidas parciales**, a M9, con D3 resuelto antes. Es lo único que haría económicamente real la ambigüedad intrabar, y no hay evidencia que lo pida.

**NO-GO — cualquier cambio en Strategy.** EMA20/50 sigue congelada; toda la gestión de salida vive en Risk, que es lo que permite comparar el mismo benchmark con y sin ella.

**NO-GO — `risk_amount` como denominador móvil.** Mientras no se resuelva D3, ningún componente nuevo debe apoyarse en él para medir R.
