# M37 — Compatibilidad estrategia / riesgo: ablación de Risk V2

**Fase 1 (ablación): causa identificada.** El churn que destruyó el edge en M36 no viene de los
breakers, ni del take profit, ni del time stop, ni del break-even, ni del trailing por separado.
Viene del **stop protector como mecanismo único** — y sus tres partes no son separables porque
son *el mismo nivel de stop* con tres formas de moverse.

Research only. No se tocó: sesiones paper del VPS, Risk productivo, estrategias productivas,
execution productivo, parámetros de B2, parámetros de regime_trend. Nada se ejecutó en el VPS.

---

## Alcance, y por qué estos números NO se comparan con M36

6 mercados (los seis de M16), 2 sleeves, breadth 6, 4H, 19 790 barras (2017-09-01 → 2026-09-14).
108 corridas del motor certificado: 9 variantes × 2 sleeves × 6 mercados.

M36 usó 30 mercados. Nueve variantes sobre 30 mercados no era pagable con un motor O(n²). Una
cartera de 6 mercados a breadth 6 **nunca selecciona** — siempre tiene los seis — así que
ninguno de los niveles de M37 es comparable con las cifras de M36. **Toda comparación aquí es
variante-contra-baseline dentro de M37**, y la variante BASE re-mide el punto de referencia sobre
este universo para que eso sea posible.

La exposición **no** se normaliza entre variantes. El allocator es idéntico en todas: mismo peso
por señal, mismo tope por activo, mismo universo, mismos costes. Solo cambian los masks. M35
tuvo que mantener la exposición constante porque cambiar la amplitud cambiaba mecánicamente el
tamaño de posición; aquí no pasa nada de eso, y normalizar **esconderia** el efecto que se mide —
un mecanismo que saca la cuenta del mercado antes debe aparecer como menos tiempo en mercado. La
exposición se reporta como columna propia.

## Configuración Risk V2 de partida (la que corrió M36, a 4H)

| mecanismo | valor |
|---|---|
| sizing (risk_budget) | 1% riesgo/trade, 50% exposición máx, stop clamp 100–2000 bps |
| initial stop | 600 bps |
| break-even | activa a 300 bps |
| trailing | arma a 600 bps, sigue a 400 bps |
| take profit | 1200 bps |
| time stop | 42 barras |
| drawdown breaker | total 20%, diario 5%, pérdida diaria 3% |
| loss-streak breaker | **None — ya estaba apagado** |
| re-entry | **no existe como mecanismo** |

Tres hechos estructurales, leídos del código **antes** de correr nada:

1. **El loss-streak breaker ya estaba apagado.** La política de latch de referencia que usa todo
   screen desde M13 pone `max_consecutive_losses=None`. Desactivarlo es un no-op, así que no
   puede ser causa de nada que M36 midiera. Se conservó como **control de harness**: tiene que
   reproducir el baseline bit a bit.
2. **El initial stop no es ablatable solo.** `RiskConfiguration` rechaza un risk budget sin
   distancia de stop — el budget mide el riesgo *desde* el stop. Se ablata junto con el sizing, y
   su efecto marginal se lee como B − A.
3. **El re-entry no tiene interruptor.** No hay cooldown ni estado post-stop en toda la capa de
   riesgo. El re-entry no es un mecanismo que Risk V2 *tenga*: es lo que *pasa* cuando un stop
   cierra una posición y la estrategia sigue queriéndola. Se mide en todas las variantes.

## Tabla de ablación

