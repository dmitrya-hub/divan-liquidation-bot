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

        for _ in range(6):
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(1500)

        links = page.locator('a[href*="/product/"]')
        count = links.count()
        print(f"Найдено ссылок /product/: {count}")

        raw_items = links.evaluate_all("""
            (els) => els.map((a) => {
                const href = a.href || "";
                const text = (a.innerText || a.textContent || "").trim();

                let cardText = "";
                let el = a;
                for (let i = 0; i < 8 && el; i++) {
                    const t = (el.innerText || el.textContent || "").trim();
                    if (t.includes("В наличии:")) {
                        cardText = t;
                        break;
                    }
                    el = el.parentElement;
                }

                return { href, text, cardText };
            })
        """)

        browser.close()

    grouped = defaultdict(lambda: {"texts": set(), "card_text": ""})

    for item in raw_items:
        href = item["href"]
        text = (item.get("text") or "").strip()
        card_text = (item.get("cardText") or "").strip()

        if not href:
            continue

        if text:
            grouped[href]["texts"].add(text)

        if card_text and len(card_text) > len(grouped[href]["card_text"]):
            grouped[href]["card_text"] = card_text

    products = []

    for href, data in grouped.items():
        texts = list(data["texts"])
        card_text = data["card_text"]

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

        price = "Цена не найдена"
        stock = ""

        if card_text:
            price_match = re.search(r"(\d[\d\s]*\s?руб\.)", card_text)
            if price_match:
                price = price_match.group(1)

            stock_match = re.search(r"В наличии:\s*(\d+)\s*шт", card_text)
            if stock_match:
                stock = stock_match.group(1)

        if not name:
            continue

        products.append({
            "url": href,
            "name": name,
            "price": price,
            "stock": stock,
        })

    return products


def main():
    products = parse_products_with_browser()
    print(f"Найдено товаров после очистки: {len(products)}")

    if not products:
        print("Товары не найдены")
        return 0

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
