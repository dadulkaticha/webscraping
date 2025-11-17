import argparse
import hashlib
import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


MAIN_URL = "https://www.lidovky.cz/"
SOURCE_SECOND_LEVEL = "lidovky"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; LidovkyProjectBot/1.0)"
}

# Rubriky, které nás zajímají jako články:
ALLOWED_PATH_PREFIXES = (
    "/domov/",
    "/zahranici/",
    "/byznys/",
    "/kultura/",
    "/sport/",
    "/magazin/",
)

# --------------------------------------------------------------------
# REGULÁRNÍ VÝRAZY – 3 ks, jeden se zachytávací skupinou
# --------------------------------------------------------------------

# 1) ČÁSTKY V KČ/KORUNÁCH – používá pojmenovanou zachytávací skupinu (?P<amount>...)
RE_MONEY = re.compile(
    r"\b(?P<amount>(?:\d{1,3}(?:[ .]\d{3})*|\d+)(?:,\d+)?)\s*(?:Kč|korun|koruny|koruna)\b",
    re.IGNORECASE,
)

# 2) PROCENTA
RE_PERCENT = re.compile(
    r"\b\d{1,3}(?:[.,]\d+)?\s*%"
)

# 3) DATUM (český formát, např. 21. 10. 2025 nebo 1.11.25)
RE_DATE = re.compile(
    r"\b\d{1,2}\.\s?\d{1,2}\.\s?(?:\d{2}|\d{4})\b"
)


def regex_derived_tags(text: str) -> list[str]:
    """
    Z plného textu článku vytvoří dodatečné tagy na základě 3 regexů.

    - RE_MONEY  -> money:150 000 Kč, money_value:150000
    - RE_PERCENT-> percent:3,2%
    - RE_DATE   -> date:21.10.2025
    """
    tags: set[str] = set()

    # --- 1) peníze (regex se ZACHYTÁVACÍ SKUPINOU) ---
    for m in RE_MONEY.finditer(text):
        full = m.group(0).strip()          # celý match, např. "150 000 Kč"
        amount = m.group("amount")         # jen číslo z pojmenované skupiny
        # trochu normalizace čísla
        amount_norm = amount.replace(" ", "").replace(".", "")
        amount_norm = amount_norm.replace(",", ".")
        tags.add(f"money:{full}")
        tags.add(f"money_value:{amount_norm}")

    # --- 2) procenta ---
    for m in RE_PERCENT.finditer(text):
        pct = m.group(0).replace(" ", "")  # "3,2 %"" -> "3,2%"
        tags.add(f"percent:{pct}")

    # --- 3) datum ---
    for m in RE_DATE.finditer(text):
        d = m.group(0)
        # "21. 10. 2025" -> "21.10.2025"
        d_norm = re.sub(r"\.\s+", ".", d)
        tags.add(f"date:{d_norm}")

    # abychom to nepřehnali s množstvím tagů
    return list(sorted(tags))[:40]


# --------------------------------------------------------------------
# Pomocné funkce pro scraping
# --------------------------------------------------------------------


def md5_tail8(s: str) -> str:
    """Posledních 8 znaků z MD5 hashe řetězce."""
    return hashlib.md5(s.encode("utf-8")).hexdigest()[-8:]


def normalize_url(base: str, href: str) -> str:
    """Relativní URL -> absolutní; nechá jen odkazy na lidovky.cz."""
    if not href:
        return ""
    absu = urljoin(base, href)
    parsed = urlparse(absu)
    if parsed.netloc not in ("www.lidovky.cz", "lidovky.cz"):
        return ""
    # odříznout případný #anchor
    return absu.split("#")[0]


def is_article_link(url: str) -> bool:
    """Hrubý filtr na články podle path v URL."""
    try:
        path = urlparse(url).path
    except Exception:
        return False
    return any(path.startswith(p) for p in ALLOWED_PATH_PREFIXES)


