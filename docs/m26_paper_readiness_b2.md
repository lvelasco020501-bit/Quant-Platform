# M26 — B2/BTC/4H listo para paper

**Estado:** **GO FOR PAPER LAUNCH** · no lanzado · 2026-09-24

No se inició ninguna sesión. No se tocó live. No se cambió ni una línea de lógica de Risk.
`git status` sobre `risk/`, `execution/`, `portfolio/` y `paper/` está vacío.

---

## 1. La sesión vieja, cerrada

**Se cerraron las tres**, no solo `week5`. Dejar estados reanudables de sesiones antiguas en
`var/state/` contradice la disciplina de "no auto-resume", y Mission Control lee ese
directorio. **Es más de lo que se pidió**, está dicho aquí, y es reversible: nada se destruyó,
todo está copiado en `var/audit/closure-2026-09-24/`.

| sesión | financial state que cargaba | cuadre |
|---|---|---|
| **paper-7c-week5** | 1 posición abierta (0.11850 BTC), balance BTC, realized −106.95 | exacto |
| **paper-7c-week4** | **9.509,13 USDT bloqueados** contra una orden sin resolver | exacto |
| paper-7c-week3 | ninguno (9 barras, nunca operó) | exacto |

**Reconciliación de week5**, la que importa:

```
balance USDT                     527.88628710
posición         0.11850 BTC @ 79030.94826627
coste de la posición            9365.16736955
marca (último cierre 2026-08-28) 79421.05
valor de la posición            9411.39442500
P&L no realizado                  +46.22705545
P&L realizado                    -106.94634335
comisiones                         28.18206358

IDENTIDAD: usdt + coste - realizado = 10000.00000000  ✅ sin residuo
EQUITY a la marca               9939.28071210   (−0.6072%)
```

**Un error mío, corregido en el camino.** La primera versión del registro de cierre solo
miraba posiciones y realized_pnl, y por eso declaró que `week4` no cargaba financial state —
teniendo **el 95% de su capital bloqueado** contra una orden abierta. Fondos bloqueados son
financial state. Corregido, con la corrección anotada dentro del propio registro.

**Por qué ninguna puede ser fuente de warm-start.** `evaluate_warm_start` rechaza la historia
escrita por una sesión que carga financial state, y su razón está en el código:

> *starting fresh from its candles would present an unreconciled account as a recovery: those
> books are still open and need closing by a person, not by a restart*

Los libros se cierran aquí **en papel, por una persona**. La posición se abandona a la marca;
no se vendió nada ni se canceló ninguna orden contra un mercado que lleva un mes moviéndose.

**Marcadas terminales de forma efectiva, no declarativa:** el fichero de estado ya no está en
`var/state/`, así que `--resume` no tiene nada que cargar. `var/state/` está vacío.

**El sello de `var/audit/week5-final/` no se tocó.** Está en solo-lectura desde el 28 de
agosto; romper el sello para escribir dentro habría sido lo contrario de una auditoría. El
cierre vive en un directorio nuevo que lo referencia.

---

## 2. Por qué B2 y no RT

**Simplicidad operativa, no calidad.** BTC ya está en `allowed_symbols`, así que esta fase no
añade un símbolo; una estrategia promovida en vez de dos reduce a la mitad la superficie de
cambio en producción. M24 no declaró ganadora y esto no la declara: cada una alcanzó PAPER
CANDIDATE en un mercado distinto, y RT sigue disponible para una fase posterior.

---

## 3. Promoción — commit `3ec6224`, separado

No bastaba con listar la clase. Vivía en `strategies/research.py`, y `registry.py` no puede
importar research porque research importa registry. Así que:

* `strategies/parametric.py` — la base paramétrica, la aritmética de warm-up y la derivación
  de metadata, **extraídas** de research;
* `strategies/breakout_trend.py` — la regla, **movida**, no reescrita;
* `research.py` importa ambas, y `breakout_trend` sale de `RESEARCH_STRATEGIES` (estar en las
  dos haría que `build_research_registry()` fallara por id duplicado).

**Research y paper resuelven el mismo objeto de clase**, y un test lo fija. Verificado además
empíricamente: re-ejecutar una celda de M24 tras el movimiento reprodujo **los nueve campos
del scorecard hasta el último dígito**. Ninguna cifra de M22–M24 describe una estrategia
distinta de la que correría una sesión.

