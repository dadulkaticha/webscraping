# Skript pro extrakci článků z lidovky.cz a jejich uložení do JSON souborů.
import argparse
import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


# Základní URL webu, ze kterého se stahují odkazy na články
MAIN_URL = "https://www.lidovky.cz/"

# Identifikátor zdroje (používá se v názvech souborů a metadatech)
SOURCE_SECOND_LEVEL = "lidovky"

# Záhlaví HTTP požadavků (User-Agent), aby požadavky vypadaly jako od prohlížeče/bota
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; LidovkyProjectBot/1.0; +https://example.org/bot-info)"
}

# Seznam prefixů v cestě URL, které považujeme za relevantní články (zpravodajství, sport, kultura ...)
ALLOWED_PATH_PREFIXES = (
    "/domov/",
    "/zahranici/",
    "/byznys/",
    "/kultura/",
    "/sport/",
    "/magazin/",
)


def md5_tail8(s: str) -> str:
    """
    Vrátí posledních 8 znaků z MD5 součtu vstupního řetězce.
    Používá se pro generování krátkého unikátního identifikátoru souboru.
    """
    return hashlib.md5(s.encode("utf-8")).hexdigest()[-8:]


def normalize_url(base: str, href: str) -> str:
    """
    Normalizuje odkaz:
    - spojí relativní href s base (urljoin),
    - zajistí, že odkaz směřuje na lidovky.cz (filtr podle hostitele),
    - odstraní fragmenty (#).
    Vrací prázdný řetězec, pokud odkaz není validní pro tento scraper.
    """
    if not href:
        return ""
    absu = urljoin(base, href)
    parsed = urlparse(absu)
    if parsed.netloc not in ("www.lidovky.cz", "lidovky.cz"):
        return ""
    return absu.split("#")[0]


def is_article_link(url: str) -> bool:
    """
    Hrubá heuristika: považuje za článek jen URL, jejíž cesta začíná jedním z povolených prefixů.
    Slouží k rychlému odfiltrování odkazů např. na rubriky, obrázky, autority apod.
    """
    try:
        path = urlparse(url).path
    except Exception:
        return False
    return any(path.startswith(p) for p in ALLOWED_PATH_PREFIXES)


