# M17 — Rediseño del latch de drawdown

**Estado:** COMPLETADO · research/backtesting únicamente · 2026-09-20

No se tocó paper, Risk V2 productivo, execution, portfolio ni las estrategias productivas.
`regime_trend`, sus parámetros, el timeframe 4H, los datasets de M16 y los costes son los mismos.
Lo único que cambia es **cuándo vuelve a operar un mercado detenido por el breaker**.

## 1. El latch nuevo, en una frase

> Si el drawdown llega al 10%, se detiene la apertura de exposición durante 30 días; al reabrir,
> el drawdown se vuelve a medir **desde ahí**, no desde un máximo que la cuenta ya no puede
> alcanzar.

Eso es la política **G**. Las dos mitades importan, y la segunda es la que casi se nos escapa.

**Por qué no basta con "esperar 30 días" (política E).** El *límite* de drawdown no es el latch:
mientras el equity esté 10% por debajo de su referencia, el límite rechaza exposición nueva haya
latch o no. Una regla que sólo termina la pausa reabre el mercado directamente contra el mismo
rechazo. Bajo el peor escenario de costes, E reabrió XRP **veinte veces** y terminó exactamente en
el resultado de C: 22 trades, −6.01%. Aletea, no se recupera.

## 2. Las cinco políticas

| Política | Regla | Papel |
|---|---|---|
| **A** | Risk V2 productivo: 5 pérdidas seguidas y 20% de drawdown, ambos permanentes | referencia |
| **C** | drawdown 10%, permanente (la de M16, el defecto) | referencia |
| **E** | drawdown 10%, cooldown 30 días, **referencia conservada** | candidata |
| **F** | drawdown 10%, **reset por trimestre** | referencia (ver §7) |
| **G** | drawdown 10%, cooldown 30 días, **nuevo high-water mark** | candidata |

Umbral (10%) y cooldown (30 días) son **idénticos** en todas las candidatas, por test: así la
comparación aísla la regla de recuperación y no mezcla un límite distinto. El cooldown sale de la
estrategia, no de un retorno: su filtro de régimen mira 72 barras, que a 4H son doce días, y 30
días son ~2.5 de sus propias ventanas.

## 3. Resultado por activo (costes reales)

| ASSET | POLICY | TRADES | NET | PF | DD | BLOCKED | HALTS | REABIERTO Y OPERÓ |
|---|---|---|---|---|---|---|---|---|
| BTC | A / C / E / F / G | 45 / 124 / 124 / 124 / 124 | +8.23% / +34.32% (×4) | 1.58 / 2.13 | 4.36% / 4.41% | **73.92%** / 0% | 0 | — |
| ETH | A / C / E / F / G | 78 / 146 (×4) | +10.37% / +18.59% | 1.37 / 1.39 | 4.19% / 5.49% | **59.37%** / 0% | 0 | — |
| BNB | A / C / E / F / G | 43 / 129 (×4) | **−2.80%** / +24.84% | 0.84 / 1.58 | 6.80% / 7.10% | **65.23%** / 0% | 0 | — |
| SOL | A / C / E / F / G | 66 / 106 (×4) | +14.00% / +23.01% | 1.59 / 1.60 | 3.20% / 3.94% | **48.59%** / 0% | 0 | — |
| XRP | A | 11 | −1.15% | 0.83 | 5.69% | **89.86%** | 0 | — |
| XRP | **C** | **27** | **−2.95%** | 0.79 | 10.14% | **73.93%** | 1 (permanente, **2 239 d**) | 0/0 |
| XRP | **E** | 140 | +49.22% | 1.90 | 10.14% | 0.99% | 1 (30 d) | 1/1 |
| XRP | **F** | 144 | +50.46% | 1.91 | 10.14% | 0.00% | **0** | — |
| XRP | **G** | 140 | +49.22% | 1.90 | 10.14% | 0.99% | 1 (30 d) | 1/1 |
| ADA | A / C / E / F / G | 13 / 139 (×4) | −2.93% / +4.40% | 0.56 / 1.08 | 5.90% / 6.02% | **79.45%** / 0% | 0 | — |

