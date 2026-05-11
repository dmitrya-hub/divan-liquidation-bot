import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs, urlunparse

from playwright.sync_api import sync_playwright

URL = "https://www.divan.ru/category/rasprodaza-mebeli?types%5B%5D=54&types%5B%5D=1&types%5B%5D=4&types%5B%5D=43&defect=1"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}


def normalize_text(text: str) -> str:
    text = text or ""
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


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

    # На карточке часто есть старая и новая цена.
    # Актуальная цена обычно меньше старой.
    return format_price(min(prices))


def goto_with_retry(page, url, wait_until="domcontentloaded", attempts=2, timeout=60000):
    for attempt in range(1, attempts + 1):
        try:
            print(f"Открываю {url}, попытка {attempt}...")
            page.goto(url, wait_until=wait_until, timeout=timeout)
            page.wait_for_timeout(3000)
            return True
        except Exception as e:
            print(f"Ошибка открытия {url} на попытке {attempt}: {e}")
            if attempt == attempts:
                return False
            page.wait_for_timeout(5000)


def extract_items_from_current_page(page):
    links = page.locator('a[href*="/product/"]')
    page_count = links.count()
    print(f"Найдено ссылок /product/ на текущей странице: {page_count}")

    return links.evaluate_all("""
        (els) => els.map((a) => {
            const href = a.href || "";
            const text = (a.innerText || a.textContent || "").trim();
            const ariaLabel = a.getAttribute("aria-label") || "";
            const titleAttr = a.getAttribute("title") || "";

            let cardText = "";
            let containerText = "";
            let imageUrl = "";
            let imageAlt = "";

            let el = a;
            for (let i = 0; i < 20 && el; i++) {
                const t = (el.innerText || el.textContent || "").trim();

                if (!cardText && (
                    t.includes("В наличии:") ||
                    t.includes("Изменить опции") ||
                    t.includes("руб.") ||
                    t.includes("₽")
                )) {
                    cardText = t;
                }

                if (!containerText && t.length > 10) {
                    containerText = t;
                }

                if (!imageUrl || !imageAlt) {
                    const img = el.querySelector("img");
                    if (img) {
                        imageUrl =
                            imageUrl ||
                            img.src ||
                            img.getAttribute("src") ||
                            img.getAttribute("data-src") ||
                            "";
                        imageAlt =
                            imageAlt ||
                            img.getAttribute("alt") ||
                            img.getAttribute("title") ||
                            "";
                    }
                }

                el = el.parentElement;
            }

            if (!imageUrl || !imageAlt) {
                const img = a.querySelector("img");
                if (img) {
                    imageUrl =
                        imageUrl ||
                        img.src ||
                        img.getAttribute("src") ||
                        img.getAttribute("data-src") ||
                        "";
                    imageAlt =
                        imageAlt ||
                        img.getAttribute("alt") ||
                        img.getAttribute("title") ||
                        "";
                }
            }

            return {
                href,
                text,
                ariaLabel,
                titleAttr,
                cardText,
                containerText,
                imageUrl,
                imageAlt
            };
        })
    """)


def merge_unique_items(raw_items, new_items):
    added = 0
    existing_urls = {
        normalize_text(item.get("href") or "")
        for item in raw_items
        if normalize_text(item.get("href") or "")
    }

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
    max_rounds = 25

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
        page.wait_for_timeout(1200)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1200)

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

        for page_num in range(2, 15):
            page_url = build_page_url(base_url, page_num)
            print(f"Обхожу страницу пагинации {page_num}: {page_url}")

            ok = goto_with_retry(page, page_url, wait_until="domcontentloaded", attempts=2, timeout=60000)
            if not ok:
                print(f"Не удалось открыть page={page_num}, завершаю пагинацию.")
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


def clean_candidate_name(text: str) -> str:
    text = normalize_text(text)
    if not text:
        return ""

    text = re.sub(r"^Image:\s*", "", text, flags=re.IGNORECASE)
    text = normalize_text(text)

    low = text.lower()

    if "руб" in low or "₽" in low:
        return ""

    bad_exact = {
        "купить",
        "в корзину",
        "изменить опции",
    }
    if low in bad_exact:
        return ""

    if "в наличии" in low:
        return ""

    if len(text) < 5:
        return ""

    return text


