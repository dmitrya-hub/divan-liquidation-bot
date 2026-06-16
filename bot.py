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
CATALOG_URL = (
    "https://www.divan.ru/category/rasprodaza-mebeli"
    "?types%5B%5D=54"
    "&types%5B%5D=1"
    "&types%5B%5D=4"
    "&types%5B%5D=43"
    "&defect=1"
)

STATE_FILE = Path("state.json")

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

MAX_PAGES = 10
REQUEST_TIMEOUT = 45

MIN_PRODUCTS_TO_SAVE_STATE = 50
MIN_PRODUCTS_AFTER_PAGINATION_END = 80

TELEGRAM_TIMEOUT = 60
TELEGRAM_ATTEMPTS = 3
TELEGRAM_PAUSE_SECONDS = 1.2

NOTIFY_COOLDOWN_HOURS = 24

TARGET_PRODUCT_URL_FRAGMENT = "krovat-pajl-180-art--2004181402"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}


def normalize_text(text: str) -> str:
    text = text or ""
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def normalize_for_key(text: str) -> str:
    text = normalize_text(text).lower()
    text = text.replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9]+", " ", text)
    return normalize_text(text)


def make_product_key(product: dict) -> str:
    name = normalize_for_key(product.get("name", ""))
    sale_price = normalize_text(product.get("sale_price", ""))
    old_price = normalize_text(product.get("old_price", ""))
    discount = normalize_text(product.get("discount", ""))

    return "|".join([name, sale_price, old_price, discount])


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return {
                "seen": [],
                "seen_keys": [],
                "notified_keys": {},
            }

        data.setdefault("seen", [])
        data.setdefault("seen_keys", [])
        data.setdefault("notified_keys", {})

        return data

    return {
        "seen": [],
        "seen_keys": [],
        "notified_keys": {},
    }


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


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


def fetch_html(url: str, attempts: int = 3):
    last_error = None
    last_status_code = None

    for attempt in range(1, attempts + 1):
        try:
            print(f"Открываю страницу: {url} | попытка {attempt}", flush=True)

            response = requests.get(
                url,
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
            )

            last_status_code = response.status_code
            print(f"HTTP status: {response.status_code}", flush=True)

            if response.status_code == 404:
                return None, 404

            response.raise_for_status()
            return response.text, response.status_code

        except Exception as e:
            last_error = e
            print(f"Ошибка загрузки страницы: {e}", flush=True)

            if attempt < attempts:
                time.sleep(5)

    raise RuntimeError(
        f"Не удалось загрузить страницу после {attempts} попыток. "
        f"Последний HTTP status: {last_status_code}. Ошибка: {last_error}"
    )


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


def normalize_url(url: str) -> str:
    if not url:
        return ""

    url = urljoin(BASE_URL, url)
    parsed = urlparse(url)

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path.rstrip("/"),
            "",
            "",
            "",
        )
    )


def format_price(value):
    if value is None:
        return "Цена не найдена"

    return f"{value:,}".replace(",", " ") + " руб."


def parse_price_values(text: str):
    if not text:
        return []

    text = text.replace("\xa0", " ")

    pattern = r"(?<!\d)(\d{1,3}(?:[ \t]\d{3})+|\d{1,6})\s*(?:руб\.?|₽)"
    matches = re.findall(pattern, text, flags=re.IGNORECASE)

    values = []

    for raw in matches:
        digits = re.sub(r"[^\d]", "", raw)

        if not digits:
            continue

        try:
            value = int(digits)
        except ValueError:
            continue

        if value >= 500:
            values.append(value)

    return values


def parse_discount_values(text: str):
    if not text:
        return []

    text = text.replace("\xa0", " ")

    values = []

    for raw in re.findall(r"(?:^|\s)-?(\d{1,2})\s*%", text):
        try:
            value = int(raw)
        except ValueError:
            continue

        if 1 <= value <= 95:
            values.append(value)

    lines = [normalize_text(x) for x in text.splitlines() if normalize_text(x)]

    for line in lines:
        if re.fullmatch(r"\d{1,2}", line):
            try:
                value = int(line)
            except ValueError:
                continue

            if 1 <= value <= 95:
                values.append(value)

    return values


