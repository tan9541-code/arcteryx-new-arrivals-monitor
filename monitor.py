import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup

STATE_FILE = Path("data/products.json")
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

SITES = [
    ("Vallgatan 12 - Page 1", "https://vallgatan12.com/en/collections/arc-teryx"),
    ("Vallgatan 12 - Page 2", "https://vallgatan12.com/en/collections/arc-teryx?page=2"),
    ("KA-YO", "https://www.ka-yo.com/hk/en/l/arcteryx"),
    ("Le Labo Store", "https://lelabostore.com/en/collections/arcteryx?filter.v.availability=1"),
    ("Labels Fashion - Page 1", "https://labelsfashion.com/collections/men-arcteryx"),
    ("Labels Fashion - Page 2", "https://labelsfashion.com/collections/men-arcteryx?page=2"),
    ("Behind the Pines - Page 1", "https://www.behindthepines.eu/collections/arcteryx"),
    ("Behind the Pines - Page 2", "https://www.behindthepines.eu/collections/arcteryx?page=2"),
    ("Graduate Store", "https://graduatestore.fr/en/276_arc-teryx"),
    ("Livio Sport - Page 1", "https://www.liviosport.it/en/brand/23-arc-teryx"),
    ("Livio Sport - Page 2", "https://www.liviosport.it/en/brand/23-arc-teryx?page=2"),
    ("Livio Sport - Page 3", "https://www.liviosport.it/en/brand/23-arc-teryx?page=3"),
    ("Livio Sport - Page 4", "https://www.liviosport.it/en/brand/23-arc-teryx?page=4"),
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PersonalProductMonitor/1.0; +https://github.com/)"
}
EXCLUDE_WORDS = (
    "collection", "collections", "category", "categories", "brand", "brands",
    "search", "account", "cart", "wishlist", "login", "register", "customer",
    "contact", "about", "pages", "blogs", "blog", "arcteryx?sort", "filter"
)
PRODUCT_HINTS = ("/products/", "/product/", "/p/", "/en/product/", "/shop/")

def clean_url(url):
    url, _ = urldefrag(url)
    parsed = urlparse(url)
    # Remove common tracking parameters; preserve page/category pagination URLs only for source URLs.
    return parsed._replace(fragment="", query="").geturl().rstrip("/")

def is_probable_product_link(href, text, source_url):
    parsed = urlparse(href)
    path = parsed.path.lower().rstrip("/")
    source = urlparse(source_url)
    if not parsed.scheme.startswith("http") or parsed.netloc.lower() != source.netloc.lower():
        return False
    if not path or path == source.path.lower().rstrip("/"):
        return False
    if any(word in path for word in EXCLUDE_WORDS):
        # Product slugs can include words like "collection"; explicit product routes win below.
        if not any(hint in path for hint in PRODUCT_HINTS):
            return False
    if any(hint in path for hint in PRODUCT_HINTS):
        return True

    # Fallback for non-Shopify sites: product pages usually have a deeper, unique path and
    # a meaningful link label. Exclude pagination and generic navigation.
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        return False
    if re.search(r"/(page|brand|category|categories|collection|collections|search|manufacturer)(/|$)", path):
        return False
    label = re.sub(r"\s+", " ", text or "").strip()
    if len(label) < 4:
        return False
    if label.lower() in {"view all", "read more", "next", "previous", "shop now", "arc'teryx", "arcteryx"}:
        return False
    if re.fullmatch(r"\d+", parts[-1]):
        return True
    # Conservative heuristic: product listing links tend to have multiple path segments.
    return len(parts) >= 3 and not any(x in path for x in ("/en/276_arc-teryx", "/en/brand/23-arc-teryx"))

def get_products(site_name, source_url):
    response = requests.get(source_url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    found = {}
    for a in soup.select("a[href]"):
        href = urljoin(response.url, a.get("href", "").strip())
        label = " ".join(a.get_text(" ", strip=True).split())
        if not is_probable_product_link(href, label, response.url):
            continue
        url = clean_url(href)
        if not label:
            # Try common product-card image alt text.
            img = a.select_one("img[alt]")
            label = img.get("alt", "").strip() if img else ""
        if len(label) > 160:
            label = label[:157] + "..."
        found[url] = {"name": label or urlparse(url).path.split("/")[-1].replace("-", " "),
                      "url": url, "source": site_name}
    return found

def send_push(title, content):
    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    if not token:
        print("PUSHPLUS_TOKEN secret not configured; notification skipped.")
        return
    payload = {
        "token": token,
        "title": title,
        "content": content,
        "template": "html",
        "channel": "wechat",
    }
    response = requests.post("https://www.pushplus.plus/send", json=payload, timeout=20)
    response.raise_for_status()
    result = response.json()
    print("PushPlus response:", json.dumps({k: result.get(k) for k in ("code", "msg")}, ensure_ascii=False))
    if result.get("code") not in (200, "200"):
        raise RuntimeError("PushPlus returned an error; check your Token, verification status, and daily limits.")

def main():
    previous = {}
    if STATE_FILE.exists():
        try:
            previous = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            previous = {}

    current = {}
    errors = []
    for i, (site_name, url) in enumerate(SITES, start=1):
        try:
            products = get_products(site_name, url)
            print(f"[OK] {site_name}: found {len(products)} candidate product links")
            for key, item in products.items():
                current[key] = item
        except Exception as exc:
            message = f"{site_name}: {type(exc).__name__}: {exc}"
            errors.append(message)
            print("[ERROR]", message)
        if i < len(SITES):
            time.sleep(2)

    # Do not overwrite a valid baseline if every website failed.
    if not current:
        raise RuntimeError("No product links were detected on any page. Keeping previous baseline unchanged.")

    # First successful run establishes the baseline and deliberately sends no new-product alert.
    if not previous:
        STATE_FILE.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Baseline initialized with {len(current)} candidate links; no alert on first run.")
        if errors:
            print("Some pages failed:", "; ".join(errors))
        return

    new_items = [item for key, item in current.items() if key not in previous]
    STATE_FILE.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")

    if new_items:
        lines = [
            "<h2>Arc'teryx 可能有新品上架</h2>",
            f"<p>检测时间（UTC）：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}</p>",
            "<ul>",
        ]
        for item in new_items[:50]:
            lines.append(f"<li><b>{item['name']}</b><br>{item['source']}<br><a href=\"{item['url']}\">查看商品</a></li>")
        lines.append("</ul>")
        if len(new_items) > 50:
            lines.append(f"<p>另有 {len(new_items) - 50} 个新增链接未在消息中展开。</p>")
        send_push(f"Arc'teryx 发现 {len(new_items)} 个可能新品", "\n".join(lines))
        print(f"Found {len(new_items)} new candidate links.")
    else:
        print("No new candidate links.")

    if errors:
        print("Some pages failed this run:", "; ".join(errors))

if __name__ == "__main__":
    main()
