import json
import os
import sys
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

URL = "https://www.divan.ru/category/likvidatsiya"
STATE_FILE = Path("state.json")

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")


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


def parse_products_with_browser():
    products = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="ru-RU",
            viewport={"width": 1440, "height": 2400},
        )

        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)

        # Прокрутка для догрузки карточек
        for _ in range(6):
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(1500)

        # Все ссылки на товары
        links = page.locator('a[href*="/product/"]')
        count = links.count()
        print(f"Найдено ссылок /product/: {count}")

        seen = set()

        for i in range(count):
            link = links.nth(i)
            href = link.get_attribute("href")
            if not href:
                continue

            if href.startswith("/"):
                href = "https://www.divan.ru" + href

            if href in seen:
                continue
            seen.add(href)

            text = (link.inner_text() or "").strip()
            if not text:
                continue

            # Отбрасываем мусорные ссылки без названия товара
            if text.lower() == "купить":
                continue
            if len(text) < 5:
                continue

            # Пытаемся взять текст карточки рядом
            card_text = ""
            try:
                card_text = link.locator("xpath=ancestor::*[self::article or self::div][1]").inner_text(timeout=1000)
            except Exception:
                pass

            price = "Цена не найдена"
            stock = ""

            import re

            price_match = re.search(r"(\d[\d\s]*\s?руб\.)", card_text)
            if price_match:
                price = price_match.group(1)

            stock_match = re.search(r"В наличии:\s*(\d+)\s*шт", card_text)
            if stock_match:
                stock = stock_match.group(1)

            products.append({
                "url": href,
                "name": text,
                "price": price,
                "stock": stock,
            })

        browser.close()

    # Фильтр дублей и мусора
    cleaned = {}
    for p in products:
        name = p["name"].strip()
        if any(x in name.lower() for x in ["купить", "image"]):
            continue
        cleaned[p["url"]] = p

    return list(cleaned.values())


def main():
    products = parse_products_with_browser()
    print(f"Найдено товаров после очистки: {len(products)}")

    if not products:
        print("Товары не найдены")
        return 1

    state = load_state()
    seen = set(state.get("seen", []))

    current_urls = {p["url"] for p in products}
    new_products = [p for p in products if p["url"] not in seen]

    if not seen:
        print(f"Первый запуск. Сохраняю {len(current_urls)} товаров без отправки уведомлений.")
        save_state({"seen": sorted(current_urls)})
        return 0

    if new_products:
        for p in new_products:
            stock_part = f"\nОстаток: {p['stock']} шт." if p["stock"] else ""
            text = (
                f"🆕 Новый товар в ликвидации Divan.ru\n\n"
                f"{p['name']}\n"
                f"{p['price']}{stock_part}\n"
                f"{p['url']}"
            )
            send_telegram_message(text)
            print(f"Отправлено: {p['name']}")
    else:
        print("Новых товаров нет.")

    save_state({"seen": sorted(current_urls)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
