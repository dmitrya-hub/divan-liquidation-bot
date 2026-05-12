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

TELEGRAM_TIMEOUT = 60
TELEGRAM_ATTEMPTS = 3
TELEGRAM_PAUSE_SECONDS = 1.2

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

    raise RuntimeError(
        f"Не удалось загрузить страницу после {attempts} попыток: {last_error}"
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


def format_price(value):
    if value is None:
        return "Цена не найдена"

    return f"{value:,}".replace(",", " ") + " руб."


def price_to_int(price: str):
    if not price or price == "Цена не найдена":
        return None

    digits = re.sub(r"[^\d]", "", price)

    if not digits:
        return None

    try:
        return int(digits)
    except ValueError:
        return None


def is_price_line(text: str) -> bool:
    text = normalize_text(text)
    return bool(re.fullmatch(r"\d{1,3}(?:\s\d{3})*\s*(?:руб\.?|₽)", text, flags=re.IGNORECASE))


def parse_price_line(text: str):
    if not is_price_line(text):
        return None

    digits = re.sub(r"[^\d]", "", text)

    if not digits:
        return None

    try:
        value = int(digits)
    except ValueError:
        return None

    if value < 500:
        return None

    return value


def is_discount_line(text: str) -> bool:
    text = normalize_text(text)
    return bool(re.fullmatch(r"\d{1,2}", text))


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
    low = normalize_text(text).lower()

    return (
        low.startswith("диван")
        or low.startswith("кровать")
        or low.startswith("модульный диван")
    )


def extract_image_links(soup: BeautifulSoup):
    """
    Возвращает список ссылок-картинок товаров в порядке их появления.
    В HTML divan.ru изображение товара обычно идёт как ссылка:
    <a href="/product/..."><img alt="Диван ..."></a>
    """
    result = []

    for a in soup.select('a[href*="/product/"]'):
        img = a.select_one("img")
        if not img:
            continue

        href = a.get("href")
        if not href:
            continue

        product_url = urljoin(BASE_URL, href)

        name = clean_product_name(img.get("alt", "") or img.get("title", ""))

        image_url = ""
        for attr in ["src", "data-src", "data-original", "data-lazy"]:
            value = img.get(attr)
            if value:
                image_url = urljoin(BASE_URL, value)
                break

        if not image_url:
            srcset = img.get("srcset") or img.get("data-srcset")
            if srcset:
                first = srcset.split(",")[0].strip().split(" ")[0]
                if first:
                    image_url = urljoin(BASE_URL, first)

        result.append(
            {
                "url": product_url,
                "name": name,
                "image_url": image_url,
            }
        )

    return result


def extract_title_links(soup: BeautifulSoup):
    """
    Возвращает ссылки-названия товаров в порядке их появления.
    """
    result = []

    for a in soup.select('a[href*="/product/"]'):
        href = a.get("href")
        if not href:
            continue

        product_url = urljoin(BASE_URL, href)
        text = clean_product_name(a.get_text(" ", strip=True))

        if not text:
            continue

        if not is_product_name(text):
            continue

        result.append(
            {
                "url": product_url,
                "name": text,
            }
        )

    return result


def extract_price_blocks_from_text(soup: BeautifulSoup):
    """
    В HTML каталог отдаёт карточки последовательно:
    Image: Название
    скидка
    скидочная цена
    старая цена
    ...
    Купить
    Название

    Берём первый нормальный price после Image как актуальную цену.
    """
    lines = [
        normalize_text(line)
        for line in soup.get_text("\n", strip=True).split("\n")
        if normalize_text(line)
    ]

    blocks = []

    i = 0
    while i < len(lines):
        line = lines[i]

        if not line.lower().startswith("image:"):
            i += 1
            continue

        name = clean_product_name(line)
        prices = []

        j = i + 1
        while j < len(lines):
            current = lines[j]

            if current.lower().startswith("image:"):
                break

            if current.lower() == "купить":
                break

            value = parse_price_line(current)
            if value is not None:
                prices.append(value)

            j += 1

        if prices:
            blocks.append(
                {
                    "image_name": name,
                    "price": format_price(prices[0]),
                    "min_price": format_price(min(prices)),
                }
            )

        i += 1

    return blocks


def fallback_name_from_url(product_url: str) -> str:
    slug = product_url.rstrip("/").split("/")[-1]
    slug = re.sub(r"-art--.*$", "", slug)
    slug = slug.replace("-", " ")
    return normalize_text(slug).title()


def extract_products_from_html(html: str):
    soup = BeautifulSoup(html, "html.parser")

    image_links = extract_image_links(soup)
    title_links = extract_title_links(soup)
    price_blocks = extract_price_blocks_from_text(soup)

    print(f"Найдено image-ссылок товаров: {len(image_links)}", flush=True)
    print(f"Найдено title-ссылок товаров: {len(title_links)}", flush=True)
    print(f"Найдено price-блоков товаров: {len(price_blocks)}", flush=True)

    products = []

    count = max(len(image_links), len(title_links), len(price_blocks))

    for idx in range(count):
        image_item = image_links[idx] if idx < len(image_links) else {}
        title_item = title_links[idx] if idx < len(title_links) else {}
        price_item = price_blocks[idx] if idx < len(price_blocks) else {}

        product_url = title_item.get("url") or image_item.get("url")
        if not product_url:
            continue

        image_url = image_item.get("image_url", "")

        name = (
            title_item.get("name")
            or image_item.get("name")
            or price_item.get("image_name")
            or fallback_name_from_url(product_url)
        )

        price = price_item.get("price") or price_item.get("min_price") or "Цена не найдена"

        products.append(
            {
                "url": product_url,
                "name": name,
                "price": price,
                "image_url": image_url,
            }
        )

    # Дедупликация по URL
    by_url = {}

    for p in products:
        if p["url"] not in by_url:
            by_url[p["url"]] = p
            continue

        existing = by_url[p["url"]]

        if len(p.get("name", "")) > len(existing.get("name", "")):
            existing["name"] = p["name"]

        if existing.get("price") == "Цена не найдена" and p.get("price") != "Цена не найдена":
            existing["price"] = p["price"]

        if not existing.get("image_url") and p.get("image_url"):
            existing["image_url"] = p["image_url"]

    return list(by_url.values())


def collect_all_products():
    all_by_url = {}
    expected_total = None

    for page_num in range(1, MAX_PAGES + 1):
        page_url = build_page_url(CATALOG_URL, page_num)
        html = fetch_html(page_url)

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


def send_telegram_photo(photo_url, name, price, product_url):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Не заданы BOT_TOKEN или CHAT_ID")

    caption = f"{name}\n{price}\n{product_url}"

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
        sent_count = 0
        failed_count = 0

        for p in new_products:
            ok = False

            if p["image_url"]:
                ok = send_telegram_photo(
                    photo_url=p["image_url"],
                    name=p["name"],
                    price=p["price"],
                    product_url=p["url"],
                )
            else:
                text = f"{p['name']}\n{p['price']}\n{p['url']}"
                ok = send_telegram_message(text)

            if ok:
                sent_count += 1
                print(
                    f"Отправлено: {p['name']} | {p['price']} | {p['url']}",
                    flush=True,
                )
            else:
                failed_count += 1
                print(
                    f"Не удалось отправить: {p['name']} | {p['price']} | {p['url']}",
                    flush=True,
                )

            time.sleep(TELEGRAM_PAUSE_SECONDS)

        print(
            f"Итог отправки Telegram: успешно {sent_count}, ошибок {failed_count}",
            flush=True,
        )
    else:
        print("Новых подходящих товаров нет.", flush=True)

    if len(current_urls) < MIN_PRODUCTS_TO_SAVE_STATE:
        print(
            f"Найдено подозрительно мало товаров: {len(current_urls)}. "
            "state.json не обновляю, чтобы не вызвать ложные повторные уведомления.",
            flush=True,
        )
    else:
        save_state({"seen": sorted(current_urls)})
        print(
            f"state.json обновлён. Сохранено товаров: {len(current_urls)}",
            flush=True,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
