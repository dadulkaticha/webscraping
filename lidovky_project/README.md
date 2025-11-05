## Instalace

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS:
. .venv/bin/activate

pip install -r requirements.txt
```

## Spuštění

```bash
python lidovky_scraper.py /cesta/ke/korenove_slozce --limit 40 --sleep 1.0
```

- **base_dir** (povinné): kořenová složka, kam se vytvoří `data/lidovky/YYYY/MM/...`
- `--limit`: maximální počet odkazů z hlavní stránky (default 40)
- `--sleep`: prodleva mezi požadavky v sekundách (default 1.0)

### Příklady

```bash
# Windows (PowerShell)
python .\lidovky_scraper.py D:\projekty\wsc --limit 60 --sleep 1.2

# Linux
python ./lidovky_scraper.py ~/projekty/wsc --limit 50 --sleep 1.0
```

## Formát JSON

```json
{
  "title": "Název článku",
  "url": "https://www.lidovky.cz/...",
  "date": "2025-11-04T12:34:56+01:00",
  "author": "Redakce LN",
  "source": "lidovky.cz",
  "content_snippet": "Krátký úryvek...",
  "full_content": "Plný text článku...",
  "tags": ["ekonomika", "politika"]
}
```

- Název souboru: `lidovky-YYYYMMDD-<hash8>.json` (hash = posledních 8 znaků MD5 z URL)
- Kódování: UTF-8

## Spouštění 1× za hodinu

### Windows – Plánovač úloh
Spustitelný program `python.exe`, argumenty například:
```
C:\cesta\k\lidovky_scraper.py C:\data\wsc --limit 50 --sleep 1.2
```