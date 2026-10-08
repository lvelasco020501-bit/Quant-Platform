# M39 — Búsqueda de edge risk-native

**Veredicto: NO-GO. Ninguna familia pasa 1D. Se para aquí, según la declaración.**

Y una predicción predeclarada **falló**: la familia que declaré como control —la que debía
conservar *menos* edge bajo Risk V2— resultó la mejor de las tres. El razonamiento que eligió las
familias de M39 era equivocado, y abajo está por qué.

Research only. Risk V2 **no se modificó** —ésa es la premisa de la milestone—. No se diseñó
Risk V3/V4, no se optimizó ningún stop, y no se tocaron VPS, sesiones paper, Risk productivo,
execution productivo ni parámetros de estrategia.

---

## Fase 0 — bugfix separado

Corregido antes del research y en su propio commit: `max_stop_distance_bps` ahora es **inclusivo
con tolerancia de exactamente un tick**, el mínimo sigue estricto. 6 tests de regresión y
equivalencia demostrada sobre 12 parejas reales (12 idénticas, 0 distintas, en dos corridas
independientes). Detalle completo en `docs/issue_max_stop_distance_bps.md`.

## Diseño

Cada variante corre desde su primera medición por **Market → Strategy → Risk V2 → Execution →
Portfolio**, sobre los 6 mercados, 1D, 3 301 barras (2017-09-01 → 2026-09-14). Las posiciones que
el motor realmente ocupó se ensamblan en una cartera equiponderada; la base de señal del mismo
regla se ensambla igual y sirve **solo como diagnóstico**.

Risk V2 a 1D por la conversión de M15: **stop 1470 bps**, take profit 2939, trailing 1470,
**time stop 7 barras**, breakers desplegados, política de referencia (sin latch).

Parámetros de la escala 1D de M33 (24/168/20/10); segundas variantes con una ventana duplicada
por la convención de M22. Mismos parámetros en todos los activos. Sin grid search.

## Resultados bajo Risk V2

| VAR | familia | CAGR | DD | Calmar | PF | trades | turnover | fees | forced | re-ent | held% | OOS | ×2 | ×3 | act+ | top act | año1 | años+ | señal | **ratio** |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| PB1 | pullback | −7.17% | 51.95% | −0.14 | 0.87 | 758 | 262.4 | 2 773 | 656 | 618 | 52.7% | −8.37% | −11.12% | −14.91% | 1 | — | — | 10% | +12.21% | **−0.59** |
| PB2 | pullback | −0.38% | 40.72% | −0.01 | 0.99 | 594 | 205.6 | 2 915 | 506 | 467 | 47.9% | +16.06% | −3.72% | −6.95% | 3 | — | — | 50% | +15.67% | **−0.02** |
| RT1 | retest | +22.65% | 52.76% | 0.43 | 1.25 | 895 | 311.4 | 19 761 | 777 | 666 | 61.6% | +30.28% | +16.47% | +10.60% | 5 | 52.4% | 59.6% | 70% | +19.86% | **1.14** |
| RT2 | retest | +21.29% | 50.94% | 0.42 | 1.27 | 879 | 305.7 | 16 127 | 775 | 718 | 65.2% | +23.54% | +15.29% | +9.59% | 6 | 42.4% | 46.4% | 60% | +28.06% | **0.76** |
| **MC1** | **confirmed (control)** | **+26.54%** | **36.15%** | **0.73** | 1.31 | 1 086 | 374.4 | 26 144 | 723 | 716 | 74.5% | +29.16% | +18.92% | +11.75% | 6 | 44.2% | 47.8% | 40% | +34.79% | **0.76** |
| MC2 | confirmed | +20.88% | 38.12% | 0.55 | 1.18 | 1 089 | 377.0 | 23 953 | 877 | 840 | 77.3% | −14.00% | +13.55% | +6.66% | 5 | 36.1% | 75.1% | 50% | +34.45% | **0.61** |

### Puertas falladas

| VAR | puertas |
|---|---|
| PB1 | negativo, **drawdown**, calmar, PF, OOS negativo, frágil a costes, pocos activos positivos, años inconsistentes, risk destruye el edge |
| PB2 | negativo, **drawdown**, calmar, PF, frágil a costes, años inconsistentes, risk destruye el edge |
| RT1 | **drawdown**, calmar, concentración en un año |
| RT2 | **drawdown**, calmar |
| MC1 | **drawdown**, años inconsistentes |
| MC2 | **drawdown**, OOS negativo, concentración en un año, años inconsistentes |

**Las seis fallan drawdown** (36.15% – 52.76% contra un tope de 35%). **Shortlist: vacía.**

MC1 falla por **1.15 puntos** de drawdown, y además por consistencia anual (40% de años positivos
contra un mínimo de 60%). No se rescata: el tope no se mueve.

## La predicción falló, y eso es el hallazgo

Declaré en código, antes de medir, `PREDICTED_WORST_COMPATIBILITY = CONFIRMED`: la familia que
entra extendida debía conservar menos edge bajo Risk V2, porque el stop quedaría donde un
retroceso ordinario lo alcanza.

