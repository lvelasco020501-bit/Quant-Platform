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

---

# Fase 2 — alternativa estructural

## ALT1: **NO CONSTRUIBLE**, y el límite que la rechaza es un hallazgo

ALT1 (stop de seguridad en `max_stop_distance_bps` = 2000 bps) corrió tal como estaba declarado
y dio **0 trades en los 12 pares**: 211 rechazos de entrada con *"the stop is further than
max_stop_distance_bps permits"*.

**Mecanismo.** El motor deriva el **precio** del stop, lo redondea al tick del venue
**alejándolo** de la entrada (dirección conservadora y correcta), y luego **re-deriva la
distancia desde ese nivel redondeado** y la compara contra el límite del budget. Un stop
configurado exactamente en el límite realiza marginalmente *fuera* de él, y la entrada se
rechaza:

| entry | stop redondeado | distancia realizada | veredicto |
|---|---|---|---|
| 100.00 | 80.00 | 2000.000000 bps | ok |
| 23.456 | 18.76 | 2002.046385 bps | **RECHAZADO** |
| 0.4567 | 0.36 | 2117.363696 bps | **RECHAZADO** |
| 61 234.57 | 48 987.65 | 2000.000980 bps | **RECHAZADO** |

12 de 16 combinaciones precio/tick rechazan; en la práctica **todos** los precios reales, porque
solo sobrevive un precio cuyo nivel al 80% cae exacto en un tick.

### BUG documentado (no arreglado en producción)

**`max_stop_distance_bps` se lee como límite inclusivo y se comporta como exclusivo.**

- Dónde: `src/quantplatform/risk/sizing.py`, `_check_distance_window` —
  `if distance_bps > budget.max_stop_distance_bps: raise`.
- Causa: la distancia comparada es la **realizada** (post-redondeo a tick), no la **configurada**.
  El redondeo conservador siempre aleja el stop, así que la realizada ≥ la configurada.
- Efecto: una configuración legal (`initial_stop_distance_bps == max_stop_distance_bps`, que el
  validador de `RiskConfiguration` acepta) **rechaza el 100% de las entradas en runtime**. Falla
  cerrado, que es lo correcto en seguridad, pero de forma indistinguible de "la estrategia no
  quiso entrar".
- **No se corrigió.** Research only; la capa de riesgo no se tocó. Va a la propuesta Risk V3.

ALT1 **queda declarado**, no borrado, con su fallo registrado como dato en `INFEASIBLE` y con
tests que verifican que el registro existe y que ALT1 sigue declarado. Su fila en el informe se
reporta como **NOT CONSTRUCTIBLE**, no como un rechazo por rendimiento: nunca abrió una posición,
así que no se midió nada.

## ALT2: re-declaración — viabilidad, no ambición

Misma estructura, con la distancia tomada de la **convención de duplicado del propio proyecto**
(la que usa M22 en `Horizon.DOUBLED`, y M29 y M30 en sus vecinos de sensibilidad) aplicada al
stop que reemplaza: **600 → 1200 bps**. No es un número elegido por rendimiento; es la forma
establecida en este código de ensanchar un parámetro. Se comprobó antes **solo por viabilidad**
(22 trades contra los 0 de ALT1, sin rechazos de budget), nunca por retorno, y se commiteó
**antes** de correrse.

La distancia es **dirigida por regla, no por valor**: `stop_distance_for` toma un `StopRule`, no
tiene ninguna rama que devuelva un literal, rechaza una distancia que no sea más ancha que la que
reemplaza, y rechaza una que exceda el máximo del budget **en vez de recortarla** — recortar
produciría silenciosamente un stop que la declaración nunca describió.

Coste declarado de antemano: la posición pasa de 16.7% a **8.33%** del equity, porque el sizing
por riesgo divide el budget por la distancia del stop. Un test fija esa relación.

## BASE vs ALT2

| medida | BASE | **ALT2** | SIG (solo señales) |
|---|---|---|---|
| CAGR | +14.41% | **+33.58%** | +43.19% |
| DD máximo | 22.15% | **33.84%** | 23.65% |
| Calmar | 0.65 | **0.99** | 1.83 |
| retorno total | 2.38× | **12.69×** | 24.63× |
| turnover | 397.8 | **193.8** | 192.9 |
| fees | 12 219 | 18 864 | 31 584 |
| re-entries | 1 350 | **16** | — |
| stops | 1 778 | **103** | — |
| forced exits | 2 134 | **103** | — |
| exposición | 41.3% | 56.7% | 58.7% |
| **held % de lo que la señal quería** | 47.3% | **91.6%** | 100% |
| episodios | 2 301 | 958 | 947 |
| OOS retorno | +47.53% | **+69.33%** | +75.07% |
| OOS DD | 9.65% | 19.51% | 16.20% |
| stress ×2 | +7.10% | **+29.35%** | +38.67% |
| stress ×3 | +0.25% | **+25.26%** | +34.30% |
| mejor año / beneficio | 30.24% | 43.30% | 38.72% |

