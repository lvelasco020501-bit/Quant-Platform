# M31 — rotación con exposición controlada: el drawdown SÍ se puede controlar, y cuesta ~70% del CAGR

**Estado:** 1D COMPLETO (16 celdas + 26 sondas de sensibilidad, 87 s) · 4H COMPLETO (16 celdas + 4 sondas, 260 s, 311 MB pico) · **1 variante pasa en 1D, 1 variante pasa en 4H, ninguna pasa en ambos** · 2026-09-30

Pre-declaración commiteada en `5c58401`, **antes** de ver un solo resultado. Nada se tocó después.
Las señales de CS2 y RF1 vienen de `RULES_M30` **por referencia**: este milestone no reescribe ni
un lookback, así que no hay copia que pueda derivar. Un test lo comprueba por identidad de objeto.
El VPS no se tocó: esta sesión no abrió una sola conexión.

---

## 1. Referencia sin control

| | CAGR | DD | Calmar | ×2 | ×3 | OOS | turnover |
|---|---|---|---|---|---|---|---|
| CS2 1D | +65.39% | **78.09%** | 0.84 | +56.32% | +47.74% | +127.65% | 339 |
| RF1 1D | +69.02% | **75.02%** | 0.92 | +58.64% | +48.89% | +29.93% | 381 |
| CS2 4H | +95.61% | **73.86%** | 1.29 | +41.04% | +1.64% | **−31.07%** | 1966 |
| RF1 4H | +123.90% | **70.43%** | 1.76 | +56.08% | +8.74% | **−9.22%** | 2169 |

Las cuatro reproducen M30 exactamente donde M30 las midió — el script lo comprueba en cada
corrida y aborta si discrepan. Nótese ya aquí que **a 4H el OOS es negativo sin control**, algo
que a 1D no pasaba.

---

## 2. Resultados por mecanismo — 1D

`Δ` = contra la referencia sin control del mismo señal.

| celda | CAGR | ΔCAGR | DD | ΔDD | Calmar | ×2 | ×3 | OOS | año | activo | turnover | veredicto |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| CS2 sin control | +65.39% | — | 78.09% | — | 0.84 | +56.32% | +47.74% | +127.65% | 0.55 | 0.46 | 339 | ❌ dd, año |
| **CS2 fijo 25%** | +19.53% | **−45.86** | **29.34%** | **−48.75** | 0.67 | +17.69% | +15.88% | +30.83% | 0.38 | 0.40 | 93 | ✅ **PASA** |
| CS2 fijo 50% | +37.96% | −27.43 | 51.11% | −26.98 | 0.74 | +33.87% | +29.91% | +64.07% | 0.36 | 0.41 | 181 | ❌ dd |
| CS2 fijo 75% | +53.77% | −11.62 | 66.90% | −11.19 | 0.80 | +47.20% | +40.91% | +97.33% | 0.47 | 0.39 | 263 | ❌ dd |
| CS2 vol target 72 | +66.34% | **+0.95** | 70.98% | −7.11 | 0.93 | +57.75% | +49.60% | +127.65% | 0.55 | 0.46 | 319 | ❌ dd, año |
| CS2 vol target 24 | +68.49% | **+3.10** | 68.56% | −9.53 | **1.00** | +59.76% | +51.48% | +132.18% | 0.56 | 0.46 | 320 | ❌ dd, año |
| CS2 drawdown ^1 | +45.27% | −20.11 | 61.28% | −16.81 | 0.74 | +38.02% | +31.61% | +83.17% | 0.50 | 0.37 | 261 | ❌ dd, año |
| CS2 drawdown ^2 | +34.12% | −31.27 | 51.48% | −26.61 | 0.66 | +29.23% | +24.54% | +54.83% | 0.46 | 0.28 | 209 | ❌ dd |
| RF1 sin control | +69.02% | — | 75.02% | — | 0.92 | +58.64% | +48.89% | +29.93% | 0.50 | 0.57 | 381 | ❌ dd, año |
| **RF1 fijo 25%** | +20.57% | −48.45 | **27.06%** | −47.96 | 0.76 | +18.48% | +16.42% | +17.67% | **0.5227** | 0.45 | 105 | ❌ **año, por 0.0227** |
| RF1 fijo 50% | +39.98% | −29.03 | 47.88% | −27.14 | 0.84 | +35.32% | +30.81% | +29.65% | 0.50 | 0.49 | 204 | ❌ dd, año |
| RF1 fijo 75% | +56.65% | −12.37 | 63.53% | −11.49 | 0.89 | +49.13% | +41.97% | +33.97% | 0.49 | 0.50 | 296 | ❌ dd |
| RF1 vol target 72 | +64.49% | −4.53 | 75.03% | **+0.00** | 0.86 | +54.90% | +45.86% | +43.09% | 0.45 | 0.57 | 361 | ❌ dd |
| RF1 vol target 24 | +73.95% | **+4.94** | 73.54% | −1.48 | **1.01** | +63.75% | +54.13% | +70.70% | 0.36 | 0.54 | 364 | ❌ dd |
| RF1 drawdown ^1 | +46.81% | −22.21 | 58.99% | −16.03 | 0.79 | +38.44% | +32.06% | +6.56% | 0.66 | 0.49 | 290 | ❌ dd, año |
| RF1 drawdown ^2 | +34.10% | −34.92 | 49.59% | −25.44 | 0.69 | +27.83% | +23.51% | **−3.51%** | 0.76 | 0.48 | 239 | ❌ dd, año, oos |