`tests/unit/test_promoted_strategies.py` es la puerta nueva: una lista blanca literal donde
cada entrada lleva una línea diciendo qué milestone la admitió. Añadir una estrategia en
cualquier parte de la plataforma no la hace ejecutable; solo editar ese fichero, a propósito.

Cuatro tests que fijaban "exactamente dos" se editaron **deliberadamente**, cada uno con un
comentario de qué cambió y por qué. Ninguno se relajó.

---

## 4. Riesgo 4H — `deploy/paper-b2-btc-4h.env`

Los números **no están escritos a mano**: se generaron desde
`risk_for_timeframe(risk_v2_1h, H4)`, la conversión que declaró M15 y que usaron todas las
corridas de M22, M23 y M24.

| valor | 4H | por qué |
|---|---|---|
| stop inicial | **600 bps** | distancia de precio, ×√4 |
| take profit | **1200 bps** | distancia de precio, ×√4 |
| break-even | **300 bps** | distancia de precio, ×√4 |
| holding máximo | **42 barras** | constante en *tiempo*: 168 barras de 1h = 7 días = 42 de 4h |
| volatilidad máx. | **0.30** | tasa por barra, escala con la barra |
| drawdown total | 0.20 · **latcheado** | límite de cuenta, nada por barra: sin cambio |
| pérdida diaria | 0.03 | límite de día natural: sin cambio |
| pérdidas seguidas | 5 · **latcheado** | cuenta trades, no barras: sin cambio |
| **riesgo por trade** | **0.01** | **es una fracción de equity, no una distancia: NO escala** |

Ese último es el que más importa: si hubiera escalado, cada posición sería del doble del
tamaño con el que se produjo la evidencia. Hay un test dedicado.

**El test no compara cadenas.** Deja que la plataforma construya su propio `RiskConfiguration`
desde el fichero, igual que hace `orchestration.paper` al arrancar, y lo compara **campo a
campo** con la conversión de research. Esa distinción se ganó el sueldo: la primera versión
del fichero usaba los nombres de `RiskConfiguration`, y los settings exponen `RiskSettings`,
cuyos nombres **no coinciden** — `max_volatility` es `max_realized_volatility_fraction`, el
presupuesto de riesgo es plano y no anidado, y `market_buy_buffer_bps` no se expone. Una
comparación de cadenas habría aprobado algo que la plataforma rechaza al cargar.

**Segunda trampa encontrada y cerrada:** los valores JSON van entrecomillados porque, sin
comillas, `set -a; . fichero` deja que el shell se coma las comillas internas y la plataforma
rechaza la sección `risk`. Verificado que carga **por las dos vías**.

---

## 5. Warm-start: **COLD START**

* **Requerido:** 400 barras (`sma_400` necesita exactamente su ventana; las `donchian`
  necesitan una más que la suya, 41 y 21 — el máximo es el de la SMA). A 4H son **66,7 días**.
* **Fuentes candidatas:** las tres sesiones cerradas. Dos cargan financial state y
  `evaluate_warm_start` las rechaza. La tercera (`week3`) tiene 9 barras **de 1h**: timeframe
  equivocado y 391 barras de menos.
* **Veredicto: no existe fuente válida. Arranque en frío.** La sesión no emitirá una señal
  hasta acumular 400 barras de 4H. Eso es una propiedad de la regla, no un fallo.

---

## 6. Aislamiento

```
var/sessions/paper-b2-btc-4h-w1/state
var/sessions/paper-b2-btc-4h-w1/logs
var/sessions/paper-b2-btc-4h-w1/reports
```

`session_id` = `paper-b2-btc-4h-w1`, y **el id aparece literalmente en las tres rutas** — hay
un test que lo exige. `SessionLock` mantiene una sesión por *directorio de estado*, así que
ids distintos no bastarían; directorios propios sí. `var/state/`, el árbol compartido donde
dos procesos se pisaron 18 horas, queda fuera y vacío.

---

## 7. Checklist pre-lanzamiento

