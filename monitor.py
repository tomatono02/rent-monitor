import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

SEEN_IDS_FILE = "seen_ids.json"

SITE_RULES: dict[str, dict[str, list[str] | str]] = {
    "suumo": {
        "name": "SUUMO",
        "card_selectors": [
            "li.cassetteitem",
            "div.cassetteitem",
            "li[class*='cassetteitem']",
            "div[class*='cassetteitem']",
        ],
        "name_selectors": [
            ".cassetteitem_content-title",
            ".js-cassette_link_href",
        ],
        "link_tokens": ["/chintai/"],
    },
    "homes": {
        "name": "HOME'S",
        "card_selectors": [
            "div.mod-mergeBuilding",
            "section.mod-mergeBuilding",
            "li.mod-mergeBuilding",
            "article",
            "li",
        ],
        "name_selectors": [
            ".mod-mergeBuilding__buildingName",
            ".prg-buildingName",
            ".moduleInner__title",
        ],
        "link_tokens": ["/chintai/", "/room/", "/b-"],
    },
    "generic": {
        "name": "GENERIC",
        "card_selectors": ["article", "li", "div"],
        "name_selectors": ["h2", "h3", ".title", ".name"],
        "link_tokens": ["/chintai/", "/rent/", "/room/", "/b-"],
    },
}


@dataclass
class Config:
    search_urls: list[str]
    slack_webhook_url: str
    notify_on_no_new: bool


@dataclass
class Property:
    property_id: str
    source_site: str
    name: str
    detail_url: str
    rent_yen: int
    management_fee_yen: int
    parking_fee_yen: int
    total_yen: int
    layout: str
    area_m2: float
    age_years: float
    nearest_station: str
    station_walk_min: int

    @property
    def unique_id(self) -> str:
        return f"{self.source_site}:{self.property_id}"


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def str_to_bool(v: str | None, default: bool = False) -> bool:
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def parse_search_urls(single: str, multiple: str) -> list[str]:
    urls: list[str] = []
    if multiple:
        for token in re.split(r"[\r\n,]+", multiple):
            t = token.strip()
            if t:
                urls.append(t)
    if single and single not in urls:
        urls.append(single)
    return urls


def load_config() -> Config:
    load_dotenv()
    venv_env = Path("venv/.env")
    if venv_env.exists():
        load_dotenv(venv_env)

    search_urls = parse_search_urls(
        single=os.getenv("SEARCH_URL", "").strip(),
        multiple=os.getenv("SEARCH_URLS", "").strip(),
    )
    slack_webhook_url = os.getenv("SLACK_WEBHOOK_URL", "").strip()

    missing: list[str] = []
    if not search_urls:
        missing.append("SEARCH_URL or SEARCH_URLS")
    if not slack_webhook_url:
        missing.append("SLACK_WEBHOOK_URL")
    if missing:
        raise ValueError(f".env の必須項目が不足しています: {', '.join(missing)}")

    return Config(
        search_urls=search_urls,
        slack_webhook_url=slack_webhook_url,
        notify_on_no_new=str_to_bool(os.getenv("SLACK_NOTIFY_ON_NO_NEW"), default=False),
    )