### Lo que cada mecanismo hace de verdad

**A — Exposición fija: el único que resuelve el problema, y sólo al 25%.** Lleva el DD de 78% a
29% y de 75% a 27%. Cuesta unos 46–48 puntos de CAGR. Es un instrumento tosco: no sabe *cuándo*
hay riesgo, sólo reduce todo por igual.

**B — Volatility targeting: mejora el Calmar y no toca el drawdown.** Es el resultado más
interesante del milestone. `vol target 24` **sube** el CAGR (+3.10 en CS2, +4.94 en RF1) y da los
mejores Calmar de las 16 celdas (1.00 y 1.01), pero el DD baja apenas 1.5–9.5 puntos. La razón es
clara: los drawdowns de esta clase de activo vienen de **bajadas sostenidas**, no de picos de
volatilidad, y en cripto los mercados alcistas son al menos tan volátiles como los bajistas — así
que el mecanismo recorta tamaño en las subidas tanto como en las caídas. Mejora la calidad del
retorno sin resolver el riesgo que se le pidió resolver.

**C — Drawdown-aware: reduce el drawdown a la mitad, insuficiente.** De 78% a 51% con potencia 2.
Y se paga en OOS: RF1 drawdown^2 es la **única celda con OOS negativo en 1D** (−3.51%). Un
mecanismo que desapalanca dentro de la caída sigue desapalancado durante la recuperación, que es
la consecuencia directa de "NO resetear referencias" — tu propia restricción, funcionando como
estaba pedido.

**D — Régimen + exposición** es la mitad RF1 de esta matriz, no un noveno mecanismo: el filtro de
régimen vive en el spec congelado de RF1 y ya lo pone en caja por su cuenta. Sus ocho filas son
precisamente "conservar RF1 exacto, caja cuando el régimen no es favorable, control de exposición
cuando sí".

---

## 3. La variante que pasa en 1D — CS2 fijo 25%, en detalle

| | valor | puerta |
|---|---|---|
| CAGR | **+19.53%** | — |
| max DD | **29.34%** | ≤ 35% ✅ |
| Calmar | **0.67** | ≥ 0.50 ✅ |
| CAGR ×2 coste | +17.69% | > 0 ✅ |
| CAGR ×3 coste | +15.88% | > 0 ✅ |
| OOS total | **+30.83%** | > 0 ✅ |
| OOS CAGR / DD / Calmar | +10.45% / 9.89% / **1.06** | — |
| cuota del mejor año | 0.3795 | ≤ 0.50 ✅ |
| cuota del mejor activo | 0.4021 | ≤ 0.60 ✅ |
| activos positivos | 5 de 6 (sólo XRP, −495) | — |
| años positivos | 7 de 10 | — |
| peor año | **−16.3%** (2018) · 2022 sólo **−8.9%** | — |
| trades / caja / turnover | 170 / 56.3% / 93 | — |
| comisiones | 4 047 sobre 10 000 iniciales | — |
| equity final | **10 000 → 50 160** (4.02×) | — |

Año a año: +21.9%, −16.3%, +43.7%, +30.9%, +79.4%, −8.9%, +19.8%, +7.4%, +24.6%, −0.2%.
**Ningún año hace el resultado.** Es la primera vez en este proyecto que una configuración pasa
una puerta completa.

### RF1 fijo 25% falla por 0.0227, y es mejor en casi todo lo demás

DD **27.06%** (mejor que 29.34%) y Calmar **0.76** (mejor que 0.67). Falla **sólo** `single_year`:
0.5227 contra un tope de 0.50. Es la segunda vez que este proyecto pierde un candidato por una
centésima en una puerta de concentración — M29 perdió ETH con 0.51 contra 0.50.

