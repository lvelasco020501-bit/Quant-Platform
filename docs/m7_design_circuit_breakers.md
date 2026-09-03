# M7 — DISEÑO: Circuit Breakers + Protection Requirement

**Estado:** DISEÑO. Nada implementado. Base: commit `a16fe95`, suite 1588 passed.
**Principio rector:** preservar capital tiene prioridad sobre frecuencia de trading.

---

## 0. Evidencia recogida (trazado real, no supuesto)

| ID | Hecho | Fuente |
|---|---|---|
| E1 | `PaperTradingSession.submit_bar` llama `self._engine.advance(bar, self._state)` | `paper/session.py:391` |
| E2 | `advance` → `_process_bar`, donde M6 cableó `_forced_exit_intents` | `backtesting/engine.py:246`, `:298-306` |
| E3 | `_health()` devuelve `SystemState.HEALTHY` y `circuit_breakers=()` **hardcodeado**, en backtest y paper | `backtesting/engine.py:559-577` |
| E4 | Paper se prohíbe explícitamente alimentar la salud del feed al trading | `paper/session.py:427-431` |
| E5 | `CircuitBreakerState` existe con `consecutive_losses`, `daily_loss`, `tripped_at`, `reason`, `resets_at`, y contrato "no se limpia sola" | `core/models/risk.py:380-424` |
| E6 | `CircuitBreakerReason`, `CircuitBreakerTripped`, `CircuitBreakerTrippedError`, `RiskActionKind.HALT_NEW_ENTRIES` existen; **cero consumidores** | `enums.py:532`, `events.py:257`, `errors.py:376`, `enums.py:586` |
| E7 | `HealthStatus` invariante: HEALTHY + breaker tripped → ValueError | `core/models/health.py:88-90` |
| E8 | `SYSTEM_STATE` es BLOQUEANTE salvo HEALTHY (o DEGRADED con `allow_degraded_state`) | `risk/engine.py:472-486` |
| E9 | `_check_market_conditions` corre sin parámetro `forced_exit`; `_record_optional_metric` graba con severidad bloqueante por defecto | `risk/engine.py:381`, `:702-745` |
| E10 | `RunState.trade_results` se alimenta al cerrar un ciclo de posición | `backtesting/engine.py:134`, `:730` |
| E11 | `_record_equity` mantiene `peak_equity` y `day_start_equity` (reset por cambio de fecha) | `backtesting/engine.py:706-717` |
| E12 | Resume de paper es fail-closed ante CUALQUIER estado financiero, incluido `position_risk` | `paper/session.py:315-343` |
| E13 | `RiskContext` no tiene campo de breaker | `core/models/risk.py:101-162` |
| E14 | V2 se activa hoy implícitamente por `risk_budget is not None` + `initial_stop_distance_bps is not None` | `risk/config.py:90`, `:112`, `:128` |
| E15 | `risk` no importa `backtesting`; la dirección es `backtesting` → `risk` | tests de arquitectura, 168 passed |

---

## 1. Archivos que tocaría

| Archivo | Cambio |
|---|---|
| `core/enums.py` | Nuevos `RiskCheckCode`: `MAX_DAILY_LOSS`, `MAX_PORTFOLIO_DRAWDOWN`, `MAX_CONSECUTIVE_LOSSES` |
| `core/models/risk.py` | `RiskContext.circuit_breaker`; enmienda del docstring de `CircuitBreakerState` sobre reset diario |
| `core/models/paper.py` | `PaperSessionState.circuit_breaker` |
| `risk/config.py` | `risk_v2_active`; umbrales de breaker (sin defaults); invariantes de construcción |
| `risk/engine.py` | `_check_market_conditions(forced_exit=)`; `_check_circuit_breakers()`; huérfanos y divergencia de cantidad en `evaluate_open_positions` |
| `backtesting/engine.py` | `RunState.circuit_breaker`; actualización en `_record_equity`/`_record_closed_trades`; `require_protection=` en el call site; `circuit_breaker=` en `_risk_context` |
| `paper/session.py` | Persistencia del breaker; rechazo de resume con breaker tripped; corrección del log mal atribuido |
| tests | `tests/unit/test_circuit_breakers.py` (nuevo) + adiciones a `test_forced_exit.py`, `test_risk_engine.py`, `test_backtest_engine.py`, `test_paper_session.py` |