**Hecho incómodo y central:** en cinco de seis mercados el breaker del 10% **nunca se dispara** a
costes reales — sus drawdowns máximos van de 3.94% a 7.10%. C, E, F y G dan resultados idénticos
ahí. Toda la comparación entre reglas de recuperación descansa sobre **XRP**, y dentro de XRP sobre
**un único episodio** (2020-07-29). Ver §8.

**Risk V2 productivo (A) es el peor de los cinco en todo lo que mide este milestone:** bloquea
entre el 48% y el 90% de la historia de cada mercado y pierde dinero en BNB, XRP y ADA.

## 4. Continuidad ante cambios pequeños de costes

Trades con comisión ×1.00 / ×1.10 / ×1.25 (slippage sin tocar):

| ASSET | C | E | F | G |
|---|---|---|---|---|
| BTC | 124 / 125 / 125 | igual | igual | igual |
| ETH | 146 / 146 / 146 | igual | igual | igual |
| BNB | 129 / 129 / 130 | igual | igual | igual |
| SOL | 106 / 106 / 106 | igual | igual | igual |
| **XRP** | **27 / 144 / 144 → swing 81%** ⚠️ | 140 / 144 / 144 → 2.78% | 144 / 144 / 144 → 0% | 140 / 144 / 144 → **2.78%** |
| ADA | 139 / 139 / 139 | igual | igual | igual |

**La discontinuidad extrema desaparece.** Con C, un 10% más de comisión cambiaba XRP de 27 trades
y −2.95% a 144 trades y +50.77%. Con E, F o G el mercado opera en los tres escenarios.

## 5. Dónde se separan E y G: el stress

Peor escenario (fees ×2 + slippage ×3 + spread 5 bps). En BTC, ETH, BNB, SOL y ADA las tres dan
exactamente lo mismo; en XRP no:

| POLICY | TRADES | NET | DD | BLOCKED | HALTS | REABIERTO Y OPERÓ |
|---|---|---|---|---|---|---|
| C | 22 | −6.01% | 10.08% | 79.62% | 1 (permanente, 2 411 d) | 0/0 |
| **E** | **22** | **−6.01%** | 10.08% | 19.67% | **20** | **0/20** |
| F | 142 | +31.70% | 11.43% | 2.71% | 1 (82 d) | 1/1 |
| **G** | **142** | **+31.70%** | 11.43% | 0.99% | 1 (30 d) | 1/1 |

E termina en el resultado exacto de C tras veinte reaperturas. Ese es el argumento decisivo contra
conservar la referencia.

## 6. E vs G — las cuatro preguntas

| | E (conserva referencia) | G (nuevo high-water mark) |
|---|---|---|
| **Más segura** | DD 10.08% bajo stress vs 11.43% de G — pero esa "seguridad" es la de no operar: mismo resultado que el latch permanente | 1.35 pp más de DD, a cambio de seguir operando. Un solo halt, nunca encadenó reinicios |
| **Más predecible** | 20 pausas bajo stress, todas inútiles | 1 pausa de exactamente 30 días |
| **Menos sensible a variaciones pequeñas** | empate: swing 2.78% | empate: swing 2.78% |
| **Riesgo de quedar apagada / reabrir demasiado pronto** | se queda apagada de facto (5% de sus pausas terminan en un trade) | reabre y opera; el riesgo teórico es el contrario — cada reinicio admite otro 10% de caída |

**El riesgo real de G**, dicho sin adornos: al mover la referencia, G acepta otro 10% de caída desde
el reinicio. En el peor caso eso encadena ("ratchet"). En esta evidencia **no ocurrió** — G nunca
tuvo más de un halt en ningún mercado ni escenario — pero seis mercados y un episodio no bastan
para descartarlo.

## 7. Por qué F queda fuera aunque gane en los números

