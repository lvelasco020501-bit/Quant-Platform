# M30 fase 1 — criba de rotación a 1D: ninguna pasa, línea cerrada, 4H no se corre

**Estado:** CRIBA 1D COMPLETA · 6 reglas · 66 simulaciones · 21 s · 1 worker · 91 MB pico de RSS · **ninguna configuración pasa la puerta** · **4H CANCELADO por la stop rule** · 2026-09-30

Umbrales y parámetros pre-declarados y commiteados en `c448a92`, **antes** de ver un solo
resultado. Nada se tocó después. Las dos sesiones paper del VPS quedaron intactas: esto corrió
entero en el Mac y no tocó ningún servicio, checkout ni estado de paper.

---

## 1. La pregunta y el cambio de espacio

M29 agotó un espacio de búsqueda: catorce configuraciones de siete familias mono-activo, dos
timeframes, cuatro mercados, y nada superó una puerta calibrada para admitir al incumbente que ya
teníamos. M30 no ajusta nada de eso. Cambia la pregunta:

> En vez de "¿cuándo entro en BTC?", **"¿qué activo tiene la mejor oportunidad relativa ahora?"**

Eso es un tipo de regla distinto. Toda estrategia que esta plataforma ha corrido mira la historia
de **un** símbolo. Una regla de rotación mira **entre** símbolos en el mismo instante. El edge que
persigue no es una entrada mejor: es la dispersión entre activos, una cantidad que ningún backtest
mono-activo puede ver.

---

## 2. Los datos

**Nada se descargó.** Los seis activos — ADA y XRP incluidos — ya fueron bajados y validados por
M16 contra dos archivos con checksum más la API del exchange. 1D sale de esos mismos CSV de 1h por
el resampler que M29 ya usó.

| activo | barras 1D | desde | lineage sha256 | outages | determinista | digest == M29 |
|---|---|---|---|---|---|---|
| BTCUSDT | 3301 | 2017-09-01 | ✅ | 127 = 127 | ✅ | ✅ `a247cd2196e2388a` |
| ETHUSDT | 3301 | 2017-09-01 | ✅ | 127 = 127 | ✅ | ✅ `abeb2a55d25e3423` |
| BNBUSDT | 3210 | 2017-12-01 | ✅ | 121 = 121 | ✅ | ✅ `381867b1d73e6859` |
| SOLUSDT | 2205 | 2020-09-01 | ✅ | 19 = 19 | ✅ | ✅ `4a00773c9bf52ac3` |
| ADAUSDT | 3059 | 2018-05-01 | ✅ | 87 = 87 | ✅ | nuevo `d50d2e9e2ef438af` |
| XRPUSDT | 3028 | 2018-06-01 | ✅ | 87 = 87 | ✅ | nuevo `0e750775e89a1c35` |

**Las dos comprobaciones que importan.** Los cuatro activos de M29 salen con **digest idéntico
bit a bit**, así que las cifras de M29 y las de M30 descansan sobre exactamente la misma serie
diaria. Y BTC contra la serie diaria que M15 construyó por otra ruta: **2449 barras solapadas,
2449 con OHLC idéntico, cero discrepancias.**

---

## 3. Los parámetros, todos heredados

M30 no elige un solo número nuevo en la regla. Cada parámetro viene de una estrategia que el
proyecto ya declaró, y un test falla si deja de coincidir:

| parámetro | valor | heredado de |
|---|---|---|
| lookback del ranking | 72 barras | `momentum_roc.lookback`, sin cambio desde M13 |
| ventana de volatilidad | 72 barras | `vol_momentum.vol_window` |
| filtro de régimen | 200 barras | `breakout_trend.trend_period` — el 200 que B2 usa en paper |
| segundo filtro de régimen | 400 barras | regla mecánica `doubled()` de M22 |
| umbral normalizado | 1.0 | `vol_momentum.threshold` |
| sensibilidad | 36 / 144 | mitad y doble del lookback, misma regla mecánica |
| coste por lado | **15 bps** | fee 10 + slippage 5 de la execution policy de M22–M29 |

