"""
Escáner de avisos (NO compra nada, solo te manda la información y los enlaces).

Qué hace:
  1. Lee los anuncios nuevos de armas en CSFloat (mínimo 500 por pasada, hasta llegar
     a uno ya visto).
  2. Se queda con los que llevan un charm puesto. Se descartan las Souvenir (armas y charms).
  3. Mira en el MERCADO DE STEAM el precio del arma y el de cada charm por separado,
     y cuántas unidades se venden al día (liquidez).
  4. Calcula cuánto RECIBIRÍAS vendiendo cada pieza en Steam (con las comisiones exactas
     de Steam) y lo compara con lo que pagas en CSFloat.
  5. Si ganas dinero, te avisa por Telegram con los enlaces y apunta la oferta en ofertas.csv.
  6. Te avisa por Telegram si algo falla y te manda un resumen diario a partir de las 9:00.

Todos los importes del aviso van en EUROS. CSFloat da el precio en dólares y se convierte
con el cambio del día (BCE); los precios de Steam se piden directamente en euros.

Secretos necesarios: CSFLOAT_API_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID.
"""
import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from urllib.parse import quote

import requests

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Berlin")
except Exception:          # si no hubiera base de zonas horarias
    TZ = None

API = "https://csfloat.com/api/v1/listings"
API_KEY = os.environ["CSFLOAT_API_KEY"]

# --- Ajustes ---------------------------------------------------------------
MIN_EUR = 0.03            # precio mínimo del arma en CSFloat (en €)
MAX_EUR = 1.00            # precio máximo del arma en CSFloat (en €)
FALLBACK_USD_PER_EUR = 1.08   # solo si falla la consulta del cambio
MIN_PROFIT_EUR = 0.05     # avisa si ganas al menos esto (en €), tras comisiones de Steam
COUNT_DETACH_IN_PROFIT = False  # True = restar el coste de quitar el charm del beneficio
DETACH_COST_USD = 0.33    # coste de quitar un charm (pack de 3 por ~1 $)
MIN_VOLUME_WEAPON = 10    # mínimo de ventas en 24 h del ARMA en Steam (0 = sin filtro)
MIN_VOLUME_CHARM = 5      # mínimo de ventas en 24 h del CHARM en Steam (0 = sin filtro)
STEAM_CURRENCY = 3        # moneda de Steam: 3 = euros
STEAM_PAUSE = 3.5         # segundos entre consultas a Steam (Steam limita las peticiones)
MAX_STEAM_LOOKUPS = 14    # tope de consultas a Steam por pasada
INCLUDE_CSFLOAT_LINK = True   # enlace al anuncio exacto de CSFloat (para comprar el arma)
MIN_SCAN = 500            # mínimo de anuncios a revisar en cada pasada
MAX_PAGES = 40            # tope de seguridad: 40 páginas x 50 = 2000 anuncios por pasada
SUMMARY_HOUR = 9          # hora (de Alemania) a partir de la cual se manda el resumen diario
POLL_SECONDS = 30         # solo en modo continuo
START_PAGES = 4           # solo en modo continuo
SEEN_FILE = "seen.json"   # memoria de anuncios ya vistos
STATE_FILE = "state.json" # resumen diario y avisos de fallo ya enviados
OFFERS_FILE = "ofertas.csv"   # registro de ofertas
# ---------------------------------------------------------------------------

WEAPON_FILTER = (sys.argv[1] if len(sys.argv) > 1 else os.getenv("WEAPON_FILTER", "")).lower()
ONCE = os.getenv("ONCE") == "1"          # modo una pasada (GitHub Actions)
TEST = os.getenv("TEST") == "true"       # prueba manual: manda un resumen a Telegram

EUR_PER_USD = 1 / FALLBACK_USD_PER_EUR
MIN_CENTS = 1
MAX_CENTS = 5000

stats = {"fetched": 0, "with_charm": 0, "souvenir": 0, "no_steam": 0,
         "low_volume": 0, "below_steam": 0, "alerts": 0}
shown_keys = []
all_item_keys = set()
example = []
steam_cache = {}
steam_calls = [0]
steam_ok = [0]
last_steam = [0.0]


