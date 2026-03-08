import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List

import requests
from bs4 import BeautifulSoup

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


def load_state() -> Dict:
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"seen": []}


def save_state(state: Dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fetch_html(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def normalize_url(href: str) -> str:
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return "https://www.divan.ru" + href


def extract_price(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    m = re.search(r"(\d[\d\s]*\s?руб\.)", text)
    return m.group(1) if m else "Цена не найдена"


def parse_products(html: str) -> List[Dict]:
    soup = BeautifulSoup(html, "lxml")
    products = []
    seen_urls = set()

    # Берем все ссылки на карточки товаров
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/product/" not in href:
            continue

        url = normalize_url(href)
        if url in seen_urls:
            continue
        seen_urls.add(url)

        name = a.get_text(" ", strip=True)
        if not name:
            continue

        # Пытаемся вытащить окружающий текст блока
        parent_text = a.parent.get_text(" ", strip=True) if a.parent else name
        price = extract_price(parent_text)

        products.append({
            "url": url,
            "name": name,
            "price": price,
        })

    return products


def send_telegram_message(text: str) -> None:
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


def main() -> int:
    html = fetch_html(URL)
    products = parse_products(html)

    if not products:
        print("Товары не найдены")
        return 1

    state = load_state()
    seen = set(state.get("seen", []))

    current_urls = {p["url"] for p in products}
    new_products = [p for p in products if p["url"] not in seen]

    # Первый запуск: просто сохраним текущий список, без спама
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
