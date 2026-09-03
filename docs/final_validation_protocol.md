# Final Validation Protocol — QuantPlatform `paper-7c-week3`

**Estado: DEFINITIVO — este documento no se modificará.** Define únicamente el
procedimiento a ejecutar al cierre de la corrida oficial de 7 días. No contiene
resultados, hallazgos, interpretaciones ni una certificación — esos productos se generan
el día de cierre, en `docs/official_validation_report.md` Sección 10, siguiendo este
protocolo.

---

## 1. Qué se verifica

| # | Objeto de verificación |
|---|---|
| 1 | Identidad y continuidad de la sesión oficial (session_id, PID, lock, commit) |
| 2 | Integridad del código durante la ventana (sin cambios no autorizados) |
| 3 | Estabilidad del sistema (uptime, reinicios, recursos) |
| 4 | Integridad del feed (continuidad, huecos, duplicados, frames malformados) |
| 5 | Comportamiento de la estrategia (warmup, señales generadas/descartadas/aceptadas) |
| 6 | Motor de riesgo (intents, órdenes, fills, exposición) |
| 7 | Portafolio (balances, PnL realizado/no realizado, comisiones, reconciliación) |
| 8 | Reportes diarios (generación, ausencia de fallos, coincidencia con el estado) |
| 9 | Consistencia del Control Center (dashboard vs. estado persistido vs. logs) |
| 10 | Incidentes registrados (cada uno, su evidencia y su clasificación) |
| 11 | Integridad de la cadena de evidencia (Evidence Ledger: completitud y hashes) |

---

## 2. En qué orden

1. Verificar identidad de la sesión: `paper-session.lock` vigente, PID vivo o proceso
   terminado según cierre planeado, `session_id = paper-7c-week3`.
2. Verificar integridad del código: `git diff` entre el commit registrado al inicio
   (`9155e82b343f27fafa46d0fe80bd9d28dd3e9dec`, EV-0001) y el commit vigente al cierre.
   Cualquier diferencia debe corresponder a una excepción documentada y autorizada.
3. Reconstruir la cronología completa desde `var/logs/*.log` (incluyendo archivos
   rotados) para toda la ventana de observación.
4. Revisar estabilidad del sistema, día por día, contra las consolidaciones de
   `docs/official_validation_report.md` Sección 3.
5. Revisar integridad del feed contra la Sección 4.
6. Revisar comportamiento de la estrategia contra la Sección 5.
7. Revisar el motor de riesgo contra la Sección 6.
8. Reconciliar el portafolio contra la Sección 7: balance inicial + realized_pnl −
   fees = balance final, verificado contra el estado persistido final.
9. Auditar los reportes diarios: cada rollover UTC de 00:00 dentro de la ventana debe
   tener un reporte correspondiente en `var/reports/`, sin `report_failures`.
10. Auditar consistencia del Control Center contra la Sección 8, para cada checkpoint
    registrado.
11. Revisar cada incidente registrado en la Sección 9: evidencia, causa, clasificación.
12. Auditar el Evidence Ledger completo: cada afirmación de las secciones anteriores
    debe poder rastrearse a un Evidence ID; recalcular una muestra de hashes SHA256
    contra los archivos referenciados y confirmar coincidencia.
13. Consolidar la tabla de métricas obligatorias (Sección 6 de este protocolo).
14. Aplicar los criterios de aprobación y rechazo (Secciones 4 y 5 de este protocolo)
    y las anomalías invalidantes (Sección 7 de este protocolo).
15. Completar `docs/official_validation_report.md` Sección 10 con el resultado.

Si en cualquier paso la evidencia requerida no existe o es inconsistente, el paso se
detiene, se registra como incidente en la Sección 9 del reporte oficial, y la
verificación continúa desde el siguiente paso — no se asume ni se rellena el dato
faltante.

---

## 3. Qué evidencia se consulta

| Fuente | Contenido |
|---|---|
| `var/logs/*.log` (y rotados) | Todos los eventos de las 4 fuentes de log, ventana completa |
| `var/state/paper-7c-week3.json` | Estado final persistido |
| `var/evidence/snapshots/*` | Snapshots inmutables tomados durante la corrida |
| `var/reports/YYYY/MM/DD/*` | Reportes diarios, todos los rollovers de la ventana |
| Control Center `/api/status` | Respuestas capturadas en cada checkpoint documentado |
| `docs/official_validation_report.md` | Consolidaciones de días 1, 3, 5, 7 |
| `docs/experiment_scoreboard.md` | Última tabla de estado |
| `docs/evidence_ledger.md` | Todas las entradas EV-* acumuladas |
| `var/observation/week3-monitoring-log.md` | Bitácora horaria operacional |
| `git log` / `git diff` | Historial de commits durante la ventana |
| Binance REST `api/v3/klines` | Verificación independiente de una muestra de velas |
| `ps`, `lsof`, `os.kill(pid, 0)` | Estado del proceso al momento del cierre |

