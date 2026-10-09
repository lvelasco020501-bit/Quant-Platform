# M40 — ¿El time stop de 7 días cierra las estrategias 1D antes de que madure su edge?

**Veredicto fase 1: STOP. El time stop NO queda establecido como causa. La fase 2 no se abre.**

El time stop **sí** cierra posiciones prematuramente —es la mayoría de los cierres de riesgo en
ambas variantes— y quitarlo mejora todo de forma **consistente pero modesta**: muy por debajo del
cuarto relativo que esta milestone predeclaró como "claramente". Y para RT1 el OOS **empeora**.

Research only. Risk V2 productivo, VPS, sesiones paper, parámetros de estrategia, stops de
precio, breakers, take profit, trailing y sizing: **intactos**. La fase 1 cambió **un campo**
(`max_holding_bars → None`) y solo en configuración de research.

---

## De dónde viene la pregunta

M39 cribó tres familias nuevas con Risk V2 vivo desde la primera medición y las seis variantes
fallaron. Lo útil fue por qué se rompió su propia predicción: a 1D, Risk V2 cierra **más
posiciones por time stop que por stop de precio** (371 vs 294 en MC1; 437 vs 294 en RT1). M39
había elegido familias por *dónde entran*, una propiedad a la que el time stop es indiferente.
M40 es la pregunta que ese fallo levantó.

MC1 y RT1 se estudian porque M39 las midió como las dos más compatibles con Risk V2 (ratios 0.76
y 1.14, frente a ratios negativos de la familia pullback). Se toman de M39 **por referencia**.

## 1. BASE vs sin time stop

**MC1 (confirmed)**

| arm | CAGR | DD | Calmar | PF | trades | forced | t-stop | turnover | fees | held% | OOS | ×2 | ×3 | años+ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Risk V2 completo | +26.54% | 36.15% | 0.73 | 1.31 | 1 086 | 723 | 371 | 374.4 | 26 144 | 74.5% | +29.16% | +18.92% | +11.75% | 40% |
| **sin time stop** | **+30.92%** | 37.08% | **0.83** | 1.44 | 848 | 460 | 0 | 296.8 | 23 894 | 79.0% | **+37.45%** | +24.63% | +18.64% | 50% |

**RT1 (retest)**

| arm | CAGR | DD | Calmar | PF | trades | forced | t-stop | turnover | fees | held% | OOS | ×2 | ×3 | años+ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Risk V2 completo | +22.65% | 52.76% | 0.43 | 1.25 | 895 | 777 | 437 | 311.4 | 19 761 | 61.6% | +30.28% | +16.47% | +10.60% | 70% |
| **sin time stop** | **+24.61%** | 53.39% | **0.46** | 1.25 | 679 | 504 | 0 | 242.7 | 18 073 | 75.8% | **+16.71%** | +19.69% | +14.96% | 60% |

**Ambos brazos completos reproducen M39 exactamente** (`reproduces M39`), así que las dos
milestones miden lo mismo y la atribución es válida.

## 2. Atribución exacta del time stop

| medida | MC1 | RT1 |
|---|---|---|
| cierres por time stop (de forced) | 371 de 723 = **51.3%** | 437 de 777 = **56.2%** |
| forced exits al quitarlo | 723 → 460 (**−36.4%**) | 777 → 504 (**−35.1%**) |
| re-entries | 716 → 495 (−30.9%) | 666 → 466 (−30.0%) |
| trades | 1 086 → 848 | 895 → 679 |
| turnover | 374.4 → 296.8 (**−20.7%**) | 311.4 → 242.7 (**−22.1%**) |
| fees | 26 144 → 23 894 | 19 761 → 18 073 |
| held % de lo que la señal quería | 74.5% → **79.0%** | 61.6% → **75.8%** |

**Sí reduce cierres prematuros, y de forma inequívoca.** Pero no por el número completo de sus
propios cierres, porque **los otros mecanismos absorben parte del trabajo** — el mismo patrón de
sustitución que M37 encontró en el complejo de stops:

| variante | protective stop | take profit | time stop |
|---|---|---|---|
| MC1 completo | 294 | 58 | 371 |
| MC1 sin time stop | **374 (+80)** | **86 (+28)** | 0 |
| RT1 completo | 294 | 46 | 437 |
| RT1 sin time stop | **423 (+129)** | **81 (+35)** | 0 |

