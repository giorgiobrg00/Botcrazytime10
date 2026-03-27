import os
import re
import json
import time
import random
import threading
import warnings
import logging
from datetime import datetime
from typing import Optional, Tuple

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify

warnings.filterwarnings("ignore", message="Unverified HTTPS request")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ─── CONFIGURAZIONE ────────────────────────────────────────────────────────────

TELEGRAM_TOKEN = "8726168982:AAGcxhmjVPAHEYzWbGmTYa1H_fMGY_qvS2s"
TELEGRAM_CHAT  = "@numerounoedue"

PROXY_USER = "jolbanxz"
PROXY_PASS = "rmdwbaz56j5m"
PROXY_HOSTS = [
    "31.59.20.176:6754",
    "23.95.150.145:6114",
    "198.23.239.134:6540",
    "45.38.107.97:6014",
    "107.172.163.27:6543",
    "198.105.121.200:6462",
    "216.10.27.159:6837",
    "142.111.67.146:5611",
    "191.96.254.138:6185",
    "31.58.9.4:6077",
]

SCAN_INTERVAL  = 15
JITTER_MAX     = 5
SESSION_SPINS  = 14
MAX_ERRORS     = 3
FETCH_RETRIES  = 3

STATE_FILE = "session_state.json"

USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 15_8 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.8 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.6261.119 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]

# ─── STATO GLOBALE ─────────────────────────────────────────────────────────────

state = {
    "running":               True,
    "last_update":           None,
    "spin_history":          [],
    "prev_spins_since_10":   None,
    "prev_spins_since_1":    None,
    "mode":                  "observing",
    "session_spin":          0,
    "session_start_time":    None,
    "consecutive_errors":    0,
    "sos_sent":              False,
    "total_cycles":          0,
    "last_source":           None,
    "last_spins_since":      None,
    "last_result":           None,
    # debug
    "last_html_snippet":     "",
    "last_html_len":         0,
}

# ─── PERSISTENZA STATO ─────────────────────────────────────────────────────────

def save_state():
    data = {
        "mode":                 state["mode"],
        "session_spin":         state["session_spin"],
        "prev_spins_since_10":  state["prev_spins_since_10"],
        "prev_spins_since_1":   state["prev_spins_since_1"],
        "session_start_time":   state["session_start_time"].isoformat() if state["session_start_time"] else None,
    }
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.warning("save_state errore: %s", e)


def load_state():
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        for k, v in data.items():
            if k in state:
                state[k] = v
        if state["session_start_time"] and isinstance(state["session_start_time"], str):
            state["session_start_time"] = datetime.fromisoformat(state["session_start_time"])
        logger.info("Stato caricato: mode=%s | session_spin=%d",
                    state["mode"], state["session_spin"])
    except Exception as e:
        logger.warning("load_state errore: %s", e)

# ─── UTILITY ──────────────────────────────────────────────────────────────────

def get_headers():
    return {
        "User-Agent":      random.choice(USER_AGENTS),
        "Referer":         "https://www.google.com/",
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control":   "no-cache",
        "DNT":             "1",
    }

def get_proxy():
    host = random.choice(PROXY_HOSTS)
    url  = "http://{}:{}@{}".format(PROXY_USER, PROXY_PASS, host)
    logger.debug("[proxy] usando %s", host)
    return {"http": url, "https": url}

# ─── TELEGRAM ──────────────────────────────────────────────────────────────────

def send_telegram(text):
    url = "https://api.telegram.org/bot{}/sendMessage".format(TELEGRAM_TOKEN)
    payload = {
        "chat_id":    TELEGRAM_CHAT,
        "text":       text,
        "parse_mode": "HTML",
    }
    for attempt in range(1, 4):
        try:
            r = requests.post(url, json=payload, timeout=15)
            if r.status_code == 200:
                logger.info("Telegram OK: %s", text[:80])
                return True
            else:
                resp = r.json() if r.content else {}
                logger.warning(
                    "Telegram errore %s (tentativo %d): %s",
                    r.status_code, attempt, resp.get("description", r.text[:200])
                )
        except Exception as e:
            logger.error("Telegram eccezione tentativo %d: %s", attempt, e)
        time.sleep(2)
    logger.error("Telegram: tutti i tentativi falliti per: %s", text[:80])
    return False

# ─── FETCH HTML ────────────────────────────────────────────────────────────────