---

## 2. Activation model de Risk V2

Hoy V2 es **emergente**, no declarado: `_derive_stop` mira `initial_stop_distance_bps` y `_requested_quantity` mira `risk_budget` (E14). Una config con presupuesto pero sin distancia no deriva stop ni dimensiona por riesgo: V2 medio activado, silenciosamente degradado a V1.

Propuesta:

- `RiskConfiguration.risk_v2_active` (property derivada) `= risk_budget is not None`.
- Invariante de construcción: `risk_budget is not None` exige `initial_stop_distance_bps is not None`. Una config a medias **no construye**.
- V2 activo implica `require_stop_on_entry = True`. Una entrada sin stop bajo V2 es exactamente la posición huérfana que el milestone existe para impedir.

Un único hecho, no un comportamiento emergente.

## 3. Política de `require_protection`

**Derivada, no configurable por separado.** `require_protection := risk_v2_active`. Un flag independiente es un flag que se puede dejar apagado y desactivar en silencio todo el milestone.

- V1 (`risk_budget is None`) → `False` → comportamiento idéntico. Los golden ya lo fijan.
- V2 → `True` → toda posición abierta debe tener `PositionRiskState` con `trigger_price`.

M6 ya cubre dos de las tres direcciones. Falta la conversa, que hoy **no** se comprueba:

1. posición abierta sin risk state → `PositionRiskUnavailableError` ✅ M6
2. risk state sin `trigger_price` → `PositionRiskUnavailableError` ✅ M6
3. **risk state huérfano** (registro sin posición abierta) → nuevo
4. **`risk_state.quantity != position.quantity`** → nuevo

(3) y (4) son las que impiden posiciones huérfanas de verdad: M5b reconstruye el estado tras cada fill, así que una divergencia significa que la reconstrucción falló.

## 4. Política de volatilidad

**Confirmo que tu preferencia es técnicamente correcta con el modelo actual, y hoy está invertida.**

Evidencia (E9): `_decide` ejecuta `_check_market_conditions` sin conocer `forced_exit`, y `_record_optional_metric` graba con severidad bloqueante. Una salida protectora se rechaza hoy por volatilidad extrema — exactamente la condición para la que existe el stop. Es la misma inversión que M6 corrigió con el rate limit: la cuenta seguiría expuesta *porque* el mercado se está moviendo.

Mecanismo: **el que M6 ya construyó**. `_check_market_conditions` recibe `forced_exit` y degrada a `RiskCheckSeverity.ADVISORY`:

- `EXCESSIVE_VOLATILITY` → advisory en salida forzada. Bloqueante en entrada.
- `EXCESSIVE_SPREAD` → advisory en salida forzada, por la misma lógica: un spread ancho encarece la salida, no la impide.

**Siguen bloqueando incluso en salida forzada** — y esto es lo que hace real tu jerarquía:

- `DATA_FRESHNESS`, `CLOSED_CANDLE` → datos íntegros. Salir con datos rancios es actuar sobre un precio que puede no existir.
- `RECONCILIATION_STATUS` → si no sabemos qué tenemos, no podemos dimensionar la salida.
- Todo lo de validez de venue (precisión, min/max, balance).

DATA INTEGRITY por encima de CAPITAL PROTECTION, literalmente.

## 5. Circuit breakers

Cuatro. **Ningún umbral inventado**: todos son campos de configuración sin default; `None` = desactivado = V1 intacto; activar uno sin umbral no construye.