---

## 4. Qué criterios hacen aprobar la plataforma

Deben cumplirse todos:

- Sesión oficial corrió de forma continua bajo un único `session_id`, o cada reinicio
  está documentado como incidente con causa raíz identificada y continuidad de estado
  verificada (`--resume`, `restarts` incrementado en el estado persistido).
- Cero eventos CRITICAL del watchdog sin resolver.
- Cero `report_failures` sin explicación evidenciada.
- Cero huecos (`gaps`) de vela no contabilizados.
- Reconexiones, si existieron, están todas acotadas en el tiempo (consistente con el
  endurecimiento de Fase 7C) y registradas en el log.
- El portafolio reconcilia: balance final = balance inicial + realized_pnl − fees,
  verificado contra el estado persistido.
- El Control Center coincidió con el estado persistido en el 100% de los checkpoints
  muestreados.
- Ninguna línea ERROR sin investigar y clasificada en la Sección 9.
- Toda afirmación material del reporte oficial se rastrea a un Evidence ID con hash
  verificable.
- Sin cambios de código en rutas de trading/estrategia/riesgo/portafolio/ejecución/
  marketdata durante la ventana, salvo excepción documentada y autorizada explícitamente
  por el usuario.

---

## 5. Qué criterios hacen rechazar la plataforma

Cualquiera de los siguientes:

- Un evento CRITICAL del watchdog sin resolver.
- Un `DataGapError` o hueco de vela sin contabilizar.
- `report_failures > 0` sin explicación evidenciada que no implique cambio de código.
- Un reinicio causado por una excepción no manejada sin causa raíz identificada.
- Una discrepancia entre el Control Center y el estado persistido no explicable por
  las reglas de frescura/liveness ya documentadas.
- Un desajuste en la reconciliación del portafolio (balances o PnL que no cierran).
- Una afirmación del reporte oficial sin Evidence ID rastreable.
- Una modificación de código en rutas de trading durante la ventana de congelamiento
  sin autorización explícita del usuario.

---

## 6. Qué métricas son obligatorias

| Métrica | Obligatoria |
|---|---|
| Uptime total (%) | Sí |
| Número y causa de cada reinicio | Sí |
| bars_processed final vs. esperado | Sí |
| Reconexiones (conteo, duración máxima individual) | Sí |
| Heartbeat timeouts (conteo) | Sí |
| Eventos de watchdog (conteo, por stage, severidad) | Sí |
| Gaps detectados (conteo, velas perdidas si aplica) | Sí |
| Frames malformados (conteo) | Sí |
| Reportes generados vs. rollovers esperados | Sí |
| report_failures (conteo) | Sí |
| CPU (tendencia inicio→fin) | Sí |
| Memoria RSS (tendencia inicio→fin) | Sí |
| Tamaño del estado persistido (tendencia) | Sí |
| Crecimiento de logs (por stream y total) | Sí |
| Trades ejecutados (fills totales) | Sí |
| Balance final, equity, PnL realizado/no realizado, comisiones | Sí |
| Tasa de coincidencia Control Center vs. estado (checkpoints muestreados) | Sí |
| Entradas del Evidence Ledger (conteo, tasa de verificación de hash) | Sí |

---

## 7. Qué anomalías invalidan completamente el experimento

Cualquiera de las siguientes anula la certificación (ni aprobación ni rechazo son
posibles — el experimento queda sin veredicto):

- Dos sesiones paper corriendo simultáneamente contra el mismo directorio de estado
  durante la ventana (bypass o desactivación del `SessionLock`).
- Cambio de código no documentado en rutas de trading/estrategia/riesgo/portafolio/
  ejecución/marketdata durante la ventana de congelamiento.
- Pérdida o corrupción del estado persistido sin un snapshot preservado que permita
  reconstruirlo.
- Pérdida de archivos de log que cubran cualquier porción de la ventana, dejándola
  no verificable.
- Intervención manual sobre balances, posiciones o estado fuera del mecanismo de
  persistencia propio de la plataforma.
- Evidencia de manipulación o sustitución del feed de datos del venue (por ejemplo,
  reproducción de datos no en vivo presentada como en vivo).
- Fallo o evidencia de desactivación de la protección de sesión única.
- Un vacío en el Evidence Ledger donde un período material de la ventana no tiene
  ninguna evidencia rastreable.
