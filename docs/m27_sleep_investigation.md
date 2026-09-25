# M27 — Por qué el Mac dejó que la sesión perdiera velas

**Estado:** INVESTIGACIÓN COMPLETA · **nada relanzado, nada instalado, ninguna configuración del sistema cambiada** · 2026-09-25

---

## 1. Causa confirmada

**Se cerró la tapa del portátil, con batería, a las 19:11 hora local.** Tres minutos después el
feed empezó a caerse y ya no se recuperó de verdad en toda la noche.

La línea que lo dice, de `pmset -g log`:

```
2026-09-24 19:11:52 -0600  Sleep  Entering Sleep state due to 'Clamshell Sleep'
                                  TCPKeepAlive=active  Using Batt (Charge:66%)
```

Y la correlación, al minuto, con el log del feed:

| hora local | hora UTC | evento |
|---|---|---|
| 18:00 | 00:00 | **única barra cerrada procesada** en toda la sesión |
| **19:11** | 01:11 | **`Clamshell Sleep` — tapa cerrada, batería 66%** |
| **19:14** | 01:14 | **primer `receive failed`** |
| 19:14 → 10:00 | 01:14 → 16:00 | **81 caídas, 81 reconexiones, 11 intentos fallidos** |
| 10:00 | 16:00 | `data_gap_error`, sesión detenida |

### Lo que el Mac hizo durante la ventana

En las 12 horas del hueco hubo **99 eventos de Sleep, 97 DarkWake y 101 Wake**, y
**el 100% con batería** — nunca estuvo enchufado:

| causa del sleep | veces |
|---|---|
| `Maintenance Sleep` | 78 |
| `Sleep Service Back to Sleep` | 18 |
| **`Clamshell Sleep`** | **3** |

La tapa cerrada es el disparador; las otras 96 son el ciclo normal de la máquina ya dormida
despertando brevemente para mantenimiento y volviéndose a dormir.

### Los ajustes que lo permitieron

```
sleep         1     ← duerme al MINUTO de inactividad, en batería Y en AC
tcpkeepalive  1
standby       1
powernap      1
hibernatemode 3
displaysleep  0 (batería) / 10 (AC)
```

`sleep 1` es agresivo incluso para un portátil. Pero **no es la causa principal**: aunque
fuera 60, cerrar la tapa duerme la máquina igual.

### Por qué el feed no lo salvó, aunque lo intentó

Esto es lo más interesante del diagnóstico. El feed **no se quedó callado**: detectó cada
caída y reconectó **81 veces**. Lo que pasa es que cada reconexión ocurría en la ventana de
un `DarkWake` de unos segundos, y volvía a morir en cuanto la máquina se dormía otra vez.

`tcpkeepalive=1` empeora la lectura del síntoma: mantiene el socket TCP vivo a nivel de
kernel mientras el sistema duerme, así que **la conexión parece sana y no llegan datos**. El
proceso está congelado; nadie le entrega las velas.

Cuando por la mañana volvió a haber red de verdad, el feed recibió la vela de las 12:00 UTC,
la plataforma detectó **3 velas de 4h ausentes** (00:00, 04:00, 08:00) y se negó a continuar.

**Eso último es la plataforma funcionando bien.** Prefirió morir a operar sobre una serie con
huecos, que es exactamente lo que debe hacer.

### Estado en que quedó (relevante para relanzar)

| | |
|---|---|
| barras procesadas | 1 |
| posiciones | **0** |
| P&L realizado / fees | **0 / 0** |
| USDT bloqueado | **0** |
| balance | 10.000 USDT intactos |

**La sesión murió completamente plana.** Por tanto su historia de mercado **sí es
reutilizable** como fuente de warm-start — `evaluate_warm_start` solo rechaza historias de
sesiones que cargan financial state. Con una sola barra no sirve todavía, pero **el mecanismo
existe, está escrito y funciona**, y eso importa mucho para el punto 5.

---

## 2. Solución recomendada

Tres capas, porque el problema tiene tres causas independientes y ninguna capa cubre a otra.

### Capa 1 — Alimentación y tapa (lo que realmente causó el fallo)

**AC conectado y tapa abierta.** No es un hack; es que un M2 con la tapa cerrada y sin
pantalla externa **siempre** duerme, y eso no lo evita ningún proceso en espacio de usuario.

`caffeinate` **no previene el clamshell sleep**. Conviene saberlo antes de confiar en él.

### Capa 2 — Sleep por inactividad: `caffeinate`, no `pmset`

La sesión se envuelve en `caffeinate -is`, que mantiene una power assertion **exactamente
mientras la sesión vive** y la suelta al terminar.

Prefiero esto a `sudo pmset -c sleep 0` porque:

* está **acotado al proceso**: no deja la máquina sin dormir para siempre;
* no requiere `sudo` ni cambia nada global;
* si la sesión muere, el Mac vuelve solo a su comportamiento normal.

### Capa 3 — Vida del proceso sin terminal: LaunchAgent

Un LaunchAgent de `launchd` — el mecanismo persistente del sistema, no un `nohup`.

Tres decisiones del plist son **política, no comodidad**:

