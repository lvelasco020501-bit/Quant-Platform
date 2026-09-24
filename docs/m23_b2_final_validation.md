# M23 — Validación final de B2

**Estado:** COMPLETADO · research/backtesting únicamente · 2026-09-24

No se tocó paper, Risk V2 productivo, execution, portfolio, backtesting ni las estrategias
productivas. Ni una línea de estrategia escrita: B2 es `breakout_trend` con los parámetros
canónicos de M13 duplicados mecánicamente, exactamente como M22 los corrió.

## 1. La ventana OOS, declarada antes de correr

**`2024-01-01` → `2026-09-15`. Una sola fecha, idéntica en los cuatro mercados.** Commiteada
en `f43e6a4`, antes de que existiera un solo resultado de M23.

| | in-sample | OOS |
|---|---|---|
| BTC | 2017-09 → 2024-01 (6,3 a) | 2,7 a |
| ETH | 2017-09 → 2024-01 (6,3 a) | 2,7 a |
| BNB | 2017-12 → 2024-01 (6,1 a) | 2,7 a |
| SOL | 2020-09 → 2024-01 (3,3 a) | 2,7 a |

La regla que la produjo no mira resultados: el tramo más reciente es lo que una regla
desplegada encuentra a continuación, una frontera de calendario corta cuatro historias igual,
y deja a todos ≥3 años dentro de muestra. Un test lo fija.

**Y no es una ventana virgen.** M22 corrió B2 sobre la historia completa, estas fechas
incluidas, y reportó recuentos anuales agregados. Decir lo contrario sería falso. Lo que la
ventana sí es limpia respecto de es lo único que importa para una afirmación sobre parámetros:
**ningún parámetro se eligió mirándola.**

## 2. Resultados por activo

### In-sample contra out-of-sample

| | IS net · DD | OOS net · DD | anualizado IS → OOS |
|---|---|---|---|
| BTC | +21.75% · 6.34% | +4.64% · 2.00% | 3.2% → **1.7%** |
| ETH | +16.25% · 14.99% | +4.34% · 4.76% | 2.4% → **1.6%** |
| BNB | +47.93% · 3.76% | +8.19% · 4.44% | 6.6% → **2.9%** |
| SOL | +6.28% · 13.63% | +1.79% · 5.37% | 1.9% → **0.66%** |

El ritmo anualizado **cae a la mitad** fuera de muestra en los cuatro. Es lo normal —dentro de
muestra siempre halaga— pero fija la magnitud real: **1,6% a 2,9% anual**, antes de stress. El
drawdown, en cambio, mejora fuera de muestra en tres de los cuatro.

### Las siete comprobaciones declaradas

| condición | BTC | ETH | BNB | SOL |
|---|---|---|---|---|
| OOS ≥ +3% | ✅ 4.64% | ✅ 4.34% | ✅ 8.19% | ❌ 1.79% |
| walk-forward ≥ 0.5 | ✅ 8/9 | ✅ 5/9 | ✅ 9/9 | ❌ 2/6 |
| mediana WF > 0 | ✅ +1.97% | ✅ +0.76% | ✅ +2.85% | ❌ −0.37% |
| Risk V2 positivo | ✅ +12.36% | ✅ +5.34% | ✅ +41.94% | ❌ −3.67% |
| Risk V2 ≥5 trades/año | ✅ 7.9 | ❌ **3.9** | ✅ 17.0 | ❌ 2.5 |
| ≥60% años positivos | ✅ 0.90 | ✅ 0.70 | ✅ 0.80 | ❌ 0.57 |
| mejor año ≤50% del total | ✅ 0.28 | ❌ **0.55** | ✅ 0.43 | ❌ 1.32 |

**SOL falla seis de siete.** Su cuota de concentración de **1.32** significa que el mejor año
aporta más que el total: el resto, en conjunto, resta. Es el retrato de una regla que estuvo
presente en un movimiento. **ETH falla dos**: Risk V2 lo deja en 3,9 trades al año, y el 55%
de su resultado sale de un solo año.

## 3. Walk-forward

