import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs, urlunparse

import requests
from playwright.sync_api import sync_playwright

URL = "https://www.divan.ru/category/rasprodaza-mebeli?types%5B%5D=54&types%5B%5D=1&types%5B%5D=4&types%5B%5D=43&defect=1"
STATE_FILE = Path("state.json")

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

ALLOWED_TYPES = {
    "Диван прямой",
    "Диван угловой",
    "Диван модульный",
    "Кровать",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"seen": []}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def send_telegram_message(text):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Не заданы BOT_TOKEN или CHAT_ID")

    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(
        api_url,
        data={
            "chat_id": CHAT_ID,
            "text": text,
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    resp.raise_for_status()


def send_telegram_photo(photo_url, name, price, product_url):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Не заданы BOT_TOKEN или CHAT_ID")

    caption = f"{name}\n{price}\n{product_url}"

    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    resp = requests.post(
        api_url,
        data={
            "chat_id": CHAT_ID,
            "photo": photo_url,
            "caption": caption[:1024],
        },
        timeout=30,
    )

    if resp.status_code != 200:
        print(f"Не удалось отправить фото, отправляю текстом: {resp.text}")
        send_telegram_message(caption)
        return

    resp.raise_for_status()


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def detect_product_type(name: str):
    low = name.lower().strip()

    if low.startswith("диван прямой"):
        return "Диван прямой"
    if low.startswith("диван угловой"):
        return "Диван угловой"
    if low.startswith("диван модульный"):
        return "Диван модульный"
    if low.startswith("кровать"):
        return "Кровать"

    return None


def extract_total_products(page):
    body_text = page.locator("body").inner_text()
    patterns = [
        r"Показать\s+(\d+)\s+товар",
        r"Показать\s+(\d+)\s+товаров",
        r"Смотреть все товары\s+(\d+)",
        r"Найдено\s+(\d+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, body_text, flags=re.IGNORECASE)
        if m:
            return int(m.group(1))
    return None


def build_page_url(base_url: str, page_num: int) -> str:
    parsed = urlparse(base_url)
    query = parse_qs(parsed.query)
    query["page"] = [str(page_num)]

    new_query = urlencode(query, doseq=True)
    return urlunparse((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        parsed.params,
        new_query,
        parsed.fragment,
    ))


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
    return format_price(min(prices))


def goto_with_retry(page, url, wait_until="domcontentloaded", attempts=3, timeout=120000):
    for attempt in range(1, attempts + 1):
        try:
            print(f"Открываю {url}, попытка {attempt}...")
            page.goto(url, wait_until=wait_until, timeout=timeout)
            page.wait_for_timeout(5000)
            return
        except Exception as e:
            print(f"Ошибка открытия {url} на попытке {attempt}: {e}")
            if attempt == attempts:
                raise
            page.wait_for_timeout(10000)


def fetch_price_from_product_page(browser, product_url: str) -> str:
    page = browser.new_page(
        user_agent=HEADERS["User-Agent"],
        locale="ru-RU",
        viewport={"width": 1440, "height": 2200},
    )

    try:
        goto_with_retry(page, product_url, wait_until="domcontentloaded", attempts=2, timeout=120000)
        body_text = page.locator("body").inner_text()
        return pick_actual_price(body_text)
    except Exception as e:
        print(f"Не удалось получить цену со страницы товара {product_url}: {e}")
        return "Цена не найдена"
    finally:
        page.close()


def extract_items_from_current_page(page):
    links = page.locator('a[href*="/product/"]')
    page_count = links.count()
    print(f"Найдено ссылок /product/ на текущей странице: {page_count}")

    return links.evaluate_all("""
        (els) => els.map((a) => {
            const href = a.href || "";
            const text = (a.innerText || a.textContent || "").trim();

            let cardText = "";
            let imageUrl = "";

            let el = a;
            for (let i = 0; i < 14 && el; i++) {
                const t = (el.innerText || el.textContent || "").trim();

                if (!cardText && (
                    t.includes("В наличии:") ||
                    t.includes("Изменить опции") ||
                    t.includes("руб.") ||
                    t.includes("₽")
                )) {
                    cardText = t;
                }

                if (!imageUrl) {
                    const img = el.querySelector("img");
                    if (img) {
                        imageUrl =
                            img.src ||
                            img.getAttribute("src") ||
                            img.getAttribute("data-src") ||
                            "";
                    }
                }

                el = el.parentElement;
            }

            if (!imageUrl) {
                const img = a.querySelector("img");
                if (img) {
                    imageUrl =
                        img.src ||
                        img.getAttribute("src") ||
                        img.getAttribute("data-src") ||
                        "";
                }
            }

            return { href, text, cardText, imageUrl };
        })
    """)


def merge_unique_items(raw_items, new_items):
    added = 0
    existing_urls = {normalize_text(item.get("href") or "") for item in raw_items if normalize_text(item.get("href") or "")}

    for item in new_items:
        href = normalize_text(item.get("href") or "")
        if not href:
            continue
        if href in existing_urls:
            continue
        raw_items.append(item)
        existing_urls.add(href)
        added += 1

    return added


def collect_raw_items_with_scroll(page, expected_total=None):
    raw_items = []
    prev_unique_count = 0
    stable_rounds = 0
    max_rounds = 40

    for round_num in range(1, max_rounds + 1):
        page_items = extract_items_from_current_page(page)
        added = merge_unique_items(raw_items, page_items)
        current_unique_count = len({
            normalize_text(item.get("href") or "")
            for item in raw_items
            if normalize_text(item.get("href") or "")
        })

        print(
            f"Скролл {round_num}: добавлено новых товаров: {added}, "
            f"всего уникальных товаров: {current_unique_count}"
        )

        if expected_total and current_unique_count >= expected_total:
            print("Скроллом достигли ожидаемого количества товаров.")
            break

        if current_unique_count == prev_unique_count:
            stable_rounds += 1
        else:
            stable_rounds = 0

        if stable_rounds >= 4:
            print("Количество уникальных товаров перестало расти при скролле.")
            break

        prev_unique_count = current_unique_count

        page.mouse.wheel(0, 6000)
        page.wait_for_timeout(1800)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1400)

    return raw_items


def collect_raw_items_with_pagination(browser, base_url: str, raw_items, expected_total=None):
    page = browser.new_page(
        user_agent=HEADERS["User-Agent"],
        locale="ru-RU",
        viewport={"width": 1440, "height": 2600},
    )

    try:
        prev_unique_count = len({
            normalize_text(item.get("href") or "")
            for item in raw_items
            if normalize_text(item.get("href") or "")
        })

        for page_num in range(2, 51):
            page_url = build_page_url(base_url, page_num)
            print(f"Обхожу страницу пагинации {page_num}: {page_url}")

            try:
                goto_with_retry(page, page_url, wait_until="domcontentloaded", attempts=2, timeout=120000)
            except Exception as e:
                print(f"Не удалось открыть page={page_num}: {e}")
                break

            page_items = extract_items_from_current_page(page)
            added = merge_unique_items(raw_items, page_items)
            current_unique_count = len({
                normalize_text(item.get("href") or "")
                for item in raw_items
                if normalize_text(item.get("href") or "")
            })

            print(
                f"Страница {page_num}: добавлено новых товаров: {added}, "
                f"всего уникальных товаров: {current_unique_count}"
            )

            if expected_total and current_unique_count >= expected_total:
                print("Пагинацией достигли ожидаемого количества товаров.")
                break

            if current_unique_count == prev_unique_count:
                print("Новых товаров на следующей странице не появилось, завершаю пагинацию.")
                break

            prev_unique_count = current_unique_count

    finally:
        page.close()

    return raw_items


def parse_products_with_browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=HEADERS["User-Agent"],
            locale="ru-RU",
            viewport={"width": 1440, "height": 2600},
        )

        try:
            goto_with_retry(page, URL, wait_until="domcontentloaded", attempts=3, timeout=120000)

            body_text = page.locator("body").inner_text()
            expected_total = extract_total_products(page)
            print(f"Ожидаемое количество товаров по странице: {expected_total}")

            if "Москва" not in body_text:
                print("Внимание: страница не выглядит как московская версия")

            # 1. Основной режим: скролл
            raw_items = collect_raw_items_with_scroll(page, expected_total=expected_total)

            collected_count = len({
                normalize_text(item.get("href") or "")
                for item in raw_items
                if normalize_text(item.get("href") or "")
            })
            print(f"После скролла собрано уникальных товаров: {collected_count}")

            # 2. Fallback: пагинация, если скролл не добрал всё
            if not expected_total or collected_count < expected_total:
                print("Скролл не добрал все товары, включаю обход страниц.")
                raw_items = collect_raw_items_with_pagination(
                    browser,
                    URL,
                    raw_items,
                    expected_total=expected_total,
                )

            grouped = defaultdict(lambda: {
                "texts": set(),
                "card_texts": [],
                "image_url": ""
            })

            for item in raw_items:
                href = normalize_text(item.get("href") or "")
                text = normalize_text(item.get("text") or "")
                card_text = normalize_text(item.get("cardText") or "")
                image_url = normalize_text(item.get("imageUrl") or "")

                if not href:
                    continue

                if text:
                    grouped[href]["texts"].add(text)

                if card_text:
                    grouped[href]["card_texts"].append(card_text)

                if image_url and not grouped[href]["image_url"]:
                    grouped[href]["image_url"] = image_url

            products = []

            for href, data in grouped.items():
                texts = list(data["texts"])
                all_card_text = " | ".join(data["card_texts"])
                image_url = data["image_url"]

                candidate_names = [
                    t for t in texts
                    if t
                    and t.lower() != "купить"
                    and "image" not in t.lower()
                    and len(t) >= 5
                ]

                name = max(candidate_names, key=len) if candidate_names else ""

                if not name and all_card_text:
                    lines = [x.strip() for x in all_card_text.split("|") if x.strip()]
                    for line in lines:
                        low = line.lower()
                        if (
                            "руб." not in low
                            and "₽" not in low
                            and "в наличии" not in low
                            and "изменить опции" not in low
                            and "купить" not in low
                            and len(line) >= 5
                        ):
                            name = line
                            break

                if not name:
                    continue

                product_type = detect_product_type(name)
                if product_type not in ALLOWED_TYPES:
                    continue

                price = pick_actual_price(all_card_text)
                if price == "Цена не найдена":
                    price = fetch_price_from_product_page(browser, href)

                products.append({
                    "url": href,
                    "name": name,
                    "type": product_type,
                    "price": price,
                    "image_url": image_url,
                })

            return products

        finally:
            page.close()
            browser.close()


def main():
    products = parse_products_with_browser()

    type_stats = {}
    for p in products:
        type_stats[p["type"]] = type_stats.get(p["type"], 0) + 1

    print(f"Найдено товаров после фильтрации: {len(products)}")
    print(f"По типам: {type_stats}")

    if not products:
        print("Подходящие товары не найдены")
        return 0

    state = load_state()
    seen = set(state.get("seen", []))

    current_urls = {p["url"] for p in products}
    new_products = [p for p in products if p["url"] not in seen]

    if new_products:
        for p in new_products:
            if p["image_url"]:
                send_telegram_photo(
                    photo_url=p["image_url"],
                    name=p["name"],
                    price=p["price"],
                    product_url=p["url"],
                )
                print(f"Отправлено с фото: {p['name']} | {p['price']} | {p['url']}")
            else:
                text = f"{p['name']}\n{p['price']}\n{p['url']}"
                send_telegram_message(text)
                print(f"Отправлено без фото: {p['name']} | {p['price']} | {p['url']}")
    else:
        print("Новых подходящих товаров нет.")

    save_state({"seen": sorted(current_urls)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