def extract_name_from_item(item):
    candidates = []

    for key in ["text", "ariaLabel", "titleAttr", "imageAlt"]:
        value = clean_candidate_name(item.get(key) or "")
        if value:
            candidates.append(value)

    # cardText/containerText могут быть длинными, поэтому разбиваем на строки
    for key in ["cardText", "containerText"]:
        raw = item.get(key) or ""
        for part in re.split(r"[\n|]", raw):
            value = clean_candidate_name(part)
            if value:
                candidates.append(value)

    if not candidates:
        return ""

    preferred = []
    for c in candidates:
        low = c.lower()
        if low.startswith("диван") or low.startswith("кровать"):
            preferred.append(c)

    if preferred:
        return max(preferred, key=len)

    return max(candidates, key=len)


def build_catalog_products(raw_items):
    grouped = defaultdict(lambda: {
        "items": [],
        "image_url": "",
    })

    for item in raw_items:
        href = normalize_text(item.get("href") or "")
        if not href:
            continue

        grouped[href]["items"].append(item)

        image_url = normalize_text(item.get("imageUrl") or "")
        if image_url and not grouped[href]["image_url"]:
            grouped[href]["image_url"] = image_url

    products = []

    for href, data in grouped.items():
        item_candidates = data["items"]
        image_url = data["image_url"]

        name = ""
        price = "Цена не найдена"

        for candidate in item_candidates:
            candidate_name = extract_name_from_item(candidate)
            if candidate_name and len(candidate_name) > len(name):
                name = candidate_name

            candidate_price = pick_actual_price(
                normalize_text(candidate.get("cardText") or "") + "\n" +
                normalize_text(candidate.get("containerText") or "")
            )
            if candidate_price != "Цена не найдена":
                price = candidate_price

        products.append({
            "url": href,
            "name": name,
            "price": price,
            "image_url": image_url,
        })

    return products


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        page = browser.new_page(
            user_agent=HEADERS["User-Agent"],
            locale="ru-RU",
            viewport={"width": 1440, "height": 2600},
        )

        try:
            ok = goto_with_retry(page, URL, wait_until="domcontentloaded", attempts=2, timeout=60000)
            if not ok:
                print("Не удалось открыть основную страницу.")
                return 1

            body_text = page.locator("body").inner_text()
            expected_total = extract_total_products(page)
            print(f"Ожидаемое количество товаров по странице: {expected_total}")

            if "Москва" not in body_text:
                print("Внимание: страница не выглядит как московская версия")

            raw_items = collect_raw_items_with_scroll(page, expected_total=expected_total)

            collected_count = len({
                normalize_text(item.get("href") or "")
                for item in raw_items
                if normalize_text(item.get("href") or "")
            })
            print(f"После скролла собрано уникальных товаров: {collected_count}")

            if not expected_total or collected_count < expected_total:
                print("Скролл не добрал все товары, включаю обход страниц.")
                raw_items = collect_raw_items_with_pagination(
                    browser,
                    URL,
                    raw_items,
                    expected_total=expected_total,
                )

            products = build_catalog_products(raw_items)

            print("")
            print("========== ИТОГ ==========")
            print(f"Собрано raw items: {len(raw_items)}")
            print(f"Собрано товаров: {len(products)}")

            without_name = [p for p in products if not p["name"]]
            without_price = [p for p in products if p["price"] == "Цена не найдена"]
            without_image = [p for p in products if not p["image_url"]]

            print(f"Без имени: {len(without_name)}")
            print(f"Без цены: {len(without_price)}")
            print(f"Без картинки: {len(without_image)}")

            print("")
            print("========== ПЕРВЫЕ 20 ТОВАРОВ ==========")

            for idx, p_item in enumerate(products[:20], start=1):
                print("")
                print(f"{idx}. {p_item['name'] or 'ИМЯ НЕ НАЙДЕНО'}")
                print(f"Цена: {p_item['price']}")
                print(f"Картинка: {p_item['image_url'] or 'КАРТИНКА НЕ НАЙДЕНА'}")
                print(f"Ссылка: {p_item['url']}")

            if without_name:
                print("")
                print("========== ПЕРВЫЕ 10 БЕЗ ИМЕНИ ==========")
                for idx, p_item in enumerate(without_name[:10], start=1):
                    print(f"{idx}. {p_item['url']}")

            if without_price:
                print("")
                print("========== ПЕРВЫЕ 10 БЕЗ ЦЕНЫ ==========")
                for idx, p_item in enumerate(without_price[:10], start=1):
                    print(f"{idx}. {p_item['name'] or 'ИМЯ НЕ НАЙДЕНО'} | {p_item['url']}")

            if without_image:
                print("")
                print("========== ПЕРВЫЕ 10 БЕЗ КАРТИНКИ ==========")
                for idx, p_item in enumerate(without_image[:10], start=1):
                    print(f"{idx}. {p_item['name'] or 'ИМЯ НЕ НАЙДЕНО'} | {p_item['url']}")

            return 0

        finally:
            page.close()
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
