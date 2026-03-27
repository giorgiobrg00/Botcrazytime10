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

SCAN_INTERVAL  = 20
JITTER_MAX     = 8
SESSION_SPINS  = 14
MAX_ERRORS     = 3
FETCH_RETRIES  = 2

STATE_FILE = "session_state.json"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
]

SOURCES = [
    {
        "name": "casino_guru",
        "urls": [
            "https://casino.guru/live-game-stats/crazy-time-live",
            "https://casino.guru/live-game-stats/crazy-time",
            "https://casino.guru/live-casino-crazy-time-statistics",
        ],
    },
    {
        "name": "livecasinocomparer",
        "urls": [
            "https://www.livecasinocomparer.com/live-casino-statistics/evolution-crazy-time-statistics/",
            "https://www.livecasinocomparer.com/crazy-time-statistics/",
        ],
    },
    {
        "name": "tracksino",
        "urls": [
            "https://tracksino.com/crazytime",
        ],
    },
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
    "debug_last_url":        "",
    "debug_html_len":        0,
    "debug_html_snippet":    "",
    "debug_extracted":       "",
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
        "User-Agent":                random.choice(USER_AGENTS),
        "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language":           "en-US,en;q=0.9",
        "Accept-Encoding":           "gzip, deflate",
        "Referer":                   "https://www.google.com/",
        "Cache-Control":             "no-cache",
        "DNT":                       "1",
        "Upgrade-Insecure-Requests": "1",
    }

# ─── TELEGRAM ──────────────────────────────────────────────────────────────────

def send_telegram(text):
    url = "https://api.telegram.org/bot{}/sendMessage".format(TELEGRAM_TOKEN)
    payload = {"chat_id": TELEGRAM_CHAT, "text": text, "parse_mode": "HTML"}
    for attempt in range(1, 4):
        try:
            r = requests.post(url, json=payload, timeout=15)
            if r.status_code == 200:
                logger.info("Telegram OK: %s", text[:80])
                return True
            resp = r.json() if r.content else {}
            logger.warning("Telegram errore %s (tentativo %d): %s",
                           r.status_code, attempt,
                           resp.get("description", r.text[:200]))
        except Exception as e:
            logger.error("Telegram eccezione tentativo %d: %s", attempt, e)
        time.sleep(2)
    return False

# ─── FETCH HTML ────────────────────────────────────────────────────────────────

def fetch_html(url):
    # type: (str) -> Optional[str]
    session = requests.Session()
    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            time.sleep(random.uniform(1.0, 2.5))
            r = session.get(
                url,
                headers=get_headers(),
                verify=False,
                timeout=25,
                allow_redirects=True,
            )
            logger.info("[fetch] %s → HTTP %d | bytes=%d",
                        url, r.status_code, len(r.content))

            if r.status_code == 200:
                # Decodifica esplicita UTF-8 — evita problemi con Brotli/encoding errato
                text = r.content.decode("utf-8", errors="replace")
                state["debug_last_url"]     = url
                state["debug_html_len"]     = len(text)
                state["debug_html_snippet"] = text[:3000]
                return text

            if r.status_code in (301, 302, 404, 403):
                logger.warning("[fetch] HTTP %d su %s, salto", r.status_code, url)
                return None

            time.sleep(3)

        except Exception as e:
            logger.warning("[fetch] Errore tentativo %d su %s: %s", attempt, url, e)
            time.sleep(3)

    return None

# ─── ESTRAZIONE UNIVERSALE ─────────────────────────────────────────────────────