# ---------- memoria y estado ----------
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def load_seen():
    data = load_json(SEEN_FILE, [])
    return dict.fromkeys(data if isinstance(data, list) else [], 1)


def save_seen():
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(list(seen)[-5000:], f)
    except OSError:
        pass


def save_state():
    if TEST:
        return
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError:
        pass


seen = {} if TEST else load_seen()
_state = {} if TEST else load_json(STATE_FILE, {})
state = _state if isinstance(_state, dict) else {}


def now_local() -> datetime:
    return datetime.now(TZ) if TZ else datetime.now()


# ---------- cambio de moneda ----------
def get_eur_per_usd() -> float:
    """Euros por 1 dólar, con el cambio del día. Prueba varias fuentes."""
    sources = [
        ("https://api.frankfurter.dev/v1/latest", {"base": "USD", "symbols": "EUR"}),
        ("https://api.frankfurter.app/latest", {"from": "USD", "to": "EUR"}),
        ("https://open.er-api.com/v6/latest/USD", {}),
    ]
    for url, params in sources:
        try:
            r = requests.get(url, params=params, timeout=8)
            rate = float(r.json()["rates"]["EUR"])
            if 0.3 < rate < 3:
                print(f"Cambio del día: 1 USD = {rate:.4f} EUR")
                return rate
        except Exception:
            continue
    rate = 1 / FALLBACK_USD_PER_EUR
    print(f"AVISO: no pude obtener el cambio del día; uso 1 USD = {rate:.4f} EUR (valor fijo).")
    return rate


def init_rates() -> None:
    global EUR_PER_USD, MIN_CENTS, MAX_CENTS
    EUR_PER_USD = get_eur_per_usd()
    MIN_CENTS = max(1, int(MIN_EUR / EUR_PER_USD * 100))
    MAX_CENTS = int(MAX_EUR / EUR_PER_USD * 100)


# ---------- utilidades de dinero ----------
def parse_money(txt):
    """'0,12€' -> 0.12 ; '1.234,56€' -> 1234.56 ; '1,--€' -> 1.0"""
    if not txt:
        return None
    t = str(txt).replace("--", "00")
    t = re.sub(r"[^0-9.,]", "", t)
    if not t:
        return None
    if "," in t and "." in t:
        if t.rfind(",") > t.rfind("."):
            t = t.replace(".", "").replace(",", ".")
        else:
            t = t.replace(",", "")
    elif "," in t:
        t = t.replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def parse_int(txt):
    d = re.sub(r"[^0-9]", "", str(txt or ""))
    return int(d) if d else None


def steam_net(price_eur: float) -> float:
    """Lo que RECIBES al vender a ese precio en el mercado de Steam.
    Comisiones: 5 % (Steam) + 10 % (CS2), cada una con mínimo de 0,01 €."""
    cents = int(round(price_eur * 100))
    for s in range(cents, 0, -1):
        if s + max(1, s * 5 // 100) + max(1, s * 10 // 100) <= cents:
            return s / 100
    return 0.0


def eur(x: float) -> str:
    return f"{x:.2f} €"


def seur(x: float) -> str:
    return f"{x:+.2f} €"


def num_es(x: float) -> str:
    return f"{x:.2f}".replace(".", ",")


# ---------- avisos por Telegram ----------
def notify(text: str) -> None:
    print(text + "\n", flush=True)
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("TELEGRAM: faltan los secretos TELEGRAM_TOKEN o TELEGRAM_CHAT_ID (están vacíos).")
        return
    token = token.strip()
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat.strip(), "text": text, "disable_web_page_preview": "true"},
            timeout=10,
        )
        if r.ok:
            print("TELEGRAM: mensaje enviado correctamente.")
        else:
            print(f"TELEGRAM: ERROR {r.status_code}: {r.text.replace(token, '***')}")
    except requests.RequestException as e:
        print("TELEGRAM: error de red:", str(e).replace(token, "***"))


