#!/usr/bin/env python3
"""
Monitor de funciones IMAX - Showcase Argentina (Voy al Cine).
Avisa por Telegram cuando se liberan funciones nuevas en la sala IMAX.

Usa el endpoint JSON interno de Voy al Cine en vez de scrapear el DOM:

    https://api.voyalcine.net/films/<FILM_ID>/tree/<HOUSE_ID>

Ese endpoint responde HTTP 200 application/json SIN cookies, sesion ni
navegador. Por eso el script es stdlib puro (urllib), corre en ~1s y no puede
caer en el corte de sesion de `cerrar.aspx` (no hay browser que abra sesion).

El `tree/<HOUSE_ID>` ya viene scopeado a la sala: /tree/3250 devuelve solo
"IMAX Theatre (Norcenter)" / "IMAX-Subtitulado", mientras /tree (sin house)
trae los 9 cines y formatos 2D. Confirmado contra el sitio real.

Uso:
    export TG_TOKEN="123456:ABC..."
    export TG_CHAT_ID="123456789"
    python monitor.py

Variables opcionales:
    FILM_ID        id de la peli (default 5875 = La Odisea)
    HOUSE_ID       id de la sala (default 3250 = IMAX Norcenter)
    FILM_URL       link "Comprar" que va en la alerta (default: pelicula.aspx)
    VENUE_IS_IMAX  "1" (default): el tree ya es IMAX, no filtro por formato.
                   "0": filtro dejando solo formatos que digan "IMAX".
    SUPPRESS_HORIZON_ROLL "1" (default): si lo unico nuevo es UNA fecha del
                   borde (ultimo_max + 1 dia), no avisa (es el avance natural
                   de la ventana de venta). Lanzamientos por bloque o horarios
                   nuevos en fechas ya conocidas SI avisan. "0" = avisa siempre.
    STATE_FILE     ruta del estado (default: ./state.json)
    DEBUG          "1" lista cada funcion detectada
"""

import datetime as dt
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

FILM_ID = os.environ.get("FILM_ID", "5875")
HOUSE_ID = os.environ.get("HOUSE_ID", "3250")
API_URL = f"https://api.voyalcine.net/films/{FILM_ID}/tree/{HOUSE_ID}"

# Link humano para el boton "Comprar" de la alerta (la pagina, no la API).
FILM_URL = os.environ.get(
    "FILM_URL",
    f"https://entradas.todoshowcase.com/showcase/pelicula.aspx"
    f"?filmid={FILM_ID}&house_id={HOUSE_ID}",
)
VENUE_IS_IMAX = os.environ.get("VENUE_IS_IMAX", "1") == "1"
SUPPRESS_HORIZON_ROLL = os.environ.get("SUPPRESS_HORIZON_ROLL", "1") == "1"
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
DEBUG = os.environ.get("DEBUG") == "1"

IMAX_RE = re.compile(r"imax", re.I)
DIA_ES = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