def fetch_html(url):
    # type: (str) -> Optional[str]
    used_proxies = set()

    for attempt in range(1, FETCH_RETRIES + 1):
        available = [h for h in PROXY_HOSTS if h not in used_proxies]
        if not available:
            available = list(PROXY_HOSTS)
        host = random.choice(available)
        used_proxies.add(host)
        proxy_url = "http://{}:{}@{}".format(PROXY_USER, PROXY_PASS, host)
        proxies   = {"http": proxy_url, "https": proxy_url}

        try:
            r = requests.get(
                url,
                headers=get_headers(),
                proxies=proxies,
                verify=False,
                timeout=25,
                allow_redirects=True,
            )
            if r.status_code == 200:
                logger.info("[fetch] OK via %s (tentativo %d) | len=%d",
                            host, attempt, len(r.text))
                # salva snippet per debug
                state["last_html_snippet"] = r.text[:2000]
                state["last_html_len"]     = len(r.text)
                return r.text
            elif r.status_code == 429:
                logger.warning("[fetch] Rate limit 429 proxy %s", host)
                time.sleep(random.uniform(15, 30))
            else:
                logger.warning("[fetch] HTTP %s proxy %s tentativo %d",
                               r.status_code, host, attempt)
                time.sleep(2)
        except Exception as e:
            logger.warning("[fetch] Errore proxy %s tentativo %d: %s", host, attempt, e)
            time.sleep(2)

    # fallback senza proxy
    try:
        logger.info("[fetch] Tentativo diretto (no proxy): %s", url)
        r = requests.get(url, headers=get_headers(), verify=False, timeout=25)
        if r.status_code == 200:
            logger.info("[fetch] OK diretto | len=%d", len(r.text))
            state["last_html_snippet"] = r.text[:2000]
            state["last_html_len"]     = len(r.text)
            return r.text
    except Exception as e:
        logger.warning("[fetch] Errore diretto: %s", e)

    return None

# ─── ESTRATTORI ────────────────────────────────────────────────────────────────

def extract_tracksino(html):
    # type: (str) -> Tuple[Optional[int], Optional[int], Optional[str]]
    """
    Estrae spins_since_10 e spins_since_1 dalla pagina Tracksino.
    Usa lo stesso approccio che funzionava per il numero 2,
    adattato per cercare il segmento 10.
    """
    soup = BeautifulSoup(html, "lxml")
    spins_since_10 = None
    spins_since_1  = None
    last_result    = None

    # ── Metodo 1: classe game-stats-seg (approccio originale funzionante) ─────
    segs = soup.find_all(class_=re.compile(r"game-stats-seg"))
    logger.debug("[tracksino] trovati %d elementi game-stats-seg", len(segs))

    for seg in segs:
        img = seg.find("img", alt=re.compile(r"Crazy Time", re.IGNORECASE))
        if not img:
            continue
        alt  = img.get("alt", "")
        text = seg.get_text(" ", strip=True)

        m = re.search(r'\)\s*(\d+)\s+spins?\s+since', text)
        if not m:
            m = re.search(r'[\d.]+%\s*\([^)]+\)\s*(\d+)', text)
        if not m:
            continue
        val = int(m.group(1))

        # segmento 10 — possibili varianti del nome alt
        if re.search(r'Crazy\s*Time\s*10(?:\s*Segment)?', alt, re.IGNORECASE) or \
           re.search(r'\b10\b', alt):
            if spins_since_10 is None:
                spins_since_10 = val
                logger.info("[tracksino] metodo1: spins_10=%d alt=%s", val, alt)

        # segmento 1 (contatore secondario per differenziale)
        if re.search(r'Crazy\s*Time\s*1(?:\s*Segment)?$', alt, re.IGNORECASE):
            if spins_since_1 is None:
                spins_since_1 = val

        if val == 0:
            name = re.sub(r'(?i)crazy\s*time\s*', '', alt)
            name = re.sub(r'(?i)\s*segment\s*', '', name).strip()
            last_result = name if name else alt

    if spins_since_10 is not None:
        return spins_since_10, spins_since_1, last_result

    # ── Metodo 2: cerca qualsiasi elemento che contenga "10" e "spins since" ──
    for tag in soup.find_all(True):
        txt = tag.get_text(" ", strip=True)
        if len(txt) > 300 or len(txt) < 3:
            continue
        if not re.search(r'(?<!\d)10(?!\d)', txt):
            continue
        m = re.search(r'(\d{1,4})\s+spins?\s+since', txt, re.IGNORECASE)
        if m:
            spins_since_10 = int(m.group(1))
            logger.info("[tracksino] metodo2 HTML: spins_10=%d txt=%s", spins_since_10, txt[:80])
            return spins_since_10, spins_since_1, last_result

    # ── Metodo 3: cerca nei tag <script> JSON con chiave "10" ─────────────────
    for script in soup.find_all("script"):
        text = script.string or ""
        if not text:
            continue
        patterns = [
            r'"10"\s*[,:].*?"(?:spins_since|spinsSince|last_seen|gap|count)"\s*:\s*(\d+)',
            r'"(?:spins_since|spinsSince|gap)"\s*:\s*(\d+)[^}]{0,60}"(?:label|segment|name|value)"\s*:\s*"10"',
            r'spins_since_10["\s:]*(\d+)',
            r'"segment"\s*:\s*"10"[^}]{0,80}"(?:count|spins_since|gap)"\s*:\s*(\d+)',
        ]
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                spins_since_10 = int(m.group(1))
                logger.info("[tracksino] metodo3 script: spins_10=%d", spins_since_10)
                return spins_since_10, None, None

    # ── Metodo 4: __NEXT_DATA__ ────────────────────────────────────────────────
    m = re.search(r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
                  html, re.DOTALL | re.IGNORECASE)
    if m:
        try:
            data = json.loads(m.group(1))
            raw  = json.dumps(data)
            for pat in [
                r'"10"\s*:\s*\{[^}]*"(?:spins_since|spinsSince|count|gap)"\s*:\s*(\d+)',
                r'"(?:spins_since|spinsSince|gap)"\s*:\s*(\d+)[^}]{0,80}"10"',
            ]:
                found = re.search(pat, raw)
                if found:
                    spins_since_10 = int(found.group(1))
                    logger.info("[tracksino] metodo4 NEXT_DATA: spins_10=%d", spins_since_10)
                    return spins_since_10, None, None
        except Exception as e:
            logger.debug("[tracksino] NEXT_DATA parse error: %s", e)

    # log snippet HTML per debug quando tutti i metodi falliscono
    logger.warning("[tracksino] tutti i metodi falliti | html_len=%d | snippet: %s",
                   len(html), html[:500].replace('\n', ' '))
    return None, None, None