def clean_product_name(text: str) -> str:
    text = normalize_text(text)
    text = re.sub(r"^Image:\s*", "", text, flags=re.IGNORECASE)
    text = normalize_text(text)

    if not text:
        return ""

    low = text.lower()

    bad_exact_values = {
        "купить",
        "в корзину",
        "товар",
        "новинка",
        "хит",
        "sale",
        "распродажа",
    }

    if low in bad_exact_values:
        return ""

    bad_substrings = [
        "руб",
        "₽",
        "в наличии",
        "размеры",
        "спальное место",
        "доставка",
        "самовывоз",
        "рассрочка",
        "отзывы",
        "смотреть все",
        "найдено",
        "показать",
        "фильтр",
        "сортировка",
    ]

    for value in bad_substrings:
        if value in low:
            return ""

    if re.fullmatch(r"\d+", text):
        return ""

    if len(text) < 3:
        return ""

    return text


def looks_like_product_name(text: str) -> bool:
    """
    Ослабленный фильтр.

    Раньше пропускали только названия, начинающиеся с Диван/Кровать.
    Но divan.ru иногда отдаёт названия как Маиль-1, Альди-5, Полан-4.
    Поэтому разрешаем более общий формат, но мусор режем в clean_product_name().
    """
    text = clean_product_name(text)

    if not text:
        return False

    low = text.lower()

    strong_prefixes = [
        "диван",
        "кровать",
        "модульный диван",
        "прямой диван",
        "угловой диван",
        "кресло-кровать",
    ]

    if any(low.startswith(prefix) for prefix in strong_prefixes):
        return True

    # Названия вроде Альди-5, Маиль-1, Полан-4, Спейс-М 1.
    if re.search(r"[а-яa-z]", low) and re.search(r"\d", low):
        return True

    # Названия из 1-4 слов без мусора.
    words = low.split()
    if 1 <= len(words) <= 5 and re.search(r"[а-яa-z]", low):
        return True

    return False


def get_image_url_from_card(card):
    img = card.select_one("img")

    if not img:
        return ""

    for attr in ["src", "data-src", "data-original", "data-lazy"]:
        value = img.get(attr)

        if value:
            return urljoin(BASE_URL, value)

    srcset = img.get("srcset") or img.get("data-srcset")

    if srcset:
        first = srcset.split(",")[0].strip().split(" ")[0]

        if first:
            return urljoin(BASE_URL, first)

    return ""


def get_name_from_card(card, product_url):
    candidates = []

    normalized_product_url = normalize_url(product_url)

    for img in card.select("img"):
        alt = clean_product_name(img.get("alt", ""))

        if alt:
            candidates.append(alt)

        title = clean_product_name(img.get("title", ""))

        if title:
            candidates.append(title)

    for a in card.select('a[href*="/product/"]'):
        href = normalize_url(a.get("href", ""))

        if href != normalized_product_url:
            continue

        text = clean_product_name(a.get_text(" ", strip=True))

        if text:
            candidates.append(text)

        for attr in ["title", "aria-label"]:
            attr_value = clean_product_name(a.get(attr, ""))

            if attr_value:
                candidates.append(attr_value)

    product_like = [c for c in candidates if looks_like_product_name(c)]

    if product_like:
        return max(product_like, key=len)

    if candidates:
        return max(candidates, key=len)

    return fallback_name_from_url(product_url)


def fallback_name_from_url(product_url: str) -> str:
    slug = product_url.rstrip("/").split("/")[-1]
    slug = re.sub(r"-art--.*$", "", slug)
    slug = slug.replace("-", " ")

    return normalize_text(slug).title()


def get_price_info_from_card(card):
    text = card.get_text("\n", strip=True)
    prices = parse_price_values(text)
    discounts = parse_discount_values(text)

    unique_prices = []

    for value in prices:
        if value not in unique_prices:
            unique_prices.append(value)

    if not unique_prices:
        return {
            "sale_price": "Цена не найдена",
            "old_price": "",
            "discount": "",
        }

    if len(unique_prices) == 1:
        sale_value = unique_prices[0]
        old_value = None
    else:
        sale_value = min(unique_prices)
        old_value = max(unique_prices)

    discount_value = None

    if discounts:
        discount_value = max(discounts)

    if discount_value is None and old_value and sale_value and old_value > sale_value:
        discount_value = round((1 - sale_value / old_value) * 100)

    return {
        "sale_price": format_price(sale_value),
        "old_price": format_price(old_value) if old_value else "",
        "discount": f"{discount_value}%" if discount_value else "",
    }


def product_anchors_in(element):
    return element.select('a[href*="/product/"]')


def count_distinct_product_urls(element):
    urls = set()

    for a in product_anchors_in(element):
        href = normalize_url(a.get("href", ""))

        if href:
            urls.add(href)

    return len(urls)


def has_image_for_url(element, product_url):
    normalized_product_url = normalize_url(product_url)

    for a in product_anchors_in(element):
        href = normalize_url(a.get("href", ""))

        if href != normalized_product_url:
            continue

        if a.select_one("img"):
            return True

    return False