def extract_spins_since(html, target):
    # type: (str, str) -> Optional[int]
    """
    Estrae da qualsiasi pagina HTML di statistiche quanti giri fa
    è uscito 'target'. Prova 6 strategie in cascata.
    """
    soup = BeautifulSoup(html, "lxml")
    t    = str(target).strip()
    t_re = re.escape(t)

    # ── Strategia 1: tabelle HTML ─────────────────────────────────────────────
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower()
                   for th in table.find_all("th")]
        col_idx = None
        for i, h in enumerate(headers):
            if any(kw in h for kw in ("last", "spins", "since", "ago", "fa", "giri")):
                col_idx = i
                break
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            first = cells[0].get_text(strip=True)
            if re.fullmatch(t_re + r"[x\s]*", first, re.IGNORECASE):
                search_range = ([col_idx] if col_idx is not None else []) + \
                               [i for i in range(1, len(cells))
                                if i != col_idx]
                for ci in search_range:
                    if ci >= len(cells):
                        continue
                    m = re.search(r"(\d{1,4})", cells[ci].get_text(strip=True))
                    if m:
                        v = int(m.group(1))
                        logger.info("[extract] tabella: %s → %d", t, v)
                        return v

    # ── Strategia 2: elemento etichetta + fratello ────────────────────────────
    label_re = re.compile(r"^" + t_re + r"[x\s]*$", re.IGNORECASE)
    for tag in soup.find_all(["span", "div", "p", "li", "td", "dt"]):
        if not label_re.match(tag.get_text(strip=True)):
            continue
        parent = tag.parent
        if not parent:
            continue
        siblings = [c for c in parent.children if hasattr(c, "get_text") or isinstance(c, str)]
        try:
            idx = siblings.index(tag)
        except ValueError:
            continue
        for sib in siblings[idx + 1: idx + 5]:
            sib_txt = getattr(sib, "get_text", lambda strip=False: str(sib))(strip=True)
            m = re.search(r"(\d{1,4})", sib_txt)
            if m:
                v = int(m.group(1))
                logger.info("[extract] etichetta-fratello: %s → %d", t, v)
                return v

    # ── Strategia 3: JSON negli script ────────────────────────────────────────
    key_patterns = [
        r'"' + t_re + r'"[^}]{0,150}"(?:last_seen|spins_since|spinsSince|gap|last)"\s*:\s*(\d+)',
        r'"(?:last_seen|spins_since|spinsSince|gap)"\s*:\s*(\d+)[^}]{0,150}"(?:label|name|segment|result|number|value)"\s*:\s*"' + t_re + r'"',
        r'spins_since_' + t_re + r'["\s:]+(\d+)',
        r'"segment"\s*:\s*"' + t_re + r'"[^}]{0,80}"(?:count|spins_since|gap)"\s*:\s*(\d+)',
    ]
    for script in soup.find_all("script"):
        text = script.string or ""
        if not text:
            continue
        for pat in key_patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                v = int(m.group(1))
                logger.info("[extract] JSON script: %s → %d", t, v)
                return v

    # ── Strategia 4: __NEXT_DATA__ ────────────────────────────────────────────
    nd = re.search(
        r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE
    )
    if nd:
        raw = nd.group(1)
        for pat in key_patterns:
            m = re.search(pat, raw, re.IGNORECASE)
            if m:
                v = int(m.group(1))
                logger.info("[extract] NEXT_DATA: %s → %d", t, v)
                return v

    # ── Strategia 5: testo pagina con "N spins since/ago" ────────────────────
    page_text = soup.get_text(" ")
    txt_patterns = [
        r"(?<!\d)" + t_re + r"(?!\d)[^\n]{0,80}?(\d{1,3})\s+spins?\s+(?:since|ago)",
        r"(\d{1,3})\s+spins?\s+(?:since|ago)[^\n]{0,80}?(?<!\d)" + t_re + r"(?!\d)",
        r"(?<!\d)" + t_re + r"(?!\d)[^\n]{0,50}?last\s+seen[^\d]{0,20}(\d{1,3})",
        r"(?<!\d)" + t_re + r"(?!\d)\s*[:\|]\s*(\d{1,3})\s*(?:giri|spins?)",
    ]
    for pat in txt_patterns:
        m = re.search(pat, page_text, re.IGNORECASE)
        if m:
            v = int(m.group(1))
            logger.info("[extract] testo: %s → %d", t, v)
            return v

    # ── Strategia 6: proximity nel testo grezzo ───────────────────────────────
    for m in re.finditer(r"(?<!\d)" + t_re + r"(?!\d)", page_text):
        window = page_text[m.start(): m.start() + 150]
        nums = re.findall(r"(?<!\d)(\d{1,3})(?!\d)", window[len(t):])
        if nums:
            v = int(nums[0])
            if 0 <= v <= 500:
                logger.info("[extract] proximity: %s → %d", t, v)
                return v

    return None

