# M41 — Síntesis M13–M40 y selección de la siguiente hipótesis

**Sin backtests nuevos.** Todo lo de abajo sale de evidencia ya en disco. Risk V2 no se tocó,
VPS y sesiones paper intactos.

**Veredicto: GO estrecho.** Una sola línea más, elegida porque su refutación **no cuesta tiempo
de motor** — se puede matar o confirmar con máscaras ya cacheadas. Si cae, se para el programa de
research en lugar de abrir otra familia.

---

## 1. Mapa M13–M40

### Mono-activo, 1D (M29 — medianas de 4 activos)

| familia | CAGR | DD | Calmar | PF | trades | expo | OOS | ×2 | activos+ | motivo principal de fallo |
|---|---|---|---|---|---|---|---|---|---|---|
| trend | 2.39% | 4.70% | 0.47 | 1.49 | 202 | 38.6% | +0.92% | 1.88% | 1/4 | calmar, concentración anual |
| **breakout** | 1.97% | **2.90%** | **0.58** | **2.09** | 90 | 16.1% | −0.46% | 1.69% | 1/4 | **OOS negativo ×3** |
| momentum | 2.11% | 8.90% | 0.28 | 1.35 | 278 | 44.9% | +0.74% | 1.41% | 0/4 | calmar |
| regime_trend | 0.85% | 3.28% | 0.24 | 1.88 | 47 | 7.6% | −1.18% | 0.75% | — | calmar |
| mean_reversion | −0.44% | 6.22% | −0.07 | 0.76 | 46 | 8.0% | −0.44% | −0.55% | 0/4 | negativo |
| vol_filtered | 1.63% | 8.95% | 0.21 | 1.28 | 248 | 41.9% | +2.58% | 1.07% | — | calmar |
| regime_switch | 0.53% | 6.60% | 0.08 | 1.22 | 84 | 13.7% | −3.17% | 0.34% | — | calmar, OOS |

### Mono-activo, 4H (M29, M22, M23)

| familia | CAGR | DD | Calmar | ×2 coste | activos+ | veredicto |
|---|---|---|---|---|---|---|
| trend | 0.62 – 4.34% | 13.5 – 19.1% | 0.03 – 0.26 | **negativo en los 4** | 0/4 | **REJECT** |
| breakout (B2) | 0.21 – 5.38% | 5.4 – 14.6% | 0.01 – 0.99 | pasa en 3/4 | 2/4 | WEAK |
| **B2 / BTC (M23)** | +1.7%/año OOS | — | — | retiene 63% | — | **PAPER CANDIDATE** (único del proyecto) |
| regime_trend / BTC | +34.32% total | 4.41% | — | — | — | incumbente, nunca pasó el protocolo de M23 |

### Rotación cross-sectional, 1D (M30 → M32)

| regla | CAGR | **DD** | Calmar | PF | OOS | activos+ | fallo |
|---|---|---|---|---|---|---|---|
| RS1 | +53.95% | **94.80%** | 0.57 | 1.10 | −0.61% | 4/6 | DD, OOS |
| CS1 | +54.46% | **83.85%** | 0.65 | 1.25 | +10.98% | 4/6 | DD |
| **CS2** | +65.39% | **78.09%** | 0.84 | 1.93 | +127.65% | **6/6** | DD |
| **RF1** | **+69.02%** | **75.02%** | **0.92** | 1.42 | +29.93% | 4/6 | DD, concentración |

M31: el drawdown **sí** se puede controlar con exposición, y cuesta **~70% del CAGR**; 1 variante
pasa en 1D, 1 en 4H, **ninguna en ambos**. M32: con universo point-in-time corregido, **NO-GO** —
CS2 falla sensibilidad, RF1 falla 5 de 7 condiciones.

### Compresión de volatilidad, 1D (M33)

Seis variantes, mejor caso **1/6 activos**. CAGR 0.04 – 0.85%, DD 3.96 – 5.29%. Todas cerradas.

### Carteras combinadas, 4H (M34 → M38)

| | CAGR | DD | Calmar | nota |
|---|---|---|---|---|
| B2 solo | — | 47.45% | — | |
| G1 solo | — | 35.13% | — | |
| **B2+G1 señales (M34)** | — | **31.87%** | **0.92** | DD **menor que cualquiera** de sus partes; correlación de DD **0.08** |
| M35 breadth 12, señales | +35.21% | **25.50%** | **1.38** | pasa todo |
| **M36 con Risk V2** | **+9.01%** | **46.56%** | **0.19** | **NO-GO** — Risk V2 destruye el edge |
| M38 V2 universo completo | +7.53% | 26.38% | 0.29 | **frágil a costes**: ×2 = −0.45% |
| M38 V3 candidato | +20.58% | 40.53% | 0.51 | **NO-GO** por DD |

