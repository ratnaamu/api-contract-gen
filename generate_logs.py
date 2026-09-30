"""Generate deterministic sample HTTP logs (JSON lines) for a demo e-commerce API.

Usage:
    python generate_logs.py                      # writes sample_logs.jsonl (200 entries)
    python generate_logs.py --changed            # also writes changed_logs.jsonl (breaking changes for demo step 4)
    python generate_logs.py -n 300 -o logs.jsonl

Fixed seed => every teammate gets a byte-identical file.
"""
from __future__ import annotations

import argparse
import json
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

SEED = 42
START_TIME = datetime(2026, 9, 1, 9, 0, 0, tzinfo=timezone.utc)

FIRST = ["Alice", "Bob", "Carla", "Dev", "Elena", "Farid", "Grace", "Hiro", "Ines", "Jamal",
         "Kavya", "Liam", "Mei", "Noah", "Olga", "Priya", "Quinn", "Ravi", "Sara", "Tomás"]
LAST = ["Smith", "Khan", "Garcia", "Patel", "Novak", "Okafor", "Tanaka", "Silva", "Müller", "Rossi"]
CITIES = [("Austin", "US", "73301"), ("Bengaluru", "IN", "560001"), ("Berlin", "DE", "10115"),
          ("Lisbon", "PT", "1100-148"), ("Toronto", "CA", "M5H 2N2"), ("Osaka", "JP", "530-0001")]
STREETS = ["Main St", "Oak Ave", "MG Road", "Hauptstraße", "Rua Augusta", "King St W"]
PRODUCT_NAMES = ["Wireless Mouse", "Mechanical Keyboard", "USB-C Hub", "27in Monitor", "Laptop Stand",
                 "Noise-Cancelling Headphones", "Webcam 1080p", "Desk Lamp", "Ergonomic Chair",
                 "Portable SSD 1TB", "Phone Charger", "Bluetooth Speaker"]
CATEGORIES = ["electronics", "accessories", "office", "audio", "storage", "furniture"]
USER_AGENTS = ["curl/8.4.0", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "python-requests/2.32.3",
               "ShopApp-iOS/3.2.1", "ShopApp-Android/3.1.9"]
TAGS = ["vip", "beta", "newsletter", "wholesale", "early-adopter"]
ORDER_STATUSES = ["pending", "paid", "shipped", "delivered", "cancelled"]
COUPONS = ["WELCOME10", "FALL25", "FREESHIP"]

# Endpoint -> relative weight
ENDPOINTS: list[tuple[str, str, int]] = [
    ("GET", "/users", 25),
    ("GET", "/users/{id}", 35),
    ("POST", "/users", 20),
    ("GET", "/products", 25),
    ("GET", "/products/{id}", 35),
    ("POST", "/orders", 30),
    ("GET", "/orders/{id}", 30),
]


