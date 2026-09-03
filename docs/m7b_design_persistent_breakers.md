# M7b — DISEÑO: Circuit Breakers Persistentes

**Estado:** DISEÑO. Nada implementado. Base: `ae22c7f`, suite 1608 passed.

---

## 0. Hallazgo previo — defecto vivo que contradice la decisión 2

Tu decisión 2 dice: *ningún breaker puede bloquear SELL / forced exit*. El check **instantáneo** de drawdown que ya existe hoy sí lo hace.

Demostrado, no razonado:

```
engine = make_risk_engine(max_total_drawdown_pct=0.10)
ctx    = peak_equity=100_000, equity≈10_000   →  drawdown 90%
assess(SELL 0.1, ctx, forced_exit=True)
  → rejected
  → BLOQUEA: total_drawdown | total drawdown exceeds its limit
```

Es la misma inversión que M7a corrigió para spread y volatilidad, todavía viva en `_check_drawdown` ([risk/engine.py:648](src/quantplatform/risk/engine.py:648)). Un stop-out se rechaza precisamente cuando la cuenta más lo necesita. **M7b debe corregirlo**, o los breakers cumplirán tu regla mientras un check preexistente la viola.

---

## 1. Campos en `RiskConfiguration` — respuesta a tu pregunta

**Ningún threshold numérico nuevo. Ninguno de los tres.**

| Breaker | Threshold | Estado |
|---|---|---|
| MAX_DAILY_LOSS | `max_daily_loss_pct` | **ya existe**, `None` por defecto ([config.py:97](src/quantplatform/risk/config.py:97)) |
| MAX_CONSECUTIVE_LOSSES | `max_consecutive_losses` | **ya existe**, `None` por defecto ([config.py:104](src/quantplatform/risk/config.py:104)) |
| MAX_TOTAL_DRAWDOWN | `max_total_drawdown_pct` | **ya existe** ([config.py:79](src/quantplatform/risk/config.py:79)) |

### Sobre `max_total_drawdown_pct`: se reutiliza, pero no alcanza sola

El **nivel** lo expresa correctamente: `_record_drawdown` ya calcula `(peak − equity)/peak ≥ límite`, que es exactamente el trigger del breaker. Un segundo porcentaje sería un número más que un operador tendría que justificar sin evidencia para elegirlo. **No lo propongo.**

Lo que falta no es un umbral: es un **opt-in**. `max_daily_loss_pct` y `max_consecutive_losses` son `None` por defecto — activar el breaker es configurarlos. `max_total_drawdown_pct` tiene default `Decimal("0.20")`, siempre presente. Si el breaker latched lo leyera sin más, **toda corrida V1 existente ganaría un breaker que nunca tuvo**, rompiendo tu invariante "V1 sin configuración de breakers → comportamiento idéntico".

**Propuesta: un único campo booleano, `latch_total_drawdown: bool = False`.** No es un threshold; es el interruptor que a los otros dos les da su propio `None`. Con él:

- ningún número nuevo ✅
- V1 idéntico ✅
- invariante "threshold del breaker ≥ instantáneo equivalente" satisfecho **por identidad**: es el mismo campo ✅

La diferencia entre el check instantáneo y el breaker no es el nivel — es el **enclavamiento**. El instantáneo vuelve a pasar cuando la equity se recupera; el breaker no. Modelar esa diferencia como estado, no como un segundo número, es lo que el contrato actual ya permite.

## 2. Estructura del estado

### `RunState`

```
breakers: tuple[CircuitBreakerState, ...]      # una entrada por breaker enclavado
day_start_realized_pnl: Decimal                 # ancla del día, junto a day_start_equity
```

**`CircuitBreakerState` se reutiliza tal cual** ([core/models/risk.py:380](src/quantplatform/core/models/risk.py:380)): tiene `tripped_at`, `reason`, `resets_at`, `consecutive_losses`, `daily_loss`, la property `is_tripped` y un validador que exige razón en todo enclavamiento.