def has_title_for_url(element, product_url):
    normalized_product_url = normalize_url(product_url)

    for a in product_anchors_in(element):
        href = normalize_url(a.get("href", ""))

        if href != normalized_product_url:
            continue

        text = clean_product_name(a.get_text(" ", strip=True))

        if text and looks_like_product_name(text):
            return True

    return False


def find_strict_card_container(anchor, product_url):
    current = anchor

    for _ in range(14):
        if current is None:
            break

        text = current.get_text("\n", strip=True)

        has_price = bool(parse_price_values(text))
        has_img = has_image_for_url(current, product_url)
        has_title = has_title_for_url(current, product_url)
        distinct_urls = count_distinct_product_urls(current)

        if has_price and has_img and has_title and distinct_urls <= 3:
            return current

        current = current.parent

    current = anchor

    for _ in range(14):
        if current is None:
            break

        text = current.get_text("\n", strip=True)

        if parse_price_values(text) and count_distinct_product_urls(current) <= 3:
            return current

        current = current.parent

    return anchor.parent or anchor


def extract_products_from_html(html: str):
    soup = BeautifulSoup(html, "html.parser")

    result_by_url = {}
    candidate_links = []

    for a in soup.select('a[href*="/product/"]'):
        href = a.get("href")

        if not href:
            continue

        text = clean_product_name(a.get_text(" ", strip=True))
        product_url = normalize_url(href)

        # Берём:
        # 1. ссылки с нормальным текстом-названием;
        # 2. ссылки-картинки без текста, если внутри img;
        # 3. целевой товар по URL для диагностики.
        has_img = bool(a.select_one("img"))

        if looks_like_product_name(text) or has_img or TARGET_PRODUCT_URL_FRAGMENT in product_url:
            candidate_links.append(a)

    print(f"Найдено candidate-ссылок товаров: {len(candidate_links)}", flush=True)

    for a in candidate_links:
        href = a.get("href")

        if not href:
            continue

        product_url = normalize_url(href)
        card = find_strict_card_container(a, product_url)

        name = get_name_from_card(card, product_url)
        image_url = get_image_url_from_card(card)
        price_info = get_price_info_from_card(card)

        if not name:
            continue

        if price_info["sale_price"] == "Цена не найдена":
            continue

        product = {
            "url": product_url,
            "name": name,
            "sale_price": price_info["sale_price"],
            "old_price": price_info["old_price"],
            "discount": price_info["discount"],
            "image_url": image_url,
        }

        if TARGET_PRODUCT_URL_FRAGMENT in product_url:
            print(
                f"TARGET FOUND: {product['name']} | "
                f"{product['sale_price']} | {product['old_price']} | "
                f"{product['discount']} | {product['url']}",
                flush=True,
            )

        if product_url not in result_by_url:
            result_by_url[product_url] = product
        else:
            existing = result_by_url[product_url]

            if len(product.get("name", "")) > len(existing.get("name", "")):
                existing["name"] = product["name"]

            if existing.get("sale_price") == "Цена не найдена":
                existing["sale_price"] = product["sale_price"]

            if not existing.get("old_price") and product.get("old_price"):
                existing["old_price"] = product["old_price"]

            if not existing.get("discount") and product.get("discount"):
                existing["discount"] = product["discount"]

            if not existing.get("image_url") and product.get("image_url"):
                existing["image_url"] = product["image_url"]

    return list(result_by_url.values())


def collect_all_products():
    all_by_url = {}
    expected_total = None
    pagination_ended_normally = False

    for page_num in range(1, MAX_PAGES + 1):
        page_url = build_page_url(CATALOG_URL, page_num)

        html, status_code = fetch_html(page_url)

        if status_code == 404:
            if page_num == 1:
                raise RuntimeError("Первая страница каталога вернула 404")

            print(
                f"Страница {page_num} вернула 404. "
                "Считаю это концом пагинации и завершаю обход.",
                flush=True,
            )
            pagination_ended_normally = True
            break

        if not html:
            if page_num == 1:
                raise RuntimeError("Первая страница каталога не загрузилась")

            print(
                f"Страница {page_num} пустая. Завершаю обход.",
                flush=True,
            )
            pagination_ended_normally = True
            break

        if page_num == 1:
            expected_total = extract_total_products(html)
            print(
                f"Ожидаемое количество товаров по странице: {expected_total}",
                flush=True,
            )

        products = extract_products_from_html(html)
        print(f"Страница {page_num}: собрано товаров: {len(products)}", flush=True)

        before = len(all_by_url)

        for product in products:
            all_by_url[product["url"]] = product

        after = len(all_by_url)
        added = after - before

        print(
            f"Страница {page_num}: добавлено новых уникальных товаров: {added}",
            flush=True,
        )
        print(f"Всего уникальных товаров: {after}", flush=True)

        if page_num > 1 and added == 0:
            print(
                "На странице нет новых уникальных товаров. Завершаю обход.",
                flush=True,
            )
            pagination_ended_normally = True
            break

        time.sleep(1)

    else:
        print(
            f"Достигнут лимит MAX_PAGES={MAX_PAGES}. Завершаю обход.",
            flush=True,
        )

    collected_total = len(all_by_url)

    print(
        f"Обход завершён. Собрано товаров: {collected_total}. "
        f"Ожидаемое число с сайта: {expected_total}. "
        f"Пагинация завершилась нормально: {pagination_ended_normally}",
        flush=True,
    )

    return list(all_by_url.values()), expected_total, pagination_ended_normally


