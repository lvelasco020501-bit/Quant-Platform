# M24 — B2 contra regime_trend, bajo un solo protocolo

**Estado:** COMPLETADO · research/backtesting únicamente · 2026-09-24

No se tocó paper, Risk V2 productivo, execution, portfolio, backtesting ni las estrategias
productivas. Ninguna de las dos reglas se modificó: B2 es la de M23, `regime_trend` es la
canónica de M13 que corrieron M15 y M16.

## 1. Por qué hacía falta

M23 dio a B2 un PAPER CANDIDATE y dijo explícitamente lo que **no** había demostrado: que B2
fuera mejor que la regla que el proyecto ya tenía. Los números del incumbente venían de M16,
que corrió otra política de riesgo, sin ventana OOS declarada y sin vecinos. Compararlos tal
cual habría sido comparar dos protocolos, no dos estrategias.

## 2. Cómo se garantizó la simetría

* **Ambas congeladas**, con su procedencia registrada en el modelo.
* **El protocolo viene de M23 por importación, no por copia**: la ventana, las dos funciones
  de ventana y todos los umbrales son los objetos de M23. Un test verifica que no se han
  restablecido localmente.
* **Un solo constructor**, `contender_definition`, arma todas las corridas. Pasar B2 por el
  camino de `Variant` de M22 y RT por el de `SprintCandidate` habría reutilizado más código e
  introducido exactamente la asimetría que este milestone existe para eliminar.
* **Una sola regla de vecinos**, el paso de un cuarto de M23, aplicada a los ejes que cada
  regla tiene. **El test que sostiene el milestone** comprueba que esa regla generalizada
  reproduce exactamente los cuatro vecinos que M23 calculó para B2.

**La reutilización de la evidencia de B2 se verificó, no se asumió.** Se comprobó campo a
campo que M24 y M23 construyen B2 de forma idéntica, y además se re-ejecutó una celda bajo el
nombre de M24: los nueve campos del scorecard coinciden **hasta el último dígito**
(`expectancy` 8.931202046316086538461538 en ambas) con `experiment_id` distintos.

**Asimetría real y declarada:** B2 tiene dos grupos de ventanas; RT tiene un grupo de ventanas
y un umbral. Eso es lo que son las reglas, no una elección. En el eje del umbral el paso de un
cuarto (0.225 / 0.375) es **más estricto** que los vecinos que M13 ya tenía declarados
(0.25 / 0.35); se mantuvo el más duro porque es el mismo que recibe B2.

## 3. La tabla

| | MERCADO | VEREDICTO | OOS | WF | STRESS | DD | PF | TR/AÑO | RISK V2 | VEC PF |
|---|---|---|---|---|---|---|---|---|---|---|
| **RT** | BTC | PROMISING | +4.34% | 0.89 | +17.02% | 4.41% | 2.13 | 4.98 | +8.23% | 1.35 |
| **RT** | ETH | **PAPER CANDIDATE** | +8.25% | 0.78 | +7.87% | 5.49% | 1.39 | 8.63 | +10.37% | 1.21 |
| **RT** | BNB | WEAK | +8.02% | **0.44** | +15.18% | 7.10% | 1.58 | 4.89 | **−2.80%** | 1.12 |
| **RT** | SOL | WEAK | +2.27% | 0.83 | +17.25% | 3.94% | 1.60 | 10.93 | +14.00% | **0.87** |
| **B2** | BTC | **PAPER CANDIDATE** | +4.64% | 0.89 | +10.68% | 6.34% | 1.48 | 7.85 | +12.36% | 1.30 |
| **B2** | ETH | PROMISING | +4.34% | 0.56 | +7.49% | 14.99% | 1.27 | **3.87** | +5.34% | 1.21 |
| **B2** | BNB | PROMISING | +8.19% | 1.00 | +39.61% | 4.44% | 1.74 | 16.95 | +41.94% | 1.44 |
| **B2** | SOL | WEAK | +1.79% | **0.33** | **−12.07%** | 13.63% | 1.07 | **2.48** | **−3.67%** | 1.05 |

Veredictos de `judge()` más `cap_by_sample`, sin intervención.

