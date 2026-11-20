#!/usr/bin/env python3
"""
Knuspr Food Waste Tracker
-------------------------
Überwacht die Kategorie "Rette Lebensmittel" auf knuspr.de
(https://www.knuspr.de/rette-lebensmittel) und erkennt:

  - neue gerettete Produkte
  - neue Charge (gleiches Produkt, neues Rettungsangebot)
  - Preissenkung / Preiserhöhung
  - ausverkauft / wieder verfügbar
  - nicht mehr im Angebot (verkauft oder abgelaufen)

Pro Lauf nur 2 Abrufe (Liste der Produkt-IDs + Produktdetails); Kategorienamen
werden nur nachgeladen, wenn ein unbekanntes Produkt auftaucht.

Gespeichert wird nur bei Änderungen – sonst höchstens alle 30 Minuten als
"Lebenszeichen" (schont GitHub Pages bei 5-Minuten-Takt).

Nur Python-Standardbibliothek – keine Installation nötig.
"""

from __future__ import annotations

import json
import os
import smtplib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

SHOP = "https://www.knuspr.de"
SHOP_NAME = "Knuspr"
API = f"{SHOP}/api/v1"
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data"
STATE_FILE = DATA / "state.json"
CATALOG_FILE = DATA / "catalog.json"
EVENTS_FILE = DATA / "events.json"
META_FILE = DATA / "meta.json"

MAX_EVENTS = 5000
MAX_HISTORY = 60
HEARTBEAT_MIN = 30           # ohne Änderungen höchstens so oft speichern
USER_AGENT = "Mozilla/5.0 (compatible; food-waste-tracker/1.0)"


# --------------------------------------------------------------------------- #
# Laden
# --------------------------------------------------------------------------- #
def get_json(url: str, retries: int = 3):
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                       "Accept": "application/json",
                                                       "Accept-Language": "de-DE"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"Abruf fehlgeschlagen: {url}: {last_err}")


def fetch_ids() -> list[int]:
    ids, page = [], 0
    while page < 20:
        d = get_json(f"{API}/categories/last-minute/products?page={page}&size=100&sort=expiration-asc")
        batch = d.get("productIds") or []
        ids += [i for i in batch if i not in ids]
        if len(batch) < 100:
            break
        page += 1
    return ids


def fetch_cards(ids: list[int]) -> list[dict]:
    cards = []
    for i in range(0, len(ids), 50):
        q = "&".join(f"products={pid}" for pid in ids[i:i + 50])
        cards += get_json(f"{API}/products/card?{q}&categoryType=last-minute")
    return cards


def fetch_category_map() -> dict[str, str]:
    """Produkt-ID → Unterkategorie (z. B. 'Fleisch & Fisch')."""
    out: dict[str, str] = {}
    structure = get_json(f"{API}/categories/last-minute/structure")
    cat_ids = [c["category"]["id"] for c in structure.get("components", [])
               if c.get("type") == "products-section" and c.get("category", {}).get("id")]
    for cid in cat_ids:
        try:
            name = get_json(f"{API}/categories/last-minute/{cid}").get("name") or "Sonstiges"
            page = 0
            while page < 10:
                d = get_json(f"{API}/categories/last-minute/{cid}/products?page={page}&size=100&sort=expiration-asc")
                batch = d.get("productIds") or []
                for pid in batch:
                    out.setdefault(str(pid), name)
                if len(batch) < 100:
                    break
                page += 1
        except Exception as e:  # noqa: BLE001
            print(f"Hinweis: Kategorie {cid} nicht lesbar: {e}", file=sys.stderr)
        time.sleep(0.3)
    return out


def cents(v) -> int | None:
    return None if v in (None, "") else round(float(v) * 100)