**Resultado:** el peor ratio es **PB1 (pullback) con −0.59**. El control no solo no fue el peor:
fue el **mejor en términos absolutos** (+26.54%, el único drawdown por debajo del 40%, el Calmar
más alto) y empató el mejor ratio de las otras familias.

```
worst compatibility ratio: PB1 (pullback) at -0.59
predicted worst family: confirmed -> prediction FAILED
```

### Por qué falló: a 1D el mecanismo dominante no es el stop, es el time stop

| VAR | protective stop | **time stop** | take profit |
|---|---|---|---|
| PB1 | 271 | **362** | 23 |
| RT1 | 294 | **437** | 46 |
| MC1 | 294 | **371** | 58 |

A 1D, Risk V2 cierra **más posiciones por límite de tiempo (7 barras = 7 días) que por stop de
precio**. El time stop es **indiferente a dónde se entró**: cierra a los 7 días pase lo que pase.
Toda la hipótesis de M39 era sobre *distancia al stop desde el precio de entrada*, que a 1D casi
no se ejerce — el stop está a **1470 bps (14.7%)**, no a los 600 bps de 4H, así que queda lejos
de cualquier entrada.

Dicho de otro modo: elegí las familias por una propiedad que a 1D apenas opera.

### Y la familia pullback no fue incompatible: fue mala

El ratio negativo de PB1 no dice "Risk V2 destruyó un edge". Dice que **no había edge**: la base
de señal de PB1 da +12.21% mientras la gestionada da −7.17%, con **1 de 6 activos positivos** y
10% de años positivos. Comprar debilidad dentro de tendencia con un filtro de 168 días es comprar
cuchillos cayendo: el filtro es lento y la regla sigue entrando en caídas.

Peor para la hipótesis: la familia pullback fue **más** castigada por riesgo, no menos —
86.5% de sus cierres fueron acciones de riesgo contra **66.6%** del control. Lo contrario exacto
de lo predicho.

## Compatibilidad señal vs Risk V2

| VAR | señal | Risk V2 | ratio |
|---|---|---|---|
| PB1 | +12.21% | −7.17% | −0.59 |
| PB2 | +15.67% | −0.38% | −0.02 |
| RT1 | +19.86% | +22.65% | **1.14** |
| RT2 | +28.06% | +21.29% | 0.76 |
| MC1 | +34.79% | +26.54% | 0.76 |
| MC2 | +34.45% | +20.88% | 0.61 |

Lo que **sí** se consigue y conviene registrar: **cuatro de seis variantes conservan ≥61% de su
edge de señal**, y RT1 lo *mejora* (1.14). Eso es un contraste real con M36, donde la cartera
combinada conservó el 31%. Medir con Risk V2 desde el principio no es imposible — estas reglas
son razonablemente compatibles con Risk V2. **Lo que les falta es edge suficiente, no
compatibilidad.**

Es un resultado más limpio que el de M36–M38: allí el problema era el riesgo; aquí el riesgo se
porta razonablemente y lo que no da la talla es la estrategia.

## Concentración

RT1 concentra el **59.6% del beneficio en un año** (falla) y BTC le resta −5 221. MC1 reparte
mejor por activo (6 de 6 positivos, top 44.2%) pero solo el **40% de años** cierran en positivo.
PB1 tiene **1 de 6** activos positivos.

## Reproducibilidad

- `src/quantplatform/research/m39.py` — declaración, puertas, ratio.
- `src/quantplatform/research/m39_definitions.py` — definiciones (espejo de M33).
- `scripts/m39_screen.py` — la criba; ~9 s por corrida de motor a 1D, 36 corridas.
- `scripts/m39_bugfix_equivalence.py` — equivalencia del bugfix.
- Evidencia: `var/research/m39/screen_1d.json`.

## Errores por par

**0 de 36.** Ninguna corrida falló.

---

## Veredicto

**NO-GO. Ninguna familia pasa 1D; no se avanza a 4H y no se corre 1H.**

No se ajusta ningún threshold, no se prueba otra familia, no se cambia ningún parámetro después
de ver los resultados y no se toca Risk V2.

**Lo que M39 deja establecido:**

1. **Mi hipótesis de mecanismo era falsa, y la predicción predeclarada la refutó.** La familia
   de control fue la mejor. Elegí tres familias por la distancia entre entrada y stop, una
   propiedad que a 1D apenas opera.
2. **A 1D, Risk V2 está dominado por el time stop, no por el stop de precio** (362–437 cierres
   por tiempo contra 271–298 por stop). Ese mecanismo es indiferente al precio de entrada, que
   es justo lo que las tres familias intentaban explotar. Si se retomara esta línea, el objeto de
   estudio sería el límite de 7 días, no la distancia del stop.
3. **Risk V2 no es el obstáculo en 1D.** Cuatro de seis variantes conservan ≥61% de su edge de
   señal y una lo mejora, contra el 31% de M36. El problema de estas reglas es que su edge no
   alcanza el drawdown permitido — ninguna baja del 36.15% —, no que el riesgo lo destruya.
