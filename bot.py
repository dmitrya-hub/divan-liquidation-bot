import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.divan.ru"
CATALOG_URL = "https://www.divan.ru/category/rasprodaza-mebeli?types%5B%5D=54&types%5B%5D=1&types%5B%5D=4&types%5B%5D=43&defect=1"

STATE_FILE = Path("state.json")

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

MAX_PAGES = 10
REQUEST_TIMEOUT = 45

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"seen": []}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def normalize_text(text: str) -> str:
    text = text or ""
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def build_page_url(base_url: str, page_num: int) -> str:
    parsed = urlparse(base_url)
    query = parse_qs(parsed.query)

    if page_num <= 1:
        query.pop("page", None)
    else:
        query["page"] = [str(page_num)]

    new_query = urlencode(query, doseq=True)
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            new_query,
            parsed.fragment,
        )
    )


def fetch_html(url: str, attempts: int = 3) -> str:
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            print(f"Открываю страницу: {url} | попытка {attempt}", flush=True)
            response = requests.get(
                url,
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
            )
            print(f"HTTP status: {response.status_code}", flush=True)
            response.raise_for_status()
            return response.text

        except Exception as e:
            last_error = e
            print(f"Ошибка загрузки страницы: {e}", flush=True)
            if attempt < attempts:
                time.sleep(5)

    raise RuntimeError(f"Не удалось загрузить страницу после {attempts} попыток: {last_error}")