def normalize(cards: list[dict], cat_map: dict[str, str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for c in cards:
        pid = str(c["productId"])
        p = c.get("prices") or {}
        price, orig = cents(p.get("salePrice")), cents(p.get("originalPrice"))
        if price is None:
            price, orig = orig, None
        if orig is not None and price is not None and orig <= price:
            orig = None
        expiring = next((b.get("text") for b in c.get("badges", [])
                         if b.get("type") == "EXPIRING"), None)
        stock = c.get("stock") or {}
        out[pid] = {
            "id": pid,
            "title": c.get("name", ""),
            "vendor": (c.get("brand") or {}).get("name", "") if isinstance(c.get("brand"), dict) else (c.get("brand") or ""),
            "type": cat_map.get(pid, "Sonstiges"),
            "url": f"{SHOP}/{pid}-{c.get('slug', '')}",
            "image": (c.get("image") or {}).get("path"),
            "price": price,
            "compare_at": orig,
            "unit_price": cents(p.get("unitPrice")),
            "unit": c.get("unit") or "",
            "amount": c.get("textualAmount") or "",
            "expiring": expiring,
            "valid_till": p.get("saleValidTill"),
            "sale_id": p.get("saleId"),
            "stock": stock.get("maxAvailableAmount"),
            "available": stock.get("availabilityStatus") == "AVAILABLE" and not c.get("archived"),
        }
    return out


# --------------------------------------------------------------------------- #
# Vergleich
# --------------------------------------------------------------------------- #
def load_json(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def eur(c: int | None) -> str:
    if c is None:
        return "–"
    return f"{c / 100:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def discount_pct(price, compare) -> int | None:
    if price and compare and compare > price:
        return round((1 - price / compare) * 100)
    return None


def diff(old: dict, new: dict, gone: dict, now: str) -> list[dict]:
    events: list[dict] = []

    def ev(kind: str, item: dict, **extra):
        e = {"ts": now, "type": kind, "id": item["id"], "title": item["title"], "url": item["url"],
             "image": item.get("image"), "price": item.get("price"), "compare_at": item.get("compare_at"),
             "expiring": item.get("expiring"), "amount": item.get("amount")}
        e.update(extra)
        events.append(e)

    for pid, n in new.items():
        o = old.get(pid)
        if o is None:
            ev("back_in_stock" if pid in gone else "new", n)
            continue
        if n.get("sale_id") and o.get("sale_id") and n["sale_id"] != o["sale_id"]:
            extra = {}
            if o["price"] and n["price"] != o["price"]:
                extra = {"old_price": o["price"], "change_pct": round((n["price"] / o["price"] - 1) * 100, 1)}
            ev("new_batch", n, **extra)
        elif n["price"] != o["price"] and n["price"] and o["price"]:
            ev("price_drop" if n["price"] < o["price"] else "price_up", n,
               old_price=o["price"], change_pct=round((n["price"] / o["price"] - 1) * 100, 1))
        if n["available"] != o["available"]:
            ev("back_in_stock" if n["available"] else "sold_out", n)

    for pid, o in old.items():
        if pid not in new:
            ev("removed", o)
    return events


# --------------------------------------------------------------------------- #
# Benachrichtigungen
# --------------------------------------------------------------------------- #
LABELS = {
    "new": "🆕 Neu gerettet",
    "new_batch": "🔄 Neue Charge",
    "price_drop": "📉 Preis gesenkt",
    "price_up": "📈 Preis erhöht",
    "back_in_stock": "✅ Wieder da",
    "sold_out": "⛔ Ausverkauft",
    "removed": "🗑️ Nicht mehr im Angebot",
}


def notify_types() -> set[str]:
    raw = os.environ.get("NOTIFY_TYPES") or "new,new_batch,price_drop,back_in_stock"
    return {t.strip() for t in raw.split(",") if t.strip()}


def event_line(e: dict) -> str:
    label = LABELS.get(e["type"], e["type"])
    if e.get("old_price") is not None and e.get("change_pct") is not None:
        price = f'{eur(e["old_price"])} → {eur(e["price"])} ({e["change_pct"]:+.1f} %)'
    else:
        price = eur(e["price"])
        d = discount_pct(e["price"], e["compare_at"])
        if d:
            price += f" (−{d} % ggü. {eur(e['compare_at'])})"
    extra = f" · {e['expiring']}" if e.get("expiring") else ""
    return f"{label}: {e['title']} – {price}{extra}"


def rank_events(events: list[dict]) -> list[dict]:
    """Wichtigstes zuerst: größte Preissenkung, dann höchster Rabatt."""
    def rank(e):
        if e.get("old_price") is not None and e.get("change_pct") is not None and e["change_pct"] < 0:
            return (0, e["change_pct"])
        return (1, -(discount_pct(e["price"], e["compare_at"]) or 0))
    return sorted(events, key=rank)


def push_title(e: dict) -> str:
    label = LABELS.get(e["type"], e["type"])
    if e.get("old_price") is not None and e.get("change_pct") is not None:
        return f'{SHOP_NAME} · {label} {e["change_pct"]:+.0f} %'
    d = discount_pct(e["price"], e["compare_at"])
    return f"{SHOP_NAME} · {label}" + (f" −{d} %" if d else "")


def push_body(e: dict) -> str:
    if e.get("old_price") is not None and e.get("change_pct") is not None:
        price = f'{eur(e["old_price"])} → {eur(e["price"])}'
    else:
        price = eur(e["price"])
        if e.get("compare_at") and discount_pct(e["price"], e["compare_at"]):
            price += f" (statt {eur(e['compare_at'])})"
    details = " · ".join(x for x in (e.get("amount"), e.get("expiring")) if x)
    return f"{e['title']}\n{price}" + (f"\n{details}" if details else "")


def ntfy_publish(payload: dict) -> bool:
    """Veröffentlicht per JSON (UTF-8-sicher, mit Klick-Link und Bild).
    Bei Drosselung durch ntfy (HTTP 429) oder kurzen Störungen: warten und erneut versuchen."""
    server = (os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    headers = {"Content-Type": "application/json"}
    if os.environ.get("NTFY_TOKEN"):
        headers["Authorization"] = f'Bearer {os.environ["NTFY_TOKEN"]}'
    data = json.dumps(payload).encode("utf-8")
    for attempt in range(6):
        try:
            req = urllib.request.Request(server + "/", data=data, headers=headers, method="POST")
            urllib.request.urlopen(req, timeout=20).read()
            return True
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(10 * (attempt + 1))
                continue
            raise
        except urllib.error.URLError:
            time.sleep(5 * (attempt + 1))
    return False


def send_ntfy(events: list[dict]):
    """Jede Änderung als eigene Nachricht – niemals gesammelt.
    Tippen öffnet direkt die Produktseite. Versand in ruhigem Tempo,
    damit auch viele Änderungen auf einmal vollständig ankommen."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    failed = 0
    for i, e in enumerate(events):
        payload = {"topic": topic, "title": push_title(e), "message": push_body(e),
                   "click": e["url"], "tags": ["seedling"]}
        if e.get("image"):
            payload["attach"] = e["image"]
        if not ntfy_publish(payload):
            failed += 1
        time.sleep(1.0 if i < 50 else 5.0)   # ntfy erlaubt ca. 60 auf einmal, danach etwa 1 pro 5 s
    print(f"  ntfy: {len(events) - failed} von {len(events)} Nachrichten zugestellt")
    if failed:
        print(f"Warnung: {failed} Nachrichten konnten nicht zugestellt werden", file=sys.stderr)


def text_digest(events: list[dict]) -> tuple[str, str]:
    counts: dict[str, int] = {}
    for e in events:
        counts[e["type"]] = counts.get(e["type"], 0) + 1
    title = f"{SHOP_NAME}: " + ", ".join(f"{n}× {LABELS[k].split(' ', 1)[1]}" for k, n in counts.items())
    lines = [f"{event_line(e)}\n{e['url']}" for e in events[:40]]
    if len(events) > 40:
        lines.append(f"… und {len(events) - 40} weitere")
    return title, "\n\n".join(lines)


def send_telegram(title: str, body: str):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": f"{title}\n\n{body}"[:4000],
                                   "disable_web_page_preview": "true"}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20).read()


def send_email(title: str, body: str):
    host, to = os.environ.get("SMTP_HOST"), os.environ.get("MAIL_TO")
    if not (host and to):
        return
    user = os.environ.get("SMTP_USER", "")
    sender = os.environ.get("MAIL_FROM") or user
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = title, sender, to
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT") or "587"), timeout=30) as s:
        s.starttls()
        if user:
            s.login(user, os.environ.get("SMTP_PASSWORD", ""))
        s.sendmail(sender, [a.strip() for a in to.split(",")], msg.as_string())


def select_for_notify(events: list[dict]) -> list[dict]:
    wanted = notify_types()
    events = [e for e in events if e["type"] in wanted]
    min_change = float(os.environ.get("MIN_PRICE_CHANGE_PCT") or "0")
    events = [e for e in events
              if e["type"] not in ("price_drop", "price_up") or abs(e["change_pct"]) >= min_change]
    min_disc = float(os.environ.get("MIN_DISCOUNT_PCT") or "0")
    if min_disc:
        events = [e for e in events if e["type"] not in ("new", "new_batch")
                  or (discount_pct(e["price"], e["compare_at"]) or 0) >= min_disc]
    return rank_events(events)


def notify(events: list[dict]):
    events = select_for_notify(events)
    if not events:
        return
    title, body = text_digest(events)
    for fn, args in ((send_ntfy, (events,)),
                     (send_telegram, (title, body)),
                     (send_email, (title, body))):
        try:
            fn(*args)
        except Exception as e:  # noqa: BLE001  – Benachrichtigung darf Lauf nicht abbrechen
            print(f"Warnung: {fn.__name__} fehlgeschlagen: {e}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Hauptablauf
# --------------------------------------------------------------------------- #
def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    now_dt = datetime.now(timezone.utc).replace(microsecond=0)
    now = now_dt.isoformat()

    state = load_json(STATE_FILE, {})
    old_items: dict = state.get("items", {})
    history: dict = state.get("history", {})
    first_seen: dict = state.get("first_seen", {})
    gone: dict = state.get("gone", {})
    cat_map: dict = state.get("categories", {})
    baseline = not old_items

    ids = fetch_ids()
    # Leere Liste trotz vorher vieler Produkte → eher Störung als echte Leere
    if not ids and len(old_items) > 5:
        print("Leere Produktliste erhalten – Abbruch, Stand bleibt unverändert.", file=sys.stderr)
        return 1
    cards = fetch_cards(ids) if ids else []
    if ids and len(cards) < 0.9 * len(ids):
        print(f"Nur {len(cards)} von {len(ids)} Produktdetails geladen – Abbruch.", file=sys.stderr)
        return 1
    if any(str(i) not in cat_map for i in ids):
        try:
            cat_map.update(fetch_category_map())
        except Exception as e:  # noqa: BLE001
            print(f"Hinweis: Kategorien nicht lesbar ({e})", file=sys.stderr)
    current = normalize(cards, cat_map)

    events = [] if baseline else diff(old_items, current, gone, now)

    for pid, item in current.items():
        if pid not in first_seen or (pid in gone):
            first_seen[pid] = now if not baseline else first_seen.get(pid, now)
        gone.pop(pid, None)
        h = history.setdefault(pid, [])
        if not h or h[-1][1] != item["price"] or h[-1][2] != item["compare_at"]:
            h.append([now, item["price"], item["compare_at"]])
            del h[:-MAX_HISTORY]
    for pid, o in old_items.items():
        if pid not in current:
            gone[pid] = {"ts": now, "price": o["price"], "title": o["title"]}
    # Aufräumen: nach 60 Tagen Abwesenheit vergessen
    cutoff = time.time() - 60 * 86400
    for pid in [p for p, g in gone.items() if datetime.fromisoformat(g["ts"]).timestamp() < cutoff]:
        for d in (gone, history, first_seen, cat_map):
            d.pop(pid, None)

    catalog = sorted(({**item,
                       "discount_pct": discount_pct(item["price"], item["compare_at"]),
                       "first_seen": first_seen.get(pid),
                       "history": history.get(pid, [])} for pid, item in current.items()),
                     key=lambda x: (x.get("valid_till") or "9999", x["title"].lower()))

    # Nur speichern, wenn sich etwas geändert hat – sonst höchstens alle HEARTBEAT_MIN Minuten
    meta = load_json(META_FILE, {})
    old_catalog = load_json(CATALOG_FILE, None)
    last_run = meta.get("last_run")
    heartbeat_due = not last_run or \
        (now_dt - datetime.fromisoformat(last_run)).total_seconds() >= HEARTBEAT_MIN * 60
    changed = baseline or bool(events) or json.dumps(old_catalog, sort_keys=True) != json.dumps(catalog, sort_keys=True)

    print(f"{now}: {len(current)} Produkte, {len(events)} Änderungen"
          f"{' (Basislauf)' if baseline else ''}{'' if (changed or heartbeat_due) else ' – nichts zu speichern'}")
    for e in events[:50]:
        print("  " + event_line(e))

    if changed or heartbeat_due:
        all_events = load_json(EVENTS_FILE, [])
        if baseline:
            all_events.append({"ts": now, "type": "baseline", "count": len(current),
                               "title": f"Überwachung gestartet – {len(current)} gerettete Produkte erfasst"})
        all_events = (all_events + events)[-MAX_EVENTS:]
        state = {"items": current, "history": history, "first_seen": first_seen,
                 "gone": gone, "categories": cat_map}
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")), "utf-8")
        CATALOG_FILE.write_text(json.dumps(catalog, ensure_ascii=False, separators=(",", ":")), "utf-8")
        EVENTS_FILE.write_text(json.dumps(all_events, ensure_ascii=False, separators=(",", ":")), "utf-8")
        meta.update({"shop": f"{SHOP}/rette-lebensmittel", "shop_name": SHOP_NAME, "last_run": now,
                     "last_change": now if (events or baseline) else meta.get("last_change"),
                     "products": len(current), "variants": len(current),
                     "last_run_events": len(events), "started": meta.get("started", now)})
        META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")

    if events:
        notify(events)
    return 0


if __name__ == "__main__":
    sys.exit(main())
