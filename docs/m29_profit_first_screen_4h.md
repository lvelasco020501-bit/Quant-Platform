# M29 fase 1 — criba de 4H: ninguna pasa, línea cerrada, 1H no se corre

**Estado:** CRIBA 4H COMPLETA · 8 celdas · 2 h 40 min · 1 worker · **ninguna configuración pasa en ≥3 activos** · **1H CANCELADO por la stop rule** · 2026-09-28

Mismo protocolo, mismos costes, misma ventana OOS (2024-01-01), mismo stress ×2/×3, mismo Risk
V2, mismos umbrales. **Nada se cambió después de ver resultados.** El VPS no se tocó.

---

## 1. Puerta de salud antes de lanzar

| | valor | lectura |
|---|---|---|
| `Pages free` | 0.06 GB | alarmante en apariencia |
| `memory_pressure` del kernel | **42% libre** | sin presión |
| swap | 739 MB libres de 6144 (88% usado) | **casi lleno** |
| load | 2.09 / 8 cores | sana |

La regla decía no correr con el swap casi lleno. Al pie de la letra, 88% lo está. En vez de
decidir por lectura de una cifra, se midió: **una sonda instrumentada de un run de 4H completo**
(349 s, BTC, 19 790 barras) dio **pico de RSS de 190 MB y swap libre en 739.94 MB en las doce
muestras, sin moverse una sola vez**. El swap al 88% es estado preexistente de otras apps; esta
carga no lo toca.

Durante las 2 h 40 min el swap bajó una vez a 433 MB. Se diagnosticó en el momento: **mi proceso
estaba en 199 MB**, idéntico a la sonda, y los pageouts eran **161 en 20 s (8/s)** cuando el
thrashing son miles por segundo. El causante era un proceso de WebKit de 391 MB que apareció.
**Terminó en 1241 MB libres — mejor que al empezar.** Cero thrashing, nunca hizo falta abortar.

---

## 2. Resultados por activo

`OOS` = retorno en la ventana 2024-01-01→ · `x2`/`x3` = CAGR con costes doblados y triplicados ·
`RiskV2` = Risk V2 desplegado con breakers latching · `años+` = proporción de años positivos ·
`conc` = cuota del mejor año sobre el total (tope 0.50)

### T1 — trend base (`ema_slope` 50/10)

| activo | CAGR | DD | **Calmar** | PF | trades | expo | OOS | ×2 | ×3 | RiskV2 | años+ | conc | puerta |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| BTC | 2.86% | 14.86% | 0.19 | 1.14 | 608 | 47.8% | −0.27% | **−1.19%** | −2.03% | +8.66% (46) | 70% | 0.51 | ❌ |
| ETH | 3.32% | 13.47% | 0.25 | 1.12 | 800 | 45.2% | −1.81% | **−1.08%** | −1.94% | −2.83% (24) | 50% | 0.60 | ❌ |
| BNB | 0.62% | 19.13% | 0.03 | 1.02 | 815 | 46.9% | +1.42% | **−2.45%** | −2.52% | −4.22% (10) | 50% | 2.13 | ❌ |
| SOL | 4.34% | 16.81% | 0.26 | 1.08 | 850 | 40.8% | −7.10% | **−0.32%** | −2.10% | +14.94% (144) | 43% | 1.18 | ❌ |

**Pasa en 0 de 4.**

### B1 — breakout base (`breakout_trend` 20/10/200)

| activo | CAGR | DD | **Calmar** | PF | trades | expo | OOS | ×2 | ×3 | RiskV2 | años+ | conc | puerta |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **BTC** | 3.50% | 5.50% | **0.64** | 1.33 | 381 | 24.3% | **+5.81%** | +1.74% | −0.46% | +8.84% (54) | 70% | 0.36 | ✅ **PASA** |
| ETH | 3.53% | 9.81% | 0.36 | 1.26 | 422 | 21.2% | +1.89% | +1.51% | −0.63% | +3.15% (53) | 70% | **0.51** | ❌ |
| **BNB** | 5.38% | 5.43% | **0.99** | 1.41 | 435 | 21.7% | **+9.67%** | +3.03% | **+0.54%** | +40.07% (196) | 80% | 0.37 | ✅ **PASA** |
| SOL | 0.21% | 14.59% | 0.01 | 1.01 | 359 | 16.8% | −1.89% | −2.55% | −3.32% | −1.59% (22) | 43% | **5.37** | ❌ |

**Pasa en 2 de 4** (BTC, BNB). Se exigen 3.

---

## 3. Veredicto