Composición de salidas: BASE `{protective_stop: 1778, take_profit: 304, time_stop: 52}` →
ALT2 `{protective_stop: 103}`. **Una sola vía de salida de riesgo, y usada 17× menos.**

### Sobre las fees, que suben mientras el turnover baja

No es una contradicción. El turnover cae a la mitad (397.8 → 193.8) pero las fees absolutas
suben 1.54× **porque la cuenta creció 5.34×** (retorno total 2.38× → 12.69×). Las fees son
moneda absoluta sobre un equity mucho mayor: por unidad de turnover, BASE paga 30.7 y ALT2 97.4,
que es exactamente el mismo coste proporcional aplicado a una cuenta más grande. ALT2 **opera la
mitad y gana cinco veces más**; paga más comisión en términos absolutos por eso, no por churn.

## ALT2 contra los criterios predeclarados

| criterio | umbral | ALT2 | |
|---|---|---|---|
| protección clara contra pérdidas grandes | existe un stop | stop duro a 1200 bps | **PASA** |
| reduce turnover sustancialmente | ≥ 25% | **51.3%** | **PASA** |
| OOS positivo | > 0 | +69.33% | **PASA** |
| stress positivo | ×2 y ×3 > 0 | +29.35% / +25.26% | **PASA** |
| DD controlado | ≤ 35% | **33.84%** | **PASA** |
| no introduce ratchet | — | trailing eliminado; sin latch | **PASA** |
| no aumenta fragilidad | Calmar ≥ 0.50 | **0.99** | **PASA** |
| conserva mucho más del edge | ≥ 50% de la brecha | **66.6%** | **PASA** |

**ALT2 sobrevive los ocho criterios.** Ninguno se ajustó: todos estaban fijados en
`m37.py` y `m37_alternatives.py` antes de correr.

### Las dos preguntas directas

**¿Conserva claramente más del edge de señal?** Sí. Recupera **66.6%** de los 28.78 puntos de
brecha BASE→SIG (bar predeclarado: 50%). CAGR +14.41% → +33.58% contra un techo de señal de
+43.19%. Y mantiene posición en el **91.6%** de las barras en que la estrategia la quería, contra
el 47.3% de BASE — el edge se conserva porque la cuenta **está en el mercado cuando la regla lo
pide**.

**¿Mantiene protección razonable?** Sí, con una salvedad honesta. Hay un stop duro real a 1200
bps que cierra cualquier posición antes de que la pérdida sea ilimitada, no hay ratchet, y los
breakers siguen exactamente como están desplegados. Pero el **drawdown sube de 22.15% a 33.84%**
— dentro del tope de 35%, y a 1.16 puntos de él. La protección es real pero **más laxa**: se
compra +19.2 puntos de CAGR con +11.7 puntos de drawdown. Eso es una mejora clara en Calmar
(0.65 → 0.99) y no un almuerzo gratis.

## Veredicto M37

**GO para diseño de Risk V3 — research only. Sin desplegar.**

Dos incompatibilidades concretas quedan demostradas:

1. **El stop protector gestionado destruye este edge.** Cierra el 92.7% de las posiciones, genera
   1 350 re-entries, cuadruplica el turnover y deja la cuenta fuera del mercado el 52.7% del
   tiempo en que la estrategia la quiere dentro. Sus tres partes no son separables porque son un
   solo nivel de stop con tres formas de moverse.
2. **`max_stop_distance_bps` es inalcanzable como distancia configurada**, por comparar la
   distancia realizada post-redondeo contra el límite.

Y una alternativa estructural mínima pasa todos los criterios predeclarados sobre este universo.

**Lo que esto NO autoriza:** desplegar nada. Los números de M37 son sobre 6 mercados a breadth 6
—que nunca selecciona— y **no son comparables con M36**. Una Risk V3 tendría que re-validarse
sobre el universo completo y bajo el gate del proyecto antes de acercarse a paper.
