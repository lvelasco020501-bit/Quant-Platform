# M20 — Detección por barra tras el reset, y techos puestos donde pueden morder

**Estado:** COMPLETADO · research/backtesting únicamente · 2026-09-23

No se tocó paper, Risk V2 productivo, execution, portfolio ni las estrategias productivas.
Mismos datos, estrategia, timeframe, costes y escenarios que M18/M19.

## 1. El fix por barra — qué se cambió y por qué ahí

El engine valora la cuenta **en cada barra** y evalúa ahí su propio breaker, pero al risk engine
sólo le entrega ese snapshot **cuando hay una decisión de estrategia**. El único hook por barra
que ya existía (`evaluate_open_positions`) recibe posiciones y barra, **no cash**: reconstruir el
equity ahí se equivoca por el notional entero en cuanto hay un fill entre decisiones. Eso no sirve
como base de un trigger de riesgo.

Por eso el fix va en el backtest engine: `_offer_bar` le ofrece a un risk engine que lo pida el
mismo snapshot que su propio breaker acaba de leer. Es **aditivo y opt-in** — si el engine no
define `observe_bar`, no se llama a nadie, no se lee ningún resultado y ninguna decisión del
backtest depende de ello. `backtesting` no está en la lista de intocables, pero es código
compartido, así que queda dicho aquí y no escondido en un diff.

### La evidencia del fix

| | Overshoot local máximo (límite 10%) |
|---|---|
| M19 — muestreo en puntos de decisión | **17.17%** |
| M20 — por barra | **10.73%** |

De 7.17 puntos de exceso a **0.73**, que es un movimiento de barra. Y no es cosmético: en XRP·S3
la cadena de G pasó de 3 halts a 2, y el peor overshoot de ETH/BNB quedó en 10.19–10.23%.

Cuatro tests lo fijan: que el primer halt cae **en la misma barra** que el breaker del engine; que
ningún halt deja correr más de límite + un movimiento de barra; que la profundidad de disparo no
se desvía entre el primer halt y los siguientes; y que el engine ofrece más barras que decisiones.

## 2. Las cuatro políticas

| | Regla |
|---|---|
| **G** | cooldown 30 días + reset local (referencia de M17) |
| **H12** | G + techo global 12% sobre la pérdida desde el máximo original |
| **H15** | G + techo global 15% |
| **J** | G + techo 15% **y** máximo 2 resets por 3 años móviles |

12% y 15% quedan **por debajo** del daño que M18 midió (19.16% y 19.09%), que es justo lo que el
20% de M19 no cumplía. Dos valores en vez de uno para ver **dónde empieza a morder**, no si muerde.

## 3. La tabla

| POLICY | LÍMITE | HALTS | REAPERTURAS REALES | BLOCKED % | MAX DD local | GLOBAL DD | CAP | RESETS | RATCHET | OPERABILIDAD | VEREDICTO |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **G** | 10% | 9 | 9/9 | 1.98% | 10.73% | 19.09% | — | 9 | 2 halts, 16.47% | 100% | **PASS** |
| **G** | 5% | 12 | 12/12 | 2.97% | 5.70% | 11.17% | — | 12 | 2 halts, 9.83% | 100% | **PASS** |
| H12 | 10% | 12 | 7/12 | **77.89%** | 10.56% | 12.49% | 12% | 7 | ninguna | 58% | FAIL |
| H12 | 5% | 12 | 12/12 | 2.97% | 5.70% | 11.17% | 12% | 12 | 2 halts, 9.83% | 100% | FAIL |
| H15 | 10% | 11 | 8/11 | 24.59% | 10.73% | 15.49% | 15% | 8 | 2 halts, 14.28% | 73% | FAIL |
| H15 | 5% | 12 | 12/12 | 2.97% | 5.70% | 11.17% | 15% | 12 | 2 halts, 9.83% | 100% | FAIL |
| J | 10% | 11 | 8/11 | 24.59% | 10.73% | 15.49% | 15% | 8 | 2 halts, 14.28% | 73% | FAIL |
| J | 5% | 12 | 12/12 | 2.97% | 5.70% | 11.17% | 15% | 12 | 2 halts, 9.83% | 100% | FAIL |

Detección por barra activa en **las 64 corridas**.

## 4. Los techos sí muerden — y qué cuestan

Esta vez la columna que en M19 era toda "no" tiene sí. El techo hace lo que promete: acota el
drawdown global. Lo que cuesta es tiempo de mercado apagado.

| Mercado · escenario | G | H12 | Bloqueado por H12 | H15 | Bloqueado por H15 |
|---|---|---|---|---|---|
| XRP · S2 | 12.44% · 141 trades | **12.15%** · 22 | **77.89%** | no mordió | 0% |
| XRP · S3 | 18.59% · 129 | **12.26%** · 76 | 40.71% | **15.24%** · 104 | 23.72% |
| ADA · S3 | 19.09% · 103 | **12.24%** · 61 | 54.47% | **15.15%** · 85 | 24.33% |
| ETH · S3 | 17.83% · 126 | **12.07%** · 78 | 46.73% | **15.49%** · 101 | 24.59% |
| BNB · S3 | 14.71% · 114 | **12.49%** · 34 | **71.08%** | no mordió | 0% |