class Gen:
    def __init__(self, seed: int = SEED) -> None:
        self.rng = random.Random(seed)
        self.clock = START_TIME
        self.users: dict[int, dict[str, Any]] = {}
        self.products: dict[str, dict[str, Any]] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self.next_user_id = 1
        for _ in range(15):
            self._new_user()
        for _ in range(12):
            self._new_product()

    # ---------- primitives ----------
    def uuid(self) -> str:
        return str(uuid.UUID(int=self.rng.getrandbits(128), version=4))

    def iso(self, dt: datetime) -> str:
        return dt.isoformat().replace("+00:00", "Z")

    def tick(self) -> str:
        self.clock += timedelta(seconds=self.rng.randint(1, 90), milliseconds=self.rng.randint(0, 999))
        return self.iso(self.clock)

    def past_date(self) -> str:
        return self.iso(START_TIME - timedelta(days=self.rng.randint(1, 700), seconds=self.rng.randint(0, 86399)))

    def address(self) -> dict[str, Any]:
        city, country, zip_code = self.rng.choice(CITIES)
        addr: dict[str, Any] = {
            "street": f"{self.rng.randint(1, 999)} {self.rng.choice(STREETS)}",
            "city": city,
            "country": country,
            "zip": zip_code,
        }
        if self.rng.random() < 0.3:
            addr["line2"] = f"Apt {self.rng.randint(1, 50)}"
        return addr

    def email(self, first: str, last: str) -> str:
        dom = self.rng.choice(["example.com", "mail.test", "shop.dev"])
        return f"{first.lower()}.{last.lower()}{self.rng.randint(1, 99)}@{dom}".replace("á", "a").replace("ü", "u")

    def headers(self, method: str) -> dict[str, str]:
        h = {
            "Accept": "application/json",
            "User-Agent": self.rng.choice(USER_AGENTS),
            "X-Request-ID": self.uuid(),
        }
        if method in ("POST", "PUT", "PATCH"):
            h["Content-Type"] = "application/json"
        if self.rng.random() < 0.7:
            h["Authorization"] = "Bearer <redacted>"
        return h

    # ---------- domain objects ----------
    def _new_user(self, name: str | None = None, email: str | None = None, phone: str | None = None) -> dict[str, Any]:
        first, last = self.rng.choice(FIRST), self.rng.choice(LAST)
        user: dict[str, Any] = {
            "id": self.next_user_id,
            "name": name or f"{first} {last}",
            "email": email or self.email(first, last),
            "created_at": self.past_date(),
            "is_active": self.rng.random() < 0.85,
            "tags": self.rng.sample(TAGS, self.rng.randint(0, 2)),
        }
        if phone or self.rng.random() < 0.5:
            user["phone"] = phone or f"+1-555-{self.rng.randint(100, 999)}-{self.rng.randint(1000, 9999)}"
        if self.rng.random() < 0.6:
            user["address"] = self.address()
        if self.rng.random() < 0.4:
            user["preferences"] = {"newsletter": self.rng.random() < 0.5,
                                   "language": self.rng.choice(["en", "de", "pt", "ja", "hi"])}
        self.users[user["id"]] = user
        self.next_user_id += 1
        return user

    def _new_product(self) -> dict[str, Any]:
        stock = self.rng.choice([0, self.rng.randint(1, 500)])
        p: dict[str, Any] = {
            "id": self.uuid(),
            "name": self.rng.choice(PRODUCT_NAMES),
            "sku": f"SKU-{self.rng.randint(10000, 99999)}",
            "price": round(self.rng.uniform(5, 450), 2),
            "currency": "USD",
            "in_stock": stock > 0,
            "stock": stock,
            "categories": self.rng.sample(CATEGORIES, self.rng.randint(1, 3)),
            "created_at": self.past_date(),
        }
        if self.rng.random() < 0.5:
            p["rating"] = round(self.rng.uniform(2.5, 5.0), 1)
        if self.rng.random() < 0.4:
            p["dimensions"] = {"width": round(self.rng.uniform(1, 80), 1), "height": round(self.rng.uniform(1, 80), 1),
                               "depth": round(self.rng.uniform(1, 40), 1), "unit": "cm"}
        self.products[p["id"]] = p
        return p

    # ---------- error bodies ----------
    def not_found(self, resource: str, rid: Any) -> dict[str, Any]:
        return {"error": "not_found", "message": f"{resource} {rid} not found"}

    def bad_request(self, details: list[dict[str, str]]) -> dict[str, Any]:
        return {"error": "validation_error", "message": "Request body is invalid", "details": details}

    def server_error(self) -> dict[str, Any]:
        return {"error": "internal_error", "message": "Unexpected server error", "request_id": self.uuid()}

    # ---------- endpoint handlers: return (path, query, request_body, status, response_body) ----------
    def get_users(self):
        query: dict[str, Any] = {}
        page, limit = 1, 10
        if self.rng.random() < 0.6:
            page = self.rng.randint(1, 3)
            query["page"] = str(page)
        if self.rng.random() < 0.4:
            limit = self.rng.choice([5, 10, 20])
            query["limit"] = str(limit)
        all_users = sorted(self.users.values(), key=lambda u: u["id"])
        data = all_users[(page - 1) * limit: page * limit]
        return "/users", query, None, 200, {"data": data, "page": page, "limit": limit, "total": len(all_users)}

    def get_user(self):
        if self.rng.random() < 0.2:
            rid = self.rng.randint(900, 9999)
            return f"/users/{rid}", {}, None, 404, self.not_found("User", rid)
        uid = self.rng.choice(list(self.users))
        return f"/users/{uid}", {}, None, 200, self.users[uid]

    def post_user(self):
        first, last = self.rng.choice(FIRST), self.rng.choice(LAST)
        body: dict[str, Any] = {"name": f"{first} {last}", "email": self.email(first, last)}
        if self.rng.random() < 0.4:
            body["phone"] = f"+44-20-{self.rng.randint(1000, 9999)}-{self.rng.randint(1000, 9999)}"
        roll = self.rng.random()
        if roll < 0.12:
            del body["email"]
            return "/users", {}, body, 400, self.bad_request([{"field": "email", "issue": "required"}])
        if roll < 0.22:
            body["email"] = body["email"].replace("@", "_at_")
            return "/users", {}, body, 400, self.bad_request([{"field": "email", "issue": "invalid format"}])
        user = self._new_user(body["name"], body["email"], body.get("phone"))
        return "/users", {}, body, 201, user

    def get_products(self):
        query: dict[str, Any] = {}
        items = list(self.products.values())
        if self.rng.random() < 0.5:
            cat = self.rng.choice(CATEGORIES)
            query["category"] = cat
            items = [p for p in items if cat in p["categories"]]
        if self.rng.random() < 0.3:
            query["in_stock"] = "true"
            items = [p for p in items if p["in_stock"]]
        return "/products", query, None, 200, {"data": items, "total": len(items)}

    def get_product(self):
        if self.rng.random() < 0.2:
            rid = self.uuid()
            return f"/products/{rid}", {}, None, 404, self.not_found("Product", rid)
        pid = self.rng.choice(list(self.products))
        return f"/products/{pid}", {}, None, 200, self.products[pid]

    def post_order(self):
        uid = self.rng.choice(list(self.users))
        picks = self.rng.sample(list(self.products), self.rng.randint(1, 3))
        body: dict[str, Any] = {
            "user_id": uid,
            "items": [{"product_id": pid, "quantity": self.rng.randint(1, 4)} for pid in picks],
            "shipping_address": self.address(),
        }
        if self.rng.random() < 0.3:
            body["coupon_code"] = self.rng.choice(COUPONS)
        roll = self.rng.random()
        if roll < 0.1:
            body["items"] = []
            return "/orders", {}, body, 400, self.bad_request([{"field": "items", "issue": "must not be empty"}])
        if roll < 0.18:
            del body["user_id"]
            return "/orders", {}, body, 400, self.bad_request([{"field": "user_id", "issue": "required"}])
        items = [{"product_id": it["product_id"], "quantity": it["quantity"],
                  "unit_price": self.products[it["product_id"]]["price"]} for it in body["items"]]
        subtotal = round(sum(i["quantity"] * i["unit_price"] for i in items), 2)
        discount = round(subtotal * 0.1, 2) if "coupon_code" in body else 0.0
        order: dict[str, Any] = {
            "id": self.uuid(),
            "user_id": uid,
            "status": "pending",
            "items": items,
            "subtotal": subtotal,
            "discount": discount,
            "total": round(subtotal - discount, 2),
            "currency": "USD",
            "shipping_address": body["shipping_address"],
            "created_at": self.iso(self.clock),
            "notes": None if self.rng.random() < 0.7 else "Leave at front door",
        }
        if "coupon_code" in body:
            order["coupon_code"] = body["coupon_code"]
        self.orders[order["id"]] = order
        return "/orders", {}, body, 201, order

    def get_order(self):
        if not self.orders or self.rng.random() < 0.2:
            rid = self.uuid()
            return f"/orders/{rid}", {}, None, 404, self.not_found("Order", rid)
        oid = self.rng.choice(list(self.orders))
        order = self.orders[oid]
        order["status"] = self.rng.choice(ORDER_STATUSES)  # status progresses over time
        if order["status"] in ("shipped", "delivered") and "tracking" not in order:
            order["tracking"] = {"carrier": self.rng.choice(["UPS", "DHL", "FedEx"]),
                                 "number": f"1Z{self.rng.randint(10**9, 10**10 - 1)}",
                                 "events": [{"at": self.iso(self.clock), "status": "label_created"}]}
        return f"/orders/{oid}", {}, None, 200, order

    HANDLERS = {
        ("GET", "/users"): "get_users", ("GET", "/users/{id}"): "get_user", ("POST", "/users"): "post_user",
        ("GET", "/products"): "get_products", ("GET", "/products/{id}"): "get_product",
        ("POST", "/orders"): "post_order", ("GET", "/orders/{id}"): "get_order",
    }

    def entry(self, method: str, template: str) -> dict[str, Any]:
        ts = self.tick()
        headers = self.headers(method)
        path, query, req, status, resp = getattr(self, self.HANDLERS[(method, template)])()
        if status in (200, 201) and self.rng.random() < 0.03:  # sprinkle a few 500s
            status, resp = 500, self.server_error()
        return {"timestamp": ts, "method": method, "path": path, "query": query,
                "request_body": req, "status": status, "response_body": json.loads(json.dumps(resp)),
                "headers": headers}