| clave | valor | por qué |
|---|---|---|
| `KeepAlive` | **`false`** | **Restart=no.** launchd no revive una sesión caída. Una sesión que murió lo hizo por algo, y un reinicio lo taparía. Además evita que un gap se convierta en un bucle de respawn. |
| `RunAtLoad` | **`false`** | Nada arranca solo, **ni siquiera al iniciar sesión**. Empieza solo cuando una persona ejecuta `launchctl start`. |
| `ProcessType` | `Interactive` | launchd no aplica throttling de background al job — el equivalente de App Nap para un servicio. |

Con `RunAtLoad: false` más el `--fresh` explícito del launcher, **no existe ningún camino por
el que una sesión arranque o reanude sin que alguien lo decida**.

---

## 3. Cambios exactos

### 3a. Artefactos ya preparados en el repositorio (no instalados)

* `deploy/paper-b2-btc-4h.sh` — lee el mismo `.env` que es la única fuente de verdad, corre
  `paper check` primero y luego `paper run --fresh`. No decide nada.
* `deploy/com.quantplatform.paper-b2-btc-4h.plist` — validado con `plutil -lint`.

### 3b. Instalación (un acto deliberado, cuando lo apruebes)

```bash
cp /Users/luisve/quant-platform/deploy/com.quantplatform.paper-b2-btc-4h.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.quantplatform.paper-b2-btc-4h.plist
```

Eso **carga** el agente pero **no lo arranca** (`RunAtLoad: false`). Para arrancarlo:

```bash
launchctl kickstart gui/$(id -u)/com.quantplatform.paper-b2-btc-4h
```

Para pararlo y descargarlo:

```bash
launchctl kill SIGTERM gui/$(id -u)/com.quantplatform.paper-b2-btc-4h
launchctl bootout gui/$(id -u)/com.quantplatform.paper-b2-btc-4h
```

### 3c. Lo que NO recomiendo, y por qué lo menciono

```bash
sudo pmset -a disablesleep 1     # NO
```

Sí funciona: es la única forma de sobrevivir a la tapa cerrada en Apple Silicon. Pero es un
cambio **global, permanente y fácil de olvidar**, y un MacBook Air no tiene ventilación activa
— dejarlo trabajando con la tapa cerrada indefinidamente no es buena idea. Si algún día hace
falta, que sea una decisión consciente y no el arreglo por defecto.

---

## 4. Riesgos

1. **La capa 1 es humana.** `caffeinate` y launchd no impiden cerrar la tapa ni desenchufar.
   La única defensa real es no usar un portátil que te llevas para una sesión de 24/7.
2. **66,7 días de warm-up sobre un portátil móvil.** Cada corte que termine en gap significa
   empezar de cero otra vez. Es el riesgo dominante de este plan, y no lo resuelve ningún
   ajuste de energía.
3. **`RunAtLoad: false` significa que un reinicio del Mac deja la sesión parada** hasta que
   alguien la arranque. Es deliberado — es el precio de "Restart=no" — pero hay que saberlo:
   el fallo se manifiesta como silencio, no como un error.
4. **`caffeinate -s` solo actúa con AC.** Con batería, `-i` evita el sleep por inactividad
   pero no el resto.
5. **La batería está hoy al 30% y descargando.** Enchufar es condición previa.

---

## 5. La recomendación que de verdad importa

Todo lo anterior hace el fallo menos probable. **Nada lo hace improbable.**

Un warm-up de 66,7 días en un portátil que se mueve, se desenchufa y se cierra va a
interrumpirse. Las dos salidas honestas:

* **Aceptar los cortes y apoyarse en el warm-start.** La sesión persiste su historia de
  mercado en `state/*.history.jsonl`, y esa historia es reutilizable **siempre que la sesión
  termine plana**. Una sesión que muera con posición abierta pierde el warm-up entero. Esto
  no está probado en la práctica y debería probarse a propósito antes de confiar en ello.
* **Mover la sesión a un host siempre encendido.** Es la respuesta correcta a "quiero algo
  que corra meses", y ningún ajuste de `pmset` la sustituye.

---

## 6. Checklist antes de relanzar

| ITEM | CÓMO SE COMPRUEBA |
|---|---|
| Mac enchufado a AC | `pmset -g batt` dice `AC Power` |
| Batería razonable | ≥ 50% antes de empezar |
| **Tapa abierta**, y que siga abierta | físico; no hay comprobación automática |
| Sin procesos de paper vivos | `ps -eo command \| grep "cli.main paper"` vacío |
| `var/sessions/.../state/` limpio | decidir: reutilizar la historia plana o empezar de cero |
| Agente cargado pero no arrancado | `launchctl print gui/$(id -u)/com.quantplatform.paper-b2-btc-4h` |
| `paper check` limpio | `READY_FOR_PAPER_RUN` |
| Máquina sana | swap con holgura, sin backtests corriendo |
| Tras arrancar: lock coherente | el PID del lock vivo y el que reporta Mission Control |
| Tras arrancar: `live_trading_armed` | `false` |
| A las 4 h: primera barra | `bars_processed` ≥ 1 y sin `data_gap_error` |

**Nota de observabilidad que sigue pendiente de M26:** Mission Control mostrará
`Warm-up 0 / 200` cuando la estrategia necesita 400, porque lee la metadata de clase y no la
de instancia. No afecta a la operativa — el motor valida contra 400 — pero el panel dirá
"COMPLETE" un mes antes de que lo esté. Sigue sin corregir, a la espera de tu decisión.
