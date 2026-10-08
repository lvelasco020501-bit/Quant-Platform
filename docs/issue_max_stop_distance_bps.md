# ISSUE — `max_stop_distance_bps` se comportaba como límite exclusivo

**Estado: CORREGIDO en M39** (fase previa al research, por separado). Ver "Corrección" al final.
Descubierto en M37, documentado en M38 sin arreglar, corregido en M39.

**Descubierto en:** M37 fase 2 (ALT1).
**Severidad:** alta — una configuración que el validador acepta rechaza el 100% de las entradas
en runtime, y lo hace de forma indistinguible de "la estrategia no quiso entrar".
**Componente:** `src/quantplatform/risk/sizing.py` — producción. **Sin tocar.**

---

## Síntoma

Con `initial_stop_distance_bps == risk_budget.max_stop_distance_bps` (2000 bps), el motor
certificado produjo **0 trades en 12 pares** y **211 rechazos de entrada**, todos con la misma
razón:

```
risk-based sizing refused this stop: the stop is further than max_stop_distance_bps permits
```

## Causa

`src/quantplatform/risk/sizing.py`, `_check_distance_window`:

```python
distance_bps = (distance / entry) * _BASIS_POINT_DIVISOR
...
if distance_bps > budget.max_stop_distance_bps:
    raise RiskSizingError(...)
```

`distance` es la distancia **realizada**: se mide desde el nivel de stop ya redondeado al
`price_tick` del venue. El redondeo es **conservador** —aleja el stop de la entrada, que es la
dirección correcta para seguridad— así que la distancia realizada es casi siempre **mayor** que
la configurada. Un stop configurado *en* el límite realiza *fuera* del límite, y la comparación
lo rechaza.

Solo sobrevive un precio cuyo nivel al `(1 − límite)` cae exacto en un múltiplo del tick.

## Reproducción (determinista, sin motor)

`price_tick = 0.01`, límite 2000 bps:

| entry | nivel redondeado | distancia realizada | veredicto |
|---|---|---|---|
| 100.00 | 80.00 | 2000.000000 bps | ok |
| 23.456 | 18.76 | 2002.046385 bps | **RECHAZA** |
| 0.4567 | 0.36 | 2117.363696 bps | **RECHAZA** |
| 61 234.57 | 48 987.65 | 2000.000980 bps | **RECHAZA** |
| 1.0001 | — | 2000.799920 bps | **RECHAZA** |
| 7.77 | — | 2007.722008 bps | **RECHAZA** |

Sobre los 6 mercados de M16 × 6 precios = 36 casos: **24 de 36 (67%) rechazan** con el stop en
el límite. Con el stop a 1200 bps: **0 de 36**.

## Por qué es peligroso, más allá de no operar

Falla **cerrado**, que en una capa de riesgo es la dirección correcta. El problema es la
**observabilidad**: una cuenta con esta configuración produce exactamente la misma traza que una
cuenta cuya estrategia no generó señales. No hay nada que distinga *"configuración imposible"* de
*"sin oportunidades"*. En M37 se detectó solo porque una variante entera dio 0 trades en los 12
pares a la vez.

Además, el validador de `RiskConfiguration` **acepta** la configuración: el fallo aparece en
runtime, por entrada, no en construcción.

## Impacto en M38 — ninguno

El candidato Risk V3 de M38 usa 1200 bps, con el límite en 2000. En los 36 casos de reproducción
la distancia realizada máxima a 1200 bps fue **1241.5 bps**, muy por debajo del límite. El
invariante `STOP_WITHIN_BUDGET_WINDOW` de M38 además exige que el stop esté **estrictamente**
dentro de la ventana del budget, justamente para que una configuración de este tipo no pueda
declararse segura.

**Conclusión: no bloquea M38 y por tanto no se corrige aquí.**

## Opciones a investigar (ninguna elegida, ninguna implementada)

1. **Comparar la distancia configurada** contra el límite, y la realizada solo contra una
   tolerancia de un tick. Preserva la intención del límite sin que el redondeo lo viole.
2. **Documentar el límite como exclusivo** y mover la comprobación al validador de
   `RiskConfiguration`, de modo que `initial_stop_distance_bps >= max_stop_distance_bps` se
   rechace en construcción y no 211 veces en runtime.
3. **Señal operativa distinguible** cuando todas las entradas se rechazan por la misma razón
   durante N barras. Es la que más valor tiene por sí sola: hoy nada separa "sin señal" de
   "configuración imposible", y esa es la parte que convierte un fallo seguro en un fallo
   silencioso.

Cualquier corrección necesita **tests de frontera por tick** (precio cuyo nivel cae exacto en el
tick, y precio que no) antes de tocar producción.

## Qué NO hacer

- No ensanchar `max_stop_distance_bps` para que el caso quepa: eso cambia un límite de riesgo
  para acomodar un defecto de comparación.
- No recortar silenciosamente la distancia al límite: produciría un stop que la configuración
  nunca describió.


---

# Corrección (M39, fase 0)