def extract_casinoscores(html):
    # type: (str) -> Tuple[Optional[int], Optional[int]]
    soup = BeautifulSoup(html, "lxml")

    # ── Metodo 1: tabelle HTML ─────────────────────────────────────────────────
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            for i, cell in enumerate(cells):
                txt = cell.get_text(strip=True)
                if re.fullmatch(r'10', txt, re.IGNORECASE):
                    for j in range(i + 1, min(i + 4, len(cells))):
                        m = re.search(r'(\d+)', cells[j].get_text(strip=True))
                        if m:
                            v = int(m.group(1))
                            logger.info("[casinoscores] tabella: spins_10=%d", v)
                            return v, None

    # ── Metodo 2: script JSON ──────────────────────────────────────────────────
    for script in soup.find_all("script"):
        text = script.string or ""
        if not text:
            continue
        patterns = [
            r'"10"\s*:\s*\{[^}]*"(?:count|spins_since|spinsSince|frequency|gap)"\s*:\s*(\d+)',
            r'"label"\s*:\s*"10"[^}]*"(?:count|spins_since|frequency)"\s*:\s*(\d+)',
            r'"segment"\s*:\s*"10"[^}]*"(?:count|spins_since)"\s*:\s*(\d+)',
            r'spins_since[_\s]*10["\s:]*(\d+)',
        ]
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                v = int(m.group(1))
                logger.info("[casinoscores] script: spins_10=%d", v)
                return v, None

    # ── Metodo 3: elementi HTML con etichetta "10" ────────────────────────────
    for tag in soup.find_all(["span", "div", "p", "li", "td"]):
        txt = tag.get_text(strip=True)
        if not re.fullmatch(r'10', txt):
            continue
        parent = tag.parent
        if not parent:
            continue
        siblings = list(parent.children)
        try:
            idx = siblings.index(tag)
        except ValueError:
            continue
        for sib in siblings[idx + 1:idx + 4]:
            sib_txt = getattr(sib, "get_text", lambda strip=False: str(sib))(strip=True)
            m = re.search(r'(\d+)', sib_txt)
            if m:
                v = int(m.group(1))
                logger.info("[casinoscores] elemento: spins_10=%d", v)
                return v, None

    logger.warning("[casinoscores] tutti i metodi falliti | html_len=%d", len(html))
    return None, None