def get_article_links_from_main() -> list[str]:
    """
    Stáhne hlavní stránku (MAIN_URL) a projde všechny <a href=""> odkazy.
    Použije normalize_url a is_article_link — vrátí set unikátních URL článků seřazený jako list.
    """
    resp = requests.get(MAIN_URL, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    links = set()
    for a in soup.select("a[href]"):
        u = normalize_url(MAIN_URL, a.get("href"))
        if u and is_article_link(u):
            links.add(u)

    return sorted(links)


def text_or_none(el) -> str | None:
    """Pomocná funkce: vrátí text elementu nebo None."""
    return el.get_text(strip=True) if el else None


def first_nonempty(*vals) -> str | None:
    """
    Vrátí první nenulovou a ne-prázdnou hodnotu ze seznamu argumentů.
    Užívá se při hledání např. autora nebo jiných metadat.
    """
    for v in vals:
        if v:
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def extract_date(soup: BeautifulSoup) -> str:
    """
    Extrahuje datum publikace článku pomocí několika strategií (OpenGraph, <time datetime>, jiné meta).
    Pokud není nalezeno, vrátí aktuální datum ve formátu ISO.
    Vrácený formát: ISO 8601 (pokud je dostupný v meta tagu).
    """
    meta = soup.select_one('meta[property="article:published_time"]')
    if meta and meta.get("content"):
        return meta["content"]

    t = soup.select_one("time[datetime]")
    if t and t.get("datetime"):
        return t["datetime"]

    meta2 = soup.select_one('meta[name="pubdate"], meta[itemprop="datePublished"]')
    if meta2 and meta2.get("content"):
        return meta2["content"]

    return datetime.now().isoformat()


def extract_author(soup: BeautifulSoup) -> str:
    """
    Pokusí se najít autora článku v typických umístěních:
    - meta[name="author"], itemprop="author", elementy s třídou obsahující "author" nebo odkaz rel="author".
    Vrátí 'Neznámý', pokud autor není nalezen.
    """
    cand = [
        soup.select_one('meta[name="author"]'),
        soup.select_one('[itemprop="author"]'),
        soup.select_one('[class*="author"]'),
        soup.select_one('a[rel="author"]'),
    ]
    for c in cand:
        if c:
            if c.name == "meta" and c.get("content"):
                return c["content"].strip()
            txt = c.get_text(strip=True)
            if txt:
                return txt
    return "Neznámý"


def extract_title(soup: BeautifulSoup) -> str:
    """
    Extrahuje titulek článku:
    1) nejdřív <h1>,
    2) pak OpenGraph meta property "og:title",
    3) nakonec <title>.
    Vrátí prázdný řetězec, pokud nic nenajde.
    """
    h1 = text_or_none(soup.select_one("h1"))
    if h1:
        return h1
    meta = soup.select_one('meta[property="og:title"]')
    if meta and meta.get("content"):
        return meta["content"].strip()
    t = text_or_none(soup.select_one("title"))
    return t or ""


def clean_paragraph(ptext: str) -> str:
    """
    Odstraní více mezer, newlines apod. a ořízne okraje.
    Používá se pro očištění textu odstavců.
    """
    ptext = re.sub(r"\s+", " ", ptext or "").strip()
    return ptext


def extract_body_paragraphs(soup: BeautifulSoup) -> list[str]:
    """
    Najde hlavní kontejner článku (zkouší několik běžných selektorů) a z něj vybere
    všechny <p> odstavce. Filtruje krátké/nerelevantní odstavce (např. popisky fotek nebo reklamy).
    Vrací seznam unikátních odstavců v pořadí, v jakém se objevily.
    """
    candidates = [
        'div[itemprop="articleBody"]',
        "div.article-body",
        "div#article",
        "article",
        "main article",
    ]
    texts: list[str] = []
    container = None
    for sel in candidates:
        container = soup.select_one(sel)
        if container:
            break

    ps = (container or soup).select("p")
    for p in ps:
        txt = clean_paragraph(p.get_text())
        if len(txt) >= 40 and not txt.lower().startswith(("foto", "autor:", "reklama")):
            texts.append(txt)

    # Odstranění duplicitních odstavců při zachování pořadí
    uniq = []
    seen = set()
    for t in texts:
        if t not in seen:
            uniq.append(t)
            seen.add(t)
    return uniq


def extract_snippet(soup: BeautifulSoup, body_paragraphs: list[str]) -> str:
    """
    Sestaví krátký popisek (snippet) článku:
    1) meta description, pokud existuje,
    2) první odstavec těla (oříznutý na 400 znaků),
    3) fallback – první obecný <p>.
    """
    meta = soup.select_one('meta[name="description"]')
    if meta and meta.get("content"):
        return clean_paragraph(meta["content"])

    if body_paragraphs:
        return body_paragraphs[0][:400]

    p = soup.select_one("p")
    return clean_paragraph(p.get_text()) if p else ""


def extract_tags(soup: BeautifulSoup) -> list[str]:
    """
    Snaží se získat tagy/klíčová slova článku z:
    - meta keywords,
    - odkazů s rel~="tag".
    Vrací seřazený seznam unikátních tagů.
    """
    tags = set()
    mk = soup.select_one('meta[name="keywords"]')
    if mk and mk.get("content"):
        for t in mk["content"].split(","):
            t2 = t.strip()
            if t2:
                tags.add(t2)
    for a in soup.select('a[rel~="tag"]'):
        t = a.get_text(strip=True)
        if t:
            tags.add(t)
    return sorted(tags)


def parse_article(url: str) -> dict | None:
    """
    Stáhne HTML článku z dané URL a pomocí pomocných funkcí extrahuje:
    - title, date, author, body paragraphs, snippet, tags
    Sestaví slovník s klíči vhodnými pro JSON výstup.
    Vrátí None, pokud nelze článek stáhnout nebo nemá název.
    """
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
    # full_content je text všech odstavců spojených dvou-novými-řádky; vhodné pro čitelnost v JSONu
    full_content = "\n\n".join(body_pars)
    tags = extract_tags(soup)

    article = {
        "title": title,
        "url": url,
        "date": date_str,
        "author": author,
        "source": f"{SOURCE_SECOND_LEVEL}.cz",
        "content_snippet": snippet,
        "full_content": full_content,
        "tags": tags,
    }
    return article


def save_article(article: dict, base_dir: Path) -> None:
    """
    Uloží jednotlivý článek do souboru JSON do struktury:
    <base_dir>/data/lidovky/YYYY/MM/lidovky-YYYYMMDD-<hash8>.json
    - datum se čte z metadat článku; pokud chybí, použije se aktuální čas.
    - pokud soubor existuje, vypíše se SKIP a nic se nepřepisuje.
    """
    try:
        dt = datetime.fromisoformat(article["date"].replace("Z", "+00:00"))
    except Exception:
        dt = datetime.now()
    yyyy = dt.strftime("%Y")
    mm = dt.strftime("%m")
    yyyymmdd = dt.strftime("%Y%m%d")

    hash8 = md5_tail8(article["url"])
    filename = f"{SOURCE_SECOND_LEVEL}-{yyyymmdd}-{hash8}.json"

    outdir = base_dir / "data" / SOURCE_SECOND_LEVEL / yyyy / mm
    outdir.mkdir(parents=True, exist_ok=True)
    outpath = outdir / filename

    if outpath.exists():
        print(f"[SKIP] Už existuje: {outpath} (soubor: {outpath.name})")
        return

    with outpath.open("w", encoding="utf-8") as f:
        json.dump(article, f, indent=4, ensure_ascii=False)
    print(f"[OK] Uloženo: {outpath}")


def save_aggregate(articles: list[dict], base_dir: Path) -> Path | None:
    """
    Uloží všechny články najednou do jednoho agregovaného JSON souboru
    ve stejné adresářové struktuře (jméno obsahuje časovou značku).
    Vrací cestu k uloženému souboru nebo None, pokud byl seznam prázdný.
    """
    if not articles:
        return None
    now = datetime.now()
    yyyy = now.strftime("%Y")
    mm = now.strftime("%m")
    ymd = now.strftime("%Y%m%d")
    hms = now.strftime("%H%M%S")
    outdir = base_dir / "data" / SOURCE_SECOND_LEVEL / yyyy / mm
    outdir.mkdir(parents=True, exist_ok=True)
    filename = f"{SOURCE_SECOND_LEVEL}-aggregate-{ymd}-{hms}.json"
    outpath = outdir / filename
    with outpath.open("w", encoding="utf-8") as f:
        json.dump(articles, f, indent=4, ensure_ascii=False)
    return outpath


def main():
    """
    Hlavní funkce:
    - parsuje argumenty příkazové řádky (volitelná base_dir, limit, sleep, --single-file),
    - pokud uživatel nezadá base_dir, vytvoří ve složce skriptu adresář 'lidovky_data',
    - stáhne seznam odkazů z hlavní stránky a pro každý odkaz zavolá parse_article,
    - uloží články buď jednotlivě nebo do agregovaného souboru podle přepínače.
    """
    ap = argparse.ArgumentParser(
        description="Zpravodajské servery – extrakce článků pro lidovky.cz"
    )
    ap.add_argument(
        "base_dir",
        nargs="?",
        default=".",
        help="Kořenová složka pro ukládání dat (nepovinné). Pokud nebude zadána, vytvoří se ./lidovky_data ve stejné složce jako skript.",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=40,
        help="Maximální počet článků z úvodní strany ke zpracování (default 40)",
    )
    ap.add_argument(
        "--sleep",
        type=float,
        default=1.0,
        help="Prodleva (v sekundách) mezi požadavky na články (default 1.0)",
    )
    ap.add_argument(
        "--single-file",
        action="store_true",
        help="Uložit všechny stažené články do jednoho JSON souboru místo jednotlivých souborů.",
    )
    args = ap.parse_args()

    # Pokud nebyla předána cesta, vytvoří se ve stejném adresáři jako skript 'lidovky_data'
    script_dir = Path(__file__).resolve().parent
    if args.base_dir == ".":
        base_dir = script_dir / "lidovky_data"
    else:
        base_dir = Path(args.base_dir).resolve()

    base_dir.mkdir(parents=True, exist_ok=True)
    print(f"[INFO] base_dir = {base_dir} (workspace: {script_dir})")

    links = get_article_links_from_main()
    if args.limit:
        links = links[: args.limit]

    print(f"Nalezeno kandidátů: {len(links)}")

    all_articles: list[dict] = []
    for i, url in enumerate(links, 1):
        print(f"[{i}/{len(links)}] {url}")
        art = parse_article(url)
        if art and art.get("full_content"):
            if args.single_file:
                all_articles.append(art)
                print(f"[OK] Přidáno do agregace: {url}")
            else:
                save_article(art, base_dir)
        else:
            print("[WARN] Prázdný obsah, přeskočeno.")
        time.sleep(args.sleep)

    if args.single_file:
        out = save_aggregate(all_articles, base_dir)
        if out:
            print(f"[OK] Agregovaný JSON uložen: {out}")
        else:
            print("[WARN] Žádné články k uložení do agregovaného souboru.")


if __name__ == "__main__":
    main()
