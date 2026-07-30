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
    FILM_ID / HOUSE_ID / FILM_URL / VENUE_IS_IMAX / STATE_FILE / DEBUG
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

FILM_ID = os.environ.get("FILM_ID", "5875")
HOUSE_ID = os.environ.get("HOUSE_ID", "3250")
API_URL = f"https://api.voyalcine.net/films/{FILM_ID}/tree/{HOUSE_ID}"

FILM_URL = os.environ.get(
    "FILM_URL",
    f"https://entradas.todoshowcase.com/showcase/pelicula.aspx"
    f"?filmid={FILM_ID}&house_id={HOUSE_ID}",
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
def fetch_tree() -> dict:
    last = "sin detalle"
    for i in range(3):
        if i:
            time.sleep(min(2 ** (i - 1), 4))
        try:
            req = urllib.request.Request(
                API_URL, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
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
def baseline_text(funcs: dict, cur_max: str) -> str:
    dias = len({key_date(k) for k in funcs})
    return (
        f"✅ Monitor IMAX activo.\nFunciones IMAX publicadas ahora: "
        f"<b>{len(funcs)}</b> en {dias} día/s (hasta {cur_max}).\n"
        f"<a href='{escape_html(FILM_URL)}'>Ver</a>"
    )


def new_text(nuevas: dict) -> str:
    lineas = "\n".join(f"• {escape_html(v)}" for v in sorted(nuevas.values())[:25])
    extra = f"\n… y {len(nuevas) - 25} más" if len(nuevas) > 25 else ""
    return (
        f"🎬 <b>{len(nuevas)} función/es nuevas en IMAX</b>\n\n{lineas}{extra}\n\n"
        f"<a href='{escape_html(FILM_URL)}'>Comprar ahora</a>"
    )


def fault_text(detail: str) -> str:
    return f"⚠️ <b>Monitor con problemas</b>\n{escape_html(detail)}\n{escape_html(API_URL)}"


def main() -> int:
    state = load_state()
    seen: dict = state.get("funciones", {})
    prev_count = state.get("ultimo_conteo", 0)
    prev_max = state.get("max_fecha", "")
    first_run = "funciones" not in state

    # ---- fetch -----------------------------------------------------------
    try:
        data = fetch_tree()
        funcs = parse_funcs(data)
    except ScrapeError as e:
        kind = e.kind
        if state.get("falla_avisada") != kind:
            if notify(fault_text(f"No pude leer la cartelera: {e}"), "⚠️ Monitor IMAX con problemas"):
                state["falla_avisada"] = kind
                save_state(state)
        else:
            print(f"[!] {kind} (ya avisado): {e}")
        return 1

    nuevas = {k: v for k, v in funcs.items() if k not in seen}
    cur_max = max((key_date(k) for k in funcs), default="")
    print(f"detectadas={len(funcs)} nuevas={len(nuevas)} previas={prev_count} max_fecha={cur_max}")

    if DEBUG:
        for v in sorted(funcs.values()):
            print("  ", v)

    # ---- regresión N>0 -> 0 ----------------------------------------------
    if not first_run and prev_count > 0 and len(funcs) == 0:
        if state.get("falla_avisada") != "cero":
            if notify(
                fault_text(f"Antes veía {prev_count} funciones y ahora 0. Revisá antes de confiar en el silencio."),
                "⚠️ Monitor IMAX: 0 funciones",
            ):
                state["falla_avisada"] = "cero"
                save_state(state)
        return 1

    # API OK: si había falla avisada, la limpio (cambio silencioso).
    had_fault = state.pop("falla_avisada", None) is not None

    # ---- primera corrida: baseline (entrega garantizada) -----------------
    if first_run:
        if not notify(baseline_text(funcs, cur_max), "✅ Monitor IMAX activo"):
            return 1  # no siembro, reintenta
        seen.update(funcs)
        state.update({"funciones": seen, "ultimo_conteo": len(funcs), "max_fecha": cur_max})
        save_state(state)
        return 0

    # ---- funciones nuevas ------------------------------------------------
    if nuevas:
        rolled = SUPPRESS_HORIZON_ROLL and is_horizon_roll(nuevas, prev_max)
        if not rolled:
            if not notify(new_text(nuevas), f"🎬 {len(nuevas)} función/es nuevas en IMAX — La Odisea"):
                return 1  # CRÍTICO: no avanzar el set de vistas, reintentar
        else:
            print(f"[i] Roll suprimido (SUPPRESS_HORIZON_ROLL=1): {cur_max}")
        seen.update(funcs)
        state.update({"funciones": seen, "ultimo_conteo": len(funcs), "max_fecha": cur_max})
        save_state(state)
        return 0

    # ---- sin novedades: persistir solo si algo cambió --------------------
    if had_fault or prev_count != len(funcs) or prev_max != cur_max:
        seen.update(funcs)
        state.update({"funciones": seen, "ultimo_conteo": len(funcs), "max_fecha": cur_max})
        save_state(state)
    return 0


if __name__ == "__main__":
    code = main()
    ping_healthcheck()  # la corrida se ejecutó -> el scheduler está vivo
    sys.exit(code)
