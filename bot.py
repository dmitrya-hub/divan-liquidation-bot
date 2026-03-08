import json
import os
import re
import sys
from pathlib import Path

import requests

URL = "https://www.divan.ru/category/likvidatsiya"
STATE_FILE = Path("state.json")

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
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


def fetch_html(url):
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


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


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_products(html: str):
    products = []

    # Ищем карточки по повторяющемуся шаблону:
    # цена -> название -> "В наличии"
    pattern = re.compile(
        r'(?P<price>\d[\d\s]*\s?руб\.)'
        r'.{0,400}?'
        r'(?P<name>[А-ЯA-ZЁ0-9][^<>\n\r]{3,120}?)'
        r'.{0,200}?В наличии:\s*(?P<stock>\d+)\s*шт\.',
        re.DOTALL,
    )

    matches = list(pattern.finditer(html))
    used_urls = set()

    # Ищем все product-ссылки в html
    all_urls = re.findall(r'https://www\.divan\.ru/product/[^"\']+', html)
    if not all_urls:
        all_urls = re.findall(r'/product/[^"\']+', html)
        all_urls = ["https://www.divan.ru" + u for u in all_urls]

    for m in matches:
        name = clean_text(m.group("name"))
        price = clean_text(m.group("price"))
        stock = m.group("stock")

        # Пытаемся подобрать ближайшую ссылку по окрестности текста
        chunk_start = max(0, m.start() - 1500)
        chunk_end = min(len(html), m.end() + 1500)
        chunk = html[chunk_start:chunk_end]

        local_urls = re.findall(r'https://www\.divan\.ru/product/[^"\']+', chunk)
        if not local_urls:
            local_urls = re.findall(r'/product/[^"\']+', chunk)
            local_urls = ["https://www.divan.ru" + u for u in local_urls]

        url = None
        for candidate in local_urls + all_urls:
            if candidate not in used_urls:
                url = candidate
                used_urls.add(candidate)
                break

        if not url:
            continue

        products.append({
            "url": url,
            "name": name,
            "price": price,
            "stock": stock,
        })

    # Убираем явные дубли по URL
    unique = {}
    for p in products:
        unique[p["url"]] = p

    return list(unique.values())


def main():
    html = fetch_html(URL)
    products = parse_products(html)

    print(f"Найдено товаров: {len(products)}")

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
            text = (
                f"🆕 Новый товар в ликвидации Divan.ru\n\n"
                f"{p['name']}\n"
                f"{p['price']}\n"
                f"Остаток: {p['stock']} шт.\n"
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