**La frecuencia de rebalanceo se eliminó en vez de ajustarse.** El objetivo se recalcula cada
barra y sólo se opera cuando el conjunto a mantener cambia, así que el turnover es una *salida* de
la regla y nunca un mando sobre ella.

Sobre el coste: los 2 bps de `assumed_spread_basis_points` que esas definiciones llevan **no** se
cobran aquí, porque en el motor son una métrica que alimenta al risk engine y nunca mueven un
precio de fill. Cobrarlos sería inventar un coste que el motor no cobra.

---

## 4. Las seis reglas

| clave | familia | mantiene | score | umbral | filtro |
|---|---|---|---|---|---|
| RS1 | relative strength | top 1 | momentum 72 | — | — |
| RS2 | relative strength | top 2 | momentum 72 | — | — |
| CS1 | cross-sectional | top 1 | momentum / (vol·√72) | > 0 | — |
| CS2 | cross-sectional | top 1 | momentum / (vol·√72) | > 1.0 | — |
| RF1 | regime-filtered | top 1 | momentum 72 | — | cesta > SMA 200 |
| RF2 | regime-filtered | top 1 | momentum 72 | — | cesta > SMA 400 |

Dentro de cada familia se mueve **exactamente una** dimensión, así que una diferencia entre dos
variantes tiene una única causa posible. Hay un test que lo comprueba.

---

## 5. Resultados

Historia completa, 15 bps por lado. `años+` = proporción de años positivos · `mejor año` = cuota
del mejor año sobre el beneficio neto (tope 0.50) · `top activo` = cuota del mejor activo (tope
0.60) · `wf` = ventanas walk-forward positivas.

| | CAGR | **DD** | Calmar | PF | trades | expo | caja | ×2 | ×3 | OOS | wf | años+ | mejor año | top activo | activos+ | puerta |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| RS1 | +53.95% | **94.80%** | 0.57 | 1.10 | 320 | 97.8% | 2.2% | +38.43% | +24.46% | **−0.61%** | 0.60 | 0.70 | **2.40** | **1.74** | 4/6 | ❌ |
| RS2 | +46.23% | **87.71%** | 0.53 | 1.16 | 411 | 97.8% | 2.2% | +35.69% | +25.90% | +15.09% | 0.60 | 0.60 | **1.34** | **1.40** | **2/6** | ❌ |
| CS1 | +54.46% | **83.85%** | 0.65 | 1.25 | 233 | 72.0% | 28.0% | +42.97% | +32.32% | +10.98% | 0.80 | 0.70 | **0.91** | **1.03** | 4/6 | ❌ |
| **CS2** | +65.39% | **78.09%** | 0.84 | 1.93 | 170 | 43.7% | 56.3% | +56.32% | +47.74% | +127.65% | 1.00 | 0.70 | **0.55** | 0.46 | 6/6 | ❌ |
| **RF1** | +69.02% | **75.02%** | **0.92** | 1.42 | 191 | 50.6% | 49.4% | +58.64% | +48.89% | +29.93% | 1.00 | 0.80 | **0.5023** | 0.57 | 4/6 | ❌ |
| RF2 | +47.15% | **86.92%** | 0.54 | 1.48 | 191 | 52.4% | 47.6% | +38.09% | +29.58% | +99.99% | 1.00 | **0.50** | **0.55** | **0.61** | 4/6 | ❌ |

### Benchmarks

| | CAGR | DD | Calmar | trades | turnover |
|---|---|---|---|---|---|
| buy & hold BTC | +37.21% | 83.19% | 0.45 | 1 | 1.0 |
| cesta equiponderada | +56.39% | 85.21% | 0.66 | 6 | 3.7 |
| **B2** breakout_trend 40/20/400 / BTC 1D (M29) | 1.04% | 2.03% | 0.51 | 75 | — |
| B1 breakout_trend 20/10/200 / BTC 1D (M29) | 1.47% | 2.48% | 0.59 | 91 | — |
| **regime_trend** 72/72/0.30 / BTC 1D (M29) | 1.28% | 3.35% | 0.38 | 58 | — |

