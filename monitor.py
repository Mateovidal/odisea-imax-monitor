#!/usr/bin/env python3
"""
Monitor de funciones IMAX - Showcase Argentina (Voy al Cine).
Avisa por Telegram + email cuando se liberan funciones nuevas en la sala IMAX.

Usa el endpoint JSON interno de Voy al Cine en vez de scrapear el DOM:

    https://api.voyalcine.net/films/<FILM_ID>/tree/<HOUSE_ID>

Ese endpoint responde HTTP 200 application/json SIN cookies, sesion ni
navegador. Por eso el script es stdlib puro (urllib), corre en ~1s.

El `tree/<HOUSE_ID>` ya viene scopeado a la sala: /tree/3250 devuelve solo
"IMAX Theatre (Norcenter)" / "IMAX-Subtitulado".

Garantia de entrega:
    - Telegram y email chequean la respuesta real (ok:true / id), no solo que
      no haya excepcion. Se escapa HTML en los labels.
    - Entregado = al menos UN canal confirmo. Si uno entrega y el otro falla,
      se considera entregado y se loguea el que fallo.
    - El set de "vistas" NO avanza si NINGUN canal entrego -> reintenta la
      proxima corrida.

Variables:
    FILMS "5875,6027" (lista de filmids en la misma sala) / HOUSE_ID / VENUE_IS_IMAX
    STATE_FILE / DEBUG
    SUPPRESS_HORIZON_ROLL "0" (default): la fecha nueva del borde SI avisa
                   (Showcase libera fecha por fecha, el roll es el evento).
    TG_TOKEN / TG_CHAT_ID          Telegram
    RESEND_API_KEY / EMAIL_TO / EMAIL_FROM   Email (Resend HTTP API)
    HC_PING_URL    dead-man's switch: se pinguea al final de cada corrida
"""

import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HOUSE_ID = os.environ.get("HOUSE_ID", "3250")
CATALOG_URL = "https://api.voyalcine.net/films"
# Pelis fijas a vigilar en la sala IMAX (comma-separated). Back-compat FILM_ID.
FILMS = [
    f.strip()
    for f in os.environ.get("FILMS", os.environ.get("FILM_ID", "6027")).split(",")
    if f.strip()
]
# Descubrimiento por nombre: vigila cualquier peli del catálogo cuyo nombre matchee
# WATCH_NAMES, salvo que también matchee WATCH_EXCLUDE. Sirve para pelis que todavía
# no tienen filmid (ej: "avengers cuando salga"). Se resuelven a filmid en cada corrida.
WATCH_NAMES = [
    w.strip().lower()
    for w in os.environ.get("WATCH_NAMES", "avengers").split(",")
    if w.strip()
]
WATCH_EXCLUDE = [
    w.strip().lower()
    for w in os.environ.get("WATCH_EXCLUDE", "endgame,bonus").split(",")
    if w.strip()
]


def api_url(film: str) -> str:
    return f"https://api.voyalcine.net/films/{film}/tree/{HOUSE_ID}"


def buy_url(film: str) -> str:
    return (
        f"https://entradas.todoshowcase.com/showcase/pelicula.aspx"
        f"?filmid={film}&house_id={HOUSE_ID}"
    )


VENUE_IS_IMAX = os.environ.get("VENUE_IS_IMAX", "1") == "1"
# Default 0: Showcase libera fecha por fecha, el avance de la ventana es EL evento.
SUPPRESS_HORIZON_ROLL = os.environ.get("SUPPRESS_HORIZON_ROLL", "0") == "1"
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "matevidal7@gmail.com")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "Monitor IMAX <onboarding@resend.dev>")
HC_PING_URL = os.environ.get("HC_PING_URL", "")
DEBUG = os.environ.get("DEBUG") == "1"

IMAX_RE = re.compile(r"imax", re.I)
DIA_ES = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


class ScrapeError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind  # "scrape" | "estructura"