# ─── ORCHESTRAZIONE SORGENTI ───────────────────────────────────────────────────

def scrape_all_sources():
    # type: () -> Tuple[Optional[int], Optional[int], Optional[str]]

    html = fetch_html("https://www.tracksino.com/crazytime")
    if html:
        try:
            v10, v1, last_result = extract_tracksino(html)
        except Exception as e:
            logger.warning("[tracksino] Errore estrazione: %s", e)
            v10, v1, last_result = None, None, None
        if v10 is not None:
            state["last_source"] = "tracksino"
            return v10, v1, last_result
    else:
        logger.warning("[tracksino] Impossibile scaricare la pagina")

    html = fetch_html("https://casinoscores.com/crazy-time/")
    if html:
        try:
            v10, v1 = extract_casinoscores(html)
        except Exception as e:
            logger.warning("[casinoscores] Errore estrazione: %s", e)
            v10, v1 = None, None
        if v10 is not None:
            state["last_source"] = "casinoscores"
            return v10, v1, None
    else:
        logger.warning("[casinoscores] Impossibile scaricare la pagina")

    return None, None, None

# ─── RILEVAZIONE DIFFERENZIALE ─────────────────────────────────────────────────

def _detect_appeared_10(spins_since_10, prev_10, spins_since_1, prev_1, in_session):
    if spins_since_1 is not None and prev_1 is not None:
        changed_10 = (spins_since_10 != prev_10)
        changed_1  = (spins_since_1  != prev_1)
        if not changed_10 and not changed_1:
            return None
        appeared_10 = (spins_since_10 == 0)
        logger.info("Differenziale: 10: %s->%s | 1: %s->%s | appeared=%s",
                    prev_10, spins_since_10, prev_1, spins_since_1, appeared_10)
        return appeared_10
    else:
        if spins_since_10 == prev_10:
            if spins_since_10 == 0 and in_session:
                logger.info("Fallback: 10 consecutivo rilevato")
                return True
            return None
        appeared_10 = (spins_since_10 < prev_10)
        logger.info("Fallback: 10: %s->%s | appeared=%s",
                    prev_10, spins_since_10, appeared_10)
        return appeared_10

# ─── GESTIONE SESSIONE ─────────────────────────────────────────────────────────

def _enter_session():
    state["mode"]               = "session"
    state["session_spin"]       = 0
    state["session_start_time"] = datetime.now()
    save_state()
    logger.info("Sessione avviata: il 10 e' uscito")
    send_telegram(
        "\u26a0\ufe0fIL 10 E' USCITO!\n"
        "Inizia a puntare per i prossimi {} colpi\n"
        "<b>{}</b>".format(SESSION_SPINS, datetime.now().strftime("%H:%M:%S"))
    )


def _return_to_observing():
    state["mode"]               = "observing"
    state["session_spin"]       = 0
    state["session_start_time"] = None
    save_state()
    logger.info("Tornato in osservazione")


def _handle_session_spin(appeared_10):
    state["session_spin"] += 1
    colpo = state["session_spin"]

    if appeared_10:
        logger.info("VINCITA al %d colpo", colpo)
        send_telegram(
            "CA\U0001f4b2\U0001f4b2A\n"
            "Preso al {}° colpo\n"
            "<b>{}</b>".format(colpo, datetime.now().strftime("%H:%M:%S"))
        )
        _return_to_observing()
    elif colpo >= SESSION_SPINS:
        logger.info("Sessione terminata: %d colpi senza il 10", SESSION_SPINS)
        send_telegram(
            "LOSE \u274c\ufe0f\n"
            "<b>{}</b>".format(datetime.now().strftime("%H:%M:%S"))
        )
        _return_to_observing()
    else:
        save_state()
        logger.info("Colpo %d/%d – 10 non uscito", colpo, SESSION_SPINS)

# ─── PROCESSO SPIN ─────────────────────────────────────────────────────────────