La regla pre-declarada, leída al pie de la letra, **recomendaba F**: su swing (0.80%) es menor que
el de E y G (2.78%) y su tiempo bloqueado es 0%. Pero F nunca detuvo XRP **pese a su drawdown del
10.14%**, porque una referencia trimestral no puede ver un drawdown *all-time*: el drawdown dentro
de cada trimestre nunca llegó al 10%.

Eso destapa una degeneración de las propias métricas: **el óptimo de "continuidad" y "tiempo
bloqueado" es un breaker que no se dispara nunca.** F gana por no hacer su trabajo. Quedó excluida
como referencia por instrucción previa a leer los resultados de los seis mercados, y los resultados
confirman el motivo. Está codificado en `REFERENCES`, con test.

Además: tras su primer reset trimestral, la detección de F deja de venir del breaker por barra del
engine y pasa a evaluarse en los puntos de decisión, así que **puede sub-detectar**. No es la más
segura; es la más ciega.

## 8. Limitaciones — leer antes de decidir sobre producción

1. **La muestra para elegir la regla de recuperación es un mercado y un episodio.** En 5 de 6
   mercados el breaker nunca se dispara a costes reales. E y G son **idénticas** en los seis a
   costes reales; sólo se separan en XRP bajo el stress más duro.
2. **El trigger es exacto; la detección tras un reinicio, no.** El halt lo dispara el breaker real
   del engine, evaluado en cada barra. Después de un reinicio de referencia, el wrapper evalúa en
   los puntos de decisión, lo que es una **cota inferior**: detiene tarde, nunca temprano. Está
   documentado en el código.
3. **No se probó el ratchet.** G nunca encadenó reinicios en esta evidencia, pero la evidencia no
   contiene ningún mercado que cayera 10% dos veces seguidas bajo G.
4. **Un bug propio, encontrado y corregido a mitad del milestone.** La primera versión del wrapper
   calculaba el drawdown con su propio máximo, muestreado sólo cuando la estrategia pedía operar,
   mientras el engine lo calcula en cada barra. Resultado: XRP con 10.14% de drawdown y **cero
   halts** bajo un límite del 10% — un breaker que no se disparaba nunca. Los resultados de E/F/G
   de esa corrida se **descartaron por completo**. Hay un test de regresión que falla si una corrida
   sufre un drawdown por encima del umbral sin registrar ningún halt.

## 9. Tests, gate y reproducibilidad

* **36 tests nuevos**: 18 del motor de recuperación (incluido uno que prueba que la regla
  permanente reproduce el wrapper de M14 **bit por bit**, así que M14, M15 y M16 siguen válidos) y
  18 del protocolo (umbral y cooldown idénticos entre políticas, identidad propia por política, la
  rejilla de comisiones, y las cuatro puertas de decisión).
* **Gate completo verde** y `mypy .` en su línea base.
* **60 jobs, 0 fallos.** Ledgers verificados.

## 10. GO / NO-GO para cambiar Risk V2 productivo

**NO-GO por ahora**, y no porque G sea mala: porque la evidencia todavía no soporta la decisión.

* **Lo que sí está probado:** el latch permanente es defectuoso. Apaga un mercado durante años por
  un rebase de 14 puntos básicos, y un cambio mínimo de costes le cambia el resultado de −3% a
  +50%. Eso no debe ir a producción tal cual, y **C queda descartada como política objetivo**.
* **Lo que no está probado:** que G sea la regla correcta. Un episodio en un mercado no justifica
  cambiar el riesgo de producción.
* **Además:** Risk V2 productivo (A) no es sólo C con otro umbral — incluye el latch permanente por
  racha de 5 pérdidas, que es el que bloquea el 48–90% de la historia. Cambiar el drawdown sin
  tocar ese latch de racha dejaría el problema mayor intacto.

**Lo que haría falta antes de un GO**, en orden: más episodios de drawdown (más mercados, o
timeframes más rápidos donde el breaker se dispare de verdad), una prueba explícita del ratchet de
G, y el mismo tratamiento de recuperación para el latch por racha de A.
