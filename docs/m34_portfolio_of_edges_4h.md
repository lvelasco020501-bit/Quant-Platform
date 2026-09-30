# M34 — cartera de edges existentes: **combinar SÍ ayuda, pero falla la sonda declarada → STOP**

**Estado:** FASE 4H COMPLETA · 30 mercados, 27 elegibles · 3 carteras × 6 pases · 392 s · 1.21 GB pico · **C pasa 7 de 8 condiciones y falla `fragile_to_breadth`** · 2026-09-30

Pre-declaración commiteada en `bf02a5d`, **antes** de ver un solo resultado de cartera. Las dos
señales vienen de M29 **por referencia** (`SLEEVES is CANDIDATES_M29`), así que M34 no reescribe
ni un periodo. El VPS no se tocó y producción no se modificó.

---

## 1. Diseño pre-declarado

| | valor | de dónde |
|---|---|---|
| sleeve 1 | **B2** = `breakout_trend` 40/20/400 | M29 `CANDIDATES_M29["B2"]` — la config viva en el VPS |
| sleeve 2 | **G1** = `regime_trend` 72/72/0.30 | M29 `CANDIDATES_M29["G1"]` — el benchmark de M22 |
| universo | pool de 30 de M32, top-6 point-in-time | M32 sin cambios (`UNIVERSE_SIZE`=6, ventana 72) |
| peso por señal activa | **1/(6 × sleeves)** | derivado de la amplitud de M32 |
| tope por mercado | **1/6** | derivado igual |
| resto | **caja**, sin rentar | — |
| coste | 15 bps/lado, stress ×2/×3 | fee 10 + slippage 5 de la plataforma |
| sondas de amplitud | 3 y 12 | mitad y doble, regla mecánica |

**M34 no introduce ni un número propio.** El denominador fijo es lo que permite que la cartera
esté parcialmente en caja: dividir por el número de señales *activas* la pondría 100% invertida
en cuanto una sola señal se encendiera.

---

## 2. La brecha declarada, medida antes de cualquier resultado

Una cartera con su propio asignador **no puede correr en `BacktestEngine`**: ese motor es un
backtester de una cuenta y una estrategia, que dimensiona cada posición desde un stop y un
presupuesto de riesgo asumiendo que la cuenta entera es suya. Escalar una corrida de capital
completo a 1/12 no es la misma cartera.

Así que los timelines vienen de un driver que corre **los mismos objetos de estrategia
congelados** por **el mismo pipeline de features de producción**, y `scripts/m34_verify.py` lo
sujeta a la lista de trades del motor en doce series:

| | |
|---|---|
| trades del motor dentro de un stretch del driver | **321 de 322** — coinciden en *cuándo hablan* las reglas |
| rotación del motor vs las señales solas | **×1.5 a ×10.5**, mediana ≈3 |
| la excepción | B2/ETH abre a las 2017-11-29 20:00, una barra después de cerrar el stretch |

Esa excepción es instructiva, no ruido: el motor venía **stopeado**, así que la estrategia vio
posición plana y evaluó su rama de *entrada* donde el driver, aún largo, evaluaba la de *salida*.
**El estado de posición es una entrada de estas reglas**, así que un stop no sólo parte un
stretch — puede crear una entrada que la regla sin stop nunca tomaría.

**Por tanto estas son carteras de la lógica de entrada/salida de las reglas, con el stop de Risk
V2 NO modelado.** Todas las ramas y todos los benchmarks usan la misma maquinaria, así que la
pregunta del milestone —¿combinar bate a cada una sola?— se responde sobre una base consistente.
**Las cifras absolutas de CAGR y DD no son las de las estrategias desplegadas y no se ofrecen como
tales.** La reproducción en el motor certificado es tu propia siguiente puerta declarada.

La primera versión del script de verificación comparaba timestamps de entrada uno a uno y falló.
Hizo bien: la comparación era incorrecta, no el driver. Se reemplazó por contención, que es la
afirmación que puede ser verdadera.

---

## 3. Resultados

| cartera | CAGR | **DD** | **Calmar** | PF | ×2 | ×3 | OOS | año | activo | sleeve | expo | veredicto |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **A** B2 solo | +25.44% | **47.45%** | 0.54 | 1.34 | +17.61% | +10.26% | +78.49% | 0.34 | 0.22 | — | 55.6% | ❌ dd, sonda |
| **B** regime_trend solo | +31.33% | **35.13%** | 0.89 | 2.03 | +28.13% | +25.00% | +71.73% | **0.50** | 0.31 | — | 45.1% | ❌ dd, año, sonda |
| **C** B2 + regime_trend | +29.34% | **31.87%** | **0.92** | 1.55 | +23.66% | +18.22% | +77.03% | 0.41 | 0.26 | 0.55 | 65.6% | ❌ **sólo sonda** |