XRP·S2 es el caso que lo resume: H12 bloquea el **78% de la historia** del mercado para ahorrar
**0.29 puntos** de drawdown. Eso es el fallo de C otra vez, con otro nombre.

## 5. Por qué fallan los tres — dos motivos, y uno es estructural

**a) El techo se cruza por un movimiento de barra.** H12 llega a 12.49% con techo 12%; H15 a
15.49% con techo 15%. Ambos, 0.49 pp de exceso. No es un bug ni un umbral mal puesto: **bloquear
exposición nueva no impide que una posición ya abierta siga perdiendo**. Un breaker que sólo
controla la puerta de entrada no puede acotar un drawdown; para eso hace falta cerrar la posición,
que es un mecanismo distinto y vive en execution/risk, no en una política de latch.

**b) Al límite del 5% los techos no llegan a morder** (drawdown global 11.17% < 12%), así que esas
corridas reproducen G exactamente y no dicen nada sobre el techo. Falla la puerta añadida en M19.

**c) J ≡ H15 en las 32 corridas.** El allowance de 2 resets por 3 años **sigue sin probarse**: el
techo muerde antes de que aparezca un tercer reset. Emparejarlo con el techo más flojo fue la
dirección correcta y la magnitud insuficiente — variante menos grave del error de M19, pero el
mismo error.

## 6. G pasa todas las puertas — y hay que leerlo con cuidado

Con la detección por barra, **G pasa las ocho puertas en los dos límites**: para a tiempo (10.73%
contra 15% permitido), reabre siempre (9/9 y 12/12), no aletea (0%), no queda apagada nunca, y su
recuento de trades se mueve 0.78% ante un 25% más de comisión.

Pero la puerta "respeta el presupuesto global" la pasa **contra el 20% de producción**, y G llega
a **19.09%**. Es decir: G convierte un límite local del 10% en una pérdida del 19% y eso entra en
el presupuesto sólo porque el presupuesto es el que producción ya admite. La puerta dice la verdad;
el número también.

## 7. Las respuestas

1. **¿El fix por barra funciona?** **Sí, y está medido:** overshoot de 17.17% a 10.73%, con tests.
2. **¿Los techos contienen el ratchet?** **Sí** — de 19.09% a 12.2–15.2% — pero cruzándose por
   0.49 pp y bloqueando entre el 24% y el 78% del mercado.
3. **¿Alguna candidata pasa?** **No.** H12, H15 y J fallan la puerta del presupuesto.
4. **¿Cuál es la mejor regla disponible hoy?** **G con detección por barra**, sin techo.
5. **¿Hay evidencia para cambiar Risk V2 productivo?** **No todavía**, y ahora se sabe por qué.

## 8. Veredicto: **NO-GO** para el diseño productivo

No se propone ningún cambio a producción.

**Lo que este milestone deja cerrado:**

* la detección por barra tras un reset **está resuelta** y es la base de cualquier diseño futuro;
* **G con ese fix es la mejor regla de recuperación medida hasta ahora** — opera siempre que puede,
  no aletea, no se apaga, y es estable ante costes;
* un **techo global implementado como puerta de entrada es el instrumento equivocado**: no puede
  acotar el drawdown de una posición abierta y paga el control con 24–78% de mercado apagado.

**Lo que hace falta antes de volver a proponer un cambio productivo**, y es un mecanismo distinto,
no otro umbral:

1. **Cierre forzado al tocar el presupuesto**, no sólo bloqueo de exposición nueva. Eso convierte
   el techo en un stop de cuenta y es la única forma de que un límite global se respete de verdad.
   Vive en execution/risk y toca producción: hay que diseñarlo, no improvisarlo.
2. **Probar el allowance donde pueda morder** — con un techo lo bastante ancho como para que el
   tercer reset llegue antes que el techo, o sin techo.
3. Una vez exista el cierre forzado, volver a correr G + techo con esa mecánica.

## 9. Tests, gate y reproducibilidad

* **45 tests** entre el motor de recuperación (36) y el protocolo de M20 (9). Los cuatro nuevos
  del fix: **misma barra que el breaker del engine**, sin overshoot más allá de un movimiento de
  barra, misma profundidad de disparo antes y después del reset, y más barras ofrecidas que
  decisiones asesoradas. Sigue en verde el que prueba que la regla permanente reproduce el
  wrapper de M14 **bit por bit**, así que M14–M17 no se movieron.
* **Suite completa: 2 460 tests.** `ruff format`, `ruff check` y `mypy src` limpios; `mypy .` en
  su línea base de 108 errores / 10 archivos.
* **64 corridas, 16 jobs, 0 fallos.** 16 ledgers verificados, **0 fallos de reproducibilidad**.
* **Re-ejecución desde árbol limpio** (commit `773b789`, ADA bajo H15): **4 de 4 resultados
  idénticos** campo a campo salvo revisión, marcas de tiempo e identificadores del intento.
* **Producción intacta:** `git status` sobre `risk`, `execution`, `portfolio`, `paper` y
  `strategies` está vacío. El único cambio fuera de research es el hook aditivo
  `_offer_bar` en `backtesting/engine.py`, declarado en §1.
* **Nota de reproducibilidad entre milestones:** el fix cambia cómo detecta el engine de
  research, así que **M18 y M19 reproducen sólo en sus propios commits**; sus ledgers registran
  la revisión con la que se corrieron y `verify` etiqueta la diferencia como `code_changed`.