**No se toca el umbral.** Se declaró antes, y la variante que *sí* pasa es peor en drawdown y en
Calmar que la que se queda fuera: eso es exactamente la situación en la que mover el tope sería
más tentador y más deshonesto. Queda registrado para que la decisión sea tuya y no mía.

---

## 4. Sensibilidad: CS2 fijo 25% no es un pico estrecho

Las sondas son **mediciones de fragilidad, no candidatas**. Se eligen mecánicamente (pasó la
puerta, o falló exactamente una condición), no leyendo la tabla. Una vecina mejor se registra y
**no se adopta**.

| sonda | CAGR | DD | Calmar | año | activo | veredicto |
|---|---|---|---|---|---|---|
| **declarado: fijo 25% / lb 72** | +19.53% | 29.34% | 0.67 | 0.38 | 0.40 | ✅ PASA |
| fijo 20% | +15.66% | 24.13% | 0.65 | 0.38 | 0.39 | ✅ PASA |
| fijo 30% | +23.35% | 34.25% | 0.68 | 0.37 | 0.41 | ✅ PASA |
| lookback 36 | +17.86% | 30.37% | 0.59 | 0.40 | 0.32 | ✅ PASA |
| lookback 144 | +15.07% | 26.31% | 0.57 | 0.44 | 0.60 | ✅ PASA |

**Las cuatro vecinas pasan la puerta completa.** Es el resultado de robustez más fuerte que este
proyecto ha producido.

Para RF1 fijo 25%, `single_year` falla en los tres niveles de exposición (0.5237 / 0.5227 /
0.5201), así que no es un filo: es una propiedad persistente de RF1 a 1D. A lookback 36 pasaría
(0.4728) y a 144 falla sólo concentración por activo. **Ninguno de los dos se adopta** — el 144
está explícitamente fuera de M31 por tu instrucción, y cambiar el 72 porque una sonda salió mejor
es precisamente lo que la pre-declaración impide.

---

## 5. Validación a 4H

> **CORRECCIÓN (M32, `0760e9e`+).** Las cifras de 4H de este informe se midieron con un defecto:
> el score de momentum exigía 72 *índices* de histórico pero no 72 barras *contiguas en el
> calendario*, así que en las series de 4H —que sí tienen huecos, a diferencia de las de 1D—
> algunos retornos de 72 barras cruzaban agujeros de datos. Corregido en M32. **1D no cambia en
> absoluto** (verificado byte a byte). A 4H:
>
> | | antes | después |
> |---|---|---|
> | RF1 fijo 25% · DD | 22.79% | **21.67%** |
> | RF1 fijo 25% · Calmar | 1.35 | **1.40** |
> | RF1 fijo 25% · CAGR | +30.75% | +30.38% |
> | RF1 sin control · DD | 70.43% | 67.95% |
>
> **El veredicto de M31 no cambia**: ninguna celda cambia de PASA a FALLA ni al revés, y RF1 fijo
> 25% sigue pasando, con mejor DD y mejor Calmar. **Lo que sí cambia es una afirmación de
> sensibilidad de este informe:** la vecina `lookback 144` a 4H pasaba con DD 34.50% y ahora
> falla con **36.62%**, así que el vecindario de RF1 a 4H es **3 de 4**, no 4 de 4. La tabla de
> abajo conserva los números originales; los corregidos están en
> `var/research/m31/screen_4h.json`.

Autorizada por tu regla condicional ("si alguna pasa: recién entonces validar 4H y sensitivity").

| celda | CAGR | DD | Calmar | ×2 | ×3 | OOS | año | activo | turnover | final | veredicto |
|---|---|---|---|---|---|---|---|---|---|---|---|
| CS2 sin control | +95.61% | 73.86% | 1.29 | +41.04% | +1.64% | **−31.07%** | 1.19 | 0.68 | 1966 | 429.7× | ❌ ×4 |
| **CS2 fijo 25%** | +24.87% | **26.42%** | 0.94 | +14.71% | +5.37% | **−2.49%** | **0.5464** | 0.34 | 511 | 7.4× | ❌ **año, oos** |
| CS2 fijo 50% | +50.37% | 46.88% | 1.07 | +27.15% | +7.50% | −9.12% | 0.67 | 0.44 | 1009 | 39.9× | ❌ |
| CS2 drawdown ^2 | +62.94% | 49.10% | 1.28 | +31.29% | +16.79% | +3.77% | 0.73 | 0.37 | 1055 | 82.4× | ❌ dd, año |
| RF1 sin control | +123.90% | 70.43% | 1.76 | +56.08% | +8.74% | −9.22% | 0.93 | **1.21** | 2169 | 1456.0× | ❌ ×4 |
| **RF1 fijo 25%** | **+30.75%** | **22.79%** | **1.35** | +18.95% | +8.22% | **+6.23%** | 0.40 | 0.37 | 569 | 11.3× | ✅ **PASA** |
| RF1 fijo 50% | +63.51% | 42.19% | 1.51 | +35.73% | +12.65% | +6.65% | 0.52 | 0.50 | 1121 | 85.0× | ❌ dd, año |
| RF1 vol target 24 | +92.16% | 60.42% | 1.53 | +35.56% | **−4.42%** | −24.25% | 1.02 | 1.05 | 2098 | 365.7× | ❌ ×5 |