def warn(text: str, key: str = "general", hours: float = 3) -> None:
    """Aviso de fallo por Telegram, como mucho uno cada `hours` horas por tipo de fallo."""
    if TEST:
        notify("⚠️ " + text)
        return
    warns = state.setdefault("warns", {})
    if time.time() - warns.get(key, 0) < hours * 3600:
        print(f"(aviso de fallo «{key}» omitido: ya se envió hace menos de {hours} h) {text}")
        return
    warns[key] = time.time()
    notify("⚠️ " + text)


# ---------- precios de Steam ----------
def steam_price(name: str):
    """{'lowest','median','volume'} del mercado de Steam (en STEAM_CURRENCY) o None."""
    if name in steam_cache:
        return steam_cache[name]
    if steam_calls[0] >= MAX_STEAM_LOOKUPS:
        print(f"Steam: tope de {MAX_STEAM_LOOKUPS} consultas por pasada; no consulto «{name}».")
        return None
    result = None
    for attempt in range(2):
        wait = STEAM_PAUSE - (time.time() - last_steam[0])
        if wait > 0:
            time.sleep(wait)
        last_steam[0] = time.time()
        steam_calls[0] += 1
        try:
            r = requests.get(
                "https://steamcommunity.com/market/priceoverview/",
                params={"appid": 730, "currency": STEAM_CURRENCY, "market_hash_name": name},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15,
            )
        except requests.RequestException as e:
            print("Steam: error de red:", e)
            break
        if r.status_code == 429:
            if attempt == 0:
                print("Steam: 429 (demasiadas consultas). Espero 15 s y reintento.")
                time.sleep(15)
                continue
            print("Steam: 429 otra vez; sin precio para", name)
            break
        if not r.ok:
            print(f"Steam respondió {r.status_code} para «{name}»")
            break
        try:
            data = r.json()
        except ValueError:
            break
        if data.get("success"):
            low = parse_money(data.get("lowest_price"))
            med = parse_money(data.get("median_price"))
            if low is not None or med is not None:
                result = {"lowest": low, "median": med, "volume": parse_int(data.get("volume"))}
                steam_ok[0] += 1
        break
    steam_cache[name] = result
    return result


def steam_ref(sp):
    """Precio de venta de referencia en Steam: el anuncio más barato (si no hay, la mediana)."""
    if not sp:
        return None
    return sp.get("lowest") or sp.get("median")


def steam_link(name: str) -> str:
    return "https://steamcommunity.com/market/listings/730/" + quote(name)


# ---------- textos ----------
def charm_label(c: dict) -> str:
    pat = c.get("pattern")
    return f"{c.get('name', '?')}" + (f" #{pat}" if pat not in (None, "") else "")


def is_souvenir_charm(c: dict) -> bool:
    return "souvenir" in str(c.get("name", "")).lower()


def vol_txt(sp) -> str:
    v = (sp or {}).get("volume")
    return f" · ~{v} vendidos/24 h" if v is not None else ""


# ---------- registro de ofertas ----------
OFFERS_HEADER = [
    "fecha_hora", "arma", "compra_eur", "compra_usd", "steam_arma_eur", "recibes_arma_eur",
    "charms", "recibes_charms_eur", "beneficio_eur", "beneficio_con_quitar_charm_eur",
    "enlace_csfloat", "vendido_real_eur", "notas",
]


def log_offer(name, buy_eur, buy_usd, w_price, weapon_net, charm_text, charms_net,
              profit, profit_with_detach, lid) -> None:
    if TEST:
        return
    new = not os.path.exists(OFFERS_FILE)
    try:
        with open(OFFERS_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter=";")
            if new:
                w.writerow(OFFERS_HEADER)
            w.writerow([
                now_local().strftime("%Y-%m-%d %H:%M"), name, num_es(buy_eur), num_es(buy_usd),
                num_es(w_price), num_es(weapon_net), charm_text, num_es(charms_net),
                num_es(profit), num_es(profit_with_detach),
                f"https://csfloat.com/item/{lid}", "", "",
            ])
    except OSError as e:
        print("No pude escribir el registro de ofertas:", e)


# ---------- CSFloat ----------
fetch_calls = [0]


