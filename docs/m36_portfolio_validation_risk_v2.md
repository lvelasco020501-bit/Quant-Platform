# M36 — Validación final de la cartera combinada con Risk V2

**Veredicto: NO-GO definitivo.** La cartera B2 + regime_trend no sobrevive cuando Risk V2 decide
realmente las posiciones. Falla en siete criterios predeclarados a la vez, no en uno marginal.

Timeframe 4H. Universo point-in-time corregido de M32. Parámetros, reglas, costes, umbrales,
multiplicadores de estrés y ventana OOS: **idénticos** a M34/M35, tomados por referencia de los
mismos objetos. No se cambió nada después de ver resultados.

Base de evidencia: `var/research/m36/positions_4h.json` (60 pares del motor certificado) y
`var/research/m36/phase2_risk_v2_4h.json`. Reproducible con `scripts/m36_positions.py` y
`scripts/m36_phase2.py`.

---

## Fase 1 — breadths operativos (re-juicio, no medición nueva)

| breadth | CAGR | DD máx | Calmar | |
|---|---|---|---|---|
| 6 | +29.34% | 31.87% | 0.92 | PASS |
| 12 | +35.21% | 25.50% | 1.38 | PASS |

Reproduce M35 **exactamente**, cifra por cifra, en dos corridas independientes. Y como ya quedó
declarado en `m36.py` antes de ejecutar nada: **la fase 1 no aporta información nueva.**
Estrechar la condición a breadths 6 y 12 se hizo sabiendo que el 3 había fallado. Está apoyado en
evidencia — un universo de seis estuvo disponible en el 93.62% de las barras y uno de tres o menos
solo en el 3.13%, todo antes de 2018-03-31 — pero sigue siendo una puerta relajada después de ver
un resultado, y por eso no cuenta como confirmación independiente de nada.

## Fase 2 — posiciones del motor certificado, Risk V2 decidiendo

60 pares (2 sleeves × 30 mercados), extraídos del `BacktestEngine` con stops, estado de posición,
re-entradas, rechazo de órdenes y breakers no-latching **vivos**. Cobertura completa: 0 pares
ausentes, 0 fallos.

| breadth | CAGR | DD máx | Calmar | |
|---|---|---|---|---|
| 6 | **+9.01%** | **46.56%** | **0.19** | FAIL |
| 12 | **+10.27%** | **47.05%** | **0.22** | FAIL |

### Diferencias señal vs Risk V2 (breadth 6)

| medida | sobre señales | sobre motor certificado | relativo |
|---|---|---|---|
| CAGR | +29.34% | **+9.01%** | **0.31×** |
| DD máximo | 31.87% | **46.56%** | **1.46×** |
| Calmar | 0.92 | **0.19** | **0.21×** |
| profit factor | 1.55 | 1.08 | 0.69× |
| episodios | 1 376 | 2 696 | 1.96× |
| turnover | 270.8 | 1 315.5 | **4.86×** |
| comisiones pagadas | 18 624 | 31 592 | 1.70× |
| exposición | 65.62% | 43.14% | 0.66× |
| concentración top activo | 26.11% | **75.43%** | 2.89× |
| concentración top sleeve | 55.02% | **78.57%** | 1.43× |
| mejor año / beneficio total | 41.13% | 45.84% | 1.11× |

### Diferencias por par (60 pares)

- Ratio de tramos motor/señal: **1.25× – 11.14×**, mediana **2.77×**
  (B2 mediana 2.16×, G1 mediana 3.95×).
- Ratio de barras mantenidas: 0.12× – 0.74×, mediana **0.36×**.
- **35 de 60 pares mantuvieron posición donde la señal no la tenía** (1 932 de 68 244 barras de
  motor, 2.83%). Es la dirección que un stop cortando un tramo *no* puede explicar: confirma a
  escala de cartera lo que M34 observó en un solo par — el estado de posición es una entrada de
  estas reglas, así que un stop puede crear una entrada que la regla sin stop nunca toma.

## Estrés de costes y OOS

| breadth | CAGR ×2 coste | CAGR ×3 coste | OOS retorno | OOS DD |
|---|---|---|---|---|
| 6 | **−12.38%** | **−29.58%** | +32.70% | 23.51% |
| 12 | **−15.29%** | **−34.92%** | +17.13% | 30.43% |

El OOS sigue positivo, pero el estrés de costes se vuelve **negativo al doble de coste** en ambos
breadths. Con turnover 4.86× el de las señales, el edge ya no paga sus propias comisiones: la
cartera vive dentro de un margen de coste que no tiene.

## Concentración

En breadth 6 el **75.43%** del beneficio neto viene de un solo activo (26.11% sobre señales) y el
**78.57%** de un solo sleeve. Breadth 12 reparte más (44.23% / 58.51%) pero concentra el
**86.34% del beneficio en un único año natural**. Ninguna de las dos configuraciones pasa.

## Criterios predeclarados que fallan