**B2 es la sesión en paper**, con sus parámetros exactos (40/20/400): CAGR 1.04%, DD 2.03%,
Calmar 0.51, PF 1.97, exposición 14.8%, OOS +0.77%, y +1.04%→+0.87%→+0.74% a costes ×1/×2/×3.
B1 se incluye porque es la variante base de la misma familia y la que M29 midió como candidata.

Los tres vienen del **motor de producción**, no de este simulador, y operan con sizing por stop y
Risk V2. Sus cifras no son comparables barra a barra con las de arriba: miden una cantidad
distinta (una posición dimensionada por riesgo, que casi nunca está totalmente invertida) sobre un
solo activo. Están aquí porque son el incumbente, no porque la comparación sea directa — y la
diferencia de escala lo dice todo: el incumbente gana 1% al año con 2% de drawdown, y la rotación
sin control gana 65% con 78%.

### Por qué falla cada regla

| regla | razones |
|---|---|
| RS1 | drawdown, **asset_concentration**, single_year, **oos_negative** |
| RS2 | drawdown, **single_asset**, asset_concentration, single_year |
| CS1 | drawdown, asset_concentration, single_year |
| **CS2** | **drawdown, single_year** — y nada más |
| **RF1** | **drawdown, single_year** — y single_year por 0.0023 |
| RF2 | drawdown, asset_concentration, single_year, inconsistent_years |

---

## 6. El hallazgo que reencuadra el veredicto

**La puerta también rechaza buy & hold de BTC y la cesta equiponderada.** Aplicada tal cual a los
benchmarks:

| | veredicto | razones |
|---|---|---|
| buy & hold BTC | ❌ | drawdown, low_calmar, single_asset, asset_concentration, single_year |
| cesta equiponderada | ❌ | drawdown, single_year |

Eso cambia lo que significa el fallo. El tope de drawdown de **35%** es de M22, donde se fijó para
una estrategia **mono-activo con stop y con Risk V2**. Una cartera long-only totalmente invertida
en spot cripto a 1D corre a **75–95% de drawdown**, y BTC solo corre a 83%. Ninguna regla de
rotación long-only sin sizing ni stops puede acercarse a 35%: no es un veredicto sobre la rotación,
es una medición sobre la clase de estrategia.

**No se toca el umbral.** Se declaró antes, rechaza por igual a los candidatos y al incumbente
pasivo, y moverlo ahora para dejar pasar algo sería exactamente lo que M29 se negó a hacer con el
0.51 de ETH. Queda registrado como lo que es: la puerta y esta clase de estrategia miden riesgos
incompatibles, y decidir qué hacer con eso es tuyo, no mío.

---

## 7. Lo que estos números enseñan

**2021 explica casi todo.** Es el hallazgo más limpio de la criba. Año a año:

| | 2017 | 2018 | 2019 | 2020 | **2021** | **2022** | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|---|---|---|---|---|
| RS1 | +130% | −39% | +64% | +318% | **+1215%** | **−85%** | +146% | +49% | +40% | −48% |
| RS2 | +148% | −63% | +43% | +227% | **+937%** | **−77%** | +147% | +91% | −20% | −19% |
| CS1 | +130% | −55% | +316% | +115% | **+492%** | **−66%** | +126% | +11% | +22% | −11% |
| CS2 | +103% | −58% | +265% | +111% | **+383%** | **−32%** | +80% | +16% | +115% | −2% |
| RF1 | +0% | +11% | +37% | +226% | **+1157%** | **−12%** | +50% | +27% | +9% | +1% |
| RF2 | +0% | +0% | −34% | +118% | **+1215%** | **−55%** | +79% | +49% | +46% | +0% |
| cesta | +336% | −59% | +38% | +169% | +943% | −77% | +168% | +96% | −15% | −18% |