**Por qué una tupla y no una sola instancia.** El modelo lleva un único par `tripped_at`/`reason`, así que una sola instancia solo puede representar un breaker enclavado. Los tres tienen reglas de reset distintas: si daily-loss engancha a las 10:00 y drawdown a las 14:00, con un solo `reason` el rollover limpiaría un estado que debía quedar enclavado. Una entrada por razón elimina el problema en vez de gestionarlo.

Invariante: **cada `CircuitBreakerReason` aparece a lo sumo una vez**, reflejando la regla que `HealthStatus` ya aplica a su propia tupla ([health.py:78](src/quantplatform/core/models/health.py:78)). Violarla → fail loudly.

`CircuitBreakerReason` ya tiene las tres razones: `DAILY_LOSS_LIMIT`, `EXCESSIVE_DRAWDOWN`, `CONSECUTIVE_LOSSES` ([enums.py:541-542](src/quantplatform/core/enums.py:541)). Ninguna nueva.

### `PaperSessionState`

```
breakers: tuple[CircuitBreakerState, ...] = ()
```

## 3–4. Triggers y fuentes exactas

| Breaker | Trigger | Fuente |
|---|---|---|
| **MAX_DAILY_LOSS** | `(day_start_realized_pnl − snapshot.realized_pnl) / day_start_equity ≥ max_daily_loss_pct` | `PortfolioSnapshot.realized_pnl` + ancla nueva `RunState.day_start_realized_pnl` |
| **MAX_TOTAL_DRAWDOWN** | `(peak_equity − snapshot.equity) / peak_equity ≥ max_total_drawdown_pct` | `RunState.peak_equity` ([engine.py:709](src/quantplatform/backtesting/engine.py:709)), `snapshot.equity`. Aritmética idéntica a `_record_drawdown` |
| **MAX_CONSECUTIVE_LOSSES** | racha final de `t < 0` en `trade_results` ≥ `max_consecutive_losses` | `RunState.trade_results` ([engine.py:134](src/quantplatform/backtesting/engine.py:134)), alimentado por `_record_closed_trades` ([engine.py:736](src/quantplatform/backtesting/engine.py:736)) |

**Verificado: `realized_pnl` ya es neto de comisiones** — `realized_pnl=previous.realized_pnl + realized_after_fee` ([portfolio/engine.py:584](src/quantplatform/portfolio/engine.py:584)). No hace falta aritmética extra de fees, y un límite de pérdida diaria no la subestimará.

**Distinción deliberada:** `max_daily_loss_pct` cuenta dinero realizado y contabilizado; `max_daily_drawdown_pct` (instantáneo, ya existente) mide caída pico-a-valle incluyendo posiciones abiertas. No se solapan: una cuenta con gran pérdida abierta y nada realizado no dispara el breaker diario, y la cubre el instantáneo. Los dos juntos cubren ambos fallos. Es la distinción que el docstring del campo ya traza.

## 5. Cuándo se evalúa dentro de `advance()`

```
_process_bar:
    broker.process_bar            ← liquida lo autorizado en la barra anterior
    _account_for_fills
    verificación fill↔portfolio
    _update_position_risk
    history / last_close / features
    _snapshot
    _record_closed_trades         ← MOVIDO aquí desde el final
    _update_equity_anchors        ← MITAD de _record_equity, movida aquí
    _update_breakers              ← NUEVO: engancha, y resetea daily en rollover
    _forced_exit_intents → _authorise(forced_exit=True)
    _generate → _build_intents → _authorise
    _append_equity_point          ← la otra mitad de _record_equity
```

**Motivo del reordenado.** Hoy `_record_closed_trades` y `_record_equity` corren *después* de `_authorise` ([engine.py:317-318](src/quantplatform/backtesting/engine.py:317)). Un breaker alimentado desde ahí llegaría con **una barra de retraso**: la pérdida que cruza el límite se realiza en la barra N, pero la entrada de la barra N ya se habría autorizado. Se evalúa tras contabilizar los fills, que es cuando la posición y el PnL de la barra son definitivos.