def get_article_links_from_main() -> list[str]:
    """Z hlavní stránky vytáhne unikátní URL článků."""
    resp = requests.get(MAIN_URL, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    links: set[str] = set()
    for a in soup.select("a[href]"):
        u = normalize_url(MAIN_URL, a.get("href"))
        if u and is_article_link(u):
            links.add(u)

    return sorted(links)


def extract_title(soup: BeautifulSoup) -> str:
    h1 = soup.select_one("h1")
    if h1:
        return h1.get_text(strip=True)
    meta = soup.select_one('meta[property="og:title"]')
    if meta and meta.get("content"):
        return meta["content"].strip()
    t = soup.select_one("title")
    return t.get_text(strip=True) if t else ""


def extract_author(soup: BeautifulSoup) -> str:
    cands = [
        soup.select_one('meta[name="author"]'),
        soup.select_one('[itemprop="author"]'),
        soup.select_one('[class*="author"]'),
        soup.select_one('a[rel="author"]'),
    ]
    for c in cands:
        if c:
            if c.name == "meta" and c.get("content"):
                return c["content"].strip()
            txt = c.get_text(strip=True)
            if txt:
                return txt
    return "Neznámý"


def extract_date(soup: BeautifulSoup) -> str:
    meta = soup.select_one('meta[property="article:published_time"]')
    if meta and meta.get("content"):
        return meta["content"]
    t = soup.select_one("time[datetime]")
    if t and t.get("datetime"):
        return t["datetime"]
    # fallback – aktuální čas
    return datetime.now().isoformat()


def extract_body_paragraphs(soup: BeautifulSoup) -> list[str]:
    """Najde odstavce z těla článku."""
    candidates = [
        'div[itemprop="articleBody"]',
        "div.article-body",
        "div#article",
        "article",
    ]
    container = None
    for sel in candidates:
        container = soup.select_one(sel)
        if container:
            break

    ps = (container or soup).select("p")
    out: list[str] = []
    for p in ps:
        txt = re.sub(r"\s+", " ", p.get_text(strip=True))
        if len(txt) > 40:
            out.append(txt)
    return out


def extract_snippet(soup: BeautifulSoup, body_paragraphs: list[str]) -> str:
    meta = soup.select_one('meta[name="description"]')
    if meta and meta.get("content"):
        return meta["content"].strip()
    if body_paragraphs:
        return body_paragraphs[0][:400]
    return ""


def extract_tags(soup: BeautifulSoup) -> list[str]:
    tags: set[str] = set()
    mk = soup.select_one('meta[name="keywords"]')
    if mk and mk.get("content"):
        for t in mk["content"].split(","):
            t2 = t.strip()
            if t2:
                tags.add(t2)
    return sorted(tags)


# --------------------------------------------------------------------
# Hlavní logika pro jeden článek
# --------------------------------------------------------------------


def parse_article(url: str) -> dict | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
    except Exception as e:
        print(f"[WARN] Nelze stáhnout článek: {url} ({e})")
        return None

    soup = BeautifulSoup(r.text, "html.parser")

    title = extract_title(soup)
    if not title:
        print(f"[WARN] Bez názvu, přeskočeno: {url}")
        return None

    date_str = extract_date(soup)
    author = extract_author(soup)
    body_pars = extract_body_paragraphs(soup)
    snippet = extract_snippet(soup, body_pars)
    full_content = "\n\n".join(body_pars)
    tags = extract_tags(soup)

    # >>> TADY SE PŘIDÁVAJÍ REGEXOVÉ TAGY <<<
    regex_tags = regex_derived_tags(full_content)
    tags = sorted(set(tags) | set(regex_tags))

    return {
        "title": title,
        "url": url,
        "date": date_str,
        "author": author,
        "source": "lidovky.cz",
        "content_snippet": snippet,
        "full_content": full_content,
        "tags": tags,
    }


def save_article(article: dict, base_dir: Path) -> None:
    """Uloží článek jako JSON do ./data/lidovky/YYYY/MM/."""
    try:
        dt = datetime.fromisoformat(article["date"].replace("Z", "+00:00"))
    except Exception:
        dt = datetime.now()

    yyyy = dt.strftime("%Y")
    mm = dt.strftime("%m")
    yyyymmdd = dt.strftime("%Y%m%d")

    hash8 = md5_tail8(article["url"])
    filename = f"lidovky-{yyyymmdd}-{hash8}.json"

    outdir = base_dir / "data" / SOURCE_SECOND_LEVEL / yyyy / mm
    outdir.mkdir(parents=True, exist_ok=True)
    outpath = outdir / filename

    if outpath.exists():
        print(f"[SKIP] {outpath.name} už existuje.")
        return

    with outpath.open("w", encoding="utf-8") as f:
        json.dump(article, f, indent=4, ensure_ascii=False)
    print(f"[OK] Uloženo: {outpath}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scraper článků z lidovky.cz s JSON výstupem a regex tagy"
    )
    parser.add_argument(
        "base_dir",
        help="Kořenová složka, do které se uloží data/...",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=40,
        help="Maximální počet článků z úvodní strany (default 40)",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=1.0,
        help="Prodleva v sekundách mezi požadavky na články (default 1.0)",
    )
    args = parser.parse_args()

    base_dir = Path(args.base_dir).resolve()
    links = get_article_links_from_main()
    if args.limit:
        links = links[: args.limit]

    print(f"Nalezeno kandidátů: {len(links)}")

    for i, url in enumerate(links, start=1):
        print(f"[{i}/{len(links)}] {url}")
        article = parse_article(url)
        if article:
            save_article(article, base_dir)
        time.sleep(args.sleep)


if __name__ == "__main__":
    main()