def escape_html(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --------------------------------------------------------------------------
# Canales de notificación (cada uno: True entregó, False falló, None sin config)
# --------------------------------------------------------------------------
def tg_send(text: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        return None
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = json.dumps(
        {
            "chat_id": TG_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
    ).encode()
    for attempt in range(3):
        if attempt:
            time.sleep((1.5, 3.0)[attempt - 1])
        try:
            req = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=20) as r:
                body = json.loads(r.read() or b"{}")
                if r.status == 200 and body.get("ok") is True:
                    return True
                print(f"[tg] 200 ok:false desc={body.get('description')}", file=sys.stderr)
                return False
        except urllib.error.HTTPError as e:
            detail = _read_err(e)
            print(f"[tg] HTTP {e.code}: {detail}", file=sys.stderr)
            if e.code == 429:
                ra = None
                try:
                    ra = json.loads(detail or "{}").get("parameters", {}).get("retry_after")
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(min(ra or 3, 10))
                continue
            if 400 <= e.code < 500:
                return False  # 400/401: reintentar no ayuda
        except Exception as e:  # noqa: BLE001
            print(f"[tg] intento {attempt + 1}: {e}", file=sys.stderr)
    return False


def send_email(subject: str, html: str):
    if not RESEND_API_KEY:
        return None
    url = "https://api.resend.com/emails"
    payload = json.dumps(
        {"from": EMAIL_FROM, "to": [EMAIL_TO], "subject": subject, "html": html}
    ).encode()
    for attempt in range(3):
        if attempt:
            time.sleep((1.5, 3.0)[attempt - 1])
        try:
            req = urllib.request.Request(
                url,
                data=payload,
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=20) as r:
                body = json.loads(r.read() or b"{}")
                if 200 <= r.status < 300 and body.get("id"):
                    return True
                print(f"[email] {r.status} sin id: {body}", file=sys.stderr)
                return False
        except urllib.error.HTTPError as e:
            print(f"[email] HTTP {e.code}: {_read_err(e)}", file=sys.stderr)
            if 400 <= e.code < 500:
                return False  # dominio no verificado / destinatario inválido, etc.
        except Exception as e:  # noqa: BLE001
            print(f"[email] intento {attempt + 1}: {e}", file=sys.stderr)
    return False


def _read_err(e) -> str:
    try:
        return e.read().decode(errors="replace")[:300]
    except Exception:  # noqa: BLE001
        return ""


def _to_html(text: str) -> str:
    return (
        '<div style="font-family:system-ui,Arial,sans-serif;font-size:15px;line-height:1.5">'
        + text.replace("\n", "<br>")
        + "</div>"
    )


def notify(text: str, subject: str) -> bool:
    """Manda a Telegram y email. Entregado = al menos un canal confirmó.
    Si un canal entrega y otro falla -> entregado, pero se loguea."""
    tg = tg_send(text)
    em = send_email(subject, _to_html(text))
    configured = {n: ok for n, ok in (("telegram", tg), ("email", em)) if ok is not None}
    if not configured:
        print("[!] Ningún canal configurado. Mensaje:\n" + text)
        return False
    delivered = any(configured.values())
    fallidos = [n for n, ok in configured.items() if ok is False]
    if delivered and fallidos:
        print(f"[notify] entregado, pero falló: {', '.join(fallidos)}")
    elif not delivered:
        print(f"[notify] NINGÚN canal entregó ({', '.join(configured)})")
    return delivered


def ping_healthcheck() -> None:
    """Dead-man's switch: si el scheduler deja de disparar, healthchecks.io avisa."""
    if not HC_PING_URL:
        return
    try:
        with urllib.request.urlopen(HC_PING_URL, timeout=10) as r:
            r.read()
    except Exception as e:  # noqa: BLE001
        print(f"[hc] ping falló: {e}", file=sys.stderr)


# --------------------------------------------------------------------------
# Fetch + parse
# --------------------------------------------------------------------------
def fetch_tree(url: str) -> dict:
    last = "sin detalle"
    for i in range(3):
        if i:
            time.sleep(min(2 ** (i - 1), 4))
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                ra = e.headers.get("Retry-After")
                try:
                    w = min(int(ra), 10)
                except Exception:  # noqa: BLE001
                    w = 5
                last = "HTTP 429"
                time.sleep(w)
                continue
            if e.code >= 500:
                last = f"HTTP {e.code}"
                continue
            raise ScrapeError("scrape", f"HTTP {e.code}") from e
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            continue
        if not isinstance(data, dict) or not isinstance(data.get("days"), dict):
            raise ScrapeError("estructura", "falta 'days' en la respuesta")
        return data
    raise ScrapeError("scrape", f"agoté reintentos: {last}")


def parse_funcs(data: dict) -> dict:
    """{clave: texto_humano}, clave = fecha|formato|hora (estable, no performanceId)."""
    funcs: dict[str, str] = {}
    for fecha, cines in data["days"].items():
        try:
            dow = DIA_ES[dt.date.fromisoformat(fecha).weekday()]
        except Exception:  # noqa: BLE001
            dow = "?"
        for cine in cines:
            cine_name = cine.get("name", "?")
            for fmt in cine.get("formats", []):
                fdesc = fmt.get("formatDescription", "?")
                if not VENUE_IS_IMAX and not IMAX_RE.search(fdesc):
                    continue
                for perf in fmt.get("performances", []):
                    hora = perf.get("showTime", "?")
                    funcs[f"{fecha}|{fdesc}|{hora}"] = (
                        f"{dow} {fecha} {hora} — {fdesc} @ {cine_name}"
                    )
    return funcs


def key_date(key: str) -> str:
    return key.split("|", 1)[0]


def is_horizon_roll(nuevas: dict, prev_max: str) -> bool:
    if not prev_max:
        return False
    fechas = {key_date(k) for k in nuevas}
    if len(fechas) != 1:
        return False
    try:
        d_solo = dt.date.fromisoformat(next(iter(fechas)))
        d_prev = dt.date.fromisoformat(prev_max)
    except Exception:  # noqa: BLE001
        return False
    return d_solo == d_prev + dt.timedelta(days=1)


def fetch_catalog() -> list:
    """Lista de pelis del catálogo: [{'id':.., 'name':..}, ...]. Puede tirar ScrapeError.

    Nota: el catálogo devuelve una LISTA, no el shape {'days':..} del tree, así que
    no puede usar fetch_tree().
    """
    last = "sin detalle"
    for i in range(3):
        if i:
            time.sleep(min(2 ** (i - 1), 4))
        data = None
        try:
            req = urllib.request.Request(
                CATALOG_URL, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            continue
        if isinstance(data, list):
            return data
        last = "respuesta inesperada (no es lista)"
    raise ScrapeError("catalogo", f"no pude leer el catálogo: {last}")


def discover_films(catalog: list) -> list:
    """Filmids del catálogo cuyo nombre matchea WATCH_NAMES y no WATCH_EXCLUDE."""
    out = []
    for f in catalog:
        name = str(f.get("name", "")).lower()
        if not any(w in name for w in WATCH_NAMES):
            continue
        if any(x in name for x in WATCH_EXCLUDE):
            continue
        fid = f.get("id")
        if fid is not None:
            out.append(str(fid))
    return out


# --------------------------------------------------------------------------
# Estado
# --------------------------------------------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8"
    )


# --------------------------------------------------------------------------
# Mensajes
# --------------------------------------------------------------------------
def baseline_text(name: str, funcs: dict, cur_max: str, buy: str) -> str:
    if not funcs:
        return (
            f"✅ Monitor IMAX activo para <b>{escape_html(name)}</b>.\n"
            f"Todavía no hay funciones publicadas — te aviso apenas abran.\n"
            f"<a href='{escape_html(buy)}'>Ver</a>"
        )
    dias = len({key_date(k) for k in funcs})
    return (
        f"✅ Monitor IMAX activo para <b>{escape_html(name)}</b>.\n"
        f"Funciones publicadas ahora: <b>{len(funcs)}</b> en {dias} día/s (hasta {cur_max}).\n"
        f"<a href='{escape_html(buy)}'>Ver</a>"
    )


def new_text(name: str, nuevas: dict, buy: str) -> str:
    lineas = "\n".join(f"• {escape_html(v)}" for v in sorted(nuevas.values())[:25])
    extra = f"\n… y {len(nuevas) - 25} más" if len(nuevas) > 25 else ""
    return (
        f"🎬 <b>{len(nuevas)} función/es nuevas en IMAX — {escape_html(name)}</b>\n\n"
        f"{lineas}{extra}\n\n<a href='{escape_html(buy)}'>Comprar ahora</a>"
    )


def fault_text(detail: str, url: str) -> str:
    return f"⚠️ <b>Monitor con problemas</b>\n{escape_html(detail)}\n{escape_html(url)}"


def migrate_state(state: dict) -> dict:
    """Formato viejo (plano, una peli = 5875) -> {'films': {'5875': {...}}}."""
    if "films" in state:
        return state
    films: dict = {}
    if "funciones" in state:  # estado plano = era La Odisea (5875)
        old = {
            k: state[k]
            for k in ("funciones", "ultimo_conteo", "max_fecha", "nombre")
            if k in state
        }
        # Estaba en falla 'cero' y a 0 (terminó cartel): lo dejo limpio y en 0,
        # así no queda rojo perpetuo. Si vuelve con funciones nuevas, avisa.
        old["ultimo_conteo"] = 0
        old.pop("falla_avisada", None)
        films["5875"] = old
    return {"films": films}


def process_film(film: str, fs: dict) -> int:
    """Procesa UNA peli; muta su estado `fs`. Devuelve 0 (ok/verde) o 1 (reintentar)."""
    seen: dict = fs.get("funciones", {})
    prev_count = fs.get("ultimo_conteo", 0)
    prev_max = fs.get("max_fecha", "")
    first_run = "funciones" not in fs
    url = api_url(film)
    buy = buy_url(film)
    name = fs.get("nombre", film)

    try:
        data = fetch_tree(url)
        funcs = parse_funcs(data)
    except ScrapeError as e:
        kind = e.kind
        if fs.get("falla_avisada") != kind:
            if notify(
                fault_text(f"[{name}] No pude leer la cartelera: {e}", url),
                f"⚠️ Monitor IMAX ({name})",
            ):
                fs["falla_avisada"] = kind
        else:
            print(f"[{film}] {kind} (ya avisado)")
        return 1

    name = data.get("name") or name
    fs["nombre"] = name
    nuevas = {k: v for k, v in funcs.items() if k not in seen}
    cur_max = max((key_date(k) for k in funcs), default="")
    print(f"[{film} {name}] detectadas={len(funcs)} nuevas={len(nuevas)} previas={prev_count}")
    if DEBUG:
        for v in sorted(funcs.values()):
            print("   ", v)

    # ---- drop-a-0: avisar UNA vez, luego aceptar 0 como normal (verde) ----
    if not first_run and prev_count > 0 and len(funcs) == 0:
        if fs.get("falla_avisada") != "cero":
            if notify(
                fault_text(
                    f"[{name}] Antes veía {prev_count} funciones y ahora 0 "
                    "(¿terminó su ciclo o se rompió?).",
                    url,
                ),
                f"⚠️ {name}: 0 funciones",
            ):
                fs["falla_avisada"] = "cero"
                fs["ultimo_conteo"] = 0  # acepto 0 como nuevo normal
                return 0
            return 1  # no se entregó -> reintenta la próxima
        fs["ultimo_conteo"] = 0  # ya avisado: 0 es el normal, silencio + verde
        return 0

    fs.pop("falla_avisada", None)  # API ok con funciones -> limpio falla

    if first_run:
        if not notify(baseline_text(name, funcs, cur_max, buy), f"✅ Monitor IMAX: {name}"):
            return 1  # no siembro, reintenta
        seen.update(funcs)
        fs.update({"funciones": seen, "ultimo_conteo": len(funcs), "max_fecha": cur_max})
        return 0

    if nuevas:
        rolled = SUPPRESS_HORIZON_ROLL and is_horizon_roll(nuevas, prev_max)
        if not rolled:
            if not notify(new_text(name, nuevas, buy), f"🎬 {len(nuevas)} nuevas en IMAX — {name}"):
                return 1  # CRÍTICO: no avanzar el set de vistas, reintentar
        else:
            print(f"[{film}] roll suprimido")
        seen.update(funcs)
        fs.update({"funciones": seen, "ultimo_conteo": len(funcs), "max_fecha": cur_max})
        return 0

    # sin novedades
    fs.update({"funciones": seen, "ultimo_conteo": len(funcs), "max_fecha": cur_max})
    return 0


def main() -> int:
    print(
        f"cfg: FILMS={','.join(FILMS)} watch={','.join(WATCH_NAMES)} house={HOUSE_ID} "
        f"SUPPRESS_HORIZON_ROLL={'1' if SUPPRESS_HORIZON_ROLL else '0'} "
        f"telegram={'on' if TG_TOKEN and TG_CHAT_ID else 'off'} "
        f"email={'on' if RESEND_API_KEY else 'off'} "
        f"deadman={'on' if HC_PING_URL else 'off'}"
    )
    state = migrate_state(load_state())
    films_state = state.setdefault("films", {})

    # Descubrimiento por nombre (ej: Avengers cuando salga). El catálogo manda; ante
    # un blip uso lo último conocido para no dejar de vigilar una peli ya descubierta.
    discovered = state.get("watch_discovered", [])
    if WATCH_NAMES:
        try:
            discovered = discover_films(fetch_catalog())
            state["watch_discovered"] = discovered
            if discovered:
                print(f"descubiertas por nombre {WATCH_NAMES}: {discovered}")
        except Exception as e:  # noqa: BLE001
            print(f"[catalogo] no pude descubrir ({e}); uso lo último conocido: {discovered}",
                  file=sys.stderr)

    active = list(dict.fromkeys([*FILMS, *discovered]))  # dedup, orden estable
    worst = 0
    for film in active:
        fs = films_state.setdefault(film, {})
        try:
            worst = max(worst, process_film(film, fs))
        except Exception as e:  # noqa: BLE001
            print(f"[{film}] error inesperado: {type(e).__name__}: {e}", file=sys.stderr)
            worst = 1
    save_state(state)
    return worst


if __name__ == "__main__":
    code = main()
    ping_healthcheck()  # la corrida se ejecutó -> el scheduler está vivo
    sys.exit(code)
