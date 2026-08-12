#!/usr/bin/env python3
"""Refresh public Wachusett season-pass prices without exposing API tokens."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


AUTH_URL = "https://wp-api.wachusett.com/api/AccountAuthenticationJWT/AccountCreateTempCustomer"
PRODUCT_URL = "https://wp-api.wachusett.com/api/Store/GetBasicProductVUE"
REGULAR_SOURCE_URL = "https://www.wachusett.com/tickets-passes/season-passes/season-passes/"
GPS_SOURCE_URL = "https://www.wachusett.com/tickets-passes/group-season-passes/group-season-passes/"
OUTPUT_PATH = Path(__file__).resolve().parents[1] / "data" / "prices.json"

EXPECTED_KEYS = {
    f"{tier}_{age}"
    for tier in ("gold", "silver", "bronze")
    for age in ("adult", "junior", "senior")
}


class PassTileParser(HTMLParser):
    """Discover the product and variant IDs published on Wachusett's page."""

    def __init__(self) -> None:
        super().__init__()
        self.tiles: list[tuple[str, int, int]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "chute-basic-product-tile":
            return
        values = dict(attrs)
        tier = (values.get("themecolor") or "").lower()
        if tier not in {"gold", "silver", "bronze"}:
            return
        if values.get("published", "true").lower() != "true":
            return
        try:
            self.tiles.append((tier, int(values["productid"]), int(values["variantid"])))
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError(f"Invalid Wachusett pass tile: {values}") from error


def request_bytes(url: str, method: str = "GET") -> bytes:
    request = Request(
        url,
        method=method,
        headers={"Accept": "application/json", "User-Agent": "skiclub-gps-price-sync/1.0"},
    )
    with urlopen(request, timeout=30) as response:
        return response.read()


def request_json(url: str, method: str = "GET") -> dict:
    return json.loads(request_bytes(url, method=method))


def discover_variants(source_url: str) -> list[tuple[str, int, int]]:
    parser = PassTileParser()
    parser.feed(request_bytes(source_url).decode("utf-8"))
    if not parser.tiles:
        raise RuntimeError(f"No published Gold, Silver, or Bronze pass tiles were found at {source_url}")
    return parser.tiles


def fetch_price_set(source_url: str, token: str) -> tuple[dict, str | None]:
    passes = {}
    season = None
    for tier, product_id, variant_id in discover_variants(source_url):
        query = urlencode(
            {
                "productID": product_id,
                "variantID": variant_id,
                "published": "true",
                "token": token,
            }
        )
        product = request_json(f"{PRODUCT_URL}?{query}")
        variants = product.get("ProductVariants") or []
        if len(variants) != 1:
            raise RuntimeError(
                f"Expected one variant for product {product_id}/{variant_id}; received {len(variants)}"
            )

        variant = variants[0]
        variant_name = variant.get("VariantName") or variant.get("SEDescription") or ""
        age_match = re.match(r"(Adult|Junior|Senior)\b", variant_name, re.IGNORECASE)
        if not age_match:
            raise RuntimeError(f"Could not classify pass variant: {variant_name!r}")
        key = f"{tier}_{age_match.group(1).lower()}"
        if key in passes:
            raise RuntimeError(f"Wachusett published duplicate pass variant: {key}")

        price = variant.get("Price")
        if not isinstance(price, (int, float)) or price <= 0:
            raise RuntimeError(f"Invalid price for {key}: {price!r}")

        product_name = product.get("ProductName") or ""
        if season is None:
            season_match = re.search(r"\b\d{2}/\d{2}\b", product_name)
            season = season_match.group(0) if season_match else None

        passes[key] = {
            "product_id": product_id,
            "variant_id": variant_id,
            "product_name": product_name,
            "variant_name": variant_name,
            "price": float(price) if float(price) % 1 else int(price),
            "published": bool(variant.get("Published")),
        }

    missing = EXPECTED_KEYS - passes.keys()
    if missing:
        raise RuntimeError(f"Price sync returned an incomplete pass set from {source_url}: {sorted(missing)}")

    return passes, season


def fetch_prices() -> dict:
    auth = request_json(AUTH_URL, method="POST")
    token = auth.get("token")
    if not token:
        raise RuntimeError("Wachusett did not return a temporary public-session token")

    regular_passes, regular_season = fetch_price_set(REGULAR_SOURCE_URL, token)
    gps_passes, gps_season = fetch_price_set(GPS_SOURCE_URL, token)
    if regular_season and gps_season and regular_season != gps_season:
        raise RuntimeError(
            f"Regular and GPS prices are for different seasons: {regular_season} vs {gps_season}"
        )

    passes = {}
    for key in sorted(EXPECTED_KEYS):
        regular = regular_passes[key]
        gps = gps_passes[key]
        if gps["price"] > regular["price"]:
            raise RuntimeError(
                f"GPS price for {key} ({gps['price']}) exceeds regular price ({regular['price']})"
            )
        passes[key] = {
            "variant_name": gps["variant_name"],
            "regular_product_id": regular["product_id"],
            "regular_variant_id": regular["variant_id"],
            "regular_price": regular["price"],
            "gps_product_id": gps["product_id"],
            "gps_variant_id": gps["variant_id"],
            "gps_price": gps["price"],
            "published": bool(regular["published"] and gps["published"]),
        }

    return {
        "sources": {"regular": REGULAR_SOURCE_URL, "gps": GPS_SOURCE_URL},
        "season": gps_season or regular_season or "Current",
        "passes": passes,
    }


def main() -> None:
    fresh = fetch_prices()
    existing = json.loads(OUTPUT_PATH.read_text(encoding="utf-8")) if OUTPUT_PATH.exists() else {}

    comparable_existing = {key: value for key, value in existing.items() if key != "updated_at"}
    if comparable_existing == fresh:
        print("Prices are unchanged.")
        return

    fresh["updated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    ordered = {
        "sources": fresh["sources"],
        "season": fresh["season"],
        "updated_at": fresh["updated_at"],
        "passes": fresh["passes"],
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(ordered, indent=2) + "\n", encoding="utf-8")
    print(f"Updated {len(fresh['passes'])} pass prices.")


if __name__ == "__main__":
    main()
