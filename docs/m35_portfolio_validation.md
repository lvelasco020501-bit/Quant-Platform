# M35 — sonda corregida: la ventaja **no** depende de tener exactamente 6, pero falla mi condición de paso → **NO-GO**

**Estado:** FASE 1 COMPLETA · sonda corregida a exposición agregada constante · **breadth 6 reproduce C exactamente** · **breadth 3 falla, breadth 12 es mejor que 6** · **FASE 2 NO EJECUTADA** · 2026-09-30

---

## 0. Una advertencia sobre el estatus de la pre-declaración

`research/m35.py` se **escribió** antes de correr la fase 1 y **no se ha modificado** desde
entonces, pero se **commiteó después**. En M29–M34 la declaración se commiteaba antes de ver un
solo resultado; aquí no. Su contenido —los tres breadths, la normalización, la puerta y la
condición de paso— es exactamente el que había antes de la corrida, pero el estatus es más débil
y hay que decirlo en lugar de presentarlo como equivalente.

---

## 1. La sonda corregida

**El problema de M34:** la sonda movía a la vez el número de activos y el tamaño de posición.

**La corrección:** en cada barra, la corrida vecina lleva **el mismo peso desplegado que la cartera
declarada llevaba en esa barra**, repartido a partes iguales entre las señales que ese universo
admita. Capital total, exposición agregada y por tanto riesgo total son idénticos por
construcción; lo único que cambia es **en cuántas posiciones se sienta el mismo dinero**.

No introduce ninguna constante: la senda de exposición se **toma** de la cartera declarada en vez
de elegirse. Y el tope por activo deja de jugar: la normalización sobrescribe cada peso con una
parte igual de la exposición de referencia, así que la sonda es libre de tope **por construcción**
y no por otro número declarado.

**Comprobación que la valida:** a breadth 6 la sonda debe reproducir la cartera C de M34, porque
el tope de 1/6 nunca ata cuando dos sleeves llevan 1/12 cada uno. Lo hace —+29.34%, 31.87%, 0.92,
idénticos— con la única diferencia en el dígito 28 (sumar doceavos y volver a dividir redondea
distinto que escribir un doceavo).

---

## 2. Resultados 3 / 6 / 12

| breadth | CAGR | **DD** | **Calmar** | desplegado medio | cuando invertida | posiciones | barras forzadas a caja | estable |
|---|---|---|---|---|---|---|---|---|
| **3** | +26.25% | **42.68%** | 0.62 | 20.5% | 37.6% | 2.6 | **2 229** | ❌ |
| **6** (declarada) | +29.34% | 31.87% | 0.92 | 21.7% | 33.0% | 4.0 | 0 | ✅ |
| **12** | **+35.21%** | **25.50%** | **1.38** | 21.7% | 33.0% | 7.0 | 0 | ✅ |

**Tres lecturas, y la tercera es la que importa:**

**(a) La corrección no rescata breadth 3.** Con la exposición agregada ya constante (20.5% vs
21.7%), el drawdown de breadth 3 pasa de 46.84% (M34) a **42.68%**. Es decir: de los ~15 pp de
diferencia contra breadth 6, sólo **~4 pp eran tamaño** y **~11 pp son concentración**.

**(b) Eso corrige lo que escribí en M34.** Ese informe decía que la vecina de breadth 3 corría
"libros el doble de grandes", insinuando que el exceso de drawdown era aritmética. Medido bien:
el capital desplegado medio sube sólo un **8%** de breadth 6 a 3 (21.7% → 23.4% bajo la regla de
M34), no el doble. **La concentración era el efecto dominante y mi explicación fue demasiado
generosa con C.** Queda corregido aquí.

**(c) Y la respuesta a la pregunta que pediste es favorable.** "Medir si la ventaja depende
realmente de tener exactamente 6 activos": **no depende de exactamente 6.** Mejora
monótonamente con la amplitud, a capital desplegado idéntico:

| | DD | Calmar |
|---|---|---|
| 3 posiciones (2.6 medias) | 42.68% | 0.62 |
| 6 (4.0 medias) | 31.87% | 0.92 |
| 12 (7.0 medias) | **25.50%** | **1.38** |

La ventaja necesita **al menos** unos 6 mercados, y con 12 es sustancialmente mejor. Eso es
diversificación comportándose como debe, no un parámetro afinado.

### Las 2 229 barras forzadas a caja

Es la única forma en que la construcción no puede igualar la exposición: en 2 229 barras (11.3%)
la referencia estaba invertida y breadth 3 no tenía ninguna señal elegible, así que se queda en
caja. **Eso sesga breadth 3 a favor** (menos tiempo expuesta) y aun así falla. No lo suavizo: va
en el registro porque un lector necesita saberlo.

### ¿Fue breadth 3 una realidad operativa?

| mercados elegibles a breadth 6 | barras | cuota |
|---|---|---|
| 6 | 18 530 | **93.62%** |
| 5 | 570 | 2.88% |
| ≤3 | 619 | **3.13%** |

Los seis estuvieron disponibles el **93.62%** del tiempo, y un universo de ≤3 sólo ocurrió en el
3.13% de las barras, **todas antes de 2018-03-31**. Mi sonda fuerza breadth 3 sobre **toda** la
muestra: un contrafactual que se dio en el 3% inicial y nunca después.

