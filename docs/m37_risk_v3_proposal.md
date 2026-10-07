# Propuesta Risk V3 — **research only. NO desplegar.**

Derivada de M37. Nada de este documento está implementado, y la capa de riesgo
(`src/quantplatform/risk/`) **no se tocó** en M37. Esto es una propuesta para investigar, no un
plan de despliegue, y no autoriza ningún cambio en producción, paper ni VPS.

## Qué justifica abrir Risk V3

Dos incompatibilidades concretas **medidas**, no inferidas:

1. **El stop gestionado es incompatible con estas reglas de tendencia.** Cierra el 92.7% de las
   posiciones (G1: 99.5%), produce 1 350 re-entries, multiplica el turnover por ~2.05 frente a
   las señales, y deja la cuenta fuera del mercado el 52.7% del tiempo en que la estrategia la
   quiere dentro. Break-even, trailing y hard no son tres mecanismos: son un nivel de stop con
   tres formas de moverse, y quitar uno traslada sus salidas a los otros dos.
2. **`max_stop_distance_bps` es inalcanzable como distancia configurada** (ver abajo).

Y una alternativa estructural mínima —salida de estrategia primaria + stop duro al doble de la
distancia— pasó los ocho criterios predeclarados sobre el universo de M37.

---

## D1 — Defecto: el límite de distancia de stop se comporta como exclusivo

**Dónde.** `src/quantplatform/risk/sizing.py`, `_check_distance_window`:

```python
distance_bps = (distance / entry) * _BASIS_POINT_DIVISOR
...
if distance_bps > budget.max_stop_distance_bps:
    raise RiskSizingError(...)
```

**Qué pasa.** `distance` es la distancia **realizada**, medida desde el nivel de stop ya
redondeado al tick del venue. El redondeo es conservador —aleja el stop de la entrada— así que la
realizada es casi siempre mayor que la configurada. Un stop configurado *en* el límite realiza
*fuera* del límite.

**Severidad.** Una configuración que `RiskConfiguration` acepta como válida
(`initial_stop_distance_bps == risk_budget.max_stop_distance_bps`) **rechaza el 100% de las
entradas en runtime**. Medido: 0 trades en 12 pares, 211 rechazos. Falla cerrado —correcto en
seguridad— pero de forma **indistinguible de "la estrategia no quiso entrar"**, que es la parte
peligrosa: una cuenta así parece inactiva, no averiada.

**Opciones a investigar** (ninguna elegida):
- comparar la distancia **configurada** contra el límite, y la realizada solo contra un margen de
  tolerancia de un tick;
- documentar el límite como exclusivo y que el validador de `RiskConfiguration` rechace
  `initial_stop_distance_bps >= max_stop_distance_bps` en construcción, no en runtime;
- emitir una señal operativa distinguible cuando *todas* las entradas se rechazan por la misma
  razón durante N barras.

La tercera es la que más valor tiene por sí sola: hoy nada distingue "sin señal" de
"configuración imposible".

## D2 — Estructura: la salida primaria debería ser de la estrategia

**Hallazgo.** Con el stop gestionado, el 92.7% de las posiciones las cierra riesgo. Con el stop
duro y ancho, el 10.8% (103 de 958). El edge vive en tramos largos sin interrumpir, y la gestión
del stop los corta.

**Propuesta de investigación.** Separar explícitamente dos roles que hoy comparten un campo:
- **freno catastrófico**: un nivel que solo existe para acotar la pérdida máxima, estático, sin
  break-even, sin trailing, sin take profit, sin time stop;
- **gestión de la posición**: competencia de la estrategia, no de riesgo.

Esto **no** es "quitar protección": el freno sigue. Es dejar de usar el mismo mecanismo para dos
trabajos con objetivos opuestos.

**Lo que NO debe hacerse sin más evidencia.** Fijar 1200 bps como la distancia. En M37 ese número
viene de la convención de duplicado del proyecto, elegida por *viabilidad* y no por retorno, y
validada sobre **6 mercados**. No es un parámetro recomendado; es el punto que demostró que la
estructura funciona.

## D3 — Cooldown post-stop: declarado pero NO investigado

La capa de riesgo no tiene cooldown, re-entry delay ni estado post-stop (grep vacío). El re-entry
no es un mecanismo que Risk V2 tenga: es lo que pasa cuando un stop cierra y la estrategia sigue
queriendo entrar.

En M37 **no se probó**, y hay que decir por qué: con el stop ancho, los re-entries cayeron de
1 350 a **16** sin cooldown alguno. El cooldown ataca el síntoma; D2 ataca la causa. Queda como
línea abierta, no como pendiente urgente.

## D4 — Los breakers no se tocan

El breaker de drawdown total (20%), el diario (5%) y el de pérdida diaria (3%) **no dispararon ni
una vez** en todo el universo de M37 bajo el sizing de Risk V2 (variante G byte-idéntica a BASE).
No contribuyen al churn y **no hay nada que proponer sobre ellos**. El de loss-streak ya estaba
apagado en la política de referencia.

Que no dispararan no los hace inútiles: significa que en esta muestra no fueron necesarios.

---

## Qué tendría que pasar antes de que Risk V3 se acerque a paper

M37 **no** autoriza nada de esto. Lo mínimo:

1. **Re-validar sobre el universo completo.** Los números de M37 son sobre 6 mercados a breadth 6
   —que nunca selecciona— y **no son comparables con M36**. El drawdown de ALT2 (33.84%) está a
   1.16 puntos del tope de 35% en este universo; sobre 30 mercados puede estar al otro lado.
2. **Re-correr M36 fase 2 con la estructura de ALT2** y ver si la cartera combinada pasa donde
   antes dio NO-GO. Eso es lo que decidiría si el edge existe, no esta milestone.
3. **Arreglar D1** con tests de frontera por tick, antes de que cualquier configuración use una
   distancia cerca del límite del budget.
4. **Gate completo del proyecto** y el proceso habitual de pre-declaración.

## Lo que esta propuesta NO dice

- No dice que B2 + regime_trend sea desplegable. M36 dio **NO-GO definitivo** y M37 no lo revisa:
  M37 explica *por qué* falló y muestra que una estructura de riesgo distinta conserva más edge
  **en un universo reducido**.
- No propone cambiar ningún parámetro de estrategia. Ninguno se tocó.
- No propone tocar el Risk productivo, el execution productivo, las sesiones paper ni el VPS.