def process_spin(spins_since_10, spins_since_1, last_result):
    prev_10 = state["prev_spins_since_10"]
    prev_1  = state["prev_spins_since_1"]

    state["prev_spins_since_10"] = spins_since_10
    state["prev_spins_since_1"]  = spins_since_1

    if last_result:
        state["last_result"] = last_result

    if prev_10 is None:
        logger.info("Prima lettura: spins_since_10=%s spins_since_1=%s",
                    spins_since_10, spins_since_1)
        save_state()
        return

    in_session  = (state["mode"] == "session")
    appeared_10 = _detect_appeared_10(
        spins_since_10, prev_10, spins_since_1, prev_1, in_session
    )

    if appeared_10 is None:
        return

    if state["mode"] == "observing":
        if appeared_10:
            _enter_session()
    else:
        _handle_session_spin(appeared_10)

# ─── LOOP PRINCIPALE ───────────────────────────────────────────────────────────

def bot_loop():
    load_state()
    logger.info("Bot Crazy Time (10) avviato!")

    send_telegram(
        "<b>Bot Crazy Time Tracker 10 AVVIATO</b>\n"
        "Scansione ogni {}s | Monitoraggio: {} colpi\n"
        "<b>{}</b>".format(
            SCAN_INTERVAL,
            SESSION_SPINS,
            datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        )
    )

    while state["running"]:
        state["total_cycles"] += 1
        state["last_update"]  = datetime.now().isoformat()

        try:
            value10, value1, last_result = scrape_all_sources()
            if value10 is not None:
                state["consecutive_errors"] = 0
                state["sos_sent"]           = False
                state["last_spins_since"]   = value10
                state["spin_history"].append({
                    "ts":             state["last_update"],
                    "spins_since_10": value10,
                    "spins_since_1":  value1,
                    "source":         state["last_source"],
                    "last_result":    last_result,
                })
                state["spin_history"] = state["spin_history"][-200:]
                process_spin(value10, value1, last_result)
            else:
                state["consecutive_errors"] += 1
                logger.error("Nessun dato valido. Errori consecutivi: %d/%d",
                             state["consecutive_errors"], MAX_ERRORS)
                if state["consecutive_errors"] >= MAX_ERRORS and not state["sos_sent"]:
                    send_telegram(
                        "<b>ERRORE TRACCIAMENTO</b>\n"
                        "Nessun dato valido da {} tentativi consecutivi.\n"
                        "<b>{}</b>".format(
                            MAX_ERRORS,
                            datetime.now().strftime("%H:%M:%S")
                        )
                    )
                    state["sos_sent"] = True

        except Exception as e:
            state["consecutive_errors"] += 1
            logger.exception("Errore imprevisto nel loop: %s", e)

        sleep_time = SCAN_INTERVAL + random.uniform(0, JITTER_MAX)
        time.sleep(sleep_time)

# ─── FLASK WEB SERVER ──────────────────────────────────────────────────────────

app = Flask(__name__)

@app.route("/")
def index():
    elapsed = None
    if state["session_start_time"]:
        elapsed = int((datetime.now() - state["session_start_time"]).total_seconds())
    return jsonify({
        "status":              "running",
        "bot":                 "Crazy Time Tracker 10",
        "total_cycles":        state["total_cycles"],
        "last_update":         state["last_update"],
        "mode":                state["mode"],
        "session_spin":        state["session_spin"],
        "session_max":         SESSION_SPINS,
        "session_elapsed_s":   elapsed,
        "consecutive_errors":  state["consecutive_errors"],
        "last_source":         state["last_source"],
        "last_spins_since_10": state["last_spins_since"],
        "last_spins_since_1":  state["prev_spins_since_1"],
        "last_result":         state["last_result"],
        "spin_history_len":    len(state["spin_history"]),
    })

@app.route("/history")
def history():
    return jsonify({"spin_history": state["spin_history"][-20:]})

@app.route("/debug")
def debug():
    """
    Mostra un frammento dell'ultimo HTML scaricato.
    Utile per diagnosticare se i siti cambiano struttura.
    Apri /debug nel browser dopo il deploy.
    """
    return jsonify({
        "last_html_len":     state["last_html_len"],
        "last_html_snippet": state["last_html_snippet"],
        "last_source":       state["last_source"],
        "last_spins_10":     state["last_spins_since"],
        "consecutive_errors": state["consecutive_errors"],
    })

@app.route("/ping")
@app.route("/api/ping")
def ping():
    return jsonify({"pong": True, "ts": datetime.now().isoformat()})

@app.route("/health")
@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})

# ─── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    bot_thread = threading.Thread(target=bot_loop, daemon=True)
    bot_thread.start()
    logger.info("Flask web server in ascolto su porta %d", port)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
