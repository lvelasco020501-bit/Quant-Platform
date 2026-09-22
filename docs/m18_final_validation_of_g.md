# M18 — Validación dirigida de la política G

**Estado:** COMPLETADO · research/backtesting únicamente · 2026-09-21

No se tocó paper, Risk V2 productivo, execution, portfolio ni las estrategias productivas.
Mismos datos, estrategia, timeframe y costes que M16/M17.

## 1. Por qué este milestone

M17 recomendó **G** (parar al 10% de drawdown, reabrir a los 30 días y medir el siguiente
drawdown **desde la reapertura**) con evidencia fina: a costes reales el breaker sólo se dispara
en 1 de 6 mercados, así que toda la elección descansaba en XRP y un único episodio. Quedaban dos
preguntas sin responder, ambas sobre qué pasa cuando parar **no** es raro:

* **Flapping** — ¿reabre y vuelve a pararse enseguida, como hizo E veinte veces?
* **Ratchet** — cada reinicio permite otra caída del 10% desde un nivel más bajo. Encadenado,
  eso convierte un límite del 10% en una pérdida sin techo.

## 2. Cómo se forzó al breaker a dispararse

Sin inventar ningún mercado. Dos vías, ambas con la maquinaria que ya existe:

1. **Escalera de costes** — S2: fees ×3, slippage ×5, spread 10 bps · S3: fees ×5, slippage ×10,
   spread 25 bps. Duras, pero son condiciones reales para una alt fina en un mal día.
2. **Breaker más estrecho** — las mismas políticas con el límite al **5%** (el más estrecho que
   la propia configuración de Risk acepta) y costes reales. No distorsiona ningún mercado: sólo
   hace que el breaker actúe lo bastante a menudo como para verlo reabrir muchas veces. Es una
   **sonda del mecanismo**, nunca una configuración candidata.

Resultado: **370 halts** repartidos entre las tres políticas, frente a los 2 de todo M17.

## 3. La tabla

| POLICY | LÍMITE | HALTS | REAPERTURAS REALES | BLOCKED % | MAX DD | PEOR RATCHET | ESTABILIDAD | VEREDICTO |
|---|---|---|---|---|---|---|---|---|
| C | 10% | 7 | 0/0 | **80.37%** | 10.56% | ninguno | 3.70% | **FAIL** |
| C | 5% | 8 | 0/0 | **89.86%** | 5.72% | ninguno | 3.70% | **FAIL** |
| E | 10% | 130 | **0/123** | 23.21% | 10.56% | ninguno | 3.70% | **FAIL** |
| E | 5% | 188 | **0/180** | 25.08% | 5.72% | ninguno | 3.70% | **FAIL** |
| G | 10% | 11 | 10/11 | 2.97% | **19.16%** | **3 halts, 17.27%** | 0.80% | **FAIL** |
| G | 5% | 14 | 14/14 | 3.96% | **11.17%** | **3 halts, 10.89%** | 0.80% | **FAIL** |

**Las tres fallan, cada una por un motivo distinto:**

* **C** no reabre nunca — 0 reaperturas de 7 y 8 halts — y deja mercados bloqueados hasta el
  **89.86%** de su historia. Protege apagando.
* **E** reabre 123 y 180 veces y **ninguna** de esas reaperturas acabó en un trade. Es la
  confirmación a escala de lo que XRP ya mostraba: reabrir conservando la referencia es reabrir
  contra el mismo rechazo.
* **G** hace exactamente lo que se le pedía —10/11 y 14/14 reaperturas con trade, 0% de flapping,
  0.80% de swing ante cambios de coste— y rompe lo único que un límite de drawdown debe
  garantizar: **con un límite del 10% llegó a perder el 19.16%**.

## 4. El ratchet, con nombre y fecha

| Mercado · escenario | Cadena | Periodo | Caída de la cadena | Max DD |
|---|---|---|---|---|
| ADA · S3 | 3 halts | 2021-01-05 → 2025-04-02 | 17.27% | **19.09%** |
| XRP · S3 | 3 halts | 2020-04-30 → 2023-11-23 | 14.34% | **19.16%** |
| XRP · sonda 5% | 3 halts | 2019-04-04 → 2020-05-30 | 10.89% | **11.17%** |
| XRP · sonda 5% (fees ×1.25) | 3 halts | 2019-04-04 → 2020-05-30 | 10.29% | 10.56% |

No es aleteo: son cadenas de **años**. Cada reinicio, por separado, es defendible; la cadena no.
El límite del 10% se convirtió en ~19% y el del 5% en ~11%: **el ratchet duplica el daño
permitido**.

## 5. Las cinco preguntas

1. **¿G es segura?** **No como está.** Recorta el tiempo bloqueado de 80–90% a 3–4%, pero deja que
   el drawdown acumulado llegue al doble de su propio límite.
2. **¿G es operable?** **Sí, y con claridad:** 10/11 y 14/14 reaperturas terminaron en un trade,
   0% de flapping, y el swing ante un 25% más de comisión es 0.80% (C: 3.70%).
3. **¿Hay ratchet?** **Sí, confirmado**: tres cadenas de 3 halts, en dos mercados distintos y en
   dos regímenes de coste distintos.
4. **¿Supera claramente a C y E?** En operabilidad, sí, sin discusión. En control del daño, no:
   C y E mantienen el drawdown en 10.56% donde G llega a 19.16%.
5. **¿Hay evidencia suficiente para proponer un cambio productivo?** **No.** Ahora sí hay muestra
   (370 halts), y lo que dice es que ninguna de las tres reglas sirve tal cual.

## 6. Veredicto

**NO-GO.** No se propone ningún cambio a Risk V2 productivo.

Lo que este milestone sí deja cerrado:

* el latch permanente (C) está descartado — apaga mercados durante años;
* el cooldown que conserva la referencia (E) está descartado — reabre sin poder operar;
* el reinicio local (G) resuelve la operabilidad y **necesita un techo global de pérdida** que
  ningún reinicio pueda borrar. Ése es exactamente el diseño que M19 va a probar.

## 7. Tests, gate y reproducibilidad

* **42 tests** entre el motor de recuperación (22) y el protocolo de M18 (20), incluidos los dos
  que pinchan errores propios: que la regla permanente reproduce el wrapper de M14 bit por bit, y
  que un drawdown por encima del umbral **tiene** que registrar un halt.
* **72 runs, 18 jobs, 0 fallos.**
* Un error propio corregido a mitad: la primera tabla decía que C "nunca paró" mientras la misma
  corrida mostraba 80% del tiempo bloqueado — el wrapper sólo registraba los halts que él mismo
  imponía, y el de C lo impone el engine. Se corrigió, se re-corrió C entero y hay un test que lo
  fija.