`state.positions` solo lo usa `_record_closed_trades` ([engine.py:734,737](src/quantplatform/backtesting/engine.py:734)) — verificado por grep — así que moverlo no toca a nadie más.

## 6. Cómo se bloquean las entradas

`RiskContext.breakers: tuple[CircuitBreakerState, ...]`, poblado por `_risk_context`. Nuevo `_check_circuit_breakers(recorder, intent, context)` en `_decide`, después de `_check_drawdown`.

Un código por breaker configurado, **grabado exactamente una vez** por decisión. Bloquea sólo si `intent.side is OrderSide.BUY`.

Risk sigue sin estado y sin reloj: lee la tupla, no la muta. La orquestación es la única que engancha y resetea. Respeta la dirección de importación (`backtesting` → `risk`).

## 7. Cómo pasan las salidas

**Por lado, no por `forced_exit`.** Toda `SELL` registra los breakers como `ADVISORY`: el check corre, informa qué encontró, pierde el veto.

Es más fuerte que condicionar por `forced_exit` y dice la verdad sobre qué protege un breaker: reducir exposición nunca es aquello de lo que un breaker protege. Una salida estratégica bajo breaker también debe poder ejecutarse — querer cerrar es siempre legítimo.

Más el arreglo del §0: `_check_drawdown` recibe `forced_exit` y degrada `DAILY_DRAWDOWN`/`TOTAL_DRAWDOWN` a `ADVISORY` en salida forzada, igual que M7a hizo con spread y volatilidad.

## 8. Reset diario exacto

Dentro de `_update_equity_anchors`, en el mismo punto donde hoy se detecta el cambio de fecha ([engine.py:713-716](src/quantplatform/backtesting/engine.py:713)):

```
day = snapshot.taken_at.date()
if state.day_started_at != day:
    state.day_started_at        = day
    state.day_start_equity      = equity
    state.day_start_realized_pnl = snapshot.realized_pnl
    state.breakers = tuple(b for b in state.breakers
                           if b.reason is not CircuitBreakerReason.DAILY_LOSS_LIMIT)
```

**Sólo `DAILY_LOSS_LIMIT`.** `EXCESSIVE_DRAWDOWN` y `CONSECUTIVE_LOSSES` sobreviven el rollover intactos. Como cada razón vive en su propia entrada, esto no puede limpiar por accidente un enclavamiento estructural.

**Requiere enmendar el docstring de `CircuitBreakerState`**, que hoy dice que un breaker no se limpia solo. La excepción queda escrita, con su motivo, no deslizada.

## 9. Persistencia y round-trip

`PaperSessionState.breakers`, escrito en `capture()` junto a `position_risk` ([session.py:575](src/quantplatform/paper/session.py:575)).

`CircuitBreakerState` **no tiene `computed_field`** — `is_tripped` es `@property`, que no serializa — así que no interactúa con `_COMPUTED_FIELD_EXCLUSIONS` y el round-trip es plano. **A verificar con un test explícito, no a asumir**, porque es exactamente el punto donde `Balance.total` y `Position.cost_basis` ya sorprendieron antes.

## 10. Resume

Un breaker enclavado es estado financiero. Se añade a la lista `blocking` de `_refuse_stateful_resume` ([session.py:321-330](src/quantplatform/paper/session.py:321)), junto a posición abierta, balance reservado, PnL realizado, fees y risk state.

**Consecuencia explícita: la persistencia es para el operador y la forense, no para restaurar.** Una sesión con breaker enganchado no se reanuda; se lee. Es la misma política fail-closed que `position_risk`, y es lo que impide creer que un breaker sobrevive un reinicio hacia una sesión viva.