Sondas de RF1 fijo 25% a 4H: fijo 20% ✅, fijo 30% ✅, lookback 36 ✅, lookback 144 ✅ (DD 34.50%,
al filo). **Las cuatro pasan.**

### El resultado incómodo: ninguna configuración pasa en los dos timeframes

**CS2 fijo 25% pasa en 1D y falla su validación a 4H** (`single_year` 0.5464, OOS −2.49%).
**RF1 fijo 25% falla en 1D y pasa en 4H.** No hay una sola celda que sobreviva a ambos.

Eso hay que leerlo con cuidado en las dos direcciones:

- **A favor de no descartar el 1D:** el lookback son 72 **barras**, no 72 días. A 1D son ~2.4
  meses; a 4H son 12 días. 4H no es "la misma estrategia con más resolución": es una estrategia
  bastante más rápida. Que la versión rápida falle no invalida la lenta como lo haría un OOS malo.
- **En contra:** si el edge fuese estructural, esperaríamos que sobreviviera en ambas escalas al
  menos con el mismo mecanismo de exposición, y no lo hace. Y a 4H el OOS es negativo en 10 de 16
  celdas, incluidas las dos sin control — señal de que ese edge se está degradando en los años
  recientes.

### Otras banderas de 4H

- **El ×3 rompe celdas a 4H** que a 1D sobrevivían holgadas: vol target va a −4.42% y −0.98%. Con
  turnover de ~2100 y 1085 trades, el coste sí es el cuello de botella a 4H.
- **RF1 sin control tiene concentración por activo de 1.21**: un activo aporta más que el total, o
  sea que el resto en conjunto pierde.
- **El OOS de la ganadora a 4H es fino**: +6.23% total pero sólo **+2.26% anualizado**, Calmar
  0.12. Pasa la puerta tal como se declaró ("OOS positivo") y no con holgura.

---

## 6. Cuánto se sacrifica y cuánto se reduce

Para llevar el drawdown bajo el 35%, con el único mecanismo que lo consigue:

| | CAGR antes | CAGR después | sacrificio | DD antes | DD después | reducción | Calmar |
|---|---|---|---|---|---|---|---|
| CS2 1D | +65.39% | +19.53% | **−70% relativo** | 78.09% | 29.34% | **−62% relativo** | 0.84 → 0.67 |
| RF1 4H | +123.90% | +30.75% | **−75% relativo** | 70.43% | 22.79% | **−68% relativo** | 1.76 → 1.35 |

**El Calmar empeora en los dos casos.** Eso estaba pre-declarado como aritmética, no como
hallazgo: escalar la exposición escala el camino logarítmico, y el Calmar *aritmético* no sobrevive
la transformación porque comprime un retorno por `exp` y un drawdown por `1 − exp`.

**Mi estimación previa fue conservadora y se equivocó en la dirección segura.** Predije ~32% de DD
y ~0.42 de Calmar para CS2 al 25%; salió 29.34% y **0.67**. El Calmar aguantó mucho mejor de lo
que la geometría pura sugería, porque CS2 sólo está invertido el 43.7% del tiempo y el argumento
multiplicativo no aplica limpio a una cuenta que pasa más de la mitad del tiempo en caja. Si la
predicción hubiera sido optimista en vez de conservadora, la variante habría "pasado" por un
error mío; lo digo porque registrar una predicción y acertar a medias no es lo mismo que acertar.

---

## 7. Veredicto y GO / NO-GO

| | 1D | 4H |
|---|---|---|
| CS2 fijo 25% | ✅ **PASA**, con las 4 vecinas | ❌ año 0.5464, OOS −2.49% |
| RF1 fijo 25% | ❌ año 0.5227 (por 0.0227) | ✅ **PASA**, con las 4 vecinas |
| todo lo demás (14 celdas × 2 tf) | ❌ | ❌ |