### Familias risk-native, 1D (M39, Risk V2 vivo desde la primera medición)

| | CAGR | DD | Calmar | ratio señal | fallo |
|---|---|---|---|---|---|
| pullback PB1 | −7.17% | 51.95% | −0.14 | **−0.59** | sin edge |
| retest RT1 | +22.65% | 52.76% | 0.43 | **1.14** | DD |
| confirmed MC1 | **+26.54%** | **36.15%** | **0.73** | 0.76 | DD (por 1.15 pts) |

### Mecanismos de riesgo (M37, M38, M40)

| hallazgo | medida |
|---|---|
| el complejo de stops cierra casi todo | **92.7%** de las posiciones (4H) |
| sus tres partes son un solo nivel con tres nombres | break-even 34.1% / trailing 33.0% / hard 32.9% |
| breakers de drawdown | **nunca dispararon** (variante G byte-idéntica) |
| time stop a 1D | 51–56% de los cierres, pero quitarlo recupera solo 8.7–16.5% de CAGR |
| un stop protector puede **empeorar** el drawdown | M38: expo 63.0% vs 65.6% y DD 40.53% vs 31.87% |

---

## 2. Conclusiones estructurales

### 2.1 Lo que aparece en los mejores resultados

1. **Poca operación.** Breakout es la mejor familia mono-activo a 1D con **90 trades y 16% de
   exposición**, contra los 278 trades de momentum. A 4H, breakout sobrevive ×2 con 359–435
   trades y trend muere con 608–850. **Menos operaciones, más edge por operación** — es el
   patrón más repetido del proyecto.
2. **Diversificación, no selección.** El único resultado que pasó todas las puertas sobre señales
   (M35 breadth 12: Calmar 1.38, DD 25.50%) viene de **ampliar**, no de elegir mejor. Y M34
   midió que combinar dos sleeves con correlación de drawdown **0.08** da un DD **menor que
   cualquiera de los dos por separado**.
3. **La selección cross-sectional es donde está el retorno bruto.** Rotación da 46–69% de CAGR
   contra 0.5–2.4% mono-activo. Dos órdenes de magnitud.

### 2.2 Lo que aparece repetidamente en los fallos

1. **Drawdown. Es el asesino número uno**, y por mucho. Mata las seis rotaciones (75–95%), las
   seis variantes de M39 (36–53%), M36 (46.56%) y M38 (40.53%). El tope de 35% viene de M22,
   calibrado para una estrategia mono-activo gestionada por stop, y rechaza también buy&hold BTC
   (83%) y la cesta equiponderada (85%).
2. **Fragilidad a costes.** Trend a 4H se vuelve negativo a ×2 en los cuatro activos. Risk V2
   sobre el universo completo se vuelve negativo a ×2 (−0.45%). Cuando el turnover es alto, el
   coste se come el edge entero.
3. **Inconsistencia entre activos.** Breakout funciona en BTC y BNB y muere en SOL
   (concentración 5.37). El mismo retrato en M23, M24 y M29.
4. **Magnitud trivial a 1D.** Las siete familias mono-activo aterrizan en 0.5–2.4% de CAGR con
   DD de 2.4–9%. No es un problema de afinado: es el techo de la escala.
5. **Los mecanismos de riesgo se sustituyen entre sí.** Tres milestones consecutivas (M37, M38,
   M40): quitar un mecanismo traslada su trabajo a otro en vez de eliminarlo.

### 2.3 Qué viene de la estrategia y qué viene de Risk

| problema | origen | evidencia |
|---|---|---|
| CAGR de 1–2% a 1D | **estrategia** | M29: siete familias, mismo techo |
| DD de 75–95% en rotación | **estrategia** | M30: sin Risk en el bucle |
| pérdida del 69% del CAGR a 4H | **Risk** | M36/M37: el stop cierra 92.7% |
| DD que no baja del 36% en M39 | **estrategia** | el ratio señal/Risk es 0.76–1.14: Risk conserva |
| time stop | **Risk, pero secundario** | M40: recupera 8.7–16.5% |

**La conclusión incómoda y la más importante del mapa:** a **1D** Risk V2 es benigno —cuatro de
seis variantes de M39 conservan ≥61% del edge de señal y una lo mejora— pero los retornos son
triviales. A **4H** los retornos son reales (señales: 29–35% de CAGR con DD 25–32%) pero Risk V2
los destruye. **No hay un timeframe donde coincidan retorno real y compatibilidad con riesgo**, y
eso no es un fallo de ninguna familia: es la forma del espacio explorado.

