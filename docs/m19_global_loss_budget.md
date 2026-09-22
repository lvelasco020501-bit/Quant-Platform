# M19 — Reinicio local con presupuesto global de pérdida

**Estado:** COMPLETADO · research/backtesting únicamente · 2026-09-22

No se tocó paper, Risk V2 productivo, execution, portfolio ni las estrategias productivas.
Mismos datos, estrategia, timeframe, costes y escenarios que M18.

## 1. Las políticas, en una línea cada una

| Política | Regla | Papel |
|---|---|---|
| **A** | Risk V2 productivo: 5 pérdidas seguidas o 20% de drawdown, ambos permanentes | referencia (M17) |
| **C** | 10% de drawdown, latch permanente | referencia |
| **E** | 10%, cooldown 30 días, **conserva** la referencia | referencia |
| **G** | 10%, cooldown 30 días, **reinicia** la referencia en la reapertura | referencia |
| **H** | G + **techo global del 20%** sobre la pérdida desde el máximo original | candidata |
| **I** | G + **máximo 2 reinicios por año móvil**, después latch definitivo | candidata |
| **J** | G + **ambos** | candidata |

H, I y J comparten con G el límite local (10%) y el cooldown (30 días) exactos, así que cualquier
diferencia sería el presupuesto y nada más.

## 2. El resultado: los presupuestos nunca se activaron

**48 de 48 corridas de H, I y J son idénticas a G** — mismos trades, mismo retorno, mismo
drawdown, mismos halts, mismas cadenas. **Ningún presupuesto se gastó ni una sola vez.**

| POLICY | LÍMITE | HALTS | REAPERTURAS REALES | BLOCKED % | MAX DD (local) | GLOBAL DD | RESETS | PEOR CADENA | OPERABILIDAD | VEREDICTO |
|---|---|---|---|---|---|---|---|---|---|---|
| C | 10% | 7 | 0/0 | 80.37% | 10.36% | 10.56% | 0 | ninguna | — | FAIL |
| C | 5% | 8 | 0/0 | 89.86% | 5.70% | 5.72% | 0 | ninguna | — | FAIL |
| E | 10% | 130 | 0/123 | 23.21% | 10.47% | 10.56% | 123 | ninguna | 0% | FAIL |
| E | 5% | 188 | 0/180 | 25.08% | 5.72% | 5.72% | 180 | ninguna | 0% | FAIL |
| G | 10% | 11 | 10/11 | 2.97% | 17.17% | **19.16%** | 11 | 3 halts, 17.27% | 91% | FAIL |
| G | 5% | 14 | 14/14 | 3.96% | 10.87% | 11.17% | 14 | 3 halts, 10.89% | 100% | FAIL |
| **H** | 10% | 11 | 10/11 | 2.97% | 17.17% | **19.16%** | 11 | 3 halts, 17.27% | 91% | **FAIL** |
| **I** | 10% | 11 | 10/11 | 2.97% | 17.17% | **19.16%** | 11 | 3 halts, 17.27% | 91% | **FAIL** |
| **J** | 10% | 11 | 10/11 | 2.97% | 17.17% | **19.16%** | 11 | 3 halts, 17.27% | 91% | **FAIL** |

### Por qué no se activó ninguno — y por qué es culpa del diseño, no de los datos

1. **El techo del 20% quedó por encima del daño observado.** El peor drawdown global fue
   **19.16%**. El techo nunca llegó a tocarse por 0.84 puntos. Tomar el 20% de producción era
   defendible como principio ("nunca perder más de lo que producción ya permite") pero significa
   que el ratchet de M18 —17.27% y 14.34%— **cabe entero dentro del presupuesto**.
2. **La ventana del allowance estaba mal elegida, y es un error mío.** Fijé "2 reinicios por año
   móvil" razonando que era el máximo que no permite una cadena de tres dentro de un año. La frase
   es cierta y es inútil: **las cadenas de M18 nunca ocurrieron dentro de un año**. La de XRP va de
   2020-04-30 a 2023-11-23 (3.5 años) y la de ADA de 2021-01-05 a 2025-04-02 (4.3 años). Nunca hay
   más de dos reinicios en ningún año móvil, así que el allowance no podía morder. Debí medir la
   duración de las cadenas antes de elegir la ventana.

### Un requisito que añadí *después* de ver esto

Las puertas de M19 preguntan cómo se *comporta* un presupuesto; no pueden preguntar si llegó a
comportarse. Con el techo sin tocar y el allowance sin morder, H, I y J pasaban las puertas
**por el comportamiento de G**. Añadí una puerta más — *el presupuesto tiene que haber mordido al
menos una vez* — y la declaro como lo que es: **añadida después de los resultados**. No cambia
ninguna conducta ni ningún orden; cambia sólo si esto puede llamarse evidencia. No puede.

## 3. Segundo hallazgo, independiente: el breaker actúa tarde tras un reinicio

