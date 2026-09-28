#!/usr/bin/env python3
"""Monitor konkretnych wariantów WooCommerce; wyłącznie biblioteka standardowa."""

import argparse
import copy
import json
import os
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
TIMEOUT = 12
MAX_RESPONSE = 4 * 1024 * 1024


class MonitorError(Exception):
    """Wyłącznie kontrolowane komunikaty: bez URL, odpowiedzi serwera i sekretów."""


class VariationForms(HTMLParser):
    def __init__(self, product_id):
        super().__init__(convert_charrefs=True)
        self.product_id = str(product_id)
        self.variations = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if (tag == "form" and attrs.get("data-product_id") == self.product_id
                and "variations_form" in attrs.get("class", "").split()):
            self.variations.append(attrs.get("data-product_variations"))


def parse_available(document, product):
    parser = VariationForms(product["product_id"])
    parser.feed(document)
    if len(parser.variations) != 1:
        raise MonitorError("Brak jednoznacznego formularza produktu")
    try:
        variants = json.loads(parser.variations[0])
    except (TypeError, ValueError):
        raise MonitorError("Nieprawidłowe dane wariantów") from None
    if not isinstance(variants, list) or not all(isinstance(v, dict) for v in variants):
        raise MonitorError("Nieznany format wariantów")
    matches = [v for v in variants if v.get("attributes") == product["attributes"]]
    if len(matches) != 1:
        raise MonitorError("Brak jednoznacznego wariantu Gospodarstwo domowe")
    variant = matches[0]
    if (type(variant.get("variation_id")) is not int
            or variant["variation_id"] != product["variation_id"]):
        raise MonitorError("Niezgodny identyfikator wariantu")
    flags = [variant.get("is_in_stock"), variant.get("is_purchasable")]
    for name in ("variation_is_active", "variation_is_visible"):
        if name in variant:
            flags.append(variant[name])
    if not all(type(flag) is bool for flag in flags):
        raise MonitorError("Nieprawidłowe flagi dostępności wariantu")
    return all(flags)


def request_bytes(request, attempts=2):
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=TIMEOUT) as response:
                if not 200 <= response.status < 300:
                    raise MonitorError("Niepoprawny status HTTP")
                body = response.read(MAX_RESPONSE + 1)
                if len(body) > MAX_RESPONSE:
                    raise MonitorError("Odpowiedź serwera jest zbyt duża")
                return body
        except HTTPError as error:
            if error.code != 429 and error.code < 500:
                raise MonitorError("Serwer odrzucił żądanie HTTP") from None
        except (URLError, OSError, ValueError):
            pass
        if attempt + 1 < attempts:
            time.sleep(1)
    raise MonitorError("Nie udało się wykonać żądania HTTP")


def fetch_product(product):
    request = Request(product["url"], headers={
        "User-Agent": "GrudzienStockMonitor/1.0",
        "Cache-Control": "no-cache",
        "Accept": "text/html",
    })
    try:
        return request_bytes(request).decode("utf-8")
    except UnicodeError:
        raise MonitorError("Nieprawidłowe kodowanie strony") from None


def notify(topic, title, message, url=None, priority=3):
    if not topic or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", topic):
        raise MonitorError("Brak lub nieprawidłowy sekret NTFY_TOPIC")
    payload = {"topic": topic, "title": title, "message": message, "priority": priority}
    if url:
        payload["click"] = url
        payload["actions"] = [{"action": "view", "label": "Otwórz produkt", "url": url}]
    request = Request("https://ntfy.sh/", data=json.dumps(payload).encode("utf-8"),
                      headers={"Content-Type": "application/json"}, method="POST")
    # POST bez natychmiastowego retry: timeout może oznaczać utraconą odpowiedź.
    # Niezapisana zmiana będzie ponowiona podczas następnego uruchomienia.
    response = request_bytes(request, attempts=1)
    try:
        result = json.loads(response)
    except (ValueError, UnicodeError):
        raise MonitorError("Nieprawidłowe potwierdzenie ntfy") from None
    if (not isinstance(result, dict) or result.get("event") != "message"
            or result.get("topic") != topic or not result.get("id")):
        raise MonitorError("Brak potwierdzenia przyjęcia wiadomości ntfy")