Quitar el time stop no libera 371 posiciones: libera 263, porque 108 de ellas acaban saliendo por
el stop de precio o el target de todos modos.

Efecto secundario que vale registrar: quitar el time stop **baja** el turnover un 21-22% y las
fees. El límite de 7 días estaba **añadiendo churn**, no conteniéndolo — cada cierre forzado
producía una re-entrada posterior.

## 3. Impacto en CAGR / DD / Calmar, contra la regla predeclarada

| | MC1 | RT1 | umbral |
|---|---|---|---|
| ganancia relativa de CAGR | **+16.5%** | **+8.7%** | ≥ +25% |
| ganancia relativa de Calmar | **+13.6%** | **+7.4%** | ≥ +25% |
| cambio de drawdown | +0.94 pts | +0.63 pts | ≤ +5 pts ✓ |
| OOS mejora | ✓ (+29.16 → +37.45) | **✗ (+30.28 → +16.71)** | debe mejorar |
| stress ×2 y ×3 mejoran | ✓ | ✓ | deben mejorar |
| cierres prematuros se reducen | ✓ | ✓ | deben reducirse |
| **causal** | **NO** | **NO** | |

MC1 falla **solo por magnitud**: todo va en la dirección correcta, nada llega al cuarto. RT1
falla por magnitud **y** porque su OOS se deteriora casi a la mitad al mantener más tiempo.

### El punto que cierra la línea

Ambas variantes fallaron M39 **por drawdown** (36.15% y 52.76% contra un tope de 35%). Quitar el
time stop mueve el drawdown **+0.94 y +0.63 puntos — en la dirección equivocada**.

Es decir: **incluso un arreglo perfecto del time stop no habría hecho pasar ninguna de las dos.**
El límite de 7 días no es lo que separa estas reglas de un resultado aprobable; su drawdown lo
es, y el time stop no lo toca.

## 4. OOS / stress

| | OOS | ×2 | ×3 |
|---|---|---|---|
| MC1 completo | +29.16% | +18.92% | +11.75% |
| MC1 sin time stop | +37.45% | +24.63% | +18.64% |
| RT1 completo | +30.28% | +16.47% | +10.60% |
| RT1 sin time stop | **+16.71%** | +19.69% | +14.96% |

El estrés de costes mejora en los dos casos —lógico, con 21% menos de turnover—. El OOS mejora en
MC1 y **se hunde en RT1**: mantener más tiempo le funcionó en la muestra y no fuera de ella.

## 5. Veredicto

**STOP. No se abre la fase 2.**

No se prueba ningún límite nuevo —ni 14, ni 21, ni 30 días—, no se ajusta el umbral del cuarto,
no se rescata MC1 por haber quedado cerca, y Risk V2 no se toca.

Un test de la declaración escanea el espacio de nombres de `m40.py` buscando 14/21/30 para que
ninguna duración pueda colarse antes de que la causalidad quede demostrada. Sigue vacío.

**Lo que M40 deja establecido:**

1. **El time stop es una causa real pero secundaria.** Provoca el 51-56% de todos los cierres de
   riesgo y quitarlo recupera solo un 8.7-16.5% relativo de CAGR. Es mucho ruido y poco edge.
2. **Estaba añadiendo churn, no conteniéndolo**: −21% de turnover y −31% de re-entries al
   quitarlo. Un límite de tiempo que fuerza salidas genera las reentradas que luego paga.
3. **No explica el fallo de M39.** Ambas variantes fallaron por drawdown y el time stop lo mueve
   menos de un punto. La hipótesis de que el límite de 7 días estaba cerrando edge antes de
   madurar es, a esta escala, **falsa**.
4. **Los mecanismos de Risk V2 se sustituyen entre sí.** 108 de los 371/437 cierres por tiempo
   simplemente reaparecen como stop o target. Tercera milestone consecutiva (M37, M38, M40) en
   la que quitar un mecanismo de riesgo traslada su trabajo a otro en vez de eliminarlo.

## Reproducibilidad

- `src/quantplatform/research/m40.py` — declaración, condiciones causales, umbrales.
- `scripts/m40_timestop.py` — la ablación; 24 corridas de motor, verifica que el brazo completo
  reproduce M39 antes de atribuir nada.
- Evidencia: `var/research/m40/timestop_ablation_1d.json`.
- Errores por par: **0 de 24**.
