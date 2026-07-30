# Monitor de funciones IMAX — La Odisea (Showcase AR)

Chequea la cartelera de la peli, compara contra la corrida anterior y te avisa por
Telegram **solo cuando se liberan funciones nuevas en IMAX**. Corre solo cada ~10 min
en GitHub Actions, sin tu PC prendida.

## Cómo funciona (endpoint JSON, no scraping)

Showcase renderiza las funciones por AJAX contra la API interna de **Voy al Cine**:

```
https://api.voyalcine.net/films/<FILM_ID>/tree/<HOUSE_ID>
```

Ese endpoint devuelve **HTTP 200 `application/json` sin cookies, sesión ni navegador**.
Por eso el script es **stdlib pura (`urllib`)**: sin dependencias, sin Playwright, sin
Chromium. Corre en ~1s y **no puede caer en el corte de sesión** de `cerrar.aspx`
(no abre ninguna sesión — ese era el mayor riesgo del enfoque con navegador).

El `tree/<HOUSE_ID>` ya viene scopeado a la sala. Verificado contra el sitio real:

| URL | Devuelve |
|---|---|
| `/films/5875/tree/3250` | **solo** IMAX Theatre (Norcenter) / `IMAX-Subtitulado` |
| `/films/5875/tree` (sin house) | los 9 cines y formatos `2D-*` incluidos |

`house_id=3250` = sala IMAX. Por eso `VENUE_IS_IMAX=1`: todo lo que aparece ya es IMAX.
Si algún día el `house_id` deja de filtrar, poné `VENUE_IS_IMAX=0` y vuelve a filtrar
por la palabra `IMAX` en el `formatDescription`.

## Identidad de una función y anti-spam

- **Clave = `fecha|formato|hora`**, no el `performanceId`. Los ids de la API cambian
  si reprograman; si dedup por id, te avisaría de funciones "nuevas" que son la misma.
- La API expone una **ventana de ~14 días** que se corre con el tiempo. Si esa ventana
  avanza de a un día, la fecha del borde sería una alerta diaria boba. Por eso
  `SUPPRESS_HORIZON_ROLL=1` (default): si lo **único** nuevo es una sola fecha igual a
  `último_máximo + 1 día`, lo absorbe callado. **Sí** avisa cuando:
  - se agrega un horario en una fecha ya conocida,
  - aparece un bloque de varias fechas (o un salto de más de un día),
  - aparece un formato/sala nuevo.

## Fallas: nunca se queda callado por error

El silencio tiene que significar "no hay nada nuevo", nunca "el bot se rompió". Por eso:

- Si la API tira error / no-JSON / estructura inesperada → avisa `⚠️ Monitor con problemas`
  y sale con código 1 (el workflow queda en rojo).
- Si antes veía N>0 funciones y de golpe ve 0 → avisa (posible peli retirada o API rota).
- Los avisos de falla salen **una sola vez** por tipo (flag `falla_avisada` en `state.json`),
  no cada 10 min. El workflow persiste `state.json` **incluso cuando el monitor falla**,
  justamente para no perder ese flag.

## 1. Bot de Telegram (2 min)

1. Telegram → **@BotFather** → `/newbot` → te da el **token**.
2. Mandale un mensaje cualquiera a tu bot (tiene que existir el chat).
3. Tu chat_id: **@userinfobot**, o `https://api.telegram.org/bot<TOKEN>/getUpdates`.

## 2. Local

```bash
export TG_TOKEN="123456:ABC..."
export TG_CHAT_ID="123456789"
python3 monitor.py
```

No hay que instalar nada (stdlib). Primera corrida = baseline: te dice cuántas IMAX hay
y no spamea. Corré `DEBUG=1 python3 monitor.py` para listar cada función detectada.

## 3. GitHub Actions (ya configurado)

`.github/workflows/monitor.yml` corre cada 10 min. Cargá los secrets:

```bash
gh secret set TG_TOKEN
gh secret set TG_CHAT_ID
```

El `state.json` lo commitea el propio workflow para no perder memoria entre corridas.

## Variables

| Var | Default | Qué hace |
|---|---|---|
| `FILM_ID` | `5875` | La Odisea |
| `HOUSE_ID` | `3250` | sala IMAX (Norcenter) |
| `VENUE_IS_IMAX` | `1` | `0` = filtra por la palabra IMAX en el formato |
| `SUPPRESS_HORIZON_ROLL` | `1` | `0` = avisa también el avance natural de la ventana |
| `FILM_URL` | pelicula.aspx | link "Comprar" que va en la alerta |
| `STATE_FILE` | `state.json` | memoria entre corridas |
| `DEBUG` | — | `1` = lista cada función detectada |

## Nota

GitHub deshabilita los workflows programados tras 60 días sin actividad en el repo.
Para esta peli no es problema, pero si el bot deja de avisar de golpe, chequeá eso primero.