| ITEM | STATUS | EVIDENCE |
|---|---|---|
| Sesión vieja cerrada | ✅ | 3 sesiones, cuadre exacto a 10000, `var/audit/closure-2026-09-24/` |
| No reutiliza estado financiero | ✅ | ficheros fuera de `var/state/`; `--resume` no tiene qué cargar |
| B2 registrada en paper | ✅ | `build_default_registry()` resuelve `breakout_trend`; commit `3ec6224` |
| Solo lo aprobado llega a paper | ✅ | `test_promoted_strategies.py`, lista blanca literal de 3 |
| Research y paper = misma clase | ✅ | test de identidad + re-ejecución de M24 idéntica al dígito |
| BTC permitido | ✅ | `allowed_symbols=('BTC/USDT',)`, ETH ausente por test |
| 4H soportado | ✅ | `Timeframe.H4` en la metadata; `paper check` valida a 4h |
| Config de riesgo lista | ✅ | 29 tests; comparación campo a campo con `risk_for_timeframe` |
| Riesgo productivo intacto | ✅ | `git status` vacío en `risk/`, `execution/`, `portfolio/`, `paper/` |
| Warm-start | ⚠️ **COLD START** | sin fuente válida; 400 barras = 66,7 días hasta la primera señal |
| Directorios aislados | ✅ | `var/sessions/paper-b2-btc-4h-w1/{state,logs,reports}`, creados |
| Sin procesos huérfanos | ✅ | 0 procesos python vivos |
| Salud de la máquina | ⚠️ | carga 3,43; **1.193 MB de swap libre** — justo, pero una sesión de paper consume órdenes de magnitud menos que un backtest |
| Mission Control | ⚠️ **verificable solo al arrancar** | el lock nombra sesión y PID; se comprueba después de lanzar, no antes |
| PAPER only / live off | ✅ | `paper check` reporta `"live_trading_armed": false` |
| Restart = no · `--fresh` · no auto-resume | ✅ | `resume: _ResumeOption = False`; sin supervisor ni cron |
| Alertas de fallo | ✅ | watchdog de stall activo; margen configurado sobre el intervalo de barra |
| Referencias inmutables | ✅ | `3ec6224` promoción · `513789f` veredicto M23 · `2453c6c` veredicto M24 |
| Validación de la plataforma | ✅ | **`paper check` pasa** con esta configuración exacta |

---

## 8. Riesgos operativos

1. **66,7 días sin una sola señal.** Es lo esperado y hay que no confundirlo con una avería.
   Lo que se mira mientras tanto es `bars_processed`, no el contador de trades.
2. **La configuración de riesgo 4H nunca se ha desplegado.** Está validada en research desde
   M15, pero ninguna sesión la ha corrido. Esta sería la primera.
3. **~7,85 trades/año.** 90 días dan ~1,9 operaciones. La sesión valida **mecánica**, no edge;
   30 trades —la muestra mínima de la plataforma— tardarían ~3,8 años.
4. **Los requisitos de M21 siguen sin implementar:** persistencia del latch entre reinicios,
   reconciliación al arranque, `clientOrderId` idempotente. En paper el coste es bajo; antes de
   dinero real, no.
5. **Swap justo.** 1.193 MB libres tras una sesión larga de backtests.

---

## 9. Duración recomendada de observación

**90 días para mecánica.** Edge no es medible en paper a este timeframe — ver §8.3.

---

## 10. VEREDICTO: **GO FOR PAPER LAUNCH**

Con las tres reservas de §7 dichas, no escondidas: cold start de 66,7 días, swap justo, y
Mission Control comprobable solo después de arrancar.

**No se lanza.** El comando exacto que se usaría, para tu aprobación explícita:

```bash
cd /Users/luisve/quant-platform && set -a && . ./deploy/paper-b2-btc-4h.env && set +a && uv run python -m quantplatform.cli.main paper run --fresh
```

`--fresh` es además el valor por defecto; va explícito para que quede en el historial del
shell que esta sesión empieza en cero y no reanuda nada.

Antes de ejecutarlo, conviene repetir la validación, que no arranca nada ni abre sockets:

```bash
cd /Users/luisve/quant-platform && set -a && . ./deploy/paper-b2-btc-4h.env && set +a && uv run python -m quantplatform.cli.main paper check
```

---

## 11. Tests, gate y reproducibilidad

* **2 606 tests en verde** (+29 de la configuración de riesgo, +8 de la lista blanca).
* `ruff format`, `ruff check` y `mypy src` limpios; `mypy .` en su línea base de 108/10.
* **Rutas protegidas sin tocar:** `risk/`, `execution/`, `portfolio/`, `paper/`.
* **Refactor demostrado neutro:** re-ejecución de una celda de M24 idéntica campo a campo.
* **Un flake preexistente, no una regresión:**
  `test_transport_hardening::test_a_connection_error_is_raised_not_swallowed` falló una vez en
  la suite completa y pasa en aislamiento **con y sin** estos cambios (verificado con `git
  stash`). Dicho, no escondido.