| Breaker | Dispara | Bloquea | ¿Cierra? | Reset | Persiste |
|---|---|---|---|---|---|
| `MAX_DAILY_LOSS` | `(day_start_equity − equity) / day_start_equity ≥ umbral`, en la barra cerrada tras `_record_equity` | entradas nuevas | **no** | rollover de día (auto) + operador | sí |
| `MAX_PORTFOLIO_DRAWDOWN` | `(peak_equity − equity) / peak_equity ≥ umbral` | entradas nuevas | **no** | **solo operador** | sí |
| `MAX_CONSECUTIVE_LOSSES` | últimos N de `trade_results` todos `< 0` | entradas nuevas | **no** | un trade ganador, u operador. **El rollover NO resetea** | sí |
| Data integrity | reconciliación ≠ IN_SYNC, divergencia posición/risk state, fill no aplicado | **todo, incluidas salidas forzadas** | no: **levanta** | solo operador | sí |

### Semántica: ningún breaker cierra posiciones

Un breaker que liquida al cruzar un umbral de drawdown convierte una caída de mercado en pérdida realizada en el peor momento, y es una estrategia que nadie investigó ni mide ningún benchmark. Los breakers **detienen entradas nuevas**; el riesgo existente lo cierra el stop (M6), que es el componente cuyo trabajo es ese.

**Corolario:** un breaker jamás bloquea una reducción de exposición. Se expresa por lado, no por `forced_exit`: los breakers gobiernan órdenes que **aumentan** riesgo — en spot long-only, exactamente los BUY. Toda SELL (forzada o estratégica) los registra como ADVISORY. Es más simple y más fuerte que condicionar por `forced_exit`.

### Dónde se evalúan — y por qué NO en `HealthStatus`

Colisión detectada (E3/E7/E8): rutar breakers por `HealthStatus` obliga a `state ≠ HEALTHY`, lo que hace fallar `SYSTEM_STATE`, que es bloqueante, lo que **bloquearía también las salidas forzadas**. Enrutar breakers por salud invierte la jerarquía en el nivel más alto.

Diseño: `RiskContext.circuit_breaker: CircuitBreakerState | None`. La orquestación (`BacktestEngine`) posee el estado en `RunState`, lo actualiza en `_record_equity`/`_record_closed_trades` (ambos ya corren por barra) y lo pasa al contexto. Risk lo lee y graba el check. Risk sigue sin estado y sin reloj; la orquestación sigue siendo la única que muta. Respeta E15.

### Relación con `DAILY_DRAWDOWN` / `TOTAL_DRAWDOWN` existentes

No son duplicados: los existentes son **instantáneos** (vuelven a pasar cuando la equity se recupera); los breakers **enclavan**. Invariante de construcción, al estilo del ya existente `max_total_drawdown_pct ≥ max_daily_drawdown_pct` (`risk/config.py:191`): **el umbral del breaker debe ser ≥ el del check instantáneo**. El check instantáneo rechaza una orden en un mal momento; el breaker impide que la cuenta reanude en cuanto la equity repunte.

## 6. Persistencia y reset

`PaperSessionState.circuit_breaker: CircuitBreakerState | None`.

**Pero (E12): el resume de paper es fail-closed ante cualquier estado financiero.** Un breaker tripped ES estado financiero → se añade a la lista `blocking` de `_refuse_stateful_resume`. Consecuencia explícita: **la persistencia es para el operador y la forense, no para restaurar**. Igual que `position_risk`. Nadie debe creer que un breaker sobrevive un reinicio hacia una sesión viva; un snapshot con breaker tripped **impide** el resume.

### Tensión honesta sobre el reset diario

`CircuitBreakerState` documenta que un breaker no se limpia solo (E5), y la razón es buena. Pero un límite de pérdida **diaria** que nunca se resetea es un límite de pérdida total con otro nombre. Propongo una excepción acotada a `MAX_DAILY_LOSS` — reset por rollover de día — **enmendando el docstring del modelo para que lo diga**, no violándolo en silencio. Argumento con evidencia: nuestro modo de operación son corridas desatendidas de 7 días (week3/week5); exigir que un operador lo limpie cada noche hace imposible la validación que estamos haciendo. Los otros tres breakers conservan el contrato: solo operador.

