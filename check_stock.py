#!/usr/bin/env python3
"""Check Apple AU Sydney store pickup availability for iPhone Duo 256GB (Midnight).

Standard library only (Python 3.8+).

Usage:
    python3 check_stock.py                       # one check
    python3 check_stock.py --watch 300           # re-check every 5 minutes
    python3 check_stock.py --watch 300 --ntfy my-secret-topic   # phone push via ntfy.sh
    python3 check_stock.py --part MXXXXX/A       # skip page discovery, use a known part number

How it works:
  1. Fetches the product page and finds the part number(s) for the wanted
     capacity + colour (Apple embeds the product matrix as JSON in the page).
  2. Calls Apple's pickup-availability endpoint (/au/shop/fulfillment-messages)
     for those part numbers around a Sydney postcode.
  3. Prints/notifies when any Sydney store shows the item as available.
"""
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PRODUCT_URL = "https://www.apple.com/au/shop/buy-iphone/iphone-duo/7.6-inch-display-256gb-night-sky"
FULFILL_URL = "https://www.apple.com/au/shop/fulfillment-messages"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-AU,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def walk(node):
    """Yield every dict inside a nested JSON structure."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def extract_json_blobs(html):
    """Pull JSON objects out of <script> tags and window.X = {...} assignments."""
    for m in re.finditer(r"<script[^>]*>(.*?)</script>", html, re.S):
        body = m.group(1).strip()
        if body.startswith("{") or body.startswith("["):
            try:
                yield json.loads(body)
            except ValueError:
                pass
        for a in re.finditer(r"=\s*(\{.*\})\s*;?\s*$", body, re.S):
            try:
                yield json.loads(a.group(1))
            except ValueError:
                pass


def find_part_numbers(html, colour_words, capacity):
    """Return {partNumber: description} for products matching colour + capacity."""
    found = {}
    cap = capacity.lower().replace(" ", "")
    for blob in extract_json_blobs(html):
        for d in walk(blob):
            part = d.get("partNumber")
            if not isinstance(part, str):
                continue
            text = json.dumps(d).lower()
            if cap in text.replace(" ", "") and any(c in text for c in colour_words):
                found[part] = d.get("name") or d.get("productName") or d.get("description") or ""
    # Fallback: regex over raw HTML for Apple-style part numbers near the keywords.
    if not found:
        for m in re.finditer(r'"partNumber"\s*:\s*"([A-Z0-9]{5,}/[A-Z])"', html):
            window = html[max(0, m.start() - 600): m.end() + 600].lower()
            if cap in window.replace(" ", "") and any(c in window for c in colour_words):
                found[m.group(1)] = ""
    return found


def check_availability(parts, location):
    params = [("pl", "true"), ("mts.0", "regular"), ("location", location)]
    for i, p in enumerate(parts):
        params.append((f"parts.{i}", p))
    data = json.loads(http_get(FULFILL_URL + "?" + urllib.parse.urlencode(params)))
    stores = (data.get("body", {}).get("content", {})
              .get("pickupMessage", {}).get("stores", []))
    return stores


def sydney_hits(stores, parts, state="NSW"):
    """Return list of (store name, part, quote) that are available in the state."""
    hits = []
    for s in stores:
        if state and (s.get("state") or "").upper() != state.upper():
            continue
        for part, info in (s.get("partsAvailability") or {}).items():
            if part in parts and (info.get("pickupDisplay") == "available"):
                hits.append((s.get("storeName", "?"), part,
                             info.get("pickupSearchQuote") or info.get("storePickupQuote") or ""))
    return hits


def notify(topic, message):
    if not topic:
        return
    try:
        req = urllib.request.Request("https://ntfy.sh/" + urllib.parse.quote(topic),
                                     data=message.encode(), method="POST",
                                     headers={"Title": "iPhone Duo in stock!", "Priority": "high"})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:  # notification failure must not kill the watcher
        print(f"  (ntfy failed: {e})", file=sys.stderr)


def run_once(args, parts):
    stores = check_availability(parts, args.location)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    names = [s.get("storeName") for s in stores
             if not args.state or (s.get("state") or "").upper() == args.state.upper()]
    hits = sydney_hits(stores, parts, args.state)
    if hits:
        lines = [f"{n}: {q or 'available'}" for n, _, q in hits]
        print(f"[{stamp}] IN STOCK -> " + "; ".join(lines))
        notify(args.ntfy, "\n".join(lines) + f"\n{PRODUCT_URL}")
        return True
    print(f"[{stamp}] not available at {len(names)} {args.state} store(s): {', '.join(names) or 'none returned'}")
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=PRODUCT_URL, help="product page to discover part numbers from")
    ap.add_argument("--part", action="append", help="part number(s), e.g. MXXXXX/A (skips discovery)")
    ap.add_argument("--capacity", default="256GB")
    ap.add_argument("--colour", action="append",
                    help="colour keyword(s) to match (default: midnight, night sky)")
    ap.add_argument("--location", default="Sydney NSW 2000", help="postcode/suburb for store search")
    ap.add_argument("--state", default="NSW", help="only report stores in this state ('' = any)")
    ap.add_argument("--watch", type=int, metavar="SECONDS", help="repeat every N seconds")
    ap.add_argument("--ntfy", metavar="TOPIC", help="send a push via ntfy.sh to this topic")
    args = ap.parse_args()

    parts = args.part
    if not parts:
        colours = [c.lower() for c in (args.colour or ["midnight", "night sky", "night-sky"])]
        print(f"Discovering part numbers from {args.url} ...")
        try:
            found = find_part_numbers(http_get(args.url), colours, args.capacity)
        except (urllib.error.URLError, OSError) as e:
            sys.exit(f"Could not fetch product page: {e}")
        if not found:
            sys.exit("No matching part number found on the page. Apple may have changed the "
                     "layout; find the part number (e.g. in the page source, or the 'part=' value in "
                     "the URL when you click 'Check availability') and pass --part.")
        parts = list(found)
        for p, d in found.items():
            print(f"  {p}  {d}")

    while True:
        try:
            hit = run_once(args, parts)
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"[{time.strftime('%H:%M:%S')}] check failed: {e}", file=sys.stderr)
            hit = False
        if not args.watch:
            sys.exit(0 if hit else 1)
        time.sleep(args.watch)


if __name__ == "__main__":
    main()