def extract_total_products(html: str):
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)

    patterns = [
        r"Найдено\s+(\d+)",
        r"Показать\s+(\d+)\s+товар",
        r"Показать\s+(\d+)\s+товаров",
        r"Смотреть все товары\s+(\d+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))

    return None


def extract_price_candidates(text: str):
    if not text:
        return []

    matches = re.findall(r"(\d[\d\s]*\s?(?:руб\.?|₽))", text, flags=re.IGNORECASE)
    prices = []

    for raw in matches:
        digits = re.sub(r"[^\d]", "", raw)
        if not digits:
            continue

        try:
            value = int(digits)
        except ValueError:
            continue

        if value > 0:
            prices.append(value)

    return prices


def format_price(value):
    if value is None:
        return "Цена не найдена"
    return f"{value:,}".replace(",", " ") + " руб."


def pick_actual_price(text: str):
    prices = extract_price_candidates(text)
    if not prices:
        return "Цена не найдена"

    # В карточке обычно есть новая и старая цена.
    # Новая цена чаще всего меньше старой.
    return format_price(min(prices))


def clean_product_name(text: str) -> str:
    text = normalize_text(text)
    text = re.sub(r"^Image:\s*", "", text, flags=re.IGNORECASE)
    text = normalize_text(text)

    if not text:
        return ""

    low = text.lower()

    if low in {"купить", "в корзину"}:
        return ""

    if "руб" in low or "₽" in low:
        return ""

    if "в наличии" in low:
        return ""

    if "размеры" in low:
        return ""

    if "спальное место" in low:
        return ""

    if len(text) < 5:
        return ""

    return text


def is_product_name(text: str) -> bool:
    low = text.lower()
    return (
        low.startswith("диван")
        or low.startswith("кровать")
        or low.startswith("модуль")
    )


def find_card_container(a):
    """
    Идём вверх от ссылки товара и ищем ближайший контейнер,
    где есть цена и название/картинка.
    """
    current = a

    for _ in range(10):
        if current is None:
            break

        text = normalize_text(current.get_text(" ", strip=True))

        has_price = bool(extract_price_candidates(text))
        has_product_link = bool(current.select_one('a[href*="/product/"]'))
        has_image = bool(current.select_one("img"))

        if has_price and has_product_link and has_image:
            return current

        current = current.parent

    return a.parent or a


def extract_image_from_card(card):
    img = card.select_one("img")
    if not img:
        return ""

    for attr in ["src", "data-src", "data-original", "data-lazy"]:
        value = img.get(attr)
        if value:
            return urljoin(BASE_URL, value)

    # Иногда lazy-картинки лежат в srcset
    srcset = img.get("srcset") or img.get("data-srcset")
    if srcset:
        first = srcset.split(",")[0].strip().split(" ")[0]
        if first:
            return urljoin(BASE_URL, first)

    return ""


def extract_name_from_card(card, product_url):
    candidates = []

    # 1. Ссылки на этот же товар
    for a in card.select('a[href*="/product/"]'):
        href = urljoin(BASE_URL, a.get("href", ""))
        text = clean_product_name(a.get_text(" ", strip=True))

        if href == product_url and text:
            candidates.append(text)

        for attr in ["title", "aria-label"]:
            attr_value = clean_product_name(a.get(attr, ""))
            if href == product_url and attr_value:
                candidates.append(attr_value)

    # 2. Alt картинки
    for img in card.select("img"):
        alt = clean_product_name(img.get("alt", ""))
        if alt:
            candidates.append(alt)

    # 3. Любые короткие строки из карточки
    raw_lines = card.get_text("\n", strip=True).split("\n")
    for line in raw_lines:
        line = clean_product_name(line)
        if line:
            candidates.append(line)

    product_like = [c for c in candidates if is_product_name(c)]
    if product_like:
        return max(product_like, key=len)

    if candidates:
        return max(candidates, key=len)

    # 4. fallback из url
    slug = product_url.rstrip("/").split("/")[-1]
    slug = re.sub(r"-art--.*$", "", slug)
    slug = slug.replace("-", " ")
    return normalize_text(slug).title()


def extract_products_from_html(html: str):
    soup = BeautifulSoup(html, "html.parser")

    result_by_url = {}

    all_links = soup.select('a[href*="/product/"]')
    print(f"Найдено ссылок /product/ в HTML: {len(all_links)}", flush=True)

    for a in all_links:
        href = a.get("href")
        if not href:
            continue

        product_url = urljoin(BASE_URL, href)

        # Отсекаем мусорные ссылки, если появятся.
        if "/product/" not in product_url:
            continue

        card = find_card_container(a)
        card_text = normalize_text(card.get_text(" ", strip=True))

        name = extract_name_from_card(card, product_url)
        price = pick_actual_price(card_text)
        image_url = extract_image_from_card(card)

        if not name:
            continue

        if product_url not in result_by_url:
            result_by_url[product_url] = {
                "url": product_url,
                "name": name,
                "price": price,
                "image_url": image_url,
            }
        else:
            existing = result_by_url[product_url]

            if len(name) > len(existing.get("name", "")):
                existing["name"] = name

            if existing.get("price") == "Цена не найдена" and price != "Цена не найдена":
                existing["price"] = price

            if not existing.get("image_url") and image_url:
                existing["image_url"] = image_url

    return list(result_by_url.values())


def collect_all_products():
    all_by_url = {}
    expected_total = None

    for page_num in range(1, MAX_PAGES + 1):
        page_url = build_page_url(CATALOG_URL, page_num)
        html = fetch_html(page_url)

        if page_num == 1:
            expected_total = extract_total_products(html)
            print(f"Ожидаемое количество товаров по странице: {expected_total}", flush=True)

        products = extract_products_from_html(html)
        print(f"Страница {page_num}: собрано товаров: {len(products)}", flush=True)

        before = len(all_by_url)

        for product in products:
            all_by_url[product["url"]] = product

        after = len(all_by_url)
        added = after - before

        print(f"Страница {page_num}: добавлено новых уникальных товаров: {added}", flush=True)
        print(f"Всего уникальных товаров: {after}", flush=True)

        if expected_total and after >= expected_total:
            print("Достигли ожидаемого количества товаров.", flush=True)
            break

        if page_num > 1 and added == 0:
            print("Новых товаров на странице нет, завершаю обход.", flush=True)
            break

        time.sleep(1)

    return list(all_by_url.values())


def send_telegram_message(text):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Не заданы BOT_TOKEN или CHAT_ID")

    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = requests.post(
        api_url,
        data={
            "chat_id": CHAT_ID,
            "text": text,
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    response.raise_for_status()


def send_telegram_photo(photo_url, name, price, product_url):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Не заданы BOT_TOKEN или CHAT_ID")

    caption = f"{name}\n{price}\n{product_url}"

    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    response = requests.post(
        api_url,
        data={
            "chat_id": CHAT_ID,
            "photo": photo_url,
            "caption": caption[:1024],
        },
        timeout=30,
    )

    if response.status_code != 200:
        print(f"Не удалось отправить фото, отправляю текстом: {response.text}", flush=True)
        send_telegram_message(caption)
        return

    response.raise_for_status()


def print_debug(products):
    print("", flush=True)
    print("========== ИТОГ ==========", flush=True)
    print(f"Собрано товаров: {len(products)}", flush=True)

    without_name = [p for p in products if not p["name"]]
    without_price = [p for p in products if p["price"] == "Цена не найдена"]
    without_image = [p for p in products if not p["image_url"]]

    print(f"Без имени: {len(without_name)}", flush=True)
    print(f"Без цены: {len(without_price)}", flush=True)
    print(f"Без картинки: {len(without_image)}", flush=True)

    print("", flush=True)
    print("========== ПЕРВЫЕ 10 ТОВАРОВ ==========", flush=True)

    for idx, p in enumerate(products[:10], start=1):
        print("", flush=True)
        print(f"{idx}. {p['name']}", flush=True)
        print(f"Цена: {p['price']}", flush=True)
        print(f"Картинка: {p['image_url'] or 'КАРТИНКА НЕ НАЙДЕНА'}", flush=True)
        print(f"Ссылка: {p['url']}", flush=True)


def main():
    state = load_state()
    seen = set(state.get("seen", []))

    products = collect_all_products()
    print_debug(products)

    if not products:
        print("Подходящие товары не найдены", flush=True)
        return 0

    current_urls = {p["url"] for p in products}
    new_products = [p for p in products if p["url"] not in seen]

    print(f"Новых товаров относительно state.json: {len(new_products)}", flush=True)

    if new_products:
        for p in new_products:
            if p["image_url"]:
                send_telegram_photo(
                    photo_url=p["image_url"],
                    name=p["name"],
                    price=p["price"],
                    product_url=p["url"],
                )
                print(f"Отправлено с фото: {p['name']} | {p['price']} | {p['url']}", flush=True)
            else:
                text = f"{p['name']}\n{p['price']}\n{p['url']}"
                send_telegram_message(text)
                print(f"Отправлено без фото: {p['name']} | {p['price']} | {p['url']}", flush=True)
    else:
        print("Новых подходящих товаров нет.", flush=True)

    save_state({"seen": sorted(current_urls)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
