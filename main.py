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

SCAN_INTERVAL  = 15
JITTER_MAX     = 6
SESSION_SPINS  = 14
MAX_ERRORS     = 3
FETCH_RETRIES  = 3

STATE_FILE = "session_state.json"

USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.6261.119 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; Samsung Galaxy S23) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.230 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

REFERERS = [
    "https://www.google.com/",
    "https://www.google.it/",
    "https://www.bing.com/",
    "https://duckduckgo.com/",
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
    ua = random.choice(USER_AGENTS)
    is_mobile = "Mobile" in ua or "iPhone" in ua or "Android" in ua
    return {
        "User-Agent":      ua,
        "Referer":         random.choice(REFERERS),
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": random.choice([
            "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
            "en-US,en;q=0.9,it;q=0.8",
            "en-GB,en;q=0.9",
        ]),
        "Accept-Encoding": "gzip, deflate, br",
        "DNT":             "1",
        "Cache-Control":   random.choice(["no-cache", "max-age=0"]),
        "Sec-Fetch-Dest":  "document",
        "Sec-Fetch-Mode":  "navigate",
        "Sec-Fetch-Site":  "cross-site",
        "Viewport-Width":  "390" if is_mobile else "1440",
    }

def get_json_headers():
    return {
        "User-Agent":      random.choice(USER_AGENTS),
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer":         "https://www.tracksino.com/",
        "X-Requested-With": "XMLHttpRequest",
    }

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

# ─── FETCH ────────────────────────────────────────────────────────────────────

def fetch_html(url, headers=None):
    # type: (str, object) -> Optional[str]
    time.sleep(random.uniform(1.0, 3.0))
    session = requests.Session()

    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            r = session.get(
                url,
                headers=headers or get_headers(),
                verify=False,
                timeout=25,
                allow_redirects=True,
            )
            if r.status_code == 200:
                logger.info("[fetch] OK %s (tentativo %d)", url, attempt)
                return r.text
            elif r.status_code == 429:
                wait = random.uniform(25, 45)
                logger.warning("[fetch] Rate limit 429 – attendo %.0fs", wait)
                time.sleep(wait)
            elif r.status_code in (403, 503):
                wait = random.uniform(10, 20)
                logger.warning("[fetch] HTTP %s – attendo %.0fs", r.status_code, wait)
                time.sleep(wait)
            else:
                logger.warning("[fetch] HTTP %s tentativo %d", r.status_code, attempt)
                time.sleep(3)
        except requests.exceptions.Timeout:
            logger.warning("[fetch] Timeout tentativo %d", attempt)
            time.sleep(4)
        except Exception as e:
            logger.warning("[fetch] Errore tentativo %d: %s", attempt, e)
            time.sleep(3)

    return None

# ─── ESTRATTORE TRACKSINO ──────────────────────────────────────────────────────
#
# Strategia multipla:
#   1. API JSON /data  (risposta diretta, più affidabile)
#   2. __NEXT_DATA__ / JSON nei <script>
#   3. Parsing HTML classico con più selettori
#   4. Regex full-text sull'HTML grezzo

def _parse_json_for_segment(obj, target):
    # type: (object, str) -> Optional[int]
    """Cerca ricorsivamente in un oggetto JSON il valore spins per il segmento target."""
    if isinstance(obj, dict):
        # chiavi tipiche nei JSON di statistiche live
        label_keys  = ("segment", "label", "name", "value", "number", "result")
        count_keys  = ("spins_since_last", "spinsSinceLast", "spins_since",
                       "spinsSince", "last_seen", "count", "frequency", "gap")
        label_val = None
        for k in label_keys:
            if k in obj:
                label_val = str(obj[k]).strip()
                break
        if label_val == target:
            for k in count_keys:
                if k in obj:
                    try:
                        return int(obj[k])
                    except (ValueError, TypeError):
                        pass
        for v in obj.values():
            result = _parse_json_for_segment(v, target)
            if result is not None:
                return result
    elif isinstance(obj, list):
        for item in obj:
            result = _parse_json_for_segment(item, target)
            if result is not None:
                return result
    return None