def load_state(path):
    if not path.exists():
        return {"version": 1, "products": {}, "failures": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if (state["version"] != 1 or not isinstance(state["products"], dict)
                or not isinstance(state["failures"], dict)):
            raise ValueError
        for entry in state["products"].values():
            if type(entry["available"]) is not bool:
                raise ValueError
            observed = datetime.fromisoformat(entry["observed_at"])
            if observed.tzinfo is None:
                raise ValueError
        if any(value is not True for value in state["failures"].values()):
            raise ValueError
        return state
    except (ValueError, KeyError, TypeError, OSError):
        raise MonitorError("Nieprawidłowy lub nieczytelny plik stanu; stan zachowano") from None


def save_state(path, state):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def check_products(products, state, topic, *, fetch=fetch_product, push=notify, clock=None,
                   dry_run=False):
    """Aktualizuje niezależne produkty; zwraca niezerowy kod przy dowolnym błędzie."""
    failures = 0
    clock = clock or (lambda: datetime.now(timezone.utc))
    for product in products:
        key = product["key"]
        try:
            available = parse_available(fetch(product), product)
        except Exception:
            # Nigdy nie loguj surowych wyjątków bibliotek ani odpowiedzi serwerów.
            print(f"{key}: błąd pobierania lub rozpoznania wariantu", file=sys.stderr)
            failures += 1
            if not dry_run and not state["failures"].get(key):
                try:
                    push(topic, "Błąd monitora Grudzień",
                         f"{product['name']}: nie udało się sprawdzić dostępności. "
                         "Ostatni znany stan zachowano. Szczegóły: GitHub Actions.",
                         url=product["url"])
                    state["failures"][key] = True
                except Exception:
                    print(f"{key}: nie udało się wysłać alertu błędu", file=sys.stderr)
            continue
        print(f"{key}: {'dostępny' if available else 'niedostępny'}")
        if dry_run:
            continue
        observed_at = clock().astimezone(timezone.utc).isoformat(timespec="seconds")
        previous = state["products"].get(key)
        changed = previous is None or previous["available"] != available
        if changed and (previous is not None or available):
            try:
                push(topic, f"{product['name']}: {'dostępny' if available else 'wyprzedany'}",
                     f"Wariant: Gospodarstwo domowe. "
                     f"{'Produkt można kupić.' if available else 'Produkt nie jest już dostępny do zakupu.'}",
                     url=product["url"], priority=4 if available else 3)
            except Exception:
                failures += 1
                print(f"{key}: nie wysłano powiadomienia; zmiana zostanie ponowiona", file=sys.stderr)
                continue
        recovered = state["failures"].pop(key, None)
        if (changed or recovered
                or previous["observed_at"][:10] != observed_at[:10]):
            state["products"][key] = {"available": available, "observed_at": observed_at}
    return 1 if failures else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--test-notification", action="store_true")
    parser.add_argument("--products", type=Path, default=ROOT / "products.json")
    parser.add_argument("--state", type=Path, default=ROOT / "state.json")
    args = parser.parse_args(argv)
    try:
        topic = os.environ.get("NTFY_TOPIC", "")
        if args.test_notification:
            notify(topic, "Test monitora Grudzień", "Powiadomienia działają. Stan produktów pozostaje bez zmian.")
            print("Wysłano powiadomienie testowe.")
            return 0
        if not args.dry_run and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", topic):
            raise MonitorError("Brak lub nieprawidłowy sekret NTFY_TOPIC")
        products = json.loads(args.products.read_text(encoding="utf-8"))
        if not isinstance(products, list) or not products:
            raise MonitorError("Nieprawidłowa konfiguracja produktów")
        state = load_state(args.state) if not args.dry_run else {"products": {}, "failures": {}}
        original = copy.deepcopy(state)
        result = check_products(products, state, topic, dry_run=args.dry_run)
        if not args.dry_run and state != original:
            save_state(args.state, state)
        return result
    except MonitorError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print("Błąd konfiguracji lub zapisu; sprawdź pliki monitora.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