**C es mejor que cualquiera de las dos solas en las dos dimensiones que importan:**

| | DD | Calmar |
|---|---|---|
| B2 solo | 47.45% | 0.54 |
| regime_trend solo | 35.13% | 0.89 |
| **C combinada** | **31.87%** (−15.6 pp vs A, −3.3 pp vs B) | **0.92** (mejor que ambas) |

Y **C pasa 7 de las 8 condiciones**: drawdown ✅, Calmar ✅, OOS ✅, stress ×2 y ×3 ✅, un solo
año ✅ (0.41), un solo activo ✅ (0.26), **una sola estrategia ✅ (0.55)**. Falla sólo la sonda de
amplitud.

B falla el drawdown por **0.13 pp** (35.13% contra 35.00%). Tercera vez en este proyecto que un
candidato se pierde por una centésima, y no se toca el umbral.

---

## 4. Diversificación: es real y es medible

| | valor |
|---|---|
| correlación de retornos entre sleeves | **0.66** — parcialmente descorrelacionadas, como decía la hipótesis |
| **correlación de drawdowns** | **0.08** — prácticamente independientes |
| barras con solape de señal | 10 631 de 19 791 |
| contribución por sleeve (C) | B2 **41 508** · G1 **50 768** → cuota del mejor 0.55 |

**Ese 0.08 es el hallazgo.** Los drawdowns de las dos reglas casi no se solapan, que es
exactamente la condición bajo la cual combinarlas reduce el riesgo de cartera sin sacrificar
retorno — y es lo que se observa: DD de 47% y 35% por separado, **31.87%** juntas.

Ambos sleeves aportan material y comparablemente (45%/55%). No es una cartera donde una regla
lleva a la otra de pasajera.

> **Una cifra que calculé mal y no presento como hallazgo:** "barras con ambas en drawdown" sale
> 96%, pero cuenta cualquier drawdown > 0, y una curva de equity está por debajo de su máximo casi
> siempre. La medida no informa; la que informa es la correlación de 0.08.

---

## 5. Benchmarks

| | CAGR | DD | Calmar |
|---|---|---|---|
| **C combinada** | +29.34% | **31.87%** | **0.92** |
| regime_trend solo en BTC | +34.85% | 39.31% | 0.89 |
| B2 solo en BTC | +27.42% | 48.09% | 0.57 |
| cesta equiponderada | +40.82% | **91.99%** | 0.44 |
| buy & hold BTC | +36.73% | **84.11%** | 0.44 |

**C dobla el Calmar de lo pasivo** (0.92 vs 0.44) con un tercio de su drawdown. Y bate a las dos
reglas corridas solas sobre BTC. La pregunta del milestone —¿varios edges modestos y parcialmente
descorrelacionados baten a cada uno aislado?— tiene, en riesgo/retorno, un **sí** claro.

---

## 6. Sensibilidad — la única puerta que falla, y por qué

> **CORRECCIÓN (M35).** La explicación de abajo —"libros el doble de grandes"— es demasiado
> generosa con C. Medido correctamente, el capital **desplegado medio** sube sólo un **8%** de
> breadth 6 a breadth 3 (21.7% → 23.4%), no el doble; lo que se dobla es el peso *por señal*
> (1/6 vs 1/12). Con la exposición agregada mantenida constante, breadth 3 **sigue fallando**
> (DD 42.68% frente a 46.84%): de los ~15 pp de diferencia, sólo ~4 pp eran tamaño y **~11 pp son
> concentración**. La sonda mezclaba dos variables —eso era cierto— pero el efecto dominante era
> el que de verdad medía. Ver `docs/m35_portfolio_validation.md` §2.

| cartera | amplitud 3 | amplitud 6 (declarada) | amplitud 12 |
|---|---|---|---|
| A | dd **56.68%**, calmar 0.51 ❌ | dd 47.45%, calmar 0.54 | dd **39.37%**, calmar 0.80 ❌ |
| B | dd **38.29%**, calmar 0.72 ❌ | dd 35.13%, calmar 0.89 | dd 23.79%, calmar 1.13 ✅ |
| **C** | dd **46.84%**, calmar 0.63 ❌ | dd 31.87%, calmar 0.92 | dd **25.99%**, calmar **1.15** ✅ |

**El fallo es de un solo lado.** Ensanchar a 12 *mejora* C en todo (Calmar 0.92 → 1.15, DD 31.87%
→ 25.99%). Estrecharla a 3 la rompe.

**Y hay un defecto en mi propia sonda que debo declarar.** `NEIGHBOUR_UNIVERSE_SIZES` mueve dos
cosas a la vez: cuántos mercados son elegibles **y** el peso por señal (1/(3×2)=1/6 frente a
1/(12×2)=1/24). A amplitud 3 la cartera lleva libros **el doble de grandes**, así que su drawdown
mayor es en buena parte aritmética de tamaño, no fragilidad del edge.