---

## 3. Reproducción con Risk V2 — **no ejecutada**

Estaba condicionada a que la fase 1 pasara ("Si pasa Fase 1: reproducir..."), y no pasa. No se
corrieron las 54 corridas del motor certificado (≈3.3 h), no se tocó producción, no se ajustó
ningún peso ni umbral.

**Lo que ya se sabe de M34 sobre esto, y sigue vigente:** el motor rota **×1.5 a ×10.5** más que
las señales solas, y el estado de posición es una entrada de estas reglas, así que un stop puede
crear una entrada que la regla sin stop nunca tomaría (321 de 322 trades del motor caen dentro de
un stretch del driver; el uno que no, lo hace exactamente por eso). **Que C sobreviva a Risk V2
sigue sin comprobarse, y es la pregunta más importante que queda abierta.**

---

## 4–7. CAGR/DD/Calmar, OOS, stress, concentración

La candidata (breadth 6) es la de M34, sin cambios:

| | valor | puerta |
|---|---|---|
| CAGR | +29.34% | — |
| max DD | 31.87% | ≤35% ✅ |
| Calmar | 0.92 | ≥0.50 ✅ |
| ×2 / ×3 coste | +23.66% / +18.22% | >0 ✅ |
| OOS (2024→) | +77.03% | >0 ✅ |
| mejor año | 0.41 | ≤0.50 ✅ |
| mejor activo | 0.26 | ≤0.60 ✅ |
| mejor sleeve | 0.55 | ≤0.60 ✅ |
| **sensibilidad a breadth** | **breadth 3 falla** | ❌ |
| **supervivencia a Risk V2** | **sin medir** | ❌ |

---

## 8. Veredicto

Por la condición de paso que escribí antes de correr —`phase_one_passes` exige que **los tres**
breadths declarados mantengan el tope de drawdown y el suelo de Calmar:

# NO-GO — fase 1 no pasa, línea cerrada

Sin fase 2. Sin optimizar. Sin nuevas estrategias. Sin cambiar pesos. Ningún PAPER CANDIDATE.

### Y ahora lo que debo decir contra mi propio veredicto

**Es la segunda vez en dos milestones que una condición de sensibilidad mía, y no el edge, cierra
una línea.** En M34 la sonda mezclaba dos variables. Aquí la sonda está bien —y lo demuestra
reproduciendo C exactamente— pero **mi condición de paso exige estabilidad a breadth 3**, un
universo que ocurrió en el 3% de las barras iniciales, nunca después de marzo de 2018, y para el
que esta cartera nunca se diseñó. Exigir que una cartera de 6 posiciones sobreviva a tener sólo 3
mercados durante nueve años es una prueba que ninguna cartera diversificada pasaría, y no es la
prueba que hacía falta.

No la cambio: estaba escrita antes de correr y moverla ahora, cuando moverla voltearía el
veredicto, es exactamente lo que prohibiste. Pero el veredicto formal y el hallazgo sustantivo
apuntan en direcciones opuestas, y sería deshonesto darte sólo el formal:

- **Formal:** NO-GO. La candidata falla la condición declarada.
- **Sustantivo:** la ventaja **no** es un artefacto de breadth=6; mejora monótonamente con la
  amplitud a capital constante (Calmar 0.62 → 0.92 → **1.38**). La sonda corregida fortaleció el
  caso de C, no lo debilitó.

**Mi recomendación, que es tuya para decidir:** lo único que falta de verdad para saber si esta
línea es real es la **fase 2 — la reproducción con Risk V2**, que no se ha hecho y que no depende
del veredicto de breadth. Si quieres retomarla, la forma limpia es una pre-declaración nueva con
(a) la condición de amplitud especificada sobre breadths operativamente posibles —digamos 6 y 12,
o "≥6"— y (b) esa reproducción. Ninguna de las dos es búsqueda de parámetros. Y si prefieres
cerrar, queda cerrada sin ambigüedad.

---

## 9. Limitaciones

1. **La pre-declaración se commiteó después de la corrida.** Ver §0.
2. **Fase 2 no ejecutada**, así que el efecto de Risk V2 sobre C sigue sin medir. Ver §3.
3. **Las 2 229 barras forzadas a caja** a breadth 3 sesgan esa vecina a favor. Ver §2.
4. **El stop de Risk V2 no está modelado en ninguna de las tres corridas**, igual que en M34; las
   cifras absolutas no son las de las estrategias desplegadas.
5. **La caja no renta.** El ~78% del capital sin desplegar no gana nada.
6. **Sin walk-forward**; sólo OOS por corte de fecha.
7. **El pool sigue siendo un acto de 2026** en composición, aunque la elegibilidad por fecha sí es
   point-in-time (limitación heredada de M32).

---

## 10. Reproducir

```bash
uv run python scripts/m35_masks.py    # cachea los timelines, ~390 s (una vez)
uv run python scripts/m35_phase1.py   # sonda corregida 3/6/12, segundos
```

Evidencia en `var/research/m35/phase1_breadth_4h.json` y `masks_4h.json`. Los timelines se cachean
porque no dependen de amplitud, asignación, coste ni ventana: recomputarlos en cada pase serían
390 s de trabajo idéntico.