**Por qué RT no es PAPER CANDIDATE en BTC.** `judge()` sí se lo concede: 0.89 de walk-forward,
4.41% de drawdown, PF 2.13, bate a ambos benchmarks, positiva bajo Risk V2. Lo baja
`cap_by_sample` — la regla que la plataforma ya tenía — porque **dos ventanas de walk-forward
tienen menos de cinco trades** (2022 con 2, 2026 con 4). No es un fallo de rentabilidad sino
de cuántas veces llega a actuar.

**Por qué RT es WEAK en SOL pese a ganar cuatro ejes allí.** Gana OOS, Risk V2, walk-forward y
stress, pero un vecino falla: subir `er_min` un cuarto deja 35 operaciones en seis años y la
regla se vuelve negativa (−1.84%, PF 0.87). Cuatro ejes a favor no salvan un quinto en contra
cuando el umbral es binario.

## 4. Las seis preguntas

### 1. ¿Cuál tiene evidencia más consistente?

**B2, por recuento de veredictos**: 3 mercados en PROMISING o mejor, contra 2 de RT. Pero el
recuento esconde el modo de fallo: los dos WEAK de RT se deciden por **un solo eje cada uno**
(vecinos en SOL, walk-forward y Risk V2 en BNB), mientras el WEAK de B2 en SOL es un fallo
múltiple — walk-forward 0.33, stress negativo, Risk V2 negativo y 2.48 trades/año.

Ninguna domina. B2 es más uniformemente aceptable; RT es más extrema en ambas direcciones.

### 2. ¿Cuál mantiene mejor operabilidad bajo Risk V2?

**RT, aunque el recuento empate 2-2.** Lo que separa es la magnitud del fallo:

| | BTC | ETH | BNB | SOL |
|---|---|---|---|---|
| RT | 4.98 ❌ | 8.63 ✅ | 4.89 ❌ | 10.93 ✅ |
| B2 | 7.85 ✅ | 3.87 ❌ | 16.95 ✅ | 2.48 ❌ |

Los fallos de RT son de **0.02 y 0.11 trades/año** — indistinguibles del umbral. Los de B2 son
del 23% y el 50% por debajo. Y RT queda positiva bajo Risk V2 en 3 de 4 igual que B2.

### 3. ¿Cuál sobrevive mejor costes/stress?

**RT, con claridad.** Sobrevive el peor escenario en **4 de 4**; B2 en 3 de 4.

| retención del retorno | BTC | ETH | BNB | SOL |
|---|---|---|---|---|
| RT | 50% | 43% | 61% | 75% |
| B2 | 34% | 28% | 63% | **−199%** |

Y hay una diferencia estructural: **el drawdown de RT apenas se mueve al estresar los costes**
(BTC 4.41%→4.58%, ETH 5.49%→6.14%), mientras el de B2 empeora (6.34%→8.88%, 14.99%→18.65%).
Tiene mecánica, no es casualidad: RT opera la mitad, así que los costes le muerden la mitad.

### 4. ¿Cuál depende menos de un solo mercado o año?

**RT.** Cumple concentración en 3 de 4 mercados; B2 en 2 de 4.

| cuota del mejor año | BTC | ETH | BNB | SOL |
|---|---|---|---|---|
| RT | 0.20 ✅ | 0.40 ✅ | **0.84** ❌ | 0.48 ✅ |
| B2 | 0.28 ✅ | **0.55** ❌ | 0.43 ✅ | **1.32** ❌ |

La cuota de 1.32 de B2 en SOL significa que su mejor año aporta más que el total: el resto
resta. La de 0.84 de RT en BNB es el otro extremo del mismo problema — sin el +20.5% de 2021,
BNB es negativo para RT, y su 2018 hizo **cero operaciones en todo el año**.

### 5. ¿Alguna alcanza PAPER CANDIDATE bajo el mismo protocolo?

**Las dos, en un mercado cada una, y son mercados distintos.** B2 en BTC, RT en ETH.

### 6. ¿Son suficientemente distintas para justificar paper separado?