**Ningún umbral se rebajó. Ninguna señal se cambió. El 144 no se adoptó.**

### GO, estrecho y condicionado

**Recomiendo GO — no cerrar rotation — pero el siguiente paso no es más búsqueda.**

El objetivo mínimo que fijaste se cumplió: el drawdown baja claramente del 35% (29.34% a 1D,
22.79% a 4H) sin destruir el edge, y por primera vez una configuración pasa una puerta completa
*y* su vecindario. Eso es información nueva y real.

Pero lo que la cerraría o la confirmaría no es probar más mecanismos de exposición. Son dos cosas
que no son búsqueda de parámetros:

1. **El sesgo de supervivencia, que es el riesgo dominante y sigue sin modelar.** Estos seis son
   los large caps de 2026. Un test con el universo que *existía* en cada fecha es lo único que
   puede decir si esto es un edge o un artefacto de selección. Si no sobrevive eso, nada más
   importa.
2. **Portar la ganadora al motor de producción con Risk V2** antes de que se acerque a paper. Todo
   M30/M31 corre en un segundo camino de backtest sin sizing por stop, sin breakers y sin rechazo
   de órdenes. El número que importa para una decisión de paper es el que dé ese motor, no éste.

**NO-GO explícito para:** ajustar el 72, adoptar el 144, mover el tope de `single_year`, o buscar
un nivel de exposición mejor que el 25%. Todo eso convertiría un resultado pre-declarado en una
búsqueda post-hoc.

**Ningún PAPER CANDIDATE.** Falta la corrección de supervivencia y falta el motor de producción.

---

## 8. Limitaciones

1. **Sesgo de supervivencia, sin modelar y dominante.** Ver §7. Ninguna cifra de este informe está
   libre de él.
2. **Segundo camino de backtest, sin la certificación del motor de producción.** Sin Risk V2, sin
   stops, sin sizing por riesgo. Validado contra aritmética a mano (buy & hold BTC: +1645.74% /
   83.19% DD, idéntico al cálculo directo sobre el CSV) y con 44 tests que fijan sus invariantes.
3. **El overlay de exposición no puede cambiar qué activo se tiene**, y hay un test que lo
   comprueba comparando las secuencias de episodios bajo exposición plena, al 25% y drawdown-aware.
   Sin esa propiedad, "el drawdown era controlable" y "se encontró otra estrategia" serían
   indistinguibles.
4. **El pase OOS reinicia la referencia de drawdown.** Empieza en 2024-01-01 con pico = capital
   inicial, así que las variantes drawdown-aware entran al OOS sin arrastrar el pico de 2021. Es un
   trato favorable para el mecanismo C y no está corregido.
5. **La caja no renta.** El 56–75% del capital que no está invertido no gana nada; no se asume
   ningún retorno de money market. Subestima todas las variantes controladas.
6. **Sin modelo de liquidez ni de impacto.** El 25% de la cuenta en SOL en 2020 supone que ese
   tamaño se llena a 15 bps.
7. **Costes modelados, no medidos** del venue. Lo declarado fue supervivencia a ×2 y ×3.
8. **Sin walk-forward en M31.** No estaba entre las siete condiciones que declaraste, y añadir una
   condición después de los hechos es la misma falta que quitarla. M30 sí lo corrió para CS2 y RF1
   sin control: 5 de 5 ventanas positivas en ambas.
9. **72 barras significa algo distinto en cada timeframe** (~2.4 meses a 1D, 12 días a 4H). La
   comparación entre timeframes es entre dos velocidades, no entre dos resoluciones de lo mismo.

---

## 9. Reproducir

```bash
uv run python scripts/m31_screen.py --sensitivity                  # 1D: 16 celdas + 26 sondas, ~87 s
uv run python scripts/m31_screen.py --timeframe 4h --sensitivity   # 4H: 16 celdas + 4 sondas, ~260 s
```

Evidencia en `var/research/m31/screen_1d.json` y `screen_4h.json` (fuera del repo, como todo
`var/`). El script aborta si las celdas sin control dejan de reproducir M30 exactamente.

**Nota de rendimiento:** el cálculo de scores era cuadrático en la longitud del histórico (cortaba
la serie desde el origen en cada barra cuando el pipeline sólo lee las últimas `required_history`).
Se corrigió a lineal, lo que hizo 4H viable. La corrección se verificó reproduciendo el JSON de 1D
**byte a byte** antes y después.
