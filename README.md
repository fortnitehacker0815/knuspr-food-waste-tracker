# 🌱 Knuspr Food Waste Tracker

Überwacht die Kategorie **[Rette Lebensmittel](https://www.knuspr.de/rette-lebensmittel)** auf knuspr.de
**alle 5 Minuten**. Das sind Produkte kurz vor Ablauf des Haltbarkeitsdatums zu deutlich reduzierten Preisen.

| Ereignis | Bedeutung | Push standardmäßig |
|---|---|---|
| 🆕 Neu gerettet | Produkt neu in „Rette Lebensmittel“ | ✅ |
| 🔄 Neue Charge | Gleiches Produkt, neues Rettungsangebot (z. B. neues Datum/neuer Preis) | ✅ |
| 📉 Preis gesenkt | Rettungspreis gesunken | ✅ |
| ✅ Wieder da | Produkt war weg/ausverkauft und ist wieder verfügbar | ✅ |
| 📈 Preis erhöht | Rettungspreis gestiegen | – (nur Dashboard) |
| ⛔ Ausverkauft | Noch gelistet, aber nicht bestellbar | – (nur Dashboard) |
| 🗑️ Nicht mehr im Angebot | Verkauft oder abgelaufen | – (nur Dashboard) |

**Push-Nachrichten:** Jedes Produkt kommt als eigene Nachricht mit Produktbild, Preis, Menge und
Haltbarkeit (z. B. „Morgen verbrauchen“). Ein Tipp darauf öffnet direkt die Produktseite bei Knuspr.

## So funktioniert es
- Eine GitHub Action ruft alle 5 Minuten die interne Produktliste der Knuspr-Seite ab, genau wie der
  Browser beim Öffnen der Seite. Das sind nur **2 Abrufe pro Lauf**.
- Gespeichert wird nur, wenn sich etwas geändert hat, ansonsten alle 30 Minuten als Lebenszeichen.
  So bleibt GitHub Pages trotz 5-Minuten-Takt flott.
- Das Dashboard (`docs/index.html`) aktualisiert sich im Browser jede Minute von selbst.

**Hinweis zum 5-Minuten-Takt:** 5 Minuten ist das kürzeste Intervall, das GitHub erlaubt. Bei hoher
Last startet GitHub geplante Läufe manchmal einige Minuten später.

**Hinweis zum Liefergebiet:** Knuspr zeigt ohne Anmeldung das Sortiment des Standard-Lagers.
Die geretteten Produkte können sich je nach Liefergebiet unterscheiden.

## Dashboard
- **Neuigkeiten:** alle Änderungen chronologisch, filterbar
- **Neu (24 h):** Produkte, die in den letzten 24 Stunden dazugekommen sind
- **Top-Rabatte:** nach Ersparnis sortiert
- **Günstigste:** das billigste Produkt zuerst
- **Alle Produkte:** sortierbare Tabelle mit Haltbarkeit
- Auf jeder Kachel: Haltbarkeit (rot = heute, gelb = morgen), Menge und „noch X Stück“ bei geringem Bestand
- Klick auf ein Produkt → direkt zur Produktseite bei Knuspr
- Nicht verfügbare Produkte werden automatisch ausgeblendet

## Einstellungen (GitHub → Settings → Secrets and variables → Actions)

| Name | Art | Beispiel | Wirkung |
|---|---|---|---|
| `NTFY_TOPIC` | Secret | `knusprtracker` | ntfy-Kanal für Push-Nachrichten |
| `MAX_PUSH_PER_RUN` | Variable | `5` | Höchstens so viele Einzel-Nachrichten pro Lauf (Standard 10), der Rest kommt gesammelt |
| `NOTIFY_TYPES` | Variable | `new,new_batch` | Nur diese Ereignisse pushen (Standard: `new,new_batch,price_drop,back_in_stock`) |
| `MIN_DISCOUNT_PCT` | Variable | `40` | Neue Produkte nur pushen, wenn sie mindestens 40 % reduziert sind |
| `MIN_PRICE_CHANGE_PCT` | Variable | `10` | Preisänderungen unter 10 % nicht pushen |

Telegram (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) und E-Mail (`SMTP_*`, `MAIL_FROM`, `MAIL_TO`)
funktionieren wie bei den anderen Monitoren.

## Lokal testen
```bash
python3 scraper/monitor.py
python3 -m http.server 8767 --directory docs   # http://localhost:8767
```
