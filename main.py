import os
import re
import json
import time
import random
import threading
import warnings
import logging
from datetime import datetime
from typing import Optional, Tuple, List

import requests
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
JITTER_MAX     = 5
SESSION_SPINS  = 14
MAX_ERRORS     = 3
FETCH_RETRIES  = 3
HISTORY_SIZE   = 60   # quanti giri recenti chiedere all'API

STATE_FILE = "session_state.json"

USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.6261.119 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]

# Endpoint API Tracksino — proviamo più varianti
TRACKSINO_API_URLS = [
    "https://tracksino.com/api/history/crazytime?limit={}".format(HISTORY_SIZE),
    "https://tracksino.com/api/crazytime/history?limit={}".format(HISTORY_SIZE),
    "https://tracksino.com/api/crazytime?limit={}".format(HISTORY_SIZE),
    "https://tracksino.com/api/history/crazy-time?limit={}".format(HISTORY_SIZE),
    "https://tracksino.com/crazytime/api?limit={}".format(HISTORY_SIZE),
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
    "last_raw_response":     "",
    "last_api_url":          "",
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

def get_headers(referer="https://www.tracksino.com/"):
    return {
        "User-Agent":      random.choice(USER_AGENTS),
        "Accept":          "application/json, text/html, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer":         referer,
        "Origin":          "https://www.tracksino.com",
        "Cache-Control":   "no-cache",
        "DNT":             "1",
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
                logger.warning("Telegram errore %s (tentativo %d): %s",
                               r.status_code, attempt,
                               resp.get("description", r.text[:200]))
        except Exception as e:
            logger.error("Telegram eccezione tentativo %d: %s", attempt, e)
        time.sleep(2)
    return False

# ─── CALCOLO spins_since DA CRONOLOGIA ────────────────────────────────────────

def _result_matches(result_str, target):
    # type: (str, str) -> bool
    """
    Controlla se una stringa risultato corrisponde al numero target.
    Gestisce varianti come "10", "10x", "10 ", " 10", ecc.
    """
    s = str(result_str).strip().lower()
    t = target.strip().lower()
    return s == t or s == t + "x" or re.fullmatch(re.escape(t) + r'[x\s]*', s) is not None


def calc_spins_since(history, target):
    # type: (List, str) -> Optional[int]
    """
    Dato un array di giri (dal più recente al più vecchio),
    restituisce quanti giri fa è uscito 'target' l'ultima volta.
    0 = è uscito nell'ultimo giro.
    None = non trovato nella finestra.
    """
    # ogni elemento può essere dict con chiave "result", "slug", "outcome", "number", ecc.
    result_keys = ("result", "slug", "outcome", "number", "value",
                   "segment", "label", "name", "spin_result")
    for i, spin in enumerate(history):
        if isinstance(spin, dict):
            for k in result_keys:
                if k in spin:
                    if _result_matches(str(spin[k]), target):
                        return i
        elif isinstance(spin, str):
            if _result_matches(spin, target):
                return i
    return None

# ─── FETCH API TRACKSINO ───────────────────────────────────────────────────────

def fetch_tracksino_api():
    # type: () -> Tuple[Optional[int], Optional[int], Optional[str]]
    """
    Scarica la cronologia recente via API JSON (senza proxy, senza parsing HTML).
    Restituisce (spins_since_10, spins_since_1, last_result).
    """
    session = requests.Session()

    for api_url in TRACKSINO_API_URLS:
        for attempt in range(1, FETCH_RETRIES + 1):
            try:
                time.sleep(random.uniform(0.5, 2.0))
                r = session.get(
                    api_url,
                    headers=get_headers(),
                    verify=False,
                    timeout=20,
                )
                state["last_api_url"]     = api_url
                state["last_raw_response"] = r.text[:1000]

                logger.info("[api] %s → HTTP %d | len=%d",
                            api_url, r.status_code, len(r.text))

                if r.status_code == 404:
                    logger.debug("[api] 404, prossimo endpoint")
                    break  # prova prossimo URL

                if r.status_code != 200:
                    logger.warning("[api] HTTP %d tentativo %d", r.status_code, attempt)
                    time.sleep(3)
                    continue

                # Prova a parsare JSON
                try:
                    data = r.json()
                except ValueError:
                    logger.warning("[api] risposta non JSON: %s", r.text[:200])
                    break

                # data potrebbe essere lista diretta o dict con chiave "data"/"history"/"spins"
                history = None
                if isinstance(data, list):
                    history = data
                elif isinstance(data, dict):
                    for k in ("data", "history", "spins", "results", "items", "records"):
                        if k in data and isinstance(data[k], list):
                            history = data[k]
                            break

                if not history:
                    logger.warning("[api] struttura JSON non riconosciuta: %s", str(data)[:200])
                    break

                logger.info("[api] OK %s | %d giri ricevuti", api_url, len(history))

                spins_10   = calc_spins_since(history, "10")
                spins_1    = calc_spins_since(history, "1")
                last_result = None

                # ultimo giro uscito
                if history:
                    first = history[0]
                    if isinstance(first, dict):
                        for k in ("result", "slug", "outcome", "number", "value",
                                  "segment", "label", "name"):
                            if k in first:
                                last_result = str(first[k])
                                break
                    elif isinstance(first, str):
                        last_result = first

                if spins_10 is not None:
                    logger.info("[api] spins_10=%d spins_1=%s last=%s",
                                spins_10, spins_1, last_result)
                    return spins_10, spins_1, last_result
                else:
                    logger.warning("[api] '10' non trovato nei %d giri", len(history))
                    # logga i primi risultati per debug
                    logger.info("[api] primi 5 risultati: %s", str(history[:5]))
                    return None, None, None

            except requests.exceptions.Timeout:
                logger.warning("[api] Timeout tentativo %d su %s", attempt, api_url)
                time.sleep(3)
            except Exception as e:
                logger.warning("[api] Errore tentativo %d su %s: %s", attempt, api_url, e)
                time.sleep(2)

    logger.error("[api] tutti gli endpoint falliti")
    return None, None, None

# ─── ORCHESTRAZIONE SORGENTI ───────────────────────────────────────────────────

def scrape_all_sources():
    # type: () -> Tuple[Optional[int], Optional[int], Optional[str]]
    v10, v1, last_result = fetch_tracksino_api()
    if v10 is not None:
        state["last_source"] = "tracksino-api"
        return v10, v1, last_result
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
        "Fonte: Tracksino API | Monitoraggio: {} colpi\n"
        "<b>{}</b>".format(
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
    return jsonify({
        "last_api_url":      state["last_api_url"],
        "last_raw_response": state["last_raw_response"],
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