def extract_tracksino(html):
    # type: (str) -> Tuple[Optional[int], Optional[int], Optional[str]]
    spins_10 = None
    spins_1  = None
    last_result = None

    # ── Strategia 1: __NEXT_DATA__ (Next.js) ─────────────────────────────────
    m = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
                  html, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            v10 = _parse_json_for_segment(data, "10")
            v1  = _parse_json_for_segment(data, "1")
            if v10 is not None:
                logger.info("[tracksino] NEXT_DATA: spins_10=%s spins_1=%s", v10, v1)
                return v10, v1, None
        except Exception as e:
            logger.debug("[tracksino] NEXT_DATA parse error: %s", e)

    # ── Strategia 2: tutti i blocchi JSON nei <script> ────────────────────────
    soup = BeautifulSoup(html, "lxml")
    for script in soup.find_all("script"):
        text = script.string or ""
        if not text or len(text) < 20:
            continue
        # cerca ogni oggetto JSON isolato nel testo dello script
        for blob in re.findall(r'\{[^<]{15,}\}', text):
            try:
                obj = json.loads(blob)
                v10 = _parse_json_for_segment(obj, "10")
                v1  = _parse_json_for_segment(obj, "1")
                if v10 is not None:
                    logger.info("[tracksino] script-JSON: spins_10=%s spins_1=%s", v10, v1)
                    return v10, v1, None
            except Exception:
                pass
        # cerca array JSON
        for blob in re.findall(r'\[[^\]]{15,}\]', text):
            try:
                obj = json.loads(blob)
                v10 = _parse_json_for_segment(obj, "10")
                v1  = _parse_json_for_segment(obj, "1")
                if v10 is not None:
                    logger.info("[tracksino] script-array: spins_10=%s spins_1=%s", v10, v1)
                    return v10, v1, None
            except Exception:
                pass

    # ── Strategia 3: HTML parsing multi-selettore ─────────────────────────────
    selectors = [
        {"class": re.compile(r"game-stats-seg")},
        {"class": re.compile(r"stat")},
        {"class": re.compile(r"segment")},
        {"class": re.compile(r"result")},
        {"class": re.compile(r"wheel")},
    ]
    for sel in selectors:
        for seg in soup.find_all(True, sel):
            text = seg.get_text(" ", strip=True)
            # cerca "10" vicino a un numero di giri
            if not re.search(r'\b10\b', text):
                continue
            m = re.search(r'\b10\b.*?(\d+)\s+spins?\s+since', text, re.IGNORECASE)
            if not m:
                m = re.search(r'since.*?(\d+).*?\b10\b', text, re.IGNORECASE)
            if m:
                spins_10 = int(m.group(1))
                logger.info("[tracksino] HTML-sel: spins_10=%s", spins_10)
                # cerca "1" nello stesso blocco
                m1 = re.search(r'\b1\b.*?(\d+)\s+spins?\s+since', text, re.IGNORECASE)
                if m1:
                    spins_1 = int(m1.group(1))
                return spins_10, spins_1, last_result

    # ── Strategia 4: regex aggressiva sul testo completo della pagina ─────────
    raw = soup.get_text(" ")

    # pattern: "10 ... N spins since" oppure "N spins since ... 10"
    for pat in [
        r'(?<!\d)10(?!\d)[^\n]{0,60}?(\d{1,4})\s+spins?\s+since',
        r'(\d{1,4})\s+spins?\s+since[^\n]{0,60}?(?<!\d)10(?!\d)',
        r'(?<!\d)10(?!\d)[^\n]{0,40}?last\D{0,20}?(\d{1,4})',
        r'spins_since[_\s]*10["\s:]*(\d{1,4})',
        r'"10"[^}]{0,80}?(\d{1,4})',
    ]:
        m = re.search(pat, raw, re.IGNORECASE)
        if m:
            spins_10 = int(m.group(1))
            logger.info("[tracksino] regex-fulltext: spins_10=%s", spins_10)
            return spins_10, None, None

    return None, None, None


# ── API JSON diretta Tracksino ─────────────────────────────────────────────────

def fetch_tracksino_api():
    # type: () -> Tuple[Optional[int], Optional[int]]
    """
    Tracksino espone dati statistici via endpoint JSON.
    Proviamo i path più comuni.
    """
    api_urls = [
        "https://tracksino.com/api/crazytime/stats",
        "https://tracksino.com/api/stats/crazytime",
        "https://tracksino.com/crazytime/stats.json",
        "https://tracksino.com/api/live/crazytime",
    ]
    for url in api_urls:
        try:
            time.sleep(random.uniform(0.5, 1.5))
            r = requests.get(url, headers=get_json_headers(), verify=False, timeout=15)
            if r.status_code == 200:
                data = r.json()
                v10 = _parse_json_for_segment(data, "10")
                v1  = _parse_json_for_segment(data, "1")
                if v10 is not None:
                    logger.info("[tracksino-api] spins_10=%s spins_1=%s via %s", v10, v1, url)
                    return v10, v1
        except Exception as e:
            logger.debug("[tracksino-api] %s → %s", url, e)
    return None, None


# ─── ESTRATTORE CASINOSCORES ───────────────────────────────────────────────────
#
# Strategia multipla:
#   1. JSON nei <script> con parsing ricorsivo
#   2. Elementi HTML con etichetta "10"
#   3. Regex sul testo completo