| | positivas | mediana | folds con <5 trades |
|---|---|---|---|
| BTC | **8/9** | +1.97% | 0 |
| ETH | 5/9 | +0.76% | 0 |
| BNB | **9/9** | +2.85% | 0 |
| SOL | 2/6 | −0.37% | 0 |

BTC por año: +0.20, +6.61, +4.86, +2.46, **−3.09**, +1.00, +3.15, +1.97, +0.87 (%). Ningún año
domina; el mejor es el 28% del total.

## 4. Stress de costes

| peor escenario (fees ×2 + slip ×3 + spread 5bps) | net | dd | pf | retiene |
|---|---|---|---|---|
| BTC | **+10.68%** | 8.88% | 1.16 | 34% |
| ETH | **+7.49%** | **18.65%** | **1.07** | 28% |
| BNB | **+39.61%** | 4.34% | 1.49 | 63% |

Los tres sobreviven. ETH es el más ajustado: su PF baja a 1.07 —a siete puntos de perder
dinero— y su drawdown **empeora** de 14.99% a 18.65%. BNB es el más robusto con diferencia.

**Un control de reproducibilidad no planeado:** el peor escenario de BTC dio **+10.68%**, dígito
a dígito igual que en M22, en dos corridas independientes con `experiment_id` distintos y dos
días de commits de diferencia. Nada de lo tocado entre ambos milestones movió el motor.

## 5. Sensitivity — doce vecinos, los doce rentables

Vecinos calculados por `neighbour_params()`, no elegidos: dos ejes, un cuarto en cada
dirección, un eje a la vez, el canal conservando su proporción 2:1.

| | B2 40/20/400 | 30/15/400 | 50/25/400 | 40/20/300 | 40/20/500 | PF mín |
|---|---|---|---|---|---|---|
| BTC | +31.31% (1.48) | +38.35% (1.56) | +21.40% (1.30) | +36.13% (1.50) | +35.03% (1.57) | **1.30** |
| ETH | +27.17% (1.27) | +38.93% (1.37) | +23.54% (1.24) | +30.01% (1.28) | +20.16% (1.21) | **1.21** |
| BNB | +62.97% (1.74) | +42.47% (1.44) | +47.70% (1.61) | +58.65% (1.65) | +62.50% (1.74) | **1.44** |

**No hay acantilado.** Los doce vecinos tienen PF entre 1.21 y 1.74. B2 no está en un pico sino
en una meseta ancha, que es la forma que tiene un edge real frente a un ajuste afortunado.

En BTC y ETH el canal corto (30/15) rinde más que B2; en BNB rinde **menos** que todos. No hay
dirección consistente entre mercados, así que la tentación de "mejorar" a 30/15 queda sin
respaldo — y adoptarla ahora sería elegir parámetros por resultado, que es lo que este
milestone existe para no hacer. Queda anotado como hipótesis para un milestone que la declare
antes, no como conclusión de este.

## 6. Risk V2 desplegado

| | net | dd | trades | trades/año |
|---|---|---|---|---|
| BTC | +12.36% | 2.87% | 71 | 7.9 |
| ETH | +5.34% | 2.71% | 35 | **3.9** |
| BNB | +41.94% | 3.76% | 149 | 17.0 |
| SOL | **−3.67%** | 3.98% | 15 | **2.5** |

Los breakers con latch recortan entre el 70% y el 90% de los trades. En BTC y BNB queda
operable; en ETH y SOL, por debajo del suelo que la plataforma ya exige.

## 7. Veredicto

| MERCADO | VEREDICTO |
|---|---|
| **BTC** | **PAPER CANDIDATE** |
| ETH | PROMISING |
| BNB | PROMISING |
| SOL | WEAK |

Veredicto mecánico de `judge()` más `cap_by_sample`, sin intervención. **Es el primer PAPER
CANDIDATE que produce el proyecto**, y llega sin haber rebajado un solo criterio: la ventana
OOS se declaró antes, los vecinos se calcularon, los umbrales o son de la plataforma o están
escritos con su razonamiento previo.

### Tres cosas que lo matizan, y hay que decirlas

1. **La magnitud es pequeña.** +4.64% en 2,7 años out-of-sample son ~1,7% anual. Bajo Risk V2
   desplegado, +12.36% en nueve años son ~1,3% anual. Es un edge real, medido y reproducible —
   no es una máquina de hacer dinero, y desplegarlo con esa expectativa sería un error.