**Sí, y la evidencia es simétrica hasta un punto que no esperaba.** Cada una pasa la
operabilidad en exactamente 2 de 4 mercados, cada una tiene exactamente un mercado donde Risk
V2 le vuelve el retorno negativo, y **no son los mismos mercados**: RT se rompe en BNB, B2 en
SOL. RT opera donde B2 no llega (ETH, SOL); B2 opera donde RT no llega (BTC, BNB).

No son variantes la una de la otra. Fallan en sitios complementarios, con mecánicas distintas
—una filtra por régimen y opera poco, la otra rompe canales y opera el doble— y sus
correlaciones de fallo apuntan a mercados opuestos.

## 5. Conclusión

**No se declara ganadora.** Bajo instrucción explícita, y porque los datos tampoco la dan: RT
gana tres ejes (operabilidad, costes, concentración), B2 gana uno (consistencia por recuento),
y cada una alcanza PAPER CANDIDATE en un mercado distinto.

**B2 se mantiene como PAPER CANDIDATE en BTC.** No se cierra como secundaria: `regime_trend` no
superó formalmente las mismas puertas en el mismo mercado — quedó en PROMISING allí, bajada por
muestra fina.

**Lo que corresponde, según lo declarado antes de correr: preparar propuesta de paper para las
dos por separado.** B2 sobre BTC, RT sobre ETH.

## 6. Limitaciones

* **La ventana OOS no es virgen** para ninguna de las dos (ver M23 §1). Para RT es además la
  tercera vez que se la mira, tras M15 y M16.
* **Magnitudes pequeñas.** Los OOS van de +1.79% a +8.25% sobre 2,7 años: entre 0,7% y 3,0%
  anual. Ninguna de las dos es una máquina de hacer dinero.
* **El walk-forward de SOL son 6 ventanas, no 9**, y varias con 5–9 trades. La diferencia
  5/6 contra 2/6 es real pero descansa sobre muestra fina.
* **Un error de lectura propio, corregido en el camino:** afirmé "empate en fragilidad" con
  dos mercados medidos de cuatro; RT falló después en SOL. Era una conclusión sacada antes de
  tener los datos.
* **Los ejes no son independientes.** Operabilidad, stress y concentración están todos
  correlacionados con cuánto opera cada regla, así que "RT gana tres ejes" no son tres
  evidencias separadas sino en buena medida la misma: **RT opera la mitad que B2**.
* **Cuatro mercados de un solo ciclo macro**, largo-only, un mercado abierto a la vez, 4H.

## 7. Tests, gate y reproducibilidad

* **23 tests del protocolo de M24.** El que lleva el peso: la regla generalizada de vecinos
  reproduce exactamente los de M23 para B2. También se fija que los umbrales se importan y no
  se restablecen, que un umbral se escala como Decimal y no por `int()` (0.30 → 0.225, que
  redondeado sería 0 y daría un vecino que entra en cada barra), que ningún eje puede nombrar
  un parámetro que la regla no tiene, y que ambas contendientes reciben la misma configuración
  de riesgo.
* **Suite completa: 2 567 tests.** `ruff format`, `ruff check` y `mypy src` limpios; `mypy .`
  en su línea base de 108 errores / 10 archivos.
* **22 jobs, 0 fallos.** Arrancó a 1 worker; se subió a 2 con el swap en 1.197 MB libres y la
  carga en 3,55, con autorización explícita. Sin degradación.
* **Verificación de reutilización** documentada en §2.
* **Producción intacta:** `git status` sobre `risk`, `execution`, `portfolio`, `paper`,
  `strategies` y `backtesting` está vacío.

## 8. Qué sigue

**No se toca la sesión de paper actual.** Lo que M24 deja listo, y que es una decisión tuya:

1. **Propuesta de paper para B2 sobre BTC** y **para RT sobre ETH**, por separado.
2. Antes de cualquiera: los requisitos de producción que M21 dejó escritos — persistencia del
   latch entre reinicios, reconciliación al arranque, `clientOrderId` idempotente.
3. La pregunta que ningún backtest responde: si un edge de 1–3% anual justifica el coste
   operativo de una sesión de paper. Es coste de oportunidad, no estadística.