def fetch(cursor=None, page: int = 0):
    """Devuelve (anuncios, cursor_siguiente)."""
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
            warn("CSFloat limita las peticiones (error 429); esta pasada se ha cortado.",
                 key="csfloat429", hours=6)
            return [], None
        print("Límite de peticiones alcanzado, espero 2 minutos...")
        time.sleep(120)
        return [], None
    if not r.ok:
        print(f"CSFloat respondió {r.status_code}: {r.text[:200]}")
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict):
        rows, nxt = data.get("data", []), data.get("cursor")
    else:
        rows, nxt = data, None
    fetch_calls[0] += 1
    if fetch_calls[0] == 1:
        keys = sorted(data.keys()) if isinstance(data, dict) else "lista"
        print(f"Respuesta de CSFloat: claves = {keys}; ¿trae cursor? {'sí' if nxt else 'no'}")
    print(f"Llamada {fetch_calls[0]}: {len(rows)} anuncios recibidos de CSFloat.")
    return rows, nxt


# ---------- análisis de un anuncio ----------
def check(listing: dict) -> None:
    lid = listing.get("id")
    if not lid or lid in seen:
        return
    seen[lid] = 1
    stats["fetched"] += 1

    item = listing.get("item", {})
    name = item.get("market_hash_name", "")
    all_item_keys.update(item.keys())
    if not shown_keys:
        shown_keys.append(1)
        print("Campos del anuncio:", sorted(listing.keys()))
        print("Campos del objeto:", sorted(item.keys()))

    if WEAPON_FILTER and WEAPON_FILTER not in name.lower():
        return

    all_charms = item.get("keychains") or []
    if not all_charms:
        return

    # Souvenir fuera: armas souvenir y charms souvenir
    if item.get("is_souvenir") or name.lower().startswith("souvenir"):
        stats["souvenir"] += 1
        return
    charms = [c for c in all_charms if not is_souvenir_charm(c)]
    if not charms:
        stats["souvenir"] += 1
        return
    stats["with_charm"] += 1

    price_usd_cents = listing.get("price")
    if not price_usd_cents:
        return
    buy_usd = price_usd_cents / 100
    buy_eur = buy_usd * EUR_PER_USD

    if TEST and not example:
        example.append((name, charms, buy_usd, buy_eur, lid))

    # 1) precio del arma en Steam y si se vende lo bastante
    wsp = steam_price(name)
    w_price = steam_ref(wsp)
    if not w_price:
        stats["no_steam"] += 1
        print(f"Sin precio de Steam para el arma «{name}»: no se puede calcular con precisión.")
        return
    wvol = (wsp or {}).get("volume")
    if wvol is not None and wvol < MIN_VOLUME_WEAPON:
        stats["low_volume"] += 1
        print(f"Descartado por poca liquidez: {name} se vende ~{wvol}/24 h (mínimo {MIN_VOLUME_WEAPON}).")
        return

    # 2) solo si lo que pagas está por debajo del precio de Steam del arma
    if buy_eur >= w_price:
        print(f"Descartado: {name} | compras {eur(buy_eur)} >= Steam {eur(w_price)}")
        return
    stats["below_steam"] += 1
    pct_below = (w_price - buy_eur) / w_price * 100

    # 3) precio de cada charm en Steam y lo que recibirías por cada pieza
    rows = []
    charms_net = 0.0
    unknown = False
    for c in charms:
        cname = c.get("name", "")
        csp = steam_price(cname)
        cp = steam_ref(csp)
        row = {"c": c, "name": cname, "price": cp, "net": None, "sp": csp, "counts": False, "note": ""}
        if not cp:
            unknown = True
            row["note"] = "sin precio en Steam (no cuenta en el beneficio)"
        else:
            row["net"] = steam_net(cp)
            cvol = (csp or {}).get("volume")
            if cvol is not None and cvol < MIN_VOLUME_CHARM:
                row["note"] = f"se vende poco (~{cvol}/24 h): NO cuenta en el beneficio"
            else:
                row["counts"] = True
                charms_net += row["net"]
        rows.append(row)

    weapon_net = steam_net(w_price)
    total_net = weapon_net + charms_net
    detach_eur = DETACH_COST_USD * EUR_PER_USD * len(charms)
    profit = total_net - buy_eur - (detach_eur if COUNT_DETACH_IN_PROFIT else 0.0)
    profit_with_detach = total_net - buy_eur - detach_eur

    print(
        f"Candidato: {name} | compras {eur(buy_eur)} ({buy_usd:.2f} $) | Steam arma {eur(w_price)} "
        f"(recibes {eur(weapon_net)}) | charms recibes {eur(charms_net)} | beneficio {seur(profit)}"
    )
    if profit < MIN_PROFIT_EUR:
        return

    # 4) aviso
    lines = [
        f"🔔 OFERTA - ganarías {seur(profit)}",
        "",
        f"🔫 {name}",
        f"🛒 Compras en CSFloat: {buy_usd:.2f} $ = {eur(buy_eur)}",
        f"   ({pct_below:.1f} % por debajo del precio de Steam)",
        "",
        "💶 Venta por separado en Steam (lo que recibes tras comisiones de Steam):",
        f"• Arma: se vende a {eur(w_price)} → recibes {eur(weapon_net)}{vol_txt(wsp)}",
    ]
    for r_ in rows:
        label = charm_label(r_["c"])
        if r_["price"]:
            extra = f" ({r_['note']})" if r_["note"] else ""
            lines.append(
                f"• {label}: se vende a {eur(r_['price'])} → recibes {eur(r_['net'])}"
                f"{vol_txt(r_['sp'])}{extra}"
            )
        else:
            lines.append(f"• {label}: {r_['note']}")
    lines += [
        f"= Total que recibes: {eur(total_net)}",
        f"📊 Beneficio: {eur(total_net)} − {eur(buy_eur)} = {seur(total_net - buy_eur)}",
    ]
    if COUNT_DETACH_IN_PROFIT:
        lines.append(f"   (ya restado quitar el charm: {eur(detach_eur)})")
    else:
        lines.append(
            f"   Quitar el charm cuesta ~{eur(detach_eur)}; con ese gasto: {seur(profit_with_detach)}"
        )
    if unknown or any(r_["note"] for r_ in rows):
        lines.append("⚠️ Algún charm no cuenta en el beneficio (ver arriba).")
    lines.append("")
    if INCLUDE_CSFLOAT_LINK:
        lines.append(f"🔗 Comprar el arma (CSFloat, este anuncio exacto):\nhttps://csfloat.com/item/{lid}")
    for r_ in rows:
        lines.append(f"🔗 Comprar el charm suelto (Steam): {r_['name']}\n{steam_link(r_['name'])}")
    lines.append(f"🔗 Arma en Steam:\n{steam_link(name)}")

    stats["alerts"] += 1
    log_offer(
        name, buy_eur, buy_usd, w_price, weapon_net,
        "; ".join(charm_label(r_["c"]) for r_ in rows), charms_net,
        profit, profit_with_detach, lid,
    )
    notify("\n".join(lines))