Si prefieres el contrato puro sin excepción, la alternativa es reset solo por operador para los cuatro, y lo implemento así.

## 7. Jerarquía de autoridad

```
DATA INTEGRITY FAILURE
→ CAPITAL PROTECTION / FORCED EXIT
→ CIRCUIT BREAKER
→ RISK ASSESSMENT
→ STRATEGY
→ EXECUTION
```

**Encaja, con dos correcciones para que sea real.**

- Niveles 3–6: ya se cumplen. Los breakers gobiernan entradas; risk dimensiona o rechaza; strategy propone; execution ejecuta. M6 ya puso forced exit por encima de los límites de frecuencia.
- **Corrección A** — hoy está invertida en el nivel 2 vs 4: `EXCESSIVE_VOLATILITY` (risk assessment) bloquea una salida forzada (capital protection). La resuelve el punto 4.
- **Corrección B** — "DATA INTEGRITY por encima de CAPITAL PROTECTION" hoy **no es expresable**: `_health()` está hardcodeado a HEALTHY (E3), así que ninguna condición de integridad llega a risk ni en backtest ni en paper. Construir un `HealthStatus` veraz colisiona con la frontera de salud del feed en paper (E4) y con `SYSTEM_STATE` bloqueante (E8).

  **Recomendación:** no cablear salud real a risk en M7. Expresar la integridad de datos como **excepciones levantadas** (la vía fail-loudly de M5b/M6), que ya está por encima de todo porque **detiene la corrida**. Lectura coherente de la jerarquía: ante fallo de integridad no se opera — se para. El nivel superior se hace cumplir parando, no chequeando.

## 8. Trazado paper real (verificado, no supuesto)

```
PaperTradingSession.submit_bar          paper/session.py:391
  → BacktestEngine.advance              backtesting/engine.py:234
    → _validate_bar
    → _process_bar                      backtesting/engine.py:265
        broker.process_bar (liquida la barra anterior)
        _account_for_fills
        verificación fill↔portfolio
        _update_position_risk           ← M5b
        history.append / last_close
        _features.compute
        _snapshot
        _forced_exit_intents            ← M6  ✅ paper LO EJECUTA
        _authorise(forced_exit=True)
        _generate → _build_intents → _authorise
```

**Confirmado: paper ejecuta forced exits por el mismo código que backtest.** No hay segunda ruta. `_risk_context` (`engine.py:410`) también es compartido, así que el breaker entrará a paper por el mismo sitio sin cableado nuevo.

**Defecto encontrado en el trazado:** `paper/session.py:394-404` captura `DataIntegrityError`, lo loguea como `"bar rejected: unknown symbol"`, incrementa `bars_rejected` y **relanza**. Propaga correctamente, pero **mal atribuido**: un fallo de integridad de posición se registraría como símbolo desconocido. Es justo el tipo de ruido que costó horas en la investigación de week3. Corrección pequeña, entra en M7.

## 9. Tests exactos

**Protección (tus 3)**
1. V2 activo + posición protegida → corre normal
2. V2 activo + posición sin protección → `PositionRiskUnavailableError`
3. V1 sin Risk V2 → sin cambios (golden)

**Nuevos de protección**
4. risk state huérfano sin posición → falla ruidosamente
5. `risk_state.quantity` ≠ cantidad de la posición → falla ruidosamente
6. config V2 a medias (budget sin distancia) → no construye

**Volatilidad (tus 2)**
7. volatilidad excesiva bloquea entrada normal
8. volatilidad excesiva **NO** bloquea forced exit
9. la volatilidad se registra ADVISORY, no oculta (paridad con M6)
10. spread excesivo no bloquea forced exit
11. `DATA_FRESHNESS` **sí** bloquea forced exit (la jerarquía es real)