Eso lo hace una prueba peor de lo que pretendía. **No la cambio.** Estaba declarada y commiteada
antes de correr, y ajustarla ahora —cuando cambiar la sonda cambiaría el veredicto de la única
cartera que casi pasa— es exactamente lo que tu instrucción prohíbe. Queda registrado como lo que
es: un defecto de diseño mío, con la consecuencia que tiene.

---

## 7. Concentración

| | C |
|---|---|
| mercados tocados | 24 de 27 elegibles |
| cuota del mejor mercado | **0.26** (tope 0.60) |
| cuota del mejor sleeve | **0.55** (tope 0.60) |
| cuota del mejor año | **0.41** (tope 0.50) |
| episodios / turnover / comisiones | 1 376 / 271 / 18 624 |

Mejores: XRP +24 091, DOGE +18 299, BTC +15 709, SOL +15 118, ETH +14 595.
Peores: AVAX −2 745, MATIC −2 496, TRX −2 272, VET −1 794, LTC −1 636.

Año a año: +18%, −9%, +13%, +74%, +107%, −14%, +53%, **+66%**, +6%, +1%. Nueve de diez años sin
desastre y **2024 fuerte**, que es lo contrario de lo que pasaba en M31/M32, donde el edge se
degradaba en los años recientes. OOS (2024→) **+77.03%**.

---

## 8. Veredicto

Por la puerta que **yo** declaré y commiteé en `bf02a5d`, que incluye `fragile_to_breadth`:

| cartera | pasa |
|---|---|
| A — B2 multi-asset | ❌ drawdown, sonda |
| B — regime_trend multi-asset | ❌ drawdown (por 0.13 pp), año, sonda |
| **C — B2 + regime_trend** | ❌ **sólo la sonda de amplitud** |

# STOP — M34 no pasa como está declarado

No se ejecuta la reproducción en el motor certificado (estaba condicionada a que algo pasara), no
se ajusta ningún peso, ningún umbral y ninguna sonda. Ningún PAPER CANDIDATE.

### Pero el hallazgo sustantivo es positivo, y es el primero de este tipo

Hay que decir las dos cosas y no sólo la conveniente:

1. **Combinar funcionó.** C tiene menor drawdown que **ambas** reglas por separado y el mejor
   Calmar de todo el milestone, dobla el Calmar de lo pasivo, pasa 7 de 8 condiciones incluidas
   concentración por año, por activo **y por estrategia**, y su OOS es +77%.
2. **La correlación de drawdowns de 0.08 es el mecanismo**, medido y no supuesto. Las dos reglas
   sufren en momentos distintos, que es la condición exacta bajo la que una cartera gana.
3. **Lo único que falla es una sonda mía mal diseñada**, que confunde amplitud con tamaño de
   posición. No la arreglo después de ver el resultado.

**Mi recomendación**, que es tuya para decidir: esta vía **no está agotada como lo estaban
rotation (M32) o compresión (M33)** — esas fallaron por el edge, ésta falla por mi instrumento.
Lo que la resolvería es una pre-declaración **nueva** con (a) una sonda de amplitud que mantenga
el tamaño de posición constante, y (b) la reproducción en el motor certificado, que es la única
forma de saber qué hacen estos números con el stop de Risk V2 puesto. Ninguna de las dos es una
búsqueda de parámetros.

---

## 9. Limitaciones

1. **El stop de Risk V2 no está modelado**, y es material: el motor rota ×1.5 a ×10.5 más. Las
   cifras absolutas no son las de las estrategias desplegadas. Ver §2.
2. **Mi sonda de sensibilidad confunde amplitud con tamaño.** Ver §6.
3. **La caja no renta.** El 34% del capital sin invertir no gana nada; subestima las tres carteras.
4. **Sin modelo de liquidez ni impacto**, aunque el tope de 1/6 por mercado limita el tamaño por
   posición mucho más que en M30–M32.
5. **Sin walk-forward**; sólo OOS por corte de fecha, como en M31–M33.
6. **El pool sigue siendo un acto de 2026** en su composición, aunque la elegibilidad por fecha sí
   es point-in-time. Es la limitación que M32 ya declaró y que no se resuelve aquí.
7. **Los benchmarks pasivos se miden con esta misma maquinaria**, así que su comparación con C es
   consistente, pero sus drawdowns del 84–92% son los de estar siempre invertido: no son un
   candidato, son una referencia.

---

## 10. Reproducir

```bash
uv run python scripts/m34_verify.py --bars 2500   # driver vs motor certificado, 12 series
uv run python scripts/m34_screen.py               # 3 carteras, 30 mercados, ~392 s, 1.21 GB
```

Evidencia en `var/research/m34/verification.json` y `portfolio_4h.json`.
