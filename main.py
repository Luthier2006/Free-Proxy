# main.py
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
import re
import time
from datetime import datetime, timedelta, timezone
import json
import os

APP_PORT = int(os.environ.get("APP_PORT", 8000))
SCRAPE_TTL = 60  # segundos - cache TTL

URL = "https://spys.one/en/socks-proxy-list/"

app = FastAPI(title="Spys.one Proxy Scraper API")

# Permitir requisições do frontend local / rede
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# montar diretório static para servir index.html
app.mount("/static", StaticFiles(directory="static"), name="static")

# Cache simples
_cache = {"ts": 0, "data": []}

def _now_ts():
    return int(time.time())

def _format_time_gmt3(dt_utc: datetime):
    # dt_utc expected timezone-aware UTC or naive (treated as UTC)
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    gmt3 = dt_utc + timedelta(hours=3)
    return gmt3.strftime("%Y-%m-%d %H:%M:%S %Z%z")  # shows timezone offset

def scrape_spys():
    """
    Abre a página com Playwright (headless), espera a tabela e extrai informações.
    Retorna lista de dicts com campos: ip, port, ip_port, type (SOCKS4/5/HTTP), country, city,
    latency, speed, uptime, last_checked (UTC), raw (excerpt).
    """
    results = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page(user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                                               "Chrome/116.0 Safari/537.36"))
            page.goto(URL, timeout=30000)
            # esperar por algo que sinalize a presença da tabela; tentar seletores genéricos
            try:
                page.wait_for_selector("table", timeout=10000)
            except PWTimeoutError:
                # se não aparecer tabela, continuar e tentar extrair o que houver
                pass

            # coletar todas as linhas de todas as tabelas
            rows = page.query_selector_all("table tr")
            for tr in rows:
                text = tr.inner_text().strip()
                # buscar ip:port via regex
                m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3}:\d{1,5})", text)
                if not m:
                    continue

                ipport = m.group(1)
                parts = ipport.split(":")
                ip = parts[0]
                port = parts[1] if len(parts) > 1 else ""

                # heurísticas para extrair campos adicionais do texto da linha
                ttype = ""
                if re.search(r"\bSOCKS5\b", text, re.I):
                    ttype = "SOCKS5"
                elif re.search(r"\bSOCKS4\b", text, re.I):
                    ttype = "SOCKS4"
                elif re.search(r"\bHTTP\b", text, re.I):
                    ttype = "HTTP"

                # país/cidade (muito dependente do formato específico do site)
                # tentamos achar abreviações de país entre parêntesis ou colunas
                country = ""
                city = ""
                # busca por padrão "Country: NAME" ou "City"
                # fallback: procurar por abreviação de 2 letras
                cm = re.search(r"\b([A-Z]{2})\b", text)
                if cm:
                    country = cm.group(1)

                # latência
                latency = ""
                lm = re.search(r"(\d{1,4}\sms)", text, re.I)
                if lm:
                    latency = lm.group(1)

                # velocidade (speed)
                speed = ""
                sm = re.search(r"Speed[:\s]*([\d\.]+(?:[KM]B\/s|\s?KB\/s)?)", text, re.I)
                # generic fallback: number + KB/s or MB/s
                if not sm:
                    sm = re.search(r"([\d\.]+\s?(?:KB/s|MB/s|kB/s|Mb/s|Mbit/s))", text, re.I)
                if sm:
                    speed = sm.group(1)

                # uptime (%) ou uptime form
                uptime = ""
                um = re.search(r"upTime[:\s]*([\d\.%]+)", text, re.I)
                if not um:
                    um = re.search(r"(\d{1,3}\%)", text)
                if um:
                    uptime = um.group(1)

                # last checked (timestamp)
                last_checked = ""
                lc = re.search(r"Last checked[:\s]*([^\n\r]+)", text, re.I)
                if lc:
                    last_checked = lc.group(1).strip()

                results.append({
                    "ip": ip,
                    "port": port,
                    "ip_port": ipport,
                    "type": ttype,
                    "country": country,
                    "city": city,
                    "latency": latency,
                    "speed": speed,
                    "uptime": uptime,
                    "last_checked_raw": last_checked,
                    "raw": text[:300]
                })

            browser.close()
    except Exception as e:
        # retornar exceção como resultado vazio + mensagem
        return {"error": str(e), "data": []}

    # remover duplicados pelo ip_port mantendo a primeira ocorrência
    seen = set()
    uniq = []
    for r in results:
        if r["ip_port"] not in seen:
            seen.add(r["ip_port"])
            uniq.append(r)

    return {"error": None, "data": uniq}


@app.get("/api/proxies")
def api_proxies():
    """
    Retorna JSON com campos:
      - fetched_at_utc
      - fetched_at_gmt3
      - ttl_seconds
      - source_url
      - proxies: [ { ip, port, ip_port, type, country, city, latency, speed, uptime, last_checked_raw, raw } ]
      - error (if any)
    """
    now = _now_ts()
    if _cache["ts"] == 0 or (now - _cache["ts"]) > SCRAPE_TTL:
        # atualizar cache
        scraped = scrape_spys()
        if isinstance(scraped, dict) and scraped.get("error"):
            # erro ao scrapear: salvar mensagem de erro, mas não sobrescrever data se já existir
            error_msg = scraped.get("error")
            if _cache["data"]:
                # retornar cache existente junto com a nota de erro
                return JSONResponse({
                    "fetched_at_utc": datetime.utcfromtimestamp(_cache["ts"]).isoformat() if _cache["ts"] else None,
                    "fetched_at_gmt3": _format_time_gmt3(datetime.utcfromtimestamp(_cache["ts"])) if _cache["ts"] else None,
                    "ttl_seconds": SCRAPE_TTL,
                    "source_url": URL,
                    "proxies": _cache["data"],
                    "error": error_msg
                }, status_code=200)
            else:
                return JSONResponse({
                    "fetched_at_utc": datetime.utcnow().isoformat(),
                    "fetched_at_gmt3": _format_time_gmt3(datetime.utcnow()),
                    "ttl_seconds": SCRAPE_TTL,
                    "source_url": URL,
                    "proxies": [],
                    "error": error_msg
                }, status_code=500)

        # sucesso
        _cache["ts"] = now
        _cache["data"] = scraped.get("data", [])
    # preparar resposta
    fetched_utc = datetime.utcfromtimestamp(_cache["ts"])
    return {
        "fetched_at_utc": fetched_utc.isoformat(),
        "fetched_at_gmt3": _format_time_gmt3(fetched_utc),
        "ttl_seconds": SCRAPE_TTL,
        "source_url": URL,
        "proxies": _cache["data"],
        "error": None
    }

# endpoint root: servir frontend index.html
@app.get("/")
def index():
    index_path = os.path.join("static", "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path, media_type="text/html")
    return HTMLResponse("<h3>Front-end não encontrado. Coloque index.html em ./static/</h3>", status_code=404)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=APP_PORT, reload=True)