---

## 3. Espacios agotados

| espacio | evidencia | estado |
|---|---|---|
| trend, momentum, mean-reversion, regime_* mono-activo (1D y 4H) | M13, M22, M29 — 14 configs × 7 familias × 2 TF × 4 activos | **AGOTADO** |
| compresión de volatilidad | M33 — 6 variantes, mejor 1/6 | **AGOTADO** |
| rotación cross-sectional como motor de retorno | M30, M31, M32 — NO-GO con universo corregido | **AGOTADO** |
| escalar exposición para controlar DD | M31 — funciona y cuesta 70% del CAGR | **AGOTADO** |
| reestructurar Risk (V3) | M37, M38 — NO-GO en universo completo | **AGOTADO** |
| el time stop como causa | M40 — secundario | **AGOTADO** |
| familias elegidas por punto de entrada | M39 — predicción refutada | **AGOTADO** |
| 1H | nunca corrido; M29 lo declinó por coste y evidencia | **no abrir** |

## 4. Espacios realmente no explorados

1. **Construcción de cartera por riesgo (risk parity / inverse-vol) en lugar de equiponderada.**
   M31 escaló la exposición *agregada* de un sleeve de rotación; M34/M35 usaron capital/riesgo
   igual por señal. **Nadie ha ponderado por volatilidad inversa entre activos.**
2. **Correlación como *input* de construcción.** M34 **observó** correlación de DD 0.08 y vio que
   bajaba el DD por debajo de ambas partes. Nunca se usó para *construir*.
3. **Exposición bidireccional (long + short).** Todo el proyecto, sin excepción, es long-only
   spot. Los fallos por "años inconsistentes" y los drawdowns se concentran en 2018 y 2022 —
   mercados bajistas largos donde una regla long-only solo puede estar plana.
4. **Ensamblar muchos edges débiles** en vez de buscar uno fuerte. M34 combinó exactamente dos.
   M29 midió cinco familias simultáneamente positivas a 1D con DD de 2.9–8.95%.
5. **Horizonte declarado ex-ante** en lugar de salida por cruce de señal o stop.

---

## 5. Tres hipótesis (máximo declarado)

### H1 — El drawdown es un problema de *construcción*, no de selección

**Qué afirma.** Ponderar las posiciones por volatilidad inversa (risk parity entre activos), en
lugar de capital igual por señal, baja el drawdown de cartera por debajo del 35% sin sacrificar
el CAGR — porque iguala la *contribución al riesgo* en vez del capital.

**Por qué es distinta de lo probado.** M31 escaló la exposición agregada (un escalar sobre todo
el libro). M34/M35/M36/M38 usaron equiponderación por señal con tope por activo. Ninguna
milestone ha variado *el reparto entre activos*. Es una dimensión ortogonal a toda familia
agotada: no cambia qué se compra, cambia cuánto de cada cosa.

**Qué evidencia la motiva.** El drawdown es el asesino número uno y la diversificación es lo
único que ha funcionado contra él sin cobrar peaje: M34 bajó el DD de 47.45%/35.13% a **31.87%**
combinando dos sleeves con correlación 0.08, y M35 lo bajó a **25.50%** con Calmar **1.38** solo
ampliando a 12 mercados a exposición constante. Las dos veces el mecanismo fue diversificación,
no selección. La ponderación equiponderada deja SOL —el activo más volátil y el que mata
breakout con concentración 5.37— con el mismo peso que BTC.

**Por qué debería convivir mejor con Risk V2.** Risk V2 dimensiona desde el stop: posición =
presupuesto de riesgo ÷ distancia del stop. El inverse-vol **reduce** la posición exactamente en
los activos de alta volatilidad, que son donde los stops se alcanzan. Debería reducir
estructuralmente los stop-outs, que M37 midió como el 92.7% de todos los cierres.

**Qué la falsaría rápido y gratis.** Re-ponderar las máscaras de posición **ya cacheadas** de M38
(60 pares, universo completo, Risk V2 y V3) por volatilidad inversa y recomputar el drawdown de
cartera. **Cero corridas de motor.** Si el DD no baja del 35%, o si baja a costa de más de un
cuarto del CAGR, H1 muere en minutos.

### H2 — Muchos edges débiles y poco correlacionados baten a uno fuerte

**Qué afirma.** Las cinco familias simultáneamente positivas a 1D (trend, breakout, momentum,
vol_filtered, regime_trend) tienen mecanismos distintos y drawdowns pequeños; combinadas, su
Calmar supera 0.50 aunque ninguna lo haga sola.

**Por qué es distinta.** M34 combinó **dos** sleeves. Nunca se han combinado cinco, ni se ha
medido su matriz de correlación.