def generate(n: int = 200, seed: int = SEED) -> list[dict[str, Any]]:
    g = Gen(seed)
    weights = [w for _, _, w in ENDPOINTS]
    out = []
    for _ in range(n):
        method, template, _ = g.rng.choices(ENDPOINTS, weights=weights, k=1)[0]
        out.append(g.entry(method, template))
    return out


def generate_changed(seed: int = SEED + 1) -> list[dict[str, Any]]:
    """Logs for demo step 4: GET /users/{id} now returns a string `user_id`, drops `email`,
    renames `name` -> `full_name`; plus a brand-new endpoint DELETE /users/{id}."""
    g = Gen(seed)
    g.clock = START_TIME + timedelta(days=2)
    out = []
    for i in range(20):
        ts = g.tick()
        uid = g.rng.choice(list(g.users))
        if i % 4 == 3:
            out.append({"timestamp": ts, "method": "DELETE", "path": f"/users/{uid}", "query": {},
                        "request_body": None, "status": 204, "response_body": None, "headers": g.headers("DELETE")})
            continue
        u = dict(g.users[uid])
        u["user_id"] = f"usr_{u.pop('id'):06d}"
        u["full_name"] = u.pop("name")
        u.pop("email")
        out.append({"timestamp": ts, "method": "GET", "path": f"/users/{uid}", "query": {},
                    "request_body": None, "status": 200, "response_body": u, "headers": g.headers("GET")})
    return out