def detect_site(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "suumo.jp" in host:
        return "suumo"
    if "homes.co.jp" in host or "lifull" in host:
        return "homes"
    return "generic"


def fetch_search_html(search_url: str) -> str:
    logging.info("Playwrightで取得: %s", search_url)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(search_url, wait_until="networkidle", timeout=45_000)
            page.wait_for_timeout(2_000)
            return page.content()
        except PlaywrightTimeoutError as exc:
            raise RuntimeError(f"ページ取得タイムアウト: {search_url}") from exc
        finally:
            browser.close()


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def to_yen(token: str) -> int:
    token = token.replace(",", "").strip()
    if not token or token.startswith("-"):
        return 0
    if "万円" in token:
        num = re.sub(r"[^0-9.]", "", token)
        return int(float(num) * 10_000) if num else 0
    num = re.sub(r"[^0-9]", "", token)
    return int(num) if num else 0


def extract_money_by_label(text: str, labels: tuple[str, ...]) -> int:
    for label in labels:
        pat = rf"{label}[^0-9\-]*([0-9]+(?:\.[0-9]+)?万円|[0-9,]+円|-)"
        m = re.search(pat, text)
        if m:
            return to_yen(m.group(1))
    return 0


def extract_layout(text: str) -> str:
    m = re.search(r"\b([1-4]LDK|[1-4]DK|[1-4]K|ワンルーム)\b", text)
    return m.group(1) if m else ""


def extract_area_m2(text: str) -> float:
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(?:m2|㎡)", text)
    if not m:
        return 0.0
    try:
        return float(m.group(1))
    except ValueError:
        return 0.0


def extract_age_years(text: str) -> float:
    if "新築" in text:
        return 0.0
    m = re.search(r"築\s*([0-9]+)\s*年", text)
    if m:
        return float(m.group(1))
    m = re.search(r"築\s*([0-9]+)\s*ヶ月", text)
    if m:
        return round(float(m.group(1)) / 12.0, 2)
    return 999.0


def extract_walk_min(text: str, label: str = "徒歩") -> int:
    m = re.search(rf"{label}\s*([0-9]+)\s*分", text)
    return int(m.group(1)) if m else 999


def extract_property_id(url: str) -> str:
    path = urlparse(url).path
    m = re.search(r"/(?:chintai|rent|room|b)[_/\-]?([^/?.]+)", path)
    if m:
        return m.group(1)
    m2 = re.search(r"([0-9]{6,})", path)
    if m2:
        return m2.group(1)
    return re.sub(r"[^a-zA-Z0-9]", "_", path).strip("_") or "unknown"


def parse_station(text: str) -> str:
    station_match = re.search(r"([\w\u3000-\u9fff]+駅)", text)
    if station_match:
        return normalize_space(station_match.group(1))
    return ""


def collect_cards(soup: BeautifulSoup, selectors: list[str]) -> list[Any]:
    cards: list[Any] = []
    for selector in selectors:
        found = soup.select(selector)
        if found:
            cards.extend(found)

    unique_cards: list[Any] = []
    seen_keys: set[int] = set()
    for card in cards:
        key = id(card)
        if key not in seen_keys:
            unique_cards.append(card)
            seen_keys.add(key)
    return unique_cards


def find_detail_anchor(card: Any, link_tokens: list[str]) -> Any | None:
    anchors = card.select("a[href]")
    if not anchors:
        return None

    for a in anchors:
        href = str(a.get("href", ""))
        if any(token in href for token in link_tokens):
            return a

    return anchors[0]


def pick_name(card: Any, selectors: list[str], anchor: Any) -> str:
    for selector in selectors:
        t = card.select_one(selector)
        if t:
            name = normalize_space(t.get_text(" ", strip=True))
            if name:
                return name

    anchor_text = normalize_space(anchor.get_text(" ", strip=True))
    if anchor_text:
        return anchor_text[:80]

    return "名称未取得"


def parse_properties_for_site(html: str, search_url: str) -> list[Property]:
    site_key = detect_site(search_url)
    rules = SITE_RULES.get(site_key, SITE_RULES["generic"])

    soup = BeautifulSoup(html, "html.parser")
    cards = collect_cards(soup, rules["card_selectors"])  # type: ignore[arg-type]

    if not cards:
        cards = [tag for tag in soup.find_all(["li", "div", "article"]) if tag.get_text(" ", strip=True)]

    base_url = f"{urlparse(search_url).scheme}://{urlparse(search_url).netloc}"

    properties: list[Property] = []
    for card in cards:
        raw_text = normalize_space(card.get_text(" ", strip=True))
        if not raw_text:
            continue
        if "賃" not in raw_text and "円" not in raw_text and "万円" not in raw_text:
            continue

        anchor = find_detail_anchor(card, rules["link_tokens"])  # type: ignore[arg-type]
        if anchor is None:
            continue

        href = str(anchor.get("href", "")).strip()
        if not href:
            continue

        detail_url = urljoin(base_url, href)
        prop_id = extract_property_id(detail_url)
        name = pick_name(card, rules["name_selectors"], anchor)  # type: ignore[arg-type]

        rent = extract_money_by_label(raw_text, ("賃料", "家賃"))
        if rent == 0:
            rent_match = re.search(r"([0-9]+(?:\.[0-9]+)?万円)", raw_text)
            if rent_match:
                rent = to_yen(rent_match.group(1))

        mgmt = extract_money_by_label(raw_text, ("管理費", "共益費"))
        parking = extract_money_by_label(raw_text, ("駐車場", "駐車料金"))
        total = rent + mgmt + parking

        properties.append(
            Property(
                property_id=prop_id,
                source_site=site_key,
                name=name,
                detail_url=detail_url,
                rent_yen=rent,
                management_fee_yen=mgmt,
                parking_fee_yen=parking,
                total_yen=total,
                layout=extract_layout(raw_text),
                area_m2=extract_area_m2(raw_text),
                age_years=extract_age_years(raw_text),
                nearest_station=parse_station(raw_text),
                station_walk_min=extract_walk_min(raw_text),
            )
        )

    return properties


def load_seen_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logging.warning("seen_ids.json の読み込みに失敗したため初期化します")
        return set()

    if isinstance(data, list):
        return {str(v) for v in data}
    if isinstance(data, dict):
        ids = data.get("seen_ids", [])
        if isinstance(ids, list):
            return {str(v) for v in ids}
    return set()


def save_seen_ids(path: Path, ids: set[str]) -> None:
    payload = {"seen_ids": sorted(ids)}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def build_notification_message(items: list[Property]) -> str:
    head = "[賃貸新着通知] 検索URLの新着物件があります"
    lines = [head, ""]

    for p in items:
        lines.extend(
            [
                f"・[{p.source_site}] {p.name}",
                f"  合計: {p.total_yen:,}円 (家賃{p.rent_yen:,}/管理費{p.management_fee_yen:,}/駐車場{p.parking_fee_yen:,})",
                f"  間取り: {p.layout} / {p.area_m2:.1f}㎡ / 築{p.age_years:g}年",
                f"  最寄: {p.nearest_station} 徒歩{p.station_walk_min if p.station_walk_min != 999 else '不明'}分",
                f"  URL: {p.detail_url}",
                "",
            ]
        )

    return "\n".join(lines).strip()


def build_no_new_message(total: int) -> str:
    return "\n".join(
        [
            ":information_source: Rent Monitor 実行結果",
            f"取得件数: {total}",
            "新着件数: 0",
        ]
    )


def send_slack_notification(webhook_url: str, text: str) -> None:
    payload = {"text": text[:3500]}
    try:
        resp = requests.post(webhook_url, json=payload, timeout=20)
    except requests.RequestException as exc:
        raise RuntimeError(f"Slack通知失敗: {exc}") from exc

    if not resp.ok:
        raise RuntimeError(f"Slack通知失敗: {resp.status_code} {resp.text}")


def dedupe_properties(items: list[Property]) -> list[Property]:
    seen: set[str] = set()
    out: list[Property] = []
    for p in items:
        if p.unique_id in seen:
            continue
        seen.add(p.unique_id)
        out.append(p)
    return out


def main() -> int:
    setup_logging()

    try:
        config = load_config()
    except Exception as exc:
        logging.error("設定エラー: %s", exc)
        return 1

    all_properties: list[Property] = []
    for url in config.search_urls:
        try:
            html = fetch_search_html(url)
            parsed = parse_properties_for_site(html, url)
            logging.info("サイト取得件数(%s): %d", detect_site(url), len(parsed))
            all_properties.extend(parsed)
        except Exception as exc:
            logging.error("サイト処理エラー: %s (%s)", url, exc)

    if not all_properties:
        logging.warning("物件が取得できませんでした")
        if config.notify_on_no_new:
            try:
                send_slack_notification(config.slack_webhook_url, ":warning: Rent Monitor 取得件数 0")
            except Exception as exc:
                logging.error("Slack通知エラー: %s", exc)
        return 0

    all_properties = dedupe_properties(all_properties)
    logging.info("合計取得件数: %d", len(all_properties))

    seen_path = Path(SEEN_IDS_FILE)
    seen_ids = load_seen_ids(seen_path)

    new_items = [p for p in all_properties if p.unique_id not in seen_ids]
    logging.info("新着件数: %d", len(new_items))

    if not new_items:
        if config.notify_on_no_new:
            text = build_no_new_message(len(all_properties))
            try:
                send_slack_notification(config.slack_webhook_url, text)
                logging.info("Slack通知(新着なし)を送信しました")
            except Exception as exc:
                logging.error("Slack通知エラー: %s", exc)
                return 1

        all_ids = seen_ids | {p.unique_id for p in all_properties}
        try:
            save_seen_ids(seen_path, all_ids)
        except Exception as exc:
            logging.warning("seen_ids.json 保存失敗: %s", exc)
        return 0

    message = build_notification_message(new_items)
    try:
        send_slack_notification(
            config.slack_webhook_url,
            f":house: Rent Monitor 新着 {len(new_items)} 件\n{message}",
        )
        logging.info("Slack通知を送信しました")
    except Exception as exc:
        logging.error("Slack通知エラー: %s", exc)
        return 1

    all_ids = seen_ids | {p.unique_id for p in all_properties}
    try:
        save_seen_ids(seen_path, all_ids)
    except Exception as exc:
        logging.warning("seen_ids.json 保存失敗: %s", exc)

    return 0


if __name__ == "__main__":
    sys.exit(main())