`breadth_failed`, `drawdown`, `low_calmar`, `single_asset`, `single_sleeve`, `cost_fragile`,
`simulator_dependent`.

`simulator_dependent` dispara en una sola dirección por diseño: las señales pasaban y el motor
certificado no. Es exactamente el caso que la pre-declaración dijo que haría NO-GO.

## Por qué, mecánicamente

El edge medido en M34 y M35 **vivía en tramos largos sin interrumpir**. Risk V2 los corta: casi el
triple de tramos, un tercio de las barras mantenidas, 4.86× el turnover y 1.70× las comisiones. Lo
que sobrevive es una cartera que entra y sale cinco veces más, mantiene un tercio del tiempo, paga
casi el doble en comisiones, concentra el 75% de su beneficio en un solo activo y cae un 46.56%
en el camino.

No es que el edge se degrade: es que **nunca se midió sobre las posiciones que la plataforma
realmente tomaría.** M30–M35 corrieron sobre líneas temporales que ningún stop había tocado. Esta
es la primera medición sobre las reales, y es la que decide.

## Lo que esta fase NO prueba

Declarado en `m36.py` antes de medir, y sigue en pie:

- **Tamaño de posición no es fiel.** El motor corre cada par sleeve-mercado como su propia cuenta
  con el 100% del capital; la cartera impone después los pesos declarados. Una corrida por par no
  puede conocer el equity de la cartera, así que sus breakers de drawdown y sus rechazos por
  nocional mínimo ven una cuenta que las posiciones de 1/12 nunca tienen. Límite arquitectónico
  de M34, sin mover.
- **Latching liberado.** Se usa la política de referencia de M29–M33 para que las cifras sean
  comparables con las screens anteriores. El latching solo puede quitar exposición, así que su
  ausencia sesga la fase 2 hacia **más** retorno y **más** drawdown que una configuración
  desplegada. El NO-GO no depende de ese sesgo: va en la dirección favorable y aun así falla.

## Qué NO se toca

Sesiones paper del VPS, Risk productivo, producción, B2/regime_trend productivos y parámetros de
estrategia: **intactos**. Nada de esto se ejecutó en el VPS.

## Coste medido y deuda técnica (sin cambios, sigue abierta)

El `BacktestEngine` es **O(n²)** en longitud de historia. Medido en sonda de un worker:

| barras | B2 | G1 |
|---|---|---|
| 8 000 | 54.8 s | 52.7 s |
| 12 000 | 124.8 s | 117.9 s |
| 16 000 | 226.7 s | 209.4 s |
| 19 790 | 335.3 s | 320.5 s |

El ajuste cuadrático es exacto (predicho 54.8 s en 8 000 desde el punto de 19 790). Proyección
sobre los 27 mercados del breadth 6: 3.44 h de CPU. Real: **3.97 h de CPU en 1.98 h de reloj** con
2 workers, más 457 s para los 6 pares del breadth 12. Dos workers al 99% de CPU consumieron 271 MB
combinados; no hubo thrashing y no hubo que bajar a 1 worker. Sigue anotada como deuda de alto
impacto, **no optimizada**.

## Corrección aplicada durante M36

La extracción enumeraba los mercados elegibles con `size=UNIVERSE_SIZE` (6), pero la fase 2 juzga
también breadth 12, cuyo universo admite DASH, OMG y XMR — mercados que nunca entran en el top 6.
Breadth 12 se estaba juzgando con el **0.45% de sus asientos elegibles vacíos** (1 016 de 224 181,
presentes en el 5.04% de los slots). Era un agujero en la medición, no un resultado. Se corrigió
enumerando a `max(BREADTHS)` y extrayendo los 6 pares que faltaban.

Efecto real: breadth 6 quedó **idéntico** (su universo nunca contiene esos tres mercados) y
breadth 12 pasó de Calmar 0.21 a 0.22. **No cambia el veredicto** — se cerró porque publicar con un
hueco conocido es peor que gastar quince minutos, no porque pudiera mover nada.

## Errores por par

**0 de 60.** Ningún par falló. Cada par captura su propia excepción y la devuelve como dato con
razón adjunta, y la fase 2 separa un par fallido de un par vacío — un mask todo-falso es
indistinguible de un mercado que el sleeve nunca quiso, así que fundirlos dejaría que un driver
caído entrara en la cartera como una decisión silenciosa de no operar. El mecanismo quedó puesto y
probado, pero no hubo nada que registrar.

---

## Conclusión

**NO-GO definitivo para M36.** La cartera combinada no pasa a diseño de paper.

El hallazgo positivo de M34 —que B2 y regime_trend juntos dan menos drawdown que cada uno por
separado, con correlación de drawdown 0.08— **sigue siendo cierto sobre señales**. Lo que M36
establece es que ese hallazgo no sobrevive al contacto con el gestor de riesgo que la plataforma
realmente ejecuta, y que las seis milestones anteriores midieron una cartera que no es la que se
operaría.