**Semántica elegida: el máximo es inclusivo, con tolerancia de exactamente un tick.**

La distancia que comprueba el sizer es la **realizada**, medida desde un nivel ya redondeado al
tick. El redondeo es conservador —aleja el stop de la entrada, nunca lo acerca— así que la
distancia realizada **nunca es menor** que la configurada y puede ser mayor hasta en un tick. El
máximo tolera ahora exactamente `un tick expresado en bps`; el mínimo **no tolera nada**.

```python
distance_bps = (distance / entry) * _BASIS_POINT_DIVISOR
tick_bps     = (tick / entry) * _BASIS_POINT_DIVISOR
if distance_bps < budget.min_stop_distance_bps:            # estricto
    raise ...
if distance_bps > budget.max_stop_distance_bps + tick_bps: # inclusivo en un tick
    raise ...
```

**Por qué la asimetría.** El redondeo solo puede **ensanchar** un stop, así que no puede meter en
la ventana un stop demasiado cercano. Una tolerancia en el mínimo admitiría stops que el budget
prohíbe — exactamente lo contrario de lo que el límite existe para hacer. La asimetría no es un
descuido: es la consecuencia de que el redondeo tenga una sola dirección.

**Por qué un tick y no más.** El error de redondeo es **estrictamente menor que un tick** por
construcción, así que un tick es el límite ajustado, no un margen de comodidad. Admite
exactamente las configuraciones que el budget ya permite y ninguna otra. La tolerancia **escala
con el tick del venue**, no es un número fijo: un tick de 1.00 tolera más que uno de 0.01, y un
nivel que el primero explica el segundo lo sigue rechazando.

**El mensaje de error reporta el límite configurado**, no la suma interna, porque quien lo lee
necesita el número que puso.

## Tests de regresión (7, todos pasando)

| test | resultado |
|---|---|
| stop exactamente en el máximo | aceptado |
| un tick más allá (2000.001 bps, tolerancia 0.001) | aceptado |
| dos ticks más allá (2000.002 bps) | **sigue rechazado** |
| la tolerancia escala con el tick (0.01 vs 1.00) | grueso aceptado, fino rechazado |
| el mínimo no tolera nada | sigue rechazado |
| el error reporta el límite configurado | pasa |
| stop más ancho que el máximo (test previo de M16) | sigue rechazado |

## Demostración de que no cambia ningún resultado existente

El argumento es que la corrección solo ensancha una comparación en un tick, así que únicamente
puede alterar una corrida cuya distancia realizada cayera en ese margen. Es un argumento sólido,
pero sigue siendo un argumento, así que se midió: `scripts/m39_bugfix_equivalence.py` re-corre
12 parejas reales —los tres mercados más cortos del pool × 2 sleeves × las dos configuraciones
medidas (Risk V2 a 600 bps y el candidato de M37 a 1200)— y compara los conteos de trades del
motor contra las cachés producidas **antes** de la corrección. Mercados elegidos por coste de
cómputo, antes de ver ningún resultado.

```
12 identical, 0 differing, 0 not cached
```

| config | sleeve | mercado | trades | |
|---|---|---|---|---|
| v2 | B2 | LUNAUSDT | 114 | idéntico |
| v2 | B2 | MATICUSDT | 229 | idéntico |
| v2 | B2 | SOLUSDT | 249 | idéntico |
| v2 | G1 | LUNAUSDT | 78 | idéntico |
| v2 | G1 | MATICUSDT | 99 | idéntico |
| v2 | G1 | SOLUSDT | 106 | idéntico |
| v3 | B2 | LUNAUSDT | 36 | idéntico |
| v3 | B2 | MATICUSDT | 90 | idéntico |
| v3 | B2 | SOLUSDT | 97 | idéntico |
| v3 | G1 | LUNAUSDT | 8 | idéntico |
| v3 | G1 | MATICUSDT | 8 | idéntico |
| v3 | G1 | SOLUSDT | 24 | idéntico |

Ninguna configuración medida por este proyecto se acerca al límite: Risk V2 deriva stops a 600 bps
y el candidato de M37 a 1200, contra un máximo de 2000. **Lo único que cambia de comportamiento
es una configuración cuya distancia realizada caía justo pasado su máximo configurado** — que es
exactamente lo que el issue describía como roto.

## Lo que la corrección NO hace

- **No modifica ninguna estrategia.** El cambio son tres líneas en `_check_distance_window` más
  pasar `request.rules.price_tick`, que ya estaba en el ámbito del llamante.
- **No ensancha `max_stop_distance_bps`.** El límite de riesgo no se movió.
- **No recorta** la distancia al límite: una distancia más allá de lo que el redondeo explica
  sigue siendo un rechazo, no un ajuste silencioso.
- **No arregla la observabilidad.** La opción 3 de arriba —una señal distinguible cuando todas
  las entradas se rechazan por la misma razón— sigue abierta, y sigue siendo la que más valor
  tiene por sí sola. El fallo ya no ocurre en el límite, pero si ocurriera por otra causa seguiría
  siendo indistinguible de "sin señal".