| VAR | desactiva | CAGR | DD | CALMAR | TURNOVER | FEES | RE-ENT | STOPS | FORCED | EXPO | HELD% | OOS | ×2 | ×3 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| SIG | *(solo señales)* | +43.19% | 23.65% | 1.83 | 192.9 | 31 584 | — | — | — | 58.7% | 100% | +75.07% | +38.67% | +34.30% |
| BASE | *(nada)* | +14.41% | 22.15% | 0.65 | 397.8 | 12 219 | 1 350 | 1 778 | 2 134 | 41.3% | 47.3% | +47.53% | +7.10% | +0.25% |
| A | sizing | +3.54% | 9.70% | 0.36 | 95.3 | 1 689 | 340 | 429 | 521 | 17.2% | **10.0%** | +1.21% | +1.91% | +0.31% |
| B | initial stop + sizing | **+45.04%** | **36.03%** | 1.25 | **120.7** | 22 651 | **1** | **0** | **0** | 85.1% | 69.7% | +30.02% | +42.16% | +39.34% |
| C | break-even | +13.29% | 28.58% | 0.46 | 358.2 | 10 672 | 1 099 | 1 326 | 1 776 | 45.0% | 54.6% | +41.00% | +6.75% | +0.59% |
| D | trailing | +14.72% | 22.79% | 0.65 | 375.3 | 11 550 | 1 212 | 1 458 | 1 975 | 42.0% | 50.1% | +50.52% | +7.79% | +1.28% |
| E | take profit | +16.35% | 22.32% | 0.73 | 376.3 | 13 374 | 1 219 | 1 951 | 2 005 | 41.2% | 47.2% | +44.64% | +9.31% | +2.69% |
| F | time stop | +14.78% | 21.41% | 0.69 | 395.7 | 12 349 | 1 337 | 1 797 | 2 108 | 41.3% | 47.7% | +48.04% | +7.48% | +0.65% |
| G | drawdown breaker | +14.41% | 22.15% | 0.65 | 397.8 | 12 219 | 1 350 | 1 778 | 2 134 | 41.3% | 47.3% | +47.53% | +7.10% | +0.25% |
| H | **control** (streak) | +14.41% | 22.15% | 0.65 | 397.8 | 12 219 | 1 350 | 1 778 | 2 134 | 41.3% | 47.3% | +47.53% | +7.10% | +0.25% |

**El control H reproduce el baseline exactamente.** Además, BASE reproduce los conteos de trades
de M36 **al entero en los 12 pares** (B2/ETH 288, G1/SOL 106, …): el motor de registro observa
sin cambiar nada. Son dos validaciones independientes del harness.

## Atribución contra la regla predeclarada

Regla fijada antes de medir: primario sólo si al desactivarlo **solo** se recorta ≥25% del
turnover del baseline **y** se recupera ≥25% de los 28.78 puntos de distancia BASE→SIG.

| VAR | recorte de turnover | brecha recuperada | ¿primario? |
|---|---|---|---|
| A | 76.0% | **−37.8%** | no |
| B | 69.7% | **106.4%** | **SÍ** |
| C | 9.9% | −3.9% | no |
| D | 5.6% | +1.1% | no |
| E | 5.4% | +6.8% | no |
| F | 0.5% | +1.3% | no |
| G | 0.0% | 0.0% | no |
| H | 0.0% | 0.0% | no |

**Causa primaria: B — el stop protector** (ablatado junto al sizing por la invariante del config).

## Por qué las tres partes del stop no se separan

No es un hueco de la medición: **es el hallazgo**. El break-even y el trailing no son stops
propios, son *movimientos del único nivel de stop* que crea la distancia inicial. El desglose de
salidas lo muestra directamente — al quitar una vía de salida, sus salidas las absorben las otras:

| variante | break-even | trailing | hard | forced total |
|---|---|---|---|---|
| BASE | 728 | 704 | 702 | 2 134 |
| C (sin break-even) | — | 874 **(+170)** | 902 **(+200)** | 1 776 |
| D (sin trailing) | 1 321 **(+593)** | — | 654 | 1 975 |
| E (sin take profit) | 657 | 758 | 590 | 2 005 (protective_stop 1 778 → **1 951**) |
| F (sin time stop) | 724 | 709 | 675 | 2 108 (take_profit 304 → 311) |

Hay **un mecanismo con tres nombres**. Por eso ninguna ablación individual llega al 25%, y por
eso quitar el stop entero cambia todo: turnover −70%, re-entries 1 350 → **1**, forced exits
2 134 → **0**.

## Composición de salidas del baseline

De 2 301 trades, **2 134 (92.7%) los cerró una acción de riesgo**, no la estrategia
(B2 89.2%, G1 **99.5%**). De esos forced exits: protective stop 83.3%, take profit 14.2%,
time stop 2.4%.

## Dos resultados negativos limpios

- **Los breakers nunca dispararon.** G es byte-idéntico a BASE. El breaker de drawdown total
  (20%), el diario (5%) y el de pérdida diaria (3%) no actuaron ni una vez bajo el sizing de
  Risk V2. Aportan exactamente **cero** al churn.