2. **Es un veredicto por mercado, no de cartera.** Dos de cuatro mercados fallan condiciones
   declaradas. "B2 es paper candidate" significa *en BTC*.
3. **No se ha comparado con el incumbente bajo este protocolo.** En BTC, `regime_trend` hace
   +34.32% con DD 4.41% y 124 trades, contra +31.31% / 6.34% / 234 de B2. Pero `regime_trend`
   nunca pasó por la ventana OOS de M23 ni por estos vecinos. **M23 validó B2; no demostró que
   B2 sea mejor que lo que ya teníamos.** Esa comparación es un milestone aparte.

### Por qué BNB, que parece más fuerte, sale PROMISING

Su walk-forward es 9/9, su stress retiene el 63% y su Risk V2 da +41.94%. Queda en PROMISING
por una sola razón: en BNB el canal de Donchian **desnudo** hizo +68.59% y B2 hizo +62.97%, y
`judge()` no concede PAPER CANDIDATE a lo que no bate a ambos benchmarks. La regla funciona
como se diseñó y el número es el que es.

## 8. Limitaciones

* **La ventana OOS no es virgen** (§1).
* **Segundo hueco de pre-declaración, del mismo tipo que el de M22:** fijé el umbral de
  operabilidad (5 trades/año) pero **no cuántos mercados deben cumplirlo**. Se reporta por
  mercado, que es como se escribió, y no se rellena ahora con un número que convenga.
* **Sin umbral de retención ante costes**, deliberadamente: ya sabía que B2 retuvo el 34% en
  BTC, y fijar un número después de saberlo sería elegirlo por el resultado. Se reporta
  descriptivamente.
* **SOL no tiene stress ni vecinos**: su veredicto ya estaba determinado en WEAK por
  walk-forward 0.33 con mediana negativa y Risk V2 en pérdidas. No se gastó cómputo en
  confirmar lo ya decidido, y se dice en vez de omitirse.
* **`Evidence.full` usa la corrida de historia completa de M22**, idéntica en regla, ventana y
  costes. M23 corrió las mitades (IS y OOS) por separado, no el total.
* **Largo-only, un mercado abierto a la vez, 4H, nueve años de un solo ciclo macro.**

## 9. Tests, gate y reproducibilidad

* **23 tests del protocolo de M23**, más los 43 de M22. Los que llevan el peso: la ventana OOS
  es una fecha fija idéntica en los cuatro mercados; in-sample acaba exactamente donde empieza
  OOS; ningún mercado queda con menos de tres años dentro de muestra; los cuatro vecinos se
  **recalculan** desde el candidato; el canal conserva su proporción 2:1; un eje desconocido es
  rechazado; y el suelo de operabilidad es literalmente `MIN_TRADES_PER_TEST_WINDOW`.
* **Suite completa: 2 543 tests.** `ruff format`, `ruff check` y `mypy src` limpios; `mypy .` en
  su línea base de 108 errores / 10 archivos.
* **31 corridas, 22 jobs, 0 fallos**, a 1 worker de principio a fin. Ledger por job.
* **Reproducibilidad cruzada entre milestones:** el peor escenario de stress de BTC coincide
  dígito a dígito con el de M22 (§4).
* **Producción intacta:** `git status` sobre `risk`, `execution`, `portfolio`, `paper`,
  `strategies` y `backtesting` está vacío.

## 10. Qué sigue, y qué no

**No** se despliega nada. Un PAPER CANDIDATE es una etiqueta de evidencia, no una orden.

Lo que tendría sentido, en orden:

1. **Comparar B2 contra `regime_trend` bajo el protocolo de M23**, con la misma ventana OOS y
   los mismos vecinos. Hoy no se sabe cuál es mejor; se sabe que B2 pasa.
2. **Decidir si un edge de ~1,3% anual bajo Risk V2 merece una sesión de paper**, que es una
   pregunta de coste de oportunidad, no de estadística.
3. Si se decide que sí: diseñar la sesión con los requisitos de producción que M21 dejó
   escritos — persistencia del latch, reconciliación al arranque, `clientOrderId` idempotente.