def build_telegram_text(product):
    lines = [
        product["name"],
        f"Цена со скидкой: {product['sale_price']}",
    ]

    if product.get("old_price"):
        lines.append(f"Старая цена: {product['old_price']}")

    if product.get("discount"):
        lines.append(f"Скидка: {product['discount']}")

    lines.append(product["url"])

    return "\n".join(lines)


def send_telegram_message(text):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Не заданы BOT_TOKEN или CHAT_ID")

    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    last_error = None

    for attempt in range(1, TELEGRAM_ATTEMPTS + 1):
        try:
            response = requests.post(
                api_url,
                data={
                    "chat_id": CHAT_ID,
                    "text": text,
                    "disable_web_page_preview": False,
                },
                timeout=TELEGRAM_TIMEOUT,
            )

            if response.status_code == 429:
                retry_after = response.json().get("parameters", {}).get("retry_after", 10)
                print(f"Telegram rate limit. Жду {retry_after} сек.", flush=True)
                time.sleep(retry_after)
                continue

            response.raise_for_status()
            return True

        except Exception as e:
            last_error = e
            print(
                f"Ошибка отправки сообщения в Telegram, попытка {attempt}: {e}",
                flush=True,
            )

            if attempt < TELEGRAM_ATTEMPTS:
                time.sleep(5)

    print(f"Не удалось отправить сообщение в Telegram: {last_error}", flush=True)
    return False


def send_telegram_photo(photo_url, caption):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Не заданы BOT_TOKEN или CHAT_ID")

    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"

    last_error = None

    for attempt in range(1, TELEGRAM_ATTEMPTS + 1):
        try:
            response = requests.post(
                api_url,
                data={
                    "chat_id": CHAT_ID,
                    "photo": photo_url,
                    "caption": caption[:1024],
                },
                timeout=TELEGRAM_TIMEOUT,
            )

            if response.status_code == 429:
                retry_after = response.json().get("parameters", {}).get("retry_after", 10)
                print(f"Telegram rate limit. Жду {retry_after} сек.", flush=True)
                time.sleep(retry_after)
                continue

            if response.status_code != 200:
                print(
                    f"Не удалось отправить фото, пробую текстом: {response.text}",
                    flush=True,
                )
                return send_telegram_message(caption)

            response.raise_for_status()
            return True

        except Exception as e:
            last_error = e
            print(
                f"Ошибка отправки фото в Telegram, попытка {attempt}: {e}",
                flush=True,
            )

            if attempt < TELEGRAM_ATTEMPTS:
                time.sleep(5)

    print(
        f"Не удалось отправить фото в Telegram, пробую текстом: {last_error}",
        flush=True,
    )
    return send_telegram_message(caption)