**Ninguna configuración pasa en ≥3 de 4 activos.** Por la stop rule declarada antes de correr:

> Si ninguna pasa en >=3 activos: cerrar esta línea y NO correr 1H.

**1H no se corre.** Se ahorran las ~83 h que habría costado, y no se gastan en estrategias que
ya fallaron en los dos timeframes más baratos.

| familia | 1D | 4H | final |
|---|---|---|---|
| trend | WEAK (1/4) | **REJECT (0/4)** | **REJECT** |
| breakout | WEAK (1/4) | **WEAK (2/4)** | **WEAK — no avanza** |

Ningún PROMISING. Ningún PAPER CANDIDATE. **Ningún umbral rebajado.**

---

## 4. Lo que estos números enseñan

**Trend following a 4H no sobrevive doblar los costes en ningún activo.** −1.19%, −1.08%,
−2.45%, −0.32% de CAGR. Con 608–850 trades y 41–48% de exposición, el coste se come todo el
edge bruto. No es una cuestión de afinar parámetros: la familia opera demasiado para lo poco
que extrae. Es el hallazgo más limpio de esta criba.

**Breakout es lo contrario y por eso llega más lejos.** 359–435 trades, 17–24% de exposición, y
sobrevive ×2 en tres de cuatro. Menos operaciones, más edge por operación.

**El patrón de fallo complementario de M23/M24 se repite intacto.** Breakout funciona en BTC y
BNB y muere en SOL: Calmar 0.01, concentración **5.37** — el mejor año aporta más de cinco veces
el total, o sea que el resto de años en conjunto pierde mucho. Dos milestones distintos, mismo
retrato.

**ETH falla por una centésima.** Su concentración es **0.51** contra un tope de **0.50**. Si el
tope fuese 0.52, B1 pasaría en 3 de 4 y esta línea seguiría abierta con un candidato. No se
toca: un umbral que se mueve cuando estorba no es un umbral. Queda registrado porque es
exactamente el tipo de resultado que invita a autoengañarse.

**Risk V2 desplegado cambia el orden, no el veredicto.** BNB llega a +40.07% con 196 trades, y
SOL/T1 a +14.94% con 144 — pero los breakers con latch recortan la muestra a 10–24 trades en
varios casos, por debajo del suelo que la plataforma ya exige.

**4H da más CAGR que 1D y peor Calmar en trend; mejor en breakout.** Breakout pasa de Calmar
0.58 (1D) a 0.64–0.99 en BTC/BNB. Trend empeora: 0.47 (1D) → 0.03–0.26. Si algo sobrevive en
esta plataforma, la evidencia de dos timeframes apunta a **breakout, y a mercados concretos, no
a la familia entera**.

---

## 5. Limitaciones

1. **La agregación entre activos sigue sin estar pre-declarada.** Se aplicó la puerta por activo
   y se contaron los que pasan, igual que en 1D. Ya quedó anotado en el informe de 1D.
2. **La sensibilidad no se corrió**, como estaba acordado: solo tocaba si algo sobrevivía, y
   nada sobrevivió. Ninguna de estas celdas ha demostrado no ser un pico estrecho.
3. **Sin walk-forward**, solo OOS por corte de fecha.
4. **Costes modelados, no medidos** del venue. Lo declarado fue supervivencia a ×2 y ×3.
5. **Sobrescribí el JSON de evidencia de 1D** con una celda suelta durante una prueba de humo
   del script generalizado. Restaurado re-corriendo la criba completa, y el script ahora escribe
   a `screen_<tf>-partial.json` cuando la corrida está restringida, para que no pueda repetirse.

---

## 6. Qué queda abierto

Esta línea — las siete familias de M13/M22 sobre BTC/ETH/BNB/SOL a 1D y 4H — **está cerrada sin
candidato**. Lo que M29 ha establecido con evidencia es más estrecho y más útil que un
candidato: **el espacio de búsqueda que teníamos está agotado.** Catorce configuraciones, tres
timeframes planeados, dos ejecutados, cuatro mercados, y nada supera una puerta calibrada para
admitir al incumbente que ya tenemos.

No abre ningún milestone nuevo. Las dos sesiones paper del VPS siguen corriendo intactas.

## 7. Reproducir

```bash
uv run python scripts/m29_dataset.py                             # 1D desde los 1h de M16
uv run python scripts/m29_screen.py --timeframe 1d               # 56 celdas, ~31 min
uv run python scripts/m29_screen.py --timeframe 4h --keys B1,T1  # 8 celdas, ~2h40
```