Las seis fallan `single_year`, y por eso: un año aporta entre el 50% y el 240% del beneficio neto
total. El 2.40 de RS1 significa que 2021 aportó 2.4 veces el resultado y el resto de años, en
conjunto, perdió el resto.

**El filtro de régimen es lo más eficaz que se ha medido en este proyecto.** RF1 tiene el mejor
Calmar (0.92), el menor drawdown (75.02%), y en 2022 pierde **−12%** cuando la cesta pierde −77% y
RS1 pierde −85%. Reduce la exposición al 50.6% y el daño del bear a una sexta parte. Es un
resultado grande y coherente.

**La caja es una posición y paga.** CS2 pasa el 56.3% del tiempo fuera del mercado y es la única
regla que pasa la puerta de concentración por activo (0.46) con **6 de 6 activos positivos**. Su
OOS es +127.65% con drawdown de 36.35% — una décima parte del exceso que tiene en la muestra
completa.

**La sensibilidad es sólida, y es la primera vez que este proyecto llega a medirla.** A 36, 72 y
144 barras las seis reglas siguen positivas y con PF ≥ 1.10:

| | 36 | 72 (declarado) | 144 |
|---|---|---|---|
| RS1 | +48.06% / 1.10 | +53.95% / 1.10 | +34.88% / 1.38 |
| CS2 | +57.87% / 1.25 | +65.39% / 1.93 | +44.56% / 2.69 |
| RF1 | +57.76% / 1.35 | +69.02% / 1.42 | **+77.19% / 1.85** |

Ninguna es un pico estrecho. RF1 mejora con el lookback doblado, lo que sugiere que 72 no es su
óptimo — y no se toca, porque ajustar el parámetro después de ver el resultado es precisamente lo
que la pre-declaración existe para impedir.

**Los costes no son el cuello de botella a 1D.** Las seis siguen claramente positivas a ×3
(+24% a +49% de CAGR). Contraste con M29 a 4H, donde trend moría a ×2 en los cuatro activos. Pero
el turnover es enorme en términos absolutos: RS1 rota 639 unidades de peso y paga 357 769 en
comisiones sobre un capital inicial de 10 000 — porque el capital compone y las comisiones tardías
son grandes. Con ~10 barras de tenencia media, es una estrategia que opera mucho.

**Sí: seleccionar dinámicamente bate a mantener todo — en términos de riesgo/retorno.** Dos reglas
superan el Calmar de la cesta estática (0.66): **RF1 con 0.92 y CS2 con 0.84**. Esa es la pregunta
final del milestone y la respuesta es un sí cualificado. Cualificado porque ambas siguen con
drawdown del 75–78%, el doble del tope declarado.

**XRP es un lastre consistente** (negativo en 5 de 6 reglas; −585 743 en RF1) y **BNB en 4 de 6**.

---

## 8. Walk-forward

Cinco ventanas contiguas, la misma regla en todas. Mide **estabilidad entre ventanas**, no edge
out-of-sample: la configuración la eligió alguien que ya había visto todas.

| | 2017-09→2019-06 | 2019-06→2021-04 | 2021-04→2023-02 | 2023-02→2024-11 | 2024-11→2026-09 |
|---|---|---|---|---|---|
| RS1 | +377.1% (37) | +1117.2% (68) | **−50.9%** (72) | +140.3% (82) | **−23.3%** (64) |
| RS2 | +236.7% (37) | +755.2% (91) | **−47.4%** (90) | +213.3% (110) | **−31.8%** (91) |
| CS1 | +439.7% (24) | +429.9% (66) | **−22.4%** (43) | +86.8% (66) | +23.9% (37) |
| CS2 | +249.5% (19) | +479.2% (54) | +17.1% (24) | +65.6% (51) | +142.6% (25) |
| RF1 | +71.4% (19) | +1651.4% (42) | +106.1% (29) | +68.5% (75) | +17.1% (29) |
| RF2 | +11.2% (15) | +685.6% (47) | +7.0% (35) | +135.6% (76) | +56.8% (20) |