# ─── ORCHESTRAZIONE SORGENTI ───────────────────────────────────────────────────

def scrape_all_sources():
    # type: () -> Tuple[Optional[int], Optional[int], Optional[str]]
    for source in SOURCES:
        name = source["name"]
        for url in source["urls"]:
            html = fetch_html(url)
            if not html:
                continue

            v10 = extract_spins_since(html, "10")
            if v10 is None:
                soup = BeautifulSoup(html, "lxml")
                snippet = soup.get_text(" ")[:400].replace("\n", " ")
                logger.warning("[%s] '10' non trovato | testo: %s", name, snippet)
                continue

            v1 = extract_spins_since(html, "1")
            state["last_source"]    = name
            state["debug_extracted"] = "10={} 1={}".format(v10, v1)
            logger.info("[%s] OK → spins_10=%d spins_1=%s", name, v10, v1)
            return v10, v1, None

    logger.error("Tutte le sorgenti fallite")
    return None, None, None

# ─── RILEVAZIONE DIFFERENZIALE ─────────────────────────────────────────────────

def _detect_appeared_10(spins_since_10, prev_10, spins_since_1, prev_1, in_session):
    if spins_since_1 is not None and prev_1 is not None:
        changed_10 = (spins_since_10 != prev_10)
        changed_1  = (spins_since_1  != prev_1)
        if not changed_10 and not changed_1:
            return None
        appeared_10 = (spins_since_10 == 0)
        logger.info("Differenziale: 10:%s->%s | 1:%s->%s | appeared=%s",
                    prev_10, spins_since_10, prev_1, spins_since_1, appeared_10)
        return appeared_10
    else:
        if spins_since_10 == prev_10:
            if spins_since_10 == 0 and in_session:
                logger.info("Fallback: 10 consecutivo rilevato")
                return True
            return None
        appeared_10 = (spins_since_10 < prev_10)
        logger.info("Fallback: 10:%s->%s | appeared=%s",
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
        logger.info("Prima lettura: 10=%s 1=%s", spins_since_10, spins_since_1)
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
        "Monitoraggio: {} colpi dopo uscita del 10\n"
        "<b>{}</b>".format(SESSION_SPINS, datetime.now().strftime("%d/%m/%Y %H:%M:%S"))
    )

    while state["running"]:
        state["total_cycles"] += 1
        state["last_update"]  = datetime.now().isoformat()

        try:
            v10, v1, last_result = scrape_all_sources()
            if v10 is not None:
                state["consecutive_errors"] = 0
                state["sos_sent"]           = False
                state["last_spins_since"]   = v10
                state["spin_history"].append({
                    "ts":             state["last_update"],
                    "spins_since_10": v10,
                    "spins_since_1":  v1,
                    "source":         state["last_source"],
                })
                state["spin_history"] = state["spin_history"][-200:]
                process_spin(v10, v1, last_result)
            else:
                state["consecutive_errors"] += 1
                logger.error("Nessun dato. Errori consecutivi: %d/%d",
                             state["consecutive_errors"], MAX_ERRORS)
                if state["consecutive_errors"] >= MAX_ERRORS and not state["sos_sent"]:
                    send_telegram(
                        "<b>ERRORE TRACCIAMENTO</b>\n"
                        "Tutte le sorgenti non disponibili.\n"
                        "<b>{}</b>".format(datetime.now().strftime("%H:%M:%S"))
                    )
                    state["sos_sent"] = True

        except Exception as e:
            state["consecutive_errors"] += 1
            logger.exception("Errore loop: %s", e)

        time.sleep(SCAN_INTERVAL + random.uniform(0, JITTER_MAX))

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
    return jsonify({
        "last_url":      state["debug_last_url"],
        "html_len":      state["debug_html_len"],
        "extracted":     state["debug_extracted"],
        "html_snippet":  state["debug_html_snippet"],
        "last_source":   state["last_source"],
        "errors":        state["consecutive_errors"],
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
    threading.Thread(target=bot_loop, daemon=True).start()
    logger.info("Flask web server in ascolto su porta %d", port)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