La columna **MAX DD (local)** mide cuánto dejó correr **un solo** halt desde su propia referencia.
Bajo G/H/I/J llega a **17.17% con un límite del 10%**.

La causa está documentada desde M17: el disparo del **primer** halt viene del breaker real del
engine, evaluado en cada barra; después de un reinicio de referencia el wrapper sólo puede evaluar
en los puntos de decisión, que es una **cota inferior** — detiene tarde, nunca temprano. M19 le
pone número a ese coste: hasta 7 puntos porcentuales de retraso.

Esto no es un defecto de la idea del presupuesto. Es un defecto de **cómo se mide el drawdown tras
un reinicio**, y cualquier diseño productivo tendría que resolverlo dentro del engine, no en un
wrapper de research.

## 4. Ratchet, antes y después

Sin cambios, porque el presupuesto no intervino:

| Mercado · escenario | Cadena | Caída | Global DD | ¿Dentro del techo del 20%? |
|---|---|---|---|---|
| XRP · S3 | 3 halts | 14.34% | 19.16% | sí (por 0.84 pp) |
| ADA · S3 | 3 halts | 17.27% | 19.09% | sí (por 0.91 pp) |
| XRP · sonda 5% | 3 halts | 10.89% | 11.17% | sí |

## 5. Alcance: qué se corrió y qué no

Se corrieron **ETH, BNB, XRP y ADA** (12 jobs, 48 corridas). **BTC y SOL no se re-corrieron**:
M18 demostró que su breaker no se dispara en **ningún** escenario —0 halts bajo C, E y G, con
drawdowns máximos de 8.32% y 6.32%— y un presupuesto que sólo actúa después de un halt no puede
cambiar un mercado que nunca para. Está dicho en el reporte, no omitido en silencio.

La máquina entró otra vez en una fase de degradación (jobs de 2.000s pasando a 40.000s+ sin
terminar, con la carga del sistema en 39 y 6.5M de swapouts). Se abortó la corrida de 18 jobs y se
re-planificó a los 4 mercados que sí pueden activar el breaker.

## 6. Las respuestas

1. **¿Alguna política pasa?** **No.** C y E fallan como en M18. G falla por ratchet y por actuar
   tarde. H, I y J fallan porque **no hicieron nada**: son G bit a bit.
2. **¿Existe el ratchet?** Sí, sin cambios: 3 cadenas de 3 reinicios, hasta 17.27% de caída.
3. **¿El presupuesto global lo contiene?** **No se sabe.** Con estos valores no llegó a probarse.
4. **¿Hay evidencia para cambiar Risk V2 productivo?** **No, y en ninguna dirección.**

## 7. Veredicto

**NO-GO.** No se propone ningún cambio a producción.

**Lo que sí queda establecido, y no es poco:**

* el latch permanente (C) apaga mercados hasta el 89.86% de su historia — descartado;
* el cooldown que conserva la referencia (E) reabre 303 veces sin operar ni una — descartado;
* el reinicio local (G) es el único operable (91–100% de reaperturas con trade, 0% de flapping,
  0.80% de swing ante costes) **y** deja el drawdown llegar al 19.16% con un límite del 10%;
* un presupuesto global sigue siendo la dirección correcta — pero **hay que fijarlo donde muerda**.

**Diseño concreto para M20**, declarado aquí antes de correrlo:

1. **Techo global por debajo del daño observado**, probado en rango: 12% y 15% sobre un límite
   local del 10% (es decir, 1.2× y 1.5×). El 20% de producción ya se sabe que no muerde.
2. **Allowance contado en una ventana comparable a las cadenas**: 2 reinicios por **3 años
   móviles**, o 2 reinicios totales antes de revisión manual — no por año.
3. **Arreglar la detección tras el reinicio dentro del engine**, para que el segundo halt y los
   siguientes se evalúen por barra como el primero. Sin eso, cualquier techo se cruza tarde.

## 8. Tests, gate y reproducibilidad

* **46 tests** entre el motor de recuperación (28) y el protocolo de M19 (18). Los que más
  importan: que un techo muy por encima de la serie deja el comportamiento de G **idéntico**, que
  el allowance es de año móvil y no de por vida, que un presupuesto exige una regla que reinicie,
  que el techo tiene que ser más ancho que el límite local, y el añadido después de los
  resultados: **un presupuesto que nunca mordió no es evidencia de que funcione**.
* **Suite completa: 2 446 tests en verde.** `ruff format`, `ruff check`, `mypy src` limpios y
  `mypy .` en su línea base de 108 errores / 10 archivos.
* **48 corridas, 12 jobs, 0 fallos.** 16 ledgers verificados, **0 fallos de reproducibilidad**.
* **Re-ejecución desde árbol limpio** (commit `a40d58c`, ADA bajo H): **4 de 4 resultados
  idénticos** campo a campo salvo revisión, marcas de tiempo e identificadores del intento.
* Producción intacta: `git status` sobre `risk`, `execution`, `portfolio` y `paper` está vacío.