def extract_casinoscores(html):
    # type: (str) -> Tuple[Optional[int], Optional[int]]
    soup = BeautifulSoup(html, "lxml")

    # ── Strategia 1: JSON ricorsivo in tutti gli script ───────────────────────
    for script in soup.find_all("script"):
        text = script.string or ""
        if not text or len(text) < 20:
            continue
        for blob in re.findall(r'[\[{][^<]{15,}[\]}]', text):
            try:
                obj = json.loads(blob)
                v10 = _parse_json_for_segment(obj, "10")
                v1  = _parse_json_for_segment(obj, "1")
                if v10 is not None:
                    logger.info("[casinoscores] JSON: spins_10=%s spins_1=%s", v10, v1)
                    return v10, v1
            except Exception:
                pass

        # pattern string diretti
        for pat in [
            r'"10"[^}]{0,120}?"(?:spins_since|spinsSince|count|frequency|gap)"\s*:\s*(\d+)',
            r'spins_since[_\s]*10["\s:]*(\d+)',
            r'segment["\s:]+10[^}]{0,80}?(\d+)',
        ]:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                v10 = int(m.group(1))
                logger.info("[casinoscores] script-regex: spins_10=%s", v10)
                return v10, None

    # ── Strategia 2: elementi HTML con testo "10" ─────────────────────────────
    for tag in soup.find_all(True):
        txt = tag.get_text(" ", strip=True)
        if not re.search(r'(?<!\d)10(?!\d)', txt):
            continue
        if len(txt) > 200:
            continue
        m = re.search(r'(\d{1,4})\s+spins?\s+since', txt, re.IGNORECASE)
        if not m:
            m = re.search(r'(?:last|since|gap)[^\d]{0,15}(\d{1,4})', txt, re.IGNORECASE)
        if m:
            v10 = int(m.group(1))
            logger.info("[casinoscores] HTML-tag: spins_10=%s", v10)
            return v10, None

    # ── Strategia 3: regex sul testo grezzo ───────────────────────────────────
    raw = soup.get_text(" ")
    for pat in [
        r'(?<!\d)10(?!\d)[^\n]{0,60}?(\d{1,4})\s+spins?\s+since',
        r'(\d{1,4})\s+spins?\s+since[^\n]{0,60}?(?<!\d)10(?!\d)',
        r'(?<!\d)10(?!\d)[^\n]{0,40}?last\D{0,20}?(\d{1,4})',
    ]:
        m = re.search(pat, raw, re.IGNORECASE)
        if m:
            v10 = int(m.group(1))
            logger.info("[casinoscores] fulltext-regex: spins_10=%s", v10)
            return v10, None

    return None, None


# ─── ORCHESTRAZIONE SORGENTI ───────────────────────────────────────────────────

def scrape_all_sources():
    # type: () -> Tuple[Optional[int], Optional[int], Optional[str]]

    # 1. Prova API JSON diretta Tracksino
    v10, v1 = fetch_tracksino_api()
    if v10 is not None:
        state["last_source"] = "tracksino-api"
        return v10, v1, None

    # 2. Prova HTML Tracksino
    html = fetch_html("https://www.tracksino.com/crazytime")
    if html:
        try:
            v10, v1, last_result = extract_tracksino(html)
        except Exception as e:
            logger.warning("[tracksino] Errore estrazione: %s", e)
            v10, v1, last_result = None, None, None
        if v10 is not None:
            logger.info("[tracksino] spins_10=%s spins_1=%s", v10, v1)
            state["last_source"] = "tracksino"
            return v10, v1, last_result
        else:
            logger.warning("[tracksino] Nessun dato '10' trovato (tutte le strategie esaurite)")
    else:
        logger.warning("[tracksino] Impossibile scaricare la pagina")

    # 3. Prova HTML CasinoScores
    html = fetch_html("https://casinoscores.com/crazy-time/")
    if html:
        try:
            v10, v1 = extract_casinoscores(html)
        except Exception as e:
            logger.warning("[casinoscores] Errore estrazione: %s", e)
            v10, v1 = None, None
        if v10 is not None:
            logger.info("[casinoscores] spins_10=%s spins_1=%s", v10, v1)
            state["last_source"] = "casinoscores"
            return v10, v1, None
        else:
            logger.warning("[casinoscores] Nessun dato '10' trovato (tutte le strategie esaurite)")
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
        logger.info(
            "Differenziale: 10: %s->%s | 1: %s->%s | appeared_10=%s",
            prev_10, spins_since_10, prev_1, spins_since_1, appeared_10
        )
        return appeared_10
    else:
        if spins_since_10 == prev_10:
            if spins_since_10 == 0 and in_session:
                logger.info("Fallback: 10 consecutivo rilevato")
                return True
            return None
        appeared_10 = (spins_since_10 < prev_10)
        logger.info(
            "Fallback: 10: %s->%s | appeared_10=%s",
            prev_10, spins_since_10, appeared_10
        )
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
        "Scansione ogni {}s (+jitter) | Monitoraggio: {} colpi\n"
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
        logger.debug("Prossima scansione tra %.1fs", sleep_time)
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