def print_debug(products):
    print("", flush=True)
    print("========== ИТОГ ==========", flush=True)
    print(f"Собрано товаров: {len(products)}", flush=True)

    without_name = [p for p in products if not p["name"]]
    without_price = [p for p in products if p["sale_price"] == "Цена не найдена"]
    without_image = [p for p in products if not p["image_url"]]

    print(f"Без имени: {len(without_name)}", flush=True)
    print(f"Без цены: {len(without_price)}", flush=True)
    print(f"Без картинки: {len(without_image)}", flush=True)

    target_products = [
        p for p in products if TARGET_PRODUCT_URL_FRAGMENT in p["url"]
    ]

    if target_products:
        print("", flush=True)
        print("========== TARGET PRODUCT ==========", flush=True)

        for p in target_products:
            print(f"{p['name']}", flush=True)
            print(f"Цена со скидкой: {p['sale_price']}", flush=True)
            print(f"Старая цена: {p['old_price'] or 'не указана'}", flush=True)
            print(f"Скидка: {p['discount'] or 'не указана'}", flush=True)
            print(f"Картинка: {p['image_url'] or 'КАРТИНКА НЕ НАЙДЕНА'}", flush=True)
            print(f"Ссылка: {p['url']}", flush=True)
    else:
        print("", flush=True)
        print(
            f"TARGET PRODUCT НЕ НАЙДЕН: {TARGET_PRODUCT_URL_FRAGMENT}",
            flush=True,
        )

    print("", flush=True)
    print("========== ПЕРВЫЕ 10 ТОВАРОВ ==========", flush=True)

    for idx, p in enumerate(products[:10], start=1):
        print("", flush=True)
        print(f"{idx}. {p['name']}", flush=True)
        print(f"Ключ: {make_product_key(p)}", flush=True)
        print(f"Цена со скидкой: {p['sale_price']}", flush=True)
        print(f"Старая цена: {p['old_price'] or 'не указана'}", flush=True)
        print(f"Скидка: {p['discount'] or 'не указана'}", flush=True)
        print(f"Картинка: {p['image_url'] or 'КАРТИНКА НЕ НАЙДЕНА'}", flush=True)
        print(f"Ссылка: {p['url']}", flush=True)


def main():
    state = load_state()

    products, expected_total, pagination_ended_normally = collect_all_products()
    print_debug(products)

    if not products:
        print("Подходящие товары не найдены", flush=True)
        return 0

    current_urls = {p["url"] for p in products}
    current_keys = {make_product_key(p) for p in products}

    seen_urls = set(state.get("seen", []))
    seen_keys = set(state.get("seen_keys", []))
    notified_keys = dict(state.get("notified_keys", {}))

    now_ts = time.time()
    cooldown_seconds = NOTIFY_COOLDOWN_HOURS * 60 * 60

    notified_keys = {
        key: ts
        for key, ts in notified_keys.items()
        if isinstance(ts, (int, float)) and now_ts - ts <= cooldown_seconds
    }

    if not seen_keys and seen_urls:
        seen_keys = {
            make_product_key(p)
            for p in products
            if p["url"] in seen_urls
        }

    new_products = []
    keys_in_this_run = set()

    for p in products:
        product_key = make_product_key(p)

        if product_key in keys_in_this_run:
            continue

        keys_in_this_run.add(product_key)

        if product_key in seen_keys:
            continue

        if product_key in notified_keys:
            continue

        new_products.append(p)

    print(f"Новых товаров относительно state.json: {len(new_products)}", flush=True)

    if new_products:
        sent_count = 0
        failed_count = 0

        for p in new_products:
            caption = build_telegram_text(p)

            if p["image_url"]:
                ok = send_telegram_photo(p["image_url"], caption)
            else:
                ok = send_telegram_message(caption)

            if ok:
                sent_count += 1
                notified_keys[make_product_key(p)] = now_ts

                print(
                    f"Отправлено: {p['name']} | {p['sale_price']} | {p['url']}",
                    flush=True,
                )
            else:
                failed_count += 1
                print(
                    f"Не удалось отправить: {p['name']} | {p['sale_price']} | {p['url']}",
                    flush=True,
                )

            time.sleep(TELEGRAM_PAUSE_SECONDS)

        print(
            f"Итог отправки Telegram: успешно {sent_count}, ошибок {failed_count}",
            flush=True,
        )
    else:
        print("Новых подходящих товаров нет.", flush=True)

    collected_count = len(current_urls)

    too_few_absolute = collected_count < MIN_PRODUCTS_TO_SAVE_STATE

    too_few_after_normal_pagination_end = (
        pagination_ended_normally
        and collected_count < MIN_PRODUCTS_AFTER_PAGINATION_END
    )

    if too_few_absolute or too_few_after_normal_pagination_end:
        print(
            f"state.json не обновляю. "
            f"Собрано товаров: {collected_count}. "
            f"Минимум абсолютный: {MIN_PRODUCTS_TO_SAVE_STATE}. "
            f"Минимум при нормальном конце пагинации: {MIN_PRODUCTS_AFTER_PAGINATION_END}. "
            f"Ожидаемое число с сайта: {expected_total}. "
            f"Пагинация завершилась нормально: {pagination_ended_normally}.",
            flush=True,
        )
    else:
        save_state(
            {
                "seen": sorted(current_urls),
                "seen_keys": sorted(current_keys),
                "notified_keys": notified_keys,
            }
        )
        print(
            f"state.json обновлён. Сохранено товаров: {collected_count}, "
            f"ключей: {len(current_keys)}, notified_keys: {len(notified_keys)}",
            flush=True,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