## 11. `RiskCheckCode` necesarios

Tres nuevos:

- `MAX_DAILY_LOSS = "max_daily_loss"`
- `MAX_TOTAL_DRAWDOWN_BREAKER = "max_total_drawdown_breaker"`
- `MAX_CONSECUTIVE_LOSSES = "max_consecutive_losses"`

**El de drawdown no puede reutilizar `TOTAL_DRAWDOWN`**: ese código ya lo graba `_check_drawdown`, y el invariante de `RiskDecision` exige que cada código aparezca a lo sumo una vez por decisión. Un código compartido rompería el invariante en la primera decisión donde ambos corran.

## 12. Tests exactos

**Configuración (4)** — 1 sin config, ningún breaker existe · 2 `latch_total_drawdown` por defecto False · 3 `max_daily_loss_pct=None` no engancha nunca · 4 activar el latch no introduce ningún número nuevo (el nivel sale de `max_total_drawdown_pct`)

**Triggers (6)** — 5 daily loss engancha al cruzar · 6 no engancha por debajo · 7 drawdown engancha al cruzar · 8 consecutive losses engancha en la N-ésima · 9 no engancha en la N−1 · 10 una pérdida abierta no dispara el breaker de pérdida *realizada*

**Autoridad (5)** — 11 breaker bloquea BUY · 12 breaker **no** bloquea SELL estratégica · 13 breaker **no** bloquea forced exit · 14 el check se registra ADVISORY en SELL, no oculto · 15 drawdown extremo **no** bloquea forced exit (§0)

**Reset (4)** — 16 daily loss se limpia en rollover · 17 drawdown **no** se limpia en rollover · 18 consecutive losses **no** se limpia en rollover · 19 un trade ganador **no** limpia el enclavamiento por sí solo

**Estado y persistencia (4)** — 20 razón duplicada en la tupla → fail loudly · 21 breakers sobreviven round-trip del snapshot · 22 resume con breaker enganchado se rechaza · 23 el rechazo nombra qué breaker

**Orden (2)** — 24 una pérdida realizada en la barra N bloquea una entrada de la barra N, no de la N+1 · 25 mover `_record_closed_trades` no altera la curva de equity ni las estadísticas de trades

**Golden V1 (2)** — 26 corrida sin config de breakers es idéntica · 27 sin cambios en señales de Strategy

## 13. Riesgos de regresión

1. **Partir `_record_equity` y mover `_record_closed_trades`** es el riesgo mayor: tocan la curva de equity, `peak_equity`, `day_start_equity` y `trade_results`, de los que dependen todas las métricas de rendimiento. Mitigación: el test 25 fija curva y estadísticas antes y después.
2. **`RunState` gana dos campos sin default** → `_initial_state()` debe inicializar ambos. Error exacto de M5b.
3. **`day_start_realized_pnl` en la primera barra**: `day_started_at` empieza en `None`, así que el primer rollover dispara en la barra 1 y ancla ahí. Correcto, pero hay que fijarlo con test o el primer día mediría desde cero en vez de desde el arranque.
4. **Degradar `DAILY_DRAWDOWN`/`TOTAL_DRAWDOWN` a advisory en salidas forzadas** cambia comportamiento con tests existentes en `test_risk_engine.py`. Usan intents BUY por defecto, así que no deberían romper — a confirmar midiendo, como en M7a.
5. **`PaperSessionState` gana campo** → verificar `_COMPUTED_FIELD_EXCLUSIONS` con test, no por lectura.
6. **Enmienda al docstring de `CircuitBreakerState`**: cambia un contrato documentado. Queda escrita.
7. **`max_total_drawdown_pct` sigue siendo obligatorio** con default 0.20. Si alguien activa `latch_total_drawdown` sin revisarlo, engancha a 20% sin haberlo elegido. Mitigación posible: exigir que activar el latch obligue a fijar el valor explícitamente. **Decisión tuya.**