CS2, RF1 y RF2 son positivas en **5 de 5**. Las tres que no llevan filtro ni umbral caen en la
ventana del bear de 2022.

---

## 9. Clasificación

| familia | 1D |
|---|---|
| relative strength (RS1, RS2) | **REJECT** — sin caja ni filtro, 88–95% de DD y OOS plano o negativo |
| cross-sectional (CS1) | **REJECT** — concentración 1.03 y un año al 0.91 |
| cross-sectional (CS2) | **WEAK** — falla sólo drawdown y single_year; mejor perfil de robustez del proyecto |
| regime-filtered (RF1) | **WEAK** — mejor Calmar (0.92), falla single_year por 0.0023 |
| regime-filtered (RF2) | **REJECT** — 0.50 de años positivos, concentración 0.61 |

Ningún PROMISING. **Ningún PAPER CANDIDATE.** **No se rebajó ningún criterio.**

Por la stop rule declarada antes de correr:

> Si ninguna pasa en 1D: STOP. No intentar salvarla ajustando parámetros.

**4H no se corre.** No se ajusta ningún parámetro para hacer pasar nada.

---

## 10. Limitaciones, dichas antes de que alguien las encuentre

1. **Sesgo de supervivencia, y es el grande.** Los seis son los large caps de hoy, elegidos con
   retrospectiva. Un estudio de rotación es exactamente el diseño que ese sesgo favorece: los
   activos que habrían hundido una cartera real de 2018 son los que nadie lista en 2026. Ninguna
   cifra de este informe está libre de eso.
2. **Este es un segundo camino de backtest, sin la certificación del motor de producción.** No
   corre Risk V2, no dimensiona por stop, no rechaza órdenes. Reusa
   `IndicatorFeatures` para todos los scores (así que un retorno de 72 barras es la misma cantidad
   que opera `momentum_roc`) y cobra los mismos 15 bps. Se validó contra aritmética hecha a mano:
   buy & hold de BTC sale **+1645.74% y 83.19% de DD**, idéntico al cálculo directo sobre el CSV.
   Sus invariantes — sin lookahead, pesos que derivan, coste sobre el nocional que se mueve, y una
   ley de conservación que ata cada unidad de equity a un episodio por activo — están en 32 tests.
   Esa última encontró un bug real: el índice de la cesta se comía su primer movimiento.
3. **La ejecución ocurre en el cierre de la decisión.** Es la convención estándar en trabajo de
   momentum y es la única suposición optimista del simulador. Se dice, no se esconde.
4. **No hay modelo de liquidez ni de impacto.** Asignar el 100% del capital a ADA en 2018 supone
   que ese tamaño se llena a 15 bps. A capital pequeño es razonable; a capital grande no.
5. **Costes modelados, no medidos** del venue. Lo declarado fue supervivencia a ×2 y ×3.
6. **Sin shorts, sin leverage, spot only**, por instrucción. Un drawdown del 80% en esta clase de
   activo es en parte consecuencia de eso.
7. **El tope de drawdown se aplicó sin cambios desde M22**, donde se fijó para otra clase de
   estrategia. Rechaza también a los benchmarks. Ver §6.
8. **A diferencia de M29, aquí no hay ambigüedad de agregación.** La puerta se aplica a la cartera
   entera, que es una sola serie de equity, y la concentración por activo es una medición
   explícita en vez de un recuento de activos que pasan. Es la limitación de M29 resuelta.

---

## 11. Reproducir

```bash
uv run python scripts/m30_dataset.py   # deriva y verifica 1D de los seis desde los 1h de M16
uv run python scripts/m30_screen.py    # 6 reglas, 66 simulaciones, ~21 s, 91 MB pico
```

Evidencia en `var/research/m30/screen_1d.json` (fuera del repo, como todo `var/`). El screen es
determinista: mismos CSV, mismos números.