def write_jsonl(entries: list[dict[str, Any]], path: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def downsample_errors(entries: list[dict[str, Any]], rate: float, seed: int) -> list[dict[str, Any]]:
    """Keep only `rate` fraction of 4xx/5xx entries (every 2xx/3xx entry is kept); simulates a realistic
    training log where errors are rare/under-logged compared to the traffic that actually occurs (B6) —
    see --error-rate/--holdout-errors."""
    rng = random.Random(seed)
    out = []
    for e in entries:
        try:
            is_error = int(e.get("status")) >= 400
        except (TypeError, ValueError):
            is_error = False
        if is_error and rng.random() >= rate:
            continue
        out.append(e)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-n", type=int, default=200, help="number of entries (default 200)")
    ap.add_argument("-o", "--output", default="sample_logs.jsonl")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--changed", action="store_true", help="also write changed_logs.jsonl")
    ap.add_argument("--error-rate", type=float, default=None,
                    help="down-sample 4xx/5xx entries in --output to this fraction (e.g. 0.1 keeps "
                         "~10%% of the errors that actually occurred); omit to keep all of them (B6)")
    ap.add_argument("--holdout-errors", metavar="PATH",
                    help="also write the FULL traffic, before any --error-rate down-sampling, to PATH "
                         "— ground truth for `score.py --holdout` (B6)")
    args = ap.parse_args()

    entries = generate(args.n, args.seed)
    if args.holdout_errors:
        write_jsonl(entries, args.holdout_errors)
        print(f"Wrote {len(entries)} entries to {args.holdout_errors} (full traffic, holdout ground truth)")
    if args.error_rate is not None:
        entries = downsample_errors(entries, args.error_rate, args.seed + 1)
    write_jsonl(entries, args.output)
    counts: dict[str, int] = {}
    for e in entries:
        counts[str(e["status"])] = counts.get(str(e["status"]), 0) + 1
    print(f"Wrote {len(entries)} entries to {args.output}  status counts: {dict(sorted(counts.items()))}")
    if args.changed:
        changed = generate_changed()
        write_jsonl(changed, "changed_logs.jsonl")
        print(f"Wrote {len(changed)} entries to changed_logs.jsonl")


if __name__ == "__main__":
    main()
