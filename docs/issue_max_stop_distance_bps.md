# ISSUE — `max_stop_distance_bps` se comporta como límite exclusivo

**Estado: abierto. NO corregido.** Documentado como issue separado por instrucción de M38.
No bloquea M38 (ver "Impacto en M38" abajo), así que no se arregla todavía.

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
