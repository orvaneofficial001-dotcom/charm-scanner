"""
Escáner de avisos para CSFloat (NO compra nada, solo te manda los enlaces).

Qué hace:
  1. Busca armas (todas, o solo las que contengan el texto de WEAPON_FILTER, p. ej. "AK-47").
  2. Se queda solo con las que llevan un charm puesto.
  3. Solo las que están POR DEBAJO de la media (precio de referencia de CSFloat).
  4. Calcula cuánto ganarías y, si es suficiente, te avisa por Telegram con los enlaces.

Uso en GitHub Actions: ver .github/workflows/charm.yml (usa ONCE=1).
Secretos necesarios: CSFLOAT_API_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests

API = "https://csfloat.com/api/v1/listings"
API_KEY = os.environ["CSFLOAT_API_KEY"]

# --- Ajustes ---------------------------------------------------------------
MIN_EUR = 0.03
MAX_EUR = 1.00
EUR_TO_USD = 1.08         # CSFloat trabaja en dólares: pon aquí el cambio actual
POLL_SECONDS = 30         # solo en modo continuo; no bajes de esto
START_PAGES = 4           # páginas (50 listados c/u) que revisa al arrancar en modo continuo
MIN_CHARM_USD = 0.0       # el charm puede valer cualquier precio (sube esto si quieres exigir un mínimo, en $)
SEEN_FILE = "seen.json"   # recuerda lo ya avisado (modo continuo)
MIN_PROFIT_USD = 0.05     # "importante" = beneficio estimado igual o superior a esto (en $)
SELL_FEE = 0.0            # comisión de venta a descontar (0 = no contar; CSFloat cobra ~2 %)
DETACH_COST = 0.0         # coste de quitar el charm (0 = no contar; real: ~0,33 $)
INCLUDE_CSFLOAT_LINK = True   # añade el enlace de CSFloat (donde está ese anuncio concreto)
MIN_SCAN = 500            # mínimo de anuncios a revisar en cada pasada
MAX_PAGES = 40            # tope de seguridad: 40 páginas x 50 = 2000 anuncios por pasada
# ---------------------------------------------------------------------------

MIN_CENTS = int(MIN_EUR * EUR_TO_USD * 100)
MAX_CENTS = int(MAX_EUR * EUR_TO_USD * 100)
WEAPON_FILTER = (sys.argv[1] if len(sys.argv) > 1 else os.getenv("WEAPON_FILTER", "")).lower()

# Modo "una pasada" (GitHub Actions): revisa lo más reciente, avisa y termina.
ONCE = os.getenv("ONCE") == "1"
TEST = os.getenv("TEST") == "true"      # ejecución manual de prueba: manda un resumen a Telegram

charm_cache = {}
stats = {"fetched": 0, "total": 0, "with_charm": 0, "below_avg": 0, "alerts": 0}
shown_keys = []
all_item_keys = set()
example = []


def load_seen():
    """IDs ya vistos, en orden (los más recientes al final)."""
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            return dict.fromkeys(json.load(f), 1)
    except (OSError, ValueError):
        return {}


def save_seen():
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(list(seen)[-5000:], f)
    except OSError:
        pass


# En modo prueba no se usa ni se modifica la memoria, para no estropear el estado real.
seen = {} if TEST else load_seen()


def charm_label(c: dict) -> str:
    pat = c.get("pattern")
    return f"{c.get('name', '?')}" + (f" #{pat}" if pat not in (None, "") else "")


def notify(text: str) -> None:
    print(text + "\n", flush=True)
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("TELEGRAM: faltan los secretos TELEGRAM_TOKEN o TELEGRAM_CHAT_ID (están vacíos).")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token.strip()}/sendMessage",
            data={"chat_id": chat.strip(), "text": text, "disable_web_page_preview": "true"},
            timeout=10,
        )
        if r.ok:
            print("TELEGRAM: mensaje enviado correctamente.")
        else:
            print(f"TELEGRAM: ERROR {r.status_code}: {r.text}")
    except requests.RequestException as e:
        print("TELEGRAM: error de red:", e)


fetch_calls = [0]


def fetch(cursor=None, page: int = 0):
    """Devuelve (anuncios, cursor_siguiente). Usa cursor si CSFloat lo da; si no, el número de página."""
    params = {
        "limit": 50,
        "sort_by": "most_recent",
        "min_price": 1 if TEST else MIN_CENTS,
        "max_price": 5000 if TEST else MAX_CENTS,   # en prueba se amplía para encontrar algún charm
        "type": "buy_now",
    }
    if cursor:
        params["cursor"] = cursor
    else:
        params["page"] = page
    r = requests.get(API, headers={"Authorization": API_KEY}, params=params, timeout=15)
    if r.status_code == 429:
        if ONCE:
            print("CSFloat: límite de peticiones alcanzado (429). Termino esta pasada.")
            return [], None
        print("Límite de peticiones alcanzado, espero 2 minutos...")
        time.sleep(120)
        return [], None
    if not r.ok:
        print(f"CSFloat respondió {r.status_code}: {r.text[:200]}")
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict):
        rows = data.get("data", [])
        nxt = data.get("cursor")
    else:
        rows, nxt = data, None
    fetch_calls[0] += 1
    if fetch_calls[0] == 1:
        keys = sorted(data.keys()) if isinstance(data, dict) else "lista"
        print(f"Respuesta de CSFloat: claves = {keys}; ¿trae cursor? {'sí' if nxt else 'no'}")
    print(f"Llamada {fetch_calls[0]}: {len(rows)} anuncios recibidos de CSFloat.")
    return rows, nxt


def charm_lowest_price(name: str):
    """Precio más bajo (en $) de ese charm suelto en CSFloat. Solo informativo."""
    if name in charm_cache:
        return charm_cache[name]
    price = None
    try:
        r = requests.get(
            API,
            headers={"Authorization": API_KEY},
            params={"limit": 1, "sort_by": "lowest_price", "market_hash_name": name},
            timeout=15,
        )
        if r.ok:
            data = r.json()
            rows = data["data"] if isinstance(data, dict) else data
            if rows:
                price = rows[0]["price"] / 100
    except (requests.RequestException, KeyError, ValueError):
        pass
    charm_cache[name] = price
    return price


def check(listing: dict) -> None:
    lid = listing.get("id")
    if not lid or lid in seen:
        return
    seen[lid] = 1
    stats["fetched"] += 1

    item = listing.get("item", {})
    name = item.get("market_hash_name", "")
    stats["total"] += 1
    all_item_keys.update(item.keys())
    if not shown_keys:
        shown_keys.append(1)
        print("Campos del anuncio:", sorted(listing.keys()))
        print("Campos del objeto:", sorted(item.keys()))

    # 1) filtro de arma
    if WEAPON_FILTER and WEAPON_FILTER not in name.lower():
        return

    # 2) tiene que llevar charm
    charms = item.get("keychains") or []
    if not charms:
        return
    stats["with_charm"] += 1
    if TEST and not example:
        _ref = (listing.get("reference") or {}).get("predicted_price")
        example.append(
            "Ejemplo leído (solo prueba, no es una oferta):\n"
            f"🔫 {name}\n"
            f"💰 Precio {listing.get('price', 0)/100:.2f} $ | media {(_ref or 0)/100:.2f} $\n"
            f"🏷 Charm: {'; '.join(charm_label(c) for c in charms)}\n"
            f"🔗 Steam: https://steamcommunity.com/market/listings/730/{quote(name)}\n"
            f"🔗 CSFloat: https://csfloat.com/item/{lid}"
        )

    # 3) precio por debajo de la media
    ref = (listing.get("reference") or {}).get("predicted_price")
    price = listing.get("price")
    if not ref or not price:
        return
    if price >= ref:          # solo armas POR DEBAJO de la media
        return
    stats["below_avg"] += 1

    diff = (price - ref) / ref * 100
    lines = []
    charms_value = 0.0
    unknown_price = False
    for c in charms:
        low = charm_lowest_price(c.get("name", ""))
        if low is not None and low >= MIN_CHARM_USD:
            charms_value += low
        if low is None:
            unknown_price = True
        low_txt = f" (suelto desde {low:.2f} $)" if low is not None else " (precio suelto no encontrado)"
        lines.append(f"{charm_label(c)}{low_txt}")

    # 3b) ¿merece la pena? Beneficio estimado = vender arma (a la media) + vender charm
    #     - lo que pagas por el arma (comisiones y coste de quitar el charm, según los ajustes).
    profit = (1 - SELL_FEE) * (ref / 100 + charms_value) - price / 100 - DETACH_COST
    print(
        f"Candidato (con charm y bajo la media): {name} | compras {price/100:.2f} $ vs media {ref/100:.2f} $ | "
        f"charms: {'; '.join(lines)} | valor de charms contados (>= {MIN_CHARM_USD} $): {charms_value:.2f} $ | "
        f"beneficio estimado {profit:.2f} $"
    )
    if profit < MIN_PROFIT_USD:
        return

    # 4) aviso con enlaces
    real_profit = 0.98 * (ref / 100 + charms_value) - price / 100 - 0.33
    steam_link = "https://steamcommunity.com/market/listings/730/" + quote(name)
    msg = (
        f"🔔 OFERTA - ganarás ~{profit:.2f} $\n\n"
        f"🔫 {name}\n"
        f"💰 Compras a {price/100:.2f} $ ({abs(diff):.1f} % por debajo de la media de {ref/100:.2f} $)\n"
        f"🏷 Charm: {'; '.join(lines)}\n"
        f"📊 Con comisión de venta 2 % y quitar el charm: ~{real_profit:.2f} $\n"
        + ("⚠️ No pude ver el precio de algún charm: el beneficio no lo incluye.\n" if unknown_price else "")
        + "\n"
        f"🔗 Steam (página del arma, filtra por Attached Charm):\n{steam_link}"
    )
    if INCLUDE_CSFLOAT_LINK:
        msg += f"\n\n🔗 CSFloat (este anuncio exacto, para comprar):\nhttps://csfloat.com/item/{lid}"
    stats["alerts"] += 1
    notify(msg)


def main() -> None:
    if ONCE:
        first_run = not seen          # sin memoria previa: se queda en MIN_SCAN anuncios
        scanned = 0
        hit_seen = False
        run_ids = set()
        cursor = None
        for p in range(MAX_PAGES):
            rows, cursor = fetch(cursor, p)
            if not rows:
                break
            new_in_page = 0
            for listing in rows:
                lid = listing.get("id")
                if lid in run_ids:
                    continue          # repetido dentro de esta misma pasada
                run_ids.add(lid)
                new_in_page += 1
                scanned += 1
                if lid in seen:
                    hit_seen = True   # ya vimos este: hemos llegado a lo de la pasada anterior
                check(listing)
            if new_in_page == 0:
                print("Esa página no trajo anuncios distintos a los ya leídos; paro.")
                break
            if scanned >= MIN_SCAN and (hit_seen or first_run):
                break
            time.sleep(2)
        if not TEST:
            save_seen()
        resumen = (
            f"Resumen: {stats['fetched']} anuncios distintos revisados ({scanned} leídos en total), "
            f"{stats['with_charm']} con charm, "
            f"{stats['below_avg']} por debajo de la media, {stats['alerts']} avisos enviados."
        )
        print(resumen)
        extra = sorted(
            k for k in all_item_keys
            if "keychain" in k.lower() or "sticker" in k.lower() or "charm" in k.lower()
        )
        print("Campos de charm/sticker vistos en algún anuncio:", extra)
        if TEST:
            notify(
                "✅ PRUEBA OK - el bot funciona y llega a tu Telegram\n\n"
                f"📋 {resumen}\n"
                f"Campos de charm/sticker vistos: {extra}\n\n"
                + (example[0] if example else "No encontré anuncios con charm en la ventana de prueba.")
            )
        return

    print(
        f"Escáner activo. Rango {MIN_EUR:.2f}-{MAX_EUR:.2f} EUR "
        f"(~{MIN_CENTS/100:.2f}-{MAX_CENTS/100:.2f} $). Filtro: {WEAPON_FILTER or 'todas'}. Ctrl+C para parar."
    )
    try:
        cursor = None
        for p in range(START_PAGES):
            rows, cursor = fetch(cursor, p)
            for listing in rows:
                check(listing)
            save_seen()
            time.sleep(3)
    except requests.RequestException as e:
        print("Error de red en el arranque:", e)

    while True:
        try:
            rows, _ = fetch()
            for listing in rows:
                check(listing)
            save_seen()
        except requests.RequestException as e:
            print("Error de red:", e)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