**Breakers (tus 5)**
12. max daily loss bloquea entradas nuevas
13. max drawdown bloquea entradas nuevas
14. consecutive losses dispara a las N pérdidas
15. breaker no impide cerrar riesgo existente (forced exit)
16. breaker no impide salida estratégica
17. breaker state persiste en el snapshot
18. reset: daily loss se limpia en rollover
19. reset: drawdown NO se limpia en rollover
20. reset: consecutive losses se limpia con un ganador
21. snapshot con breaker tripped → resume rechazado
22. umbral de breaker < umbral instantáneo → no construye

**Camino paper (tu 1)**
23. paper ejecuta forced exits igual que backtest (mismo escenario de caída por `PaperTradingSession`)
24. paper ve el breaker por el mismo contexto

**No-regresión (tu 1)**
25. sin cambios en señales de Strategy: golden de señales idéntico con breakers activos

## 10. Riesgos de regresión

1. **`require_protection` derivado activa el fail-loudly en toda config V2.** Hay que auditar los tests `_v2_backtest` existentes: cualquiera con posición abierta sin risk state empezará a levantar. Riesgo alto de ruido, mitigable auditando antes de tocar.
2. **`RunState.circuit_breaker` sin default** → `_initial_state()` debe inicializarlo. Este error exacto ocurrió en M5b (`RunState.__init__() missing position_risk`).
3. **Invariante "cada `RiskCheckCode` a lo sumo una vez"** en `RiskDecision`. Un código por breaker, cada uno grabado exactamente una vez. Un código genérico compartido rompería el invariante en cuanto haya dos breakers.
4. **`PaperSessionState` gana un campo** → interacción con `_COMPUTED_FIELD_EXCLUSIONS`. `CircuitBreakerState.is_tripped` es `@property`, no `computed_field`, así que no serializa: verificar, no asumir.
5. **Degradar `EXCESSIVE_SPREAD` a advisory en ventas** toca comportamiento con tests existentes (`test_risk_engine.py:635`, `:657`). Usan `make_intent()` (BUY por defecto), así que no deberían romper — verificado por lectura, a confirmar al implementar.
6. **Invariante umbral-breaker ≥ umbral-instantáneo** podría rechazar configs existentes. No hay ninguna con breakers en el repo, así que hoy no rompe nada.
7. **Enmendar el docstring de `CircuitBreakerState`** cambia un contrato documentado. Debe quedar escrito y aprobado, no deslizado.

## 11. GO / NO-GO

**GO** — puntos 2, 3, 4 y 6 (activación V2, `require_protection`, política de volatilidad, fail-loudly). Superficie pequeña, mecanismo ya construido en M6, cinco de los siete riesgos no aplican.

**GO** — los tres breakers de equity/pérdidas (puntos 5 y 6), con umbrales como configuración obligatoria sin default.

**NO-GO** — cablear `HealthStatus` real a risk. Colisiona con la frontera de salud del feed en paper (E4) y con `SYSTEM_STATE` bloqueante (E8): bloquearía las salidas forzadas por el camino de `system_state`. La integridad de datos se expresa parando, no chequeando.

**NO-GO** — liquidación dirigida por breaker. Rechazado por principio: es una estrategia sin investigar.

**Recomendación de secuencia:** partir en dos, cada uno con su gate y su commit.

- **M7a** — activación V2 + `require_protection` + política de volatilidad + fail-loudly. Sin estado nuevo, sin persistencia. Bajo riesgo.
- **M7b** — los tres breakers: `RunState`, `RiskContext`, persistencia, reset, rechazo de resume. Estado nuevo y persistencia nueva; merece su propio gate.

M7a entrega el grueso de la protección de capital sin tocar persistencia. Si M7b se atasca, M7a ya está en verde y commiteado.