# ---------- resumen diario ----------
def accumulate(scanned: int) -> None:
    acc = state.setdefault("acc", {})
    add = {
        "runs": 1, "read": scanned, "new": stats["fetched"], "with_charm": stats["with_charm"],
        "souvenir": stats["souvenir"], "below_steam": stats["below_steam"],
        "no_steam": stats["no_steam"], "low_volume": stats["low_volume"], "alerts": stats["alerts"],
    }
    for k, v in add.items():
        acc[k] = acc.get(k, 0) + v


def maybe_daily_summary() -> None:
    now = now_local()
    today = now.strftime("%Y-%m-%d")
    if now.hour < SUMMARY_HOUR or state.get("summary_date") == today:
        return
    acc = state.get("acc", {})
    if acc.get("runs"):
        repo = os.getenv("GITHUB_REPOSITORY")
        text = (
            "📋 Resumen (desde el último resumen)\n"
            f"• Pasadas realizadas: {acc.get('runs', 0)}\n"
            f"• Anuncios nuevos revisados: {acc.get('new', 0)}\n"
            f"• Con charm (sin souvenir): {acc.get('with_charm', 0)} "
            f"(souvenir descartados: {acc.get('souvenir', 0)})\n"
            f"• Por debajo del precio de Steam: {acc.get('below_steam', 0)}\n"
            f"• Descartados por poca liquidez: {acc.get('low_volume', 0)}\n"
            f"• Sin precio de Steam: {acc.get('no_steam', 0)}\n"
            f"• Ofertas avisadas: {acc.get('alerts', 0)}"
        )
        if repo:
            text += f"\n📄 Registro de ofertas: https://github.com/{repo}/blob/main/{OFFERS_FILE}"
        notify(text)
    state["acc"] = {}
    state["summary_date"] = today