class ScrapeError(Exception):
    """No pude leer la cartelera (red, HTTP != 200, JSON roto o estructura rara)."""


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------
def tg_send(text: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        print("[!] Falta TG_TOKEN / TG_CHAT_ID, imprimo en consola:\n" + text)
        return
    payload = urllib.parse.urlencode(
        {
            "chat_id": TG_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=payload
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
    except Exception as e:  # noqa: BLE001
        print(f"[!] Error mandando a Telegram: {e}", file=sys.stderr)


# --------------------------------------------------------------------------
# Fetch + parse del endpoint JSON
# --------------------------------------------------------------------------
def fetch_tree() -> dict:
    req = urllib.request.Request(
        API_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.status != 200:
                raise ScrapeError(f"HTTP {r.status} en {API_URL}")
            raw = r.read()
    except ScrapeError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ScrapeError(f"{type(e).__name__}: {e}") from e

    try:
        data = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        raise ScrapeError(f"JSON invalido: {e}") from e

    if (
        not isinstance(data, dict)
        or "days" not in data
        or not isinstance(data["days"], dict)
    ):
        raise ScrapeError("La respuesta no tiene la forma esperada (falta 'days').")
    return data


def parse_funcs(data: dict) -> dict:
    """Devuelve {clave: texto_humano}. Clave = fecha|formato|hora (estable).

    NO usa performanceId como clave: los ids cambian si reprograman y no quiero
    avisar de "funciones nuevas" que en realidad son la misma funcion.
    """
    funcs: dict[str, str] = {}
    for fecha, cines in data["days"].items():
        try:
            d = dt.date.fromisoformat(fecha)
            dow = DIA_ES[d.weekday()]
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
                    key = f"{fecha}|{fdesc}|{hora}"
                    funcs[key] = f"{dow} {fecha} {hora} — {fdesc} @ {cine_name}"
    return funcs


def key_date(key: str) -> str:
    return key.split("|", 1)[0]


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


def notify_fault(state: dict, kind: str, msg: str) -> None:
    """Avisa una sola vez por tipo de falla, para no spamear cada 10 min."""
    if state.get("falla_avisada") == kind:
        print(f"[!] {kind} (ya avisado): {msg}")
        return
    tg_send(f"⚠️ <b>Monitor con problemas</b>\n{msg}")
    state["falla_avisada"] = kind


def is_horizon_roll(nuevas: dict, prev_max: str) -> bool:
    """True si lo unico nuevo es UNA fecha == prev_max + 1 dia (avance natural
    de la ventana de venta). Un salto de >1 dia o varias fechas = lanzamiento
    por bloque -> NO es roll, hay que avisar."""
    if not prev_max:
        return False
    fechas = {key_date(k) for k in nuevas}
    if len(fechas) != 1:
        return False
    solo = next(iter(fechas))
    try:
        d_solo = dt.date.fromisoformat(solo)
        d_prev = dt.date.fromisoformat(prev_max)
    except Exception:  # noqa: BLE001
        return False
    return d_solo == d_prev + dt.timedelta(days=1)


def main() -> int:
    state = load_state()
    seen: dict = state.get("funciones", {})
    prev_count = state.get("ultimo_conteo", 0)
    prev_max = state.get("max_fecha", "")
    first_run = "funciones" not in state

    # ---- fetch con manejo de fallas --------------------------------------
    try:
        data = fetch_tree()
        funcs = parse_funcs(data)
    except ScrapeError as e:
        notify_fault(
            state,
            "scrape",
            f"No pude leer la cartelera: {e}\nEndpoint: {API_URL}\n"
            "Puede que cambio la API o que la peli/sala ya no exista. Revisa manualmente.",
        )
        save_state(state)
        return 1

    nuevas = {k: v for k, v in funcs.items() if k not in seen}
    cur_max = max((key_date(k) for k in funcs), default="")
    print(
        f"detectadas={len(funcs)} nuevas={len(nuevas)} previas={prev_count} "
        f"max_fecha={cur_max}"
    )

    if DEBUG:
        print(f"--- {len(funcs)} funciones ---")
        for v in sorted(funcs.values()):
            print("  ", v)

    # ---- regresion: antes habia funciones y ahora cero -------------------
    if not first_run and prev_count > 0 and len(funcs) == 0:
        notify_fault(
            state,
            "cero",
            f"Antes detectaba {prev_count} funciones y ahora 0. "
            "Puede que sacaron la peli, cambio la API o me estan bloqueando. "
            "Revisa antes de confiar en el silencio.",
        )
        save_state(state)
        return 1

    state.pop("falla_avisada", None)  # todo ok, reseteo el aviso de falla

    if first_run:
        dias = len({key_date(k) for k in funcs})
        tg_send(
            f"✅ Monitor IMAX activo.\nFunciones IMAX publicadas ahora: "
            f"<b>{len(funcs)}</b> en {dias} día/s "
            f"(hasta {cur_max}).\n<a href='{FILM_URL}'>Ver</a>"
        )
    elif nuevas:
        rolled = SUPPRESS_HORIZON_ROLL and is_horizon_roll(nuevas, prev_max)
        if rolled:
            print(
                f"[i] Avance natural de la ventana: nueva fecha {cur_max}, "
                "no aviso (SUPPRESS_HORIZON_ROLL=1)."
            )
        else:
            lineas = "\n".join(f"• {v}" for v in sorted(nuevas.values())[:25])
            extra = f"\n… y {len(nuevas) - 25} más" if len(nuevas) > 25 else ""
            tg_send(
                f"🎬 <b>{len(nuevas)} función/es nuevas en IMAX</b>\n\n"
                f"{lineas}{extra}\n\n<a href='{FILM_URL}'>Comprar</a>"
            )

    seen.update(funcs)
    state.update(
        {"funciones": seen, "ultimo_conteo": len(funcs), "max_fecha": cur_max}
    )
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