- **El loss-streak breaker ya estaba apagado**, confirmado contra el config real.

## El confound de A y B, declarado

A y B quitan `risk_budget`, y el sizing cae al fallback V1: **`entry_fraction = 0.95`**, es decir
95% del equity por posición, contra el ~16.7% que da el budget (1% riesgo ÷ 6% stop). Eso no es
"mismo trading con otro tamaño": cada pérdida es ~6× más profunda, el equity path es otro, y los
breakers empiezan a rechazar entradas.

Se midió: bajo A el motor mantuvo posición en solo el **10.0%** de las barras en que la
estrategia la quería, contra **47.3%** en BASE, con un cierre más largo de 279 barras (46 días)
contra 199. **A no "operó menos": A no pudo operar.** Por eso su turnover cae 76% con la brecha
*empeorando* 37.8 puntos — y por eso el recorte de turnover solo no basta como criterio, que es
exactamente lo que la segunda condición de la regla existe para impedir.

C, D, E, F y G **no** llevan este confound: conservan `risk_budget`, así que su sizing y su
escala de equity son los de BASE.

## Correcciones aplicadas durante M37

**1. Masks desalineados (grave).** Los masks de señal venían cacheados sobre la rejilla de 30
mercados de M36 (19 792 barras); los del motor se construyen sobre la de 6 de M37 (19 790). Dos
instantes —2018-06-26 04:00 y 08:00, horas en que operó algún otro mercado del pool y ninguno de
estos seis— existen solo en la primera. Tomar los bits por posición desalineaba **el 91% de la
muestra** a partir del índice 1782. Corregido proyectando por *timestamp*. La fila SIG cambió de
+37.44% / 31.54% / 1.19 a **+43.19% / 23.65% / 1.83**, así que la cifra previa era inservible.
Los masks del motor siempre fueron correctos (se construyen desde instantes).

**2. Orden de suma no determinista.** La extracción escribe cada par cuando su worker termina, y
dos variantes de la misma configuración aterrizan sus pares en orden distinto; la cartera sumaba
los mismos Decimals en secuencia distinta y los totales diferían **en el dígito significativo 40**
(turnover `…4399462414` vs `…4399462415`). Suficiente para que una configuración idéntica se
comparara desigual consigo misma — justo lo que el control debe detectar. Se fijó el orden; la
afirmación del control sigue siendo igualdad exacta, no una tolerancia.

Ambos fallos eran míos y ninguno estaba en el motor ni en Risk.

## Coste medido y reproducibilidad

| variante | CPU | mediana/corrida |
|---|---|---|
| BASE | 0.99 h | 316 s |
| A | 1.08 h | 349 s |
| B | 1.16 h | 365 s |
| C | 1.18 h | 378 s |
| D | 3.83 h | 422 s |
| E | 8.23 h | 2 641 s |
| F | 10.28 h | 3 287 s |
| G | 10.17 h | 3 368 s |
| H | 11.59 h | 3 837 s |

Predicho 8.35 h CPU; medido **48.5 h** en 24.3 h de reloj con 2 workers. La diferencia **no es
del trabajo**: H tiene configuración idéntica a BASE y tardó **11.7×** más. El orden de ejecución
fue BASE→H, así que la ralentización es monótona en tiempo de reloj, no en variante: la máquina
se degradó a lo largo de las 24 h. Los resultados son deterministas y no dependen del reloj; el
control idéntico lo demuestra.

Reproducible con `scripts/m37_ablate.py` (caché reanudable por variante-sleeve-mercado) y
`scripts/m37_attribute.py`. Evidencia en `var/research/m37/ablation_4h.json` y
`var/research/m37/ablation_attribution_4h.json`.

## Errores por par

**0 de 108.** Ninguna corrida falló. Cada par captura su excepción y la devuelve como dato.

---

## Veredicto fase 1

**Causa dominante demostrada: el stop protector.** Pasa a fase 2.

Lo que B *no* es: una solución. B no tiene stop ninguno — cero protección contra pérdidas
grandes — y su drawdown es **36.03%**, por encima del tope de 35%. B es el diagnóstico, no el
remedio.
