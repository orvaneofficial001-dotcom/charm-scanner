"""
Escáner de avisos para CSFloat (NO compra nada, solo te manda el enlace).

Qué hace:
  1. Busca armas (todas, o solo las que contengan el texto que le pases, p. ej. "AK-47").
  2. Se queda solo con las que llevan un charm puesto.
  3. Comprueba que el precio sea normal: igual o por debajo del precio de referencia
     de CSFloat, o como mucho un pequeño % por encima.
  4. Te manda el enlace para que tú decidas si comprar.

Uso:
    pip install requests
    export CSFLOAT_API_KEY="tu_clave"       # csfloat.com/profile -> pestaña "developer"
    python charm_scanner.py                 # todas las armas
    python charm_scanner.py "AK-47"         # solo AK-47
    # Avisos al móvil (opcional): TELEGRAM_TOKEN y TELEGRAM_CHAT_ID

Aviso: no está probado contra la API en vivo. Revisa los campos (keychains, reference,
predicted_price) en https://docs.csfloat.com y ajusta si algo no coincide.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
import requests
from urllib.parse import quote

API = "https://csfloat.com/api/v1/listings"
API_KEY = os.environ["CSFLOAT_API_KEY"]

# --- Ajustes ---------------------------------------------------------------
MIN_EUR = 0.03
MAX_EUR = 1.00
EUR_TO_USD = 1.08        # CSFloat trabaja en dólares: pon aquí el cambio actual
POLL_SECONDS = 30        # no bajes de esto: la API limita las peticiones
START_PAGES = 4          # páginas (50 listados c/u) que revisa al arrancar
MIN_CHARM_USD = 2.0      # solo avisa si el charm suelto vale al menos esto (en $)
SEEN_FILE = "seen.json"  # recuerda lo ya avisado aunque reinicies el programa
# "Importante" = beneficio estimado igual o superior a esto (en $), tras comisiones:
MIN_PROFIT_USD = 0.05
SELL_FEE = 0.0           # comisión de venta que descontar (0 = no contar; CSFloat cobra ~2 %)
DETACH_COST = 0.0        # coste de quitar el charm (0 = no contar; real: ~0,33 $)
INCLUDE_CSFLOAT_LINK = True   # añade también el enlace de CSFloat (donde está ese anuncio concreto)
# ---------------------------------------------------------------------------

MIN_CENTS = int(MIN_EUR * EUR_TO_USD * 100)
MAX_CENTS = int(MAX_EUR * EUR_TO_USD * 100)
WEAPON_FILTER = (sys.argv[1] if len(sys.argv) > 1 else os.getenv("WEAPON_FILTER", "")).lower()

# Modo "una pasada" (para GitHub Actions): revisa lo más reciente, avisa y termina.
ONCE = os.getenv("ONCE") == "1"
TEST = os.getenv("TEST") == "true"   # ejecución manual de prueba: manda un resumen a Telegram
example = []
MAX_AGE_MIN = int(os.getenv("MAX_AGE_MIN", "12"))   # en ONCE solo mira listados de los últimos N minutos


def too_old(listing: dict) -> bool:
    ts = listing.get("created_at")
    if not ts:
        return False
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - t).total_seconds() > MAX_AGE_MIN * 60

charm_cache = {}
stats = {"total": 0, "with_charm": 0, "below_avg": 0, "alerts": 0}
shown_keys = []
all_item_keys = set()


def load_seen():
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def save_seen():
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(list(seen)[-5000:], f)
    except OSError:
        pass


seen = load_seen()


def notify(text: str) -> None:
    print(text + "\n", flush=True)
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("TELEGRAM: faltan los secretos TELEGRAM_TOKEN o TELEGRAM_CHAT_ID (estan vacios).")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token.strip()}/sendMessage",
            data={"chat_id": chat.strip(), "text": text},
            timeout=10,
        )
        if r.ok:
            print("TELEGRAM: mensaje enviado correctamente.")
        else:
            print(f"TELEGRAM: ERROR {r.status_code}: {r.text}")
    except requests.RequestException as e:
        print("TELEGRAM: error de red:", e)


def fetch(page: int = 0):
    r = requests.get(
        API,
        headers={"Authorization": API_KEY},
        params={
            "limit": 50,
            "page": page,
            "sort_by": "most_recent",
            "min_price": 1 if TEST else MIN_CENTS,
            "max_price": 5000 if TEST else MAX_CENTS,
            "type": "buy_now",
        },
        timeout=15,
    )
    if r.status_code == 429:
        print("Límite de peticiones alcanzado, espero 2 minutos...")
        time.sleep(120)
        return []
    r.raise_for_status()
    data = r.json()
    return data["data"] if isinstance(data, dict) else data


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
    seen.add(lid)
    if ONCE and too_old(listing):
        return

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
        _c = "; ".join(f"{c.get('name', '?')} #{c.get('pattern', '?')}" for c in charms)
        example.append(
            f"Ejemplo real leido: {name} | precio {listing.get('price', 0)/100:.2f} $ | "
            f"media {(_ref or 0)/100:.2f} $ | charm {_c}"
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
    for c in charms:
        cname = c.get("name", "?")
        low = charm_lowest_price(cname)
        if low is not None and low >= MIN_CHARM_USD:
            charms_value += low
        low_txt = f" (suelto desde {low:.2f} $)" if low is not None else ""
        lines.append(f"{cname} #{c.get('pattern', '?')}{low_txt}")

    # 3b) ¿merece la pena? Beneficio estimado = vender arma (a la media) + vender charm
    #     - lo que pagas por el arma (comisiones y coste de quitar el charm, según los ajustes).
    if charms_value <= 0:
        return
    profit = (1 - SELL_FEE) * (ref / 100 + charms_value) - price / 100 - DETACH_COST
    if profit < MIN_PROFIT_USD:
        return

    # 4) aviso con enlaces
    real_profit = 0.98 * (ref / 100 + charms_value) - price / 100 - 0.33
    steam_link = "https://steamcommunity.com/market/listings/730/" + quote(name)
    msg = (
        f"GANARÁS ~{profit:.2f} $\n"
        f"{name}\n"
        f"Compras a {price/100:.2f} $ ({abs(diff):.1f} % por debajo de la media de {ref/100:.2f} $)\n"
        f"Charm: {'; '.join(lines)}\n"
        f"(Con comisión de venta 2 % y quitar el charm: ~{real_profit:.2f} $)\n"
        f"Steam (página del arma, filtra por Attached Charm): {steam_link}"
    )
    if INCLUDE_CSFLOAT_LINK:
        msg += f"\nCSFloat (este anuncio exacto): https://csfloat.com/item/{lid}"
    stats["alerts"] += 1
    notify(msg)


def main() -> None:
    if ONCE:
        for p in range(int(os.getenv("PAGES", "4"))):
            for listing in fetch(p):
                check(listing)
            time.sleep(3)
        resumen = (
            f"Resumen: {stats['total']} anuncios revisados, {stats['with_charm']} con charm, "
            f"{stats['below_avg']} por debajo de la media, {stats['alerts']} avisos enviados."
        )
        print(resumen)
        extra = sorted(k for k in all_item_keys if "keychain" in k.lower() or "sticker" in k.lower() or "charm" in k.lower())
        print("Campos de charm/sticker vistos en algun anuncio:", extra)
        if TEST:
            notify(
                "PRUEBA OK: el bot funciona y llega a tu Telegram.\n"
                + resumen + "\n"
                + f"Campos de charm/sticker vistos: {extra}\n"
                + (example[0] if example else "No encontre anuncios con charm en la ventana de prueba.")
            )
        return
    print(
        f"Escáner activo. Rango {MIN_EUR:.2f}-{MAX_EUR:.2f} EUR "
        f"(~{MIN_CENTS/100:.2f}-{MAX_CENTS/100:.2f} $). Filtro: {WEAPON_FILTER or 'todas'}. Ctrl+C para parar."
    )
    try:
        for p in range(START_PAGES):
            for listing in fetch(p):
                check(listing)
            save_seen()
            time.sleep(3)
    except requests.RequestException as e:
        print("Error de red en el arranque:", e)

    while True:
        try:
            for listing in fetch(0):
                check(listing)
            save_seen()
        except requests.RequestException as e:
            print("Error de red:", e)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
