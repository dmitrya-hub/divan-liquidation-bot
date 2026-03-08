import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

URL = "https://www.divan.ru/category/likvidatsiya"
STATE_FILE = Path("state.json")

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

ALLOWED_TYPES = {
    "Диван прямой",
    "Диван угловой",
    "Кровать",
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


def detect_product_type(name: str):
    low = name.lower().strip()

    if low.startswith("диван прямой"):
        return "Диван прямой"
    if low.startswith("диван угловой"):
        return "Диван угловой"
    if low.startswith("кровать"):
        return "Кровать"

    return None


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def extract_total_products(page):
    body_text = page.locator("body").inner_text()
    m = re.search(r"Смотреть все товары\s+(\d+)", body_text)
    if m:
        return int(m.group(1))
    return None


def scroll_until_all_loaded(page):
    expected_total = extract_total_products(page)
    print(f"Ожидаемое количество товаров по странице: {expected_total}")

    prev_count = 0
    stable_rounds = 0
    max_rounds = 60

    for round_num in range(1, max_rounds + 1):
        current_count = page.locator('a[href*="/product/"]').count()
        print(f"Скролл {round_num}: найдено ссылок /product/: {current_count}")

        if expected_total and current_count >= expected_total:
            print("Достигли ожидаемого общего количества товаров.")
            break

        if current_count == prev_count:
            stable_rounds += 1
        else:
            stable_rounds = 0

        if stable_rounds >= 4:
            print("Количество ссылок перестало расти, завершаю прокрутку.")
            break

        prev_count = current_count

        page.mouse.wheel(0, 5000)
        page.wait_for_timeout(2000)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1500)


def parse_products_with_browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="ru-RU",
            viewport={"width": 1440, "height": 2600},
        )

        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)

        page_text = page.locator("body").inner_text()
        if "Москва" not in page_text:
            print("Внимание: страница не выглядит как московская версия")

        scroll_until_all_loaded(page)

        links = page.locator('a[href*="/product/"]')
        count = links.count()
        print(f"Итоговое число ссылок /product/: {count}")

        raw_items = links.evaluate_all("""
            (els) => els.map((a) => {
                const href = a.href || "";
                const text = (a.innerText || a.textContent || "").trim();

                let cardText = "";
                let imageUrl = "";

                let el = a;
                for (let i = 0; i < 10 && el; i++) {
                    const t = (el.innerText || el.textContent || "").trim();
                    if (t.includes("В наличии:") || t.includes("Изменить опции")) {
                        cardText = t;

                        const img = el.querySelector("img");
                        if (img) {
                            imageUrl =
                                img.src ||
                                img.getAttribute("src") ||
                                img.getAttribute("data-src") ||
                                "";
                        }
                        break;
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

        browser.close()

    grouped = defaultdict(lambda: {"texts": set(), "card_text": "", "image_url": ""})

    for item in raw_items:
        href = normalize_text(item.get("href") or "")
        text = normalize_text(item.get("text") or "")
        card_text = normalize_text(item.get("cardText") or "")
        image_url = normalize_text(item.get("imageUrl") or "")

        if not href:
            continue

        if text:
            grouped[href]["texts"].add(text)

        if card_text and len(card_text) > len(grouped[href]["card_text"]):
            grouped[href]["card_text"] = card_text

        if image_url and not grouped[href]["image_url"]:
            grouped[href]["image_url"] = image_url

    products = []

    for href, data in grouped.items():
        texts = list(data["texts"])
        card_text = data["card_text"]
        image_url = data["image_url"]

        candidate_names = [
            t for t in texts
            if t
            and t.lower() != "купить"
            and "image" not in t.lower()
            and len(t) >= 5
        ]

        name = max(candidate_names, key=len) if candidate_names else ""

        if not name and card_text:
            lines = [x.strip() for x in card_text.splitlines() if x.strip()]
            for line in lines:
                low = line.lower()
                if (
                    "руб." not in low
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

        price = "Цена не найдена"
        if card_text:
            price_match = re.search(r"(\d[\d\s]*\s?руб\.)", card_text)
            if price_match:
                price = price_match.group(1)

        products.append({
            "url": href,
            "name": name,
            "type": product_type,
            "price": price,
            "image_url": image_url,
        })

    return products


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
                print(f"Отправлено с фото: {p['name']}")
            else:
                text = f"{p['name']}\n{p['price']}\n{p['url']}"
                send_telegram_message(text)
                print(f"Отправлено без фото: {p['name']}")
    else:
        print("Новых подходящих товаров нет.")

    save_state({"seen": sorted(current_urls)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