**Qué evidencia la motiva.** M29: 2.39%/4.70%, 1.97%/2.90%, 2.11%/8.90%, 1.63%/8.95%,
0.85%/3.28%. Cinco positivas, DD máximo 8.95%. El tope de 35% tiene **espacio enorme** aquí, al
contrario que en cualquier otra línea. Y M34 demostró que la combinación baja el DD por debajo de
sus partes.

**Compatibilidad con Risk V2.** Alta y ya medida: M29 corrió la columna de Risk V2 para cada
familia, y a 1D M39 confirmó que Risk V2 conserva ≥61% del edge.

**Qué la falsaría rápido.** Correlacionar las curvas de equity 1D ya cacheadas de las cinco
familias. Con correlaciones >0.7 la diversificación no puede ayudar y muere ahí.

**Lo que hay que decir en contra, y es grave.** Sin leverage, combinar cinco edges de ~2% da
~2% de CAGR con menos DD. El Calmar mejora; **el retorno no**. Diversificar no crea retorno,
lo estabiliza. H2 tiene atractivo estadístico alto y potencial económico **bajo**.

### H3 — La mitad bajista del espacio nunca se ha medido

**Qué afirma.** Los fallos por años inconsistentes y los drawdowns se concentran en 2018 y 2022.
Una regla bidireccional puede ganar donde una long-only solo puede estar plana, y eso atacaría
simultáneamente la inconsistencia anual y el drawdown.

**Por qué es distinta.** Todo M13–M40 es long-only spot, sin excepción. Es el espacio no
explorado más grande del proyecto.

**Qué la falsaría rápido.** Medir qué fracción del drawdown máximo de cada familia ocurre en
régimen bajista declarado. Si es minoritaria, la mitad corta no arregla el problema.

**El bloqueo, y es de plataforma, no de research.** Operar corto en spot requiere margen. Las
restricciones declaradas de este proyecto son **spot, sin leverage, sin cortos**, y la plataforma
no tiene adaptador de margen. H3 no es un experimento que se pueda correr: es una **decisión de
alcance de producto** que habría que tomar antes. Se registra por completitud, no como candidata.

---

## 6. Ranking y recomendación

| | potencial económico | independencia de lo agotado | compatibilidad esperada con Risk V2 | coste computacional | |
|---|---|---|---|---|---|
| **H1** construcción por riesgo | **medio-alto** | **alta** (dimensión ortogonal) | **buena** (menos posición donde pegan los stops) | **~cero para refutar** | **1º** |
| H3 bidireccional | alto | **la más alta** | desconocida | **bloqueado** (spot, sin margen) | 2º |
| H2 ensamblar débiles | **bajo** (sin leverage no crea retorno) | media | alta | baja | 3º |

**Siguiente experimento: H1 — construcción de cartera por riesgo.**

Es la única que puntúa bien en los cuatro criterios a la vez, y la razón decisiva es la cuarta:
**su refutación no cuesta tiempo de motor.** Las máscaras de posición de M38 ya están en disco
para los 60 pares del universo completo bajo Risk V2 y bajo V3. Re-ponderar y recomputar el
drawdown es aritmética sobre datos existentes. Se puede matar H1 **antes** de comprometer una
sola corrida de `BacktestEngine`, lo que ninguna milestone desde M29 ha podido decir.

H3 es más prometedora en potencial puro y la descarto como *experimento* por una razón que no es
de research: requiere una capacidad de plataforma que no existe y que contradice las
restricciones declaradas. Si en algún momento se decide abrir margen, H3 pasa a ser la primera
candidata — y conviene que quede escrito aquí y no se pierda.

No se diseñan parámetros, no se declara umbral y no se corre nada: eso sería M42.

## 7. GO / STOP sobre continuar research

**GO, estrecho y con condición de cierre explícita.**

El mapa dice que el espacio explorado no contiene un edge desplegable: a 1D los retornos son
triviales y a 4H Risk V2 o el drawdown se los come. Siete líneas están agotadas y una está
bloqueada por alcance de plataforma. Eso justifica *una* línea más, no un programa abierto.

La condición: **H1 se refuta con datos en disco, antes de gastar motor.** Si el re-ponderado por
volatilidad inversa no baja el drawdown de cartera por debajo del 35% conservando el CAGR, se
para el programa de research y se cierra, en lugar de abrir una novena familia. Y si H1 sobrevive
esa criba gratis, entonces —y solo entonces— merece una pre-declaración y corridas de motor.

Dicho sin rodeos: esta es la última hipótesis que la evidencia sostiene. No hay una décima
familia que el mapa recomiende probar.
