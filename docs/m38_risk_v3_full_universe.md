# M38 — Validación de Risk V3 sobre el universo completo

**Veredicto: NO-GO. Línea cerrada.**

El candidato Risk V3 congelado en M37 **falla la puerta de drawdown en breadth 6** sobre el
universo point-in-time completo: **40.53% contra un tope de 35%**. Pasa en breadth 12. La regla
predeclarada exige pasar **todas** las puertas, y un fallo de 5.53 puntos sobre el tope no es
marginal: es un 15.8% de exceso relativo.

Research only. No se tocó: VPS, sesiones paper, Risk productivo, execution productivo,
estrategias productivas, parámetros de B2 ni de regime_trend. Nada se ejecutó en el VPS.
**No se probó otro stop, no se abrió ALT3, no se ajustó ningún threshold.**

---

## Alcance

30 mercados (universo point-in-time completo de M32/M36), 2 sleeves, 4H, 19 792 barras
(2017-09-01 → 2026-09-14). Breadths 6 y 12, los dos que M36 juzgó.

**Dos de las tres bases ya existían y se leyeron, no se re-corrieron.** La extracción de 60 pares
de M36 **es** Risk V2 sobre este universo — y el script lo **demuestra por igualdad de
configuración antes de hacer trabajo**, negándose a arrancar si difiere o si algún par cacheado
no tuvo éxito. La base de señal son los timelines cacheados de M35. Solo se extrajo el candidato:
60 pares, **4.26 h de CPU en 2.1 h de reloj**, 0 fallos.

La exposición **no** se normaliza entre bases: el allocator es idéntico y una capa de riesgo que
saca la cuenta del mercado antes debe aparecer como menos tiempo en mercado.

## Validaciones del harness

- **Risk V2 reutilizable: probado, no asumido.** Igualdad del objeto de configuración.
- **El candidato reproduce ALT2 de M37 exactamente** en todos los pares compartidos
  (B2/BTC 143t/3f, B2/BNB 144t/2f, B2/ADA 115t/16f).

## Resultados — breadth 6

| base | CAGR | DD | Calmar | PF | turnover | fees | stops | re-ent | held% | OOS | ×2 | ×3 | top activo | top sleeve | años+ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| signal | +29.34% | 31.87% | 0.92 | 1.55 | 270.8 | 18 624 | 0 | 0 | 100% | +77.03% | +23.66% | +18.22% | 26.1% | 55.0% | 80% |
| risk_v2 | +7.53% | 26.38% | 0.29 | 1.15 | 464.7 | 9 510 | *n/d* | 5 995 | 37.6% | +40.21% | **−0.45%** | **−7.84%** | 41.2% | 78.3% | 70% |
| **risk_v3** | +20.58% | **40.53%** | 0.51 | 1.41 | 268.4 | 11 397 | 675 | 118 | 87.6% | +68.48% | +15.33% | +10.30% | 30.4% | 55.6% | 70% |

**breadth 6: FALLA → drawdown.**

## Resultados — breadth 12

| base | CAGR | DD | Calmar | PF | turnover | fees | stops | re-ent | held% | OOS | ×2 | ×3 | top activo | top sleeve | años+ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| signal | +29.90% | 25.99% | 1.15 | 1.49 | 236.8 | 18 858 | 0 | 0 | 100% | +49.73% | +24.89% | +20.08% | 18.3% | 50.3% | 70% |
| risk_v2 | +7.50% | 22.44% | 0.33 | 1.14 | 439.8 | 9 528 | *n/d* | 5 995 | 37.6% | +24.10% | **−0.07%** | **−7.11%** | 23.4% | 72.4% | 80% |
| **risk_v3** | +22.91% | 31.16% | 0.74 | 1.41 | 233.4 | 12 782 | 675 | 118 | 87.6% | +38.50% | +18.24% | +13.75% | 18.6% | 50.7% | 70% |

**breadth 12: PASA** todas las puertas.

### Sobre la columna `stops` de Risk V2: **no disponible, no cero**

La caché de M36 es anterior al motor de registro y **no guarda razones de salida**. Un 0 ahí
afirmaría que Risk V2 nunca paró una posición, que es lo contrario de lo que hizo. Se reporta como
`n/d`. Lo que sí se mide de V2 en esa caché son los intervalos, de donde salen turnover,
re-entries y held%.

## Por qué falla: un stop protector puede **aumentar** el drawdown de cartera

Es el hallazgo central y es contraintuitivo. En breadth 6:

| base | exposición | episodios | DD |
|---|---|---|---|
| signal | 65.6% | 1 376 | 31.87% |
| risk_v3 | **63.0%** | 1 370 | **40.53%** |