# ---------- mensaje de prueba ----------
def build_test_example() -> str:
    if not example:
        return "No encontré anuncios con charm (no souvenir) en la ventana de prueba."
    name, charms, buy_usd, buy_eur, lid = example[0]
    wsp = steam_price(name)
    w_price = steam_ref(wsp)
    out = [
        "Ejemplo leído (solo prueba, no es una oferta):",
        f"🔫 {name}",
        f"🛒 CSFloat: {buy_usd:.2f} $ = {eur(buy_eur)} (cambio del día {EUR_PER_USD:.4f} €/$)",
        f"🏷 Charm: {'; '.join(charm_label(c) for c in charms)}",
    ]
    if w_price:
        out.append(f"💶 Steam arma: {eur(w_price)} → recibirías {eur(steam_net(w_price))}{vol_txt(wsp)}")
    else:
        out.append("💶 Steam arma: SIN PRECIO (Steam no respondió o limitó las consultas)")
    c0 = charms[0]
    csp = steam_price(c0.get("name", ""))
    cp = steam_ref(csp)
    if cp:
        out.append(f"💶 Steam charm: {eur(cp)} → recibirías {eur(steam_net(cp))}{vol_txt(csp)}")
    else:
        out.append("💶 Steam charm: SIN PRECIO (Steam no respondió o limitó las consultas)")
    out += [
        f"🔗 Charm suelto (Steam): {steam_link(c0.get('name', ''))}",
        f"🔗 CSFloat: https://csfloat.com/item/{lid}",
    ]
    return "\n".join(out)


# ---------- programa principal ----------
def run_once() -> None:
    first_run = not seen
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
                continue
            run_ids.add(lid)
            new_in_page += 1
            scanned += 1
            if lid in seen:
                hit_seen = True
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
        f"{stats['with_charm']} con charm (sin souvenir; {stats['souvenir']} souvenir descartados), "
        f"{stats['below_steam']} por debajo del precio de Steam, "
        f"{stats['low_volume']} descartados por poca liquidez, "
        f"{stats['no_steam']} sin precio de Steam, {stats['alerts']} avisos enviados."
    )
    print(resumen)
    extra = sorted(
        k for k in all_item_keys
        if "keychain" in k.lower() or "sticker" in k.lower() or "charm" in k.lower()
    )
    print("Campos de charm/sticker vistos en algún anuncio:", extra)

    # ¿Steam ha dejado de darnos precios?
    if steam_calls[0] > 0 and steam_ok[0] == 0:
        warn(
            f"Steam no devolvió ningún precio en esta pasada ({steam_calls[0]} consultas). "
            "Probablemente limita las consultas desde GitHub; sin precios de Steam no hay avisos.",
            key="steam", hours=6,
        )

    if TEST:
        notify(
            "✅ PRUEBA OK - el bot funciona y llega a tu Telegram\n\n"
            f"📋 {resumen}\n\n" + build_test_example()
        )
        return

    accumulate(scanned)
    maybe_daily_summary()
    save_state()


def main() -> None:
    init_rates()
    if ONCE:
        try:
            run_once()
        except Exception as e:  # cualquier fallo: avisar por Telegram y dejar la ejecución en rojo
            warn(f"El escáner falló: {type(e).__name__}: {str(e)[:300]}", key="crash", hours=3)
            save_state()
            raise
        return

    print(f"Escáner continuo. Rango {MIN_EUR:.2f}-{MAX_EUR:.2f} EUR. Filtro: {WEAPON_FILTER or 'todas'}.")
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