V3 tiene **menos** exposición que la señal y **casi los mismos** episodios — así que el drawdown
extra no viene de estar más tiempo dentro ni de fragmentar tramos. Viene de los **675 stops**: un
stop convierte un drawdown no realizado en una **pérdida realizada**, y dispara precisamente en
los peores momentos. Donde la base de señal aguanta la caída y recupera, V3 sale abajo y vuelve a
entrar más tarde (118 re-entries) a un precio que ya no es el mismo. El resultado es un camino de
equity cuyo máximo drawdown es **peor que el de no tener stop ninguno**.

A breadth 12 el efecto se diluye —más mercados, menos peso por posición— y el drawdown cae a
31.16%, dentro del tope. Esa dependencia de la amplitud es exactamente lo que una puerta de
drawdown existe para detectar.

## Risk V3 vs Risk V2: mejor en casi todo, y aun así NO-GO

| medida | V2 → V3 (breadth 6) |
|---|---|
| CAGR | +7.53% → **+20.58%** (2.7×) |
| Calmar | 0.29 → **0.51** |
| PF | 1.15 → **1.41** |
| turnover | 464.7 → **268.4** (−42%) |
| re-entries | 5 995 → **118** (−98%) |
| held % | 37.6% → **87.6%** |
| concentración sleeve | 78.3% → **55.6%** |
| stress ×2 | **−0.45%** → **+15.33%** |
| stress ×3 | **−7.84%** → **+10.30%** |
| **drawdown** | 26.38% → **40.53%** |

Una observación que M38 produce de paso y que conviene no perder: **Risk V2 es frágil a costes en
los dos breadths** (×2: −0.45% y −0.07%; ×3: −7.84% y −7.11%). La configuración desplegada no
sobrevive el doble de coste sobre este universo. V3 sí, con holgura.

Pero la regla es pasar **todas** las puertas, y V3 compra todo eso con 14 puntos más de drawdown
que V2 y 8.7 más que la señal. **No pasa.**

## Safety invariants

| invariante | Risk V2 | Risk V3 |
|---|---|---|
| stop existe | ✓ | ✓ |
| stop obligatorio en entrada | ✓ | ✓ |
| sizing por riesgo | ✓ | ✓ |
| breakers presentes | ✓ | ✓ |
| sin latch | ✓ | ✓ |
| **sin ratchet** | ✗ (trailing) | **✓** |
| stop estrictamente dentro de la ventana | ✓ | ✓ |
| | **6/7** | **7/7** |

El candidato **no regresa** ninguna invariante de seguridad: las mantiene todas, y es
estrictamente más seguro que V2 en la del ratchet. El fallo de M38 es de drawdown medido, no de
seguridad estructural.

## Errores por par

**0 de 60** en la extracción de V3. **0 de 60** en la caché de V2 de M36. Cada par captura su
excepción y la devuelve como dato; no hubo ninguno que registrar.

## Bug de `max_stop_distance_bps`

Documentado como issue separado en `docs/issue_max_stop_distance_bps.md`. **No bloqueó M38** —
a 1200 bps, 0 de 36 casos precio/tick rechazan, con distancia realizada máxima de 1241.5 bps
contra un límite de 2000. **No se corrigió**, por instrucción.

## Reproducibilidad

- `scripts/m38_extract.py` — extracción del candidato, caché reanudable por par, verifica la
  reutilización de V2 por igualdad antes de empezar.
- `scripts/m38_compare.py` — las tres bases por el mismo camino de medición.
- Evidencia: `var/research/m38/risk_v3_4h.json`, `var/research/m38/risk_v3_full_universe_4h.json`,
  y la caché de V2 en `var/research/m36/positions_4h.json`.

---

## Veredicto

**NO-GO. Se cierra la línea de Risk V3.**

M37 encontró ALT2 sobre 6 mercados con un drawdown de 33.84%, a 1.16 puntos del tope, y dejó
escrito que sobre 30 mercados podía caer al otro lado. **Cayó al otro lado:** 40.53%.

No se ajusta el threshold, no se prueba otra distancia de stop y no se abre ALT3. El resultado de
breadth 12 no rescata nada: la regla predeclarada exige las dos amplitudes, y una estructura cuyo
drawdown depende de operar con doce mercados en vez de seis es precisamente una fragilidad, no un
matiz.

**Lo que M38 sí deja establecido**, y vale como conocimiento aunque la línea se cierre:

1. **Un stop protector puede aumentar el drawdown de cartera.** Con exposición menor y los mismos
   episodios que la base sin riesgo, V3 llega a un drawdown 8.7 puntos peor. Realizar pérdidas en
   el peor momento y volver a entrar después cuesta más, en drawdown, que no haber salido.
2. **Risk V2 es frágil a costes sobre el universo completo**, en los dos breadths. Eso no estaba
   medido antes de M38 y no depende del candidato.
3. **La estructura de V3 sí arregla el churn**: turnover −42%, re-entries −98%, held% de 37.6% a
   87.6%, concentración por sleeve de 78.3% a 55.6%. El problema que M37 diagnosticó era real y
   esta estructura lo resuelve — pero el precio en drawdown la descalifica bajo las puertas
   vigentes.
