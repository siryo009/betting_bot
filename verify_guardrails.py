#!/usr/bin/env python3
"""Verifica FORZATA dei guardrail di rischio (11/09/2026).

Dimostra, con i log reali, che i quattro guardrail bloccano davvero le
puntate. Non tocca la produzione: usa un DATA_DIR temporaneo, un DB
temporaneo e MAI un provider reale (nessun ordine, nessuna rete).

Scenari:
  A. Kill-switch OFF           -> il giro non parte
  B. Stop-loss giornaliero -5% -> puntate bloccate 24h
  C. Kelly aggressivo           -> k=0.65 con CAP DINAMICO 12% del bankroll
                                  (il Kelly tronca, il capitale scala col
                                  capitale); sotto il ticket minimo 2.00 USDC
                                  lo stake e' 0.0 e l'ordine non parte; il
                                  percorso LEGACY a importo fisso resta
                                  ripristinabile via ORDER_FIXED_STAKE_USDC
                                  (con vincolo di cassa e fail-closed)
  D. Filtro prezzo             -> fascia bottom-up (SIM) / gate oracolo
                                  Pinnacle (LIVE): il bypass ordina solo con
                                  EV oracolo >= soglia, i gate NON-prezzo
                                  (lega) restano
  E. Liquidita' SX             -> book sottile: ordine rifiutato (no slippage)
  F. Lega STRATEGY_LEAGUES     -> campionati non vincenti mai candidati
  G. Circuit breakers T-60     -> finestra esecutiva T-120..T-15, CB1 cap per
                                  ordine, CB2 kill switch patrimoniale 30 USDC
  H. Recinto esposizione       -> 8 ordini aperti (40% impegnato): l'Advisor
                                  respinge i nuovi piani, il giro non ordina

Uso: venv/bin/python verify_guardrails.py
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --- Isolamento totale PRIMA degli import: DATA_DIR temporaneo ---------------
TMP = Path(tempfile.mkdtemp(prefix="qv_guardrails_"))
os.environ["QUOTAVERACE_DATA_DIR"] = str(TMP)
os.environ.pop("AUTO_BET_MODE", None)          # default: sim
os.environ.pop("EXECUTION_PROVIDER", None)     # nessun provider reale
os.environ.pop("EXECUTION_APP_KEY", None)
os.environ["STAKE_CAP_HARD"] = "1"
# Finestra T-60 di default (come in produzione): gli scenari che misurano
# altri guardrail a orizzonte aperto la disattivano LOCALMENTE e la
# ripristinano — l'autorevole resta lo scenario G.
os.environ["T60_EXECUTION_ONLY"] = "0"
os.environ.pop("SETTLEMENT_PAUSED", None)
# Feed di mercato OFF: questa diagnostica non deve toccare la rete (il gate
# vero e' coperto da `test_decision_feed.py` e da `python -m decision feed`).
os.environ["DECISION_FEED_ENABLED"] = "0"
# INTEL LIVE OFF (30/09/2026): gli scraper reali di `live_intel` (FBref,
# DuckDuckGo) partono dal DataAgent del ciclo Chief e rendevano questa
# diagnostica LENTA e dipendente dalla rete — misurato: il processo moriva a
# meta' dello scenario C proprio durante lo scraping FBref (i guardrail non
# erano in errore, semplicemente non arrivavano mai alla fine). E' la stessa
# protezione che `conftest.py` applica ai test: senza rete, il verdetto e'
# deterministico e gli scenari A-H si leggono in pochi secondi.
os.environ["LIVE_INTEL"] = "0"
# Corsia eSports OFF: le sue letture (discovery SX + oracolo OddsPapi) sono
# rete reale e quota a consumo. Questa diagnostica misura i GUARDRAIL, non il
# flusso eSports (coperto da `test_esports_lane.py` con provider finti).
os.environ["ESPORTS_LIVE"] = "0"

# --- Cattura dei log --------------------------------------------------------
_RECORDS: list[str] = []


class _Capture(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        _RECORDS.append(f"[{record.levelname:7}] {record.name}: "
                        f"{record.getMessage()}")


logging.basicConfig(level=logging.INFO,
                    format="%(levelname)-7s %(name)s: %(message)s")
logging.getLogger().addHandler(_Capture())
for _noisy in ("httpx", "urllib3", "telegram"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

import tracker            # noqa: E402  (dopo aver fissato DATA_DIR)
import auto_bet           # noqa: E402
import value_filter       # noqa: E402

# Fase 2 top-down (25/09): il gate EV sull'oracolo Pinnacle (TOP_DOWN_EV,
# default ON) governa la corsia LIVE ed e' fail-closed senza oracolo — senza
# lo stub lo scenario C passerebbe per `no_oracle` invece che per il CAP
# SEVERO che vuole dimostrare, e la controprova col floor non partirebbe
# mai. L'oracolo e' stubbato con p_true 0.65 (trigger su ogni quota della
# fascia); la semantica del gate e' verificata in test_top_down.py.
auto_bet._top_down_load = lambda home, away: {
    "1": 0.65, "X": 0.65, "2": 0.65, "overround": 0.0}

tracker.init_db()

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
CYAN = "\033[36m"
RESET = "\033[0m"


def _head(title: str) -> None:
    print(f"\n{BOLD}{CYAN}{'═' * 68}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'═' * 68}{RESET}")


def _logs_since(mark: int) -> list[str]:
    return _RECORDS[mark:]


def _print_logs(mark: int) -> None:
    for line in _logs_since(mark):
        print(f"  {DIM}│{RESET} {line}")


def _start(hours: float = 3.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)) \
        .isoformat().replace("+00:00", "Z")


# Lega AMMESSA dalla strategia (value_filter.STRATEGY_LEAGUES): dal 15/09 la
# corsia ordini applica il gate di lega, quindi il seed della diagnostica deve
# stare in una lega ammessa — altrimenti OGNI scenario misura solo il gate di
# lega e non il guardrail che vuole verificare.
ALLOWED_LEAGUE = "Premier League"


def _seed(mid: str, esito: str, quota: float, market_prob: float,
          edge: float, home: str = "Osasuna", away: str = "Getafe") -> None:
    tracker.save_match(mid, ALLOWED_LEAGUE, home, away, _start())
    tracker.save_prediction(mid, "1X2", esito, quota, 0.60, 0.08,
                            market_prob=market_prob, market_edge=edge,
                            status="value")


def _reset_state() -> None:
    auto_bet.clear_kill_switch()
    auto_bet.clear_daily_stop()
    # Circuit breaker SETTIMANALE (26/09): la diagnostica fa oscillare il
    # bankroll fra scenari e un solo salto verrebbe letto come drawdown
    # (osservato il 28/09: -96.8% dalla cassa al wallet micro del recinto,
    # che armava il blocco e svuotava gli scenari successivi). Ogni scenario
    # parte da uno stato pulito: si azzera il blocco E la storia dei campioni
    # (dal 04/10 la cassa del recinto passa da 38 a 33.55 USDC = -11.7%, a un
    # soffio dalla soglia del 12%: senza il reset lo scenario H dipenderebbe
    # dall'ordine di esecuzione invece che dal proprio guardrail).
    auto_bet.clear_weekly_stop()
    # CB2 patrimoniale (17/09): un wallet finto sotto soglia (scenario C2 a
    # 4 USDC) ARMA il flag persistente, e da quel momento il giro resta
    # fermo per TUTTI gli scenari successivi — la diagnosi misurava il CB2
    # invece del guardrail in esame (osservato il 04/10: D e C4 a zero per
    # il kill switch armato da C2). Ogni scenario parte da uno stato pulito.
    auto_bet.t60_clear_kill()
    try:
        auto_bet.BANKROLL_HISTORY_FILE.unlink()
    except FileNotFoundError:
        pass


class _LiqProv:
    """Provider finto per lo scenario E: nessuna rete, nessun ordine reale.

    Espone lo STESSO contratto che `_live_fill` usa su SX Bet (best price,
    order book taker, place_limit_order) cosi' il guardrail viene esercitato
    sul codice VERO, senza credenziali ne' chiamate esterne.
    """

    name = "sxbet-stub"

    def __init__(self, depth: float, order=None) -> None:
        self.depth = depth
        self.order = order
        self.place_calls: list = []

    def best_back_price(self, market_id, selection_id):
        return 1.70

    def get_market_book(self, market_id):
        return {"runners": [{"selectionId": 1, "availableToBack": [
            {"price": 1.65, "size": self.depth}]}]}

    def place_limit_order(self, market_id, selection_id, side, price, size,
                          persistence="LAPSE"):
        self.place_calls.append((market_id, selection_id, side, price, size))
        return self.order


def main() -> int:
    print(f"{BOLD}VERIFICA GUARDRAIL — DATA_DIR temporaneo: {TMP}{RESET}")
    import liquidity_monitor
    import sx_signals
    from decision.stake_engine import aggressive_config as _agg
    _a = _agg()
    print(f"STAKE_CAP_HARD={auto_bet.cap_hard_active()} "
          f"MIN_STAKE_EUR={auto_bet.MIN_STAKE_EUR} "
          f"ODDS_MIN={value_filter.ODDS_MIN} "
          f"ODDS_MAX={value_filter.ODDS_MAX}")
    print(f"KELLY AGGRESSIVO: k={_a['kelly_fraction']:.2f} | cap "
          f"{_a['max_stake_pct']*100:.0f}% del bankroll | ticket "
          f"{_a['min_ticket']:.2f} USDC | importo fisso legacy "
          f"{auto_bet.fixed_order_stake():.2f} USDC (0 = disattivo)")
    print(f"CB1 T-60: tetto per-ordine {auto_bet.order_ceiling(33.55):.2f} "
          f"USDC su equity 33.55 (12% dinamico)")
    print(f"LIQUIDITA': totale {sx_signals.MIN_DEPTH_USDC:.0f} | "
          f"esito {sx_signals.MIN_LEG_DEPTH_USDC:.0f} | leg giocata "
          f"{auto_bet.MIN_EXEC_DEPTH_USDC:.0f} | margine "
          f"x{auto_bet.SX_DEPTH_MULTIPLIER:.1f} sullo stake")

    # Seed: un segnale perfettamente valido per la strategia favoriti.
    _seed("g-valid", "Osasuna", 1.65, 0.60, 0.07)

    # --------------------------------------------------------------- A. OFF ---
    _head("A. KILL-SWITCH OFF — il giro delle puntate non deve partire")
    _reset_state()
    auto_bet.set_kill_switch("off")
    mark = len(_RECORDS)
    placed = auto_bet.run_today_bets()
    _print_logs(mark)
    ok_a = (placed == [] and tracker.get_bets() == [])
    print(f"  {GREEN if ok_a else RED}→ puntate piazzate: "
          f"{len(placed)}  (atteso 0){RESET}")
    st = auto_bet.kill_switch_status()
    print(f"  {DIM}stato kill-switch: effective={st['effective']} "
          f"override={st['override']}{RESET}")

    # ---------------------------------------------------------- B. STOP-LOSS ---
    _head("B. STOP-LOSS GIORNALIERO -5% — puntate bloccate per 24h")
    _reset_state()
    # Trigger reale della soglia (-6% dal bankroll di inizio giornata).
    mark = len(_RECORDS)
    auto_bet.check_daily_stop(100.0)
    trigger = auto_bet.check_daily_stop(94.0)
    _print_logs(mark)
    print(f"  {DIM}trigger: stopped={trigger['stopped']} "
          f"loss={trigger['loss_pct']:.1f}% until={trigger['until']}{RESET}")
    mark = len(_RECORDS)
    placed = auto_bet.run_today_bets()
    _print_logs(mark)
    ok_b = (trigger["stopped"] and placed == []
            and tracker.get_bets() == [])
    print(f"  {GREEN if ok_b else RED}→ puntate piazzate: "
          f"{len(placed)}  (atteso 0){RESET}")

    # ------------------------------------------- C. KELLY AGGRESSIVO 04/10 ---
    _head("C. KELLY AGGRESSIVO k=0.65 + CAP DINAMICO 12% + TICKET 2.00")
    from decision.stake_engine import aggressive_config
    _reset_state()
    # Forza la modalita' LIVE con un wallet di 38 USDC (mai un ordine vero:
    # _live_fill e' sostituito da uno stub che conta le chiamate).
    auto_bet._execution_mode = lambda allow_sim=True: "live"
    # Wallet reale: 36 USDC liberi + 2 in gioco (escrow) = 38 di EQUITY.
    # Il cap si misura sull'EQUITY (fix stop-loss/equity del 15/09): sul solo
    # disponibile il cap sarebbe 12% di 36 = 4.32, non 4.56.
    _wallet_reale = {"available": 36.0, "exposure": 2.0, "equity": 38.0}
    auto_bet._live_wallet_snapshot = lambda: dict(_wallet_reale)
    calls = {"fill": 0, "stake": 0.0}
    _real_live_fill = auto_bet._live_fill   # ripristinata nello scenario E
    _cfg = aggressive_config()

    def _stub_fill(pick, stake, floor):  # noqa: ANN001
        calls["fill"] += 1
        calls["stake"] = stake
        return None

    auto_bet._live_fill = _stub_fill
    # La finestra T-60 e' dimostrata dallo scenario G: qui il candidato a +3h
    # deve ARRIVARE alla fase stake, altrimenti lo stake non verrebbe mai
    # misurato (dal 17/09 il default ON del T-60 svuotava questo scenario).
    auto_bet.T60_EXECUTION_ONLY = False
    mark = len(_RECORDS)
    placed = auto_bet.run_today_bets()
    _print_logs(mark)
    # C1 — KELLY + CAP DINAMICO: lo stake e' min(kelly x k, 12% del bankroll),
    # mai un importo fisso. Con bankroll 38 il tetto e' 4.56.
    _cap_atteso = round(38.0 * _cfg["max_stake_pct"], 2)
    ok_c1 = (calls["fill"] == 1 and 0 < calls["stake"] <= _cap_atteso + 1e-9)
    print(f"  {GREEN if ok_c1 else RED}→ Kelly aggressivo: ordini "
          f"{calls['fill']} con stake {calls['stake']:.2f} USDC "
          f"(k={_cfg['kelly_fraction']:.2f}, cap 12% di 38 = "
          f"{_cap_atteso:.2f}, atteso 1 ordine sotto il cap){RESET}")

    # C2 — TICKET MINIMO 2.00: con un wallet piccolo lo stake Kelly scende
    # sotto il ticket e l'ordine NON parte (mai un ordine piu' piccolo).
    _reset_state()
    _piccolo = 4.00     # cap 12% = 0.48 < ticket 2.00
    auto_bet._live_wallet_snapshot = lambda: {
        "available": _piccolo, "exposure": 0.0, "equity": _piccolo}
    # (il ticket morde: lo stake Kelly cappato sta sotto la soglia)
    calls.update({"fill": 0, "stake": 0.0})
    placed_c2 = auto_bet.run_today_bets()
    ok_c2 = placed_c2 == [] and calls["fill"] == 0
    print(f"  {GREEN if ok_c2 else RED}→ wallet {_piccolo:.2f} (cap "
          f"{_piccolo * _cfg['max_stake_pct']:.2f} < ticket "
          f"{_cfg['min_ticket']:.2f}): ordini {calls['fill']} "
          f"(atteso 0, fail-closed){RESET}")

    # C3 — LEGACY (importo fisso 1.50) ripristinabile + vincolo di cassa:
    # con meno dell'importo fisso libero l'ordine NON parte (fail-closed),
    # mai un importo diverso dalla direttiva per farlo passare.
    auto_bet.FIXED_STAKE_USDC = 1.50
    _libero = 1.20
    auto_bet._live_wallet_snapshot = lambda: {
        "available": _libero, "exposure": 38.0 - _libero, "equity": 38.0}
    _reset_state()
    calls.update({"fill": 0, "stake": 0.0})
    placed_c3 = auto_bet.run_today_bets()
    ok_c3 = (placed_c3 == [] and calls["fill"] == 0
             and auto_bet.fixed_order_stake() == 1.50)
    print(f"  {DIM}controprova LEGACY: ORDER_FIXED_STAKE_USDC=1.50 attivo "
          f"→ lo stake torna l'importo fisso e il Kelly resta fuori{RESET}")
    print(f"  {GREEN if ok_c3 else RED}→ wallet {_libero:.2f} liberi < importo "
          f"fisso 1.50: ordini {calls['fill']} (atteso 0, fail-closed){RESET}")
    auto_bet.FIXED_STAKE_USDC = 0.0
    # C4 — Controprova: il Kelly dinamico torna attivo appena l'importo fisso
    # e' spento (un solo punto di precedenza: `aggressive_live_active`).
    auto_bet._live_wallet_snapshot = lambda: {
        "available": 36.0, "exposure": 2.0, "equity": 38.0}
    _reset_state()
    calls.update({"fill": 0, "stake": 0.0})
    placed_c4 = auto_bet.run_today_bets()
    ok_c4 = calls["fill"] == 1 and 0 < calls["stake"] <= _cap_atteso + 1e-9
    print(f"  {DIM}controprova: importo fisso spento → il Kelly aggressivo "
          f"riprende la corsia ({calls['fill']} ordine/i, stake "
          f"{calls['stake']:.2f}){RESET}")
    auto_bet.T60_EXECUTION_ONLY = True
    ok_c = ok_c1 and ok_c2 and ok_c3 and ok_c4

    # ------------------------------------------------- D. FILTRO PREZZO ----
    _head("D. FILTRO PREZZO — fascia bottom-up (SIM) vs gate oracolo (LIVE)")
    _reset_state()
    auto_bet._execution_mode = lambda allow_sim=True: "sim"
    tracker.save_match("g-high", ALLOWED_LEAGUE, "Osasuna", "Getafe", _start())
    # quota 1.90 > 1.80 (favorito troppo "lungo")
    tracker.save_prediction("g-high", "1X2", "Osasuna", 1.90, 0.55, 0.08,
                            market_prob=0.52, market_edge=0.05, status="value")
    tracker.save_match("g-low", ALLOWED_LEAGUE, "Osasuna", "Getafe", _start())
    # quota 1.20 < 1.30 (ritorno troppo basso)
    tracker.save_prediction("g-low", "1X2", "Osasuna", 1.20, 0.72, 0.05,
                            market_prob=0.75, market_edge=0.05, status="value")
    tracker.save_match("g-nfav", ALLOWED_LEAGUE, "Osasuna", "Getafe", _start())
    # quota ok ma NON e' il favorito di mercato (prob 0.30 < 0.50)
    tracker.save_prediction("g-nfav", "1X2", "Getafe", 1.70, 0.45, 0.05,
                            market_prob=0.30, market_edge=0.15, status="value")
    # g-legacy: lega VIETATA (Serie A) fuori fascia — nemmeno il gate oracolo
    # della corsia top-down la candida: il gate di lega NON e' bypassato.
    tracker.save_match("g-legacy", "Serie A", "Osasuna", "Getafe", _start())
    tracker.save_prediction("g-legacy", "1X2", "Osasuna", 1.20, 0.72, 0.05,
                            market_prob=0.75, market_edge=0.05, status="value")
    mark = len(_RECORDS)
    picks = auto_bet._today_value_picks()
    _print_logs(mark)
    ids = sorted(p["match_id"] for p in picks)
    ok_board = ids == ["g-valid"]
    print(f"  {GREEN if ok_board else RED}→ board SIM (fascia bottom-up): {ids}  "
          f"(atteso solo ['g-valid']){RESET}")
    sane, reason = value_filter.is_sane(0.72, 1.20, 0.05, market_prob=0.80)
    print(f"  {DIM}is_sane(prob .72, quota 1.20): ok={sane} → "
          f"{reason}{RESET}")
    sane2, reason2 = value_filter.is_sane(0.80, 1.31, 0.048, market_prob=0.75)
    print(f"  {DIM}is_sane(prob .80, quota 1.31): ok={sane2} "
          f"(dentro fascia){RESET}")
    # CORSIA TOP-DOWN LIVE (25/09): il filtro di prezzo e' BYPASSATO — l'unico
    # giudice e' l'oracolo Pinnacle. Con p_true 0.92 TUTTE le quote fuori
    # fascia (1.90, 1.70-underdog, 1.20) diventano candidati EV-positivi;
    # con p_true 0.20 (EV sempre negativo) NESSUNO passa: il gate oracolo
    # filtra davvero. g-legacy resta fuori per LEGA, non per quota.
    auto_bet._execution_mode = lambda allow_sim=True: "live"
    auto_bet._live_wallet_snapshot = lambda: {
        "available": 36.0, "exposure": 2.0, "equity": 38.0}
    auto_bet.T60_EXECUTION_ONLY = False      # il T-60 e' dello scenario G
    _saved_hard = auto_bet.STAKE_CAP_HARD
    auto_bet.STAKE_CAP_HARD = False          # il cap e' dello scenario C
    # BYPASS DELLA FASCIA (25/09): SPENTO di default dal 26/09 — qui lo si
    # accende ESPLICITAMENTE, altrimenti il filtro bottom-up svuota la corsia
    # e lo scenario misurerebbe la fascia invece del gate oracolo (era il
    # motivo del rosso D dal 26/09). Il default resta comunque verificato: il
    # bypass non deve governare ordini reali senza una scelta dichiarata.
    _bypass_default = bool(getattr(auto_bet, "TOP_DOWN_BYPASS", False))
    auto_bet.TOP_DOWN_BYPASS = True

    def _fill_ok(pick, stake, floor):        # noqa: ANN001
        calls_d["fill"] += 1
        return {"ok": True, "market_id": "m", "selection_id": 1,
                "bet_id": "b", "status": "FULLY_FILLED",
                "price": floor, "stake": stake}

    calls_d = {"fill": 0}
    auto_bet._live_fill = _fill_ok
    auto_bet._top_down_load = lambda home, away: {
        "1": 0.92, "X": 0.92, "2": 0.92, "overround": 0.0}
    mark = len(_RECORDS)
    live_placed = auto_bet.run_today_bets()
    _print_logs(mark)
    ids_live = sorted(p["match_id"] for p in live_placed)
    # La corsia top-down prende OGNI riga 1X2 aperta e lascia decidere
    # l'oracolo: con p_true 0.92 passa anche g-valid (favorito in fascia, che
    # la corsia bottom-up avrebbe giocato comunque — dedup (match_id, esito)
    # nel canale unico). g-legacy resta fuori per LEGA: il bypass e' di
    # PREZZO, non di strategia.
    ok_bypass = (set(ids_live) == {"g-high", "g-low", "g-nfav", "g-valid"}
                 and "g-legacy" not in ids_live)
    print(f"  {GREEN if ok_bypass else RED}→ corsia top-down LIVE (p_true "
          f"0.92): candidati {ids_live} (atteso: le quote fuori fascia DI "
          f"AMBO I LATI + g-valid, mai g-legacy){RESET}")
    # Controprova: stesso board, oracolo con EV sempre negativo -> 0 ordini.
    conn = tracker._get_conn()
    conn.execute("DELETE FROM bets")     # DB temporaneo della diagnostica
    conn.commit()
    conn.close()
    calls_d["fill"] = 0
    auto_bet._top_down_load = lambda home, away: {
        "1": 0.20, "X": 0.20, "2": 0.20, "overround": 0.0}
    mark = len(_RECORDS)
    live_vuoto = auto_bet.run_today_bets()
    _print_logs(mark)
    ok_gate = live_vuoto == [] and calls_d["fill"] == 0
    print(f"  {GREEN if ok_gate else RED}→ stesso board con EV oracolo "
          f"negativo (p_true 0.20): ordini {len(live_vuoto)} "
          f"(atteso 0: il gate oracolo filtra){RESET}")
    auto_bet.T60_EXECUTION_ONLY = True
    auto_bet.STAKE_CAP_HARD = _saved_hard
    auto_bet.TOP_DOWN_BYPASS = _bypass_default
    auto_bet._execution_mode = lambda allow_sim=True: "sim"
    auto_bet._top_down_load = lambda home, away: {   # stub storico ripristinato
        "1": 0.65, "X": 0.65, "2": 0.65, "overround": 0.0}
    ok_default = (_bypass_default is False)
    print(f"  {GREEN if ok_default else RED}→ default TOP_DOWN_BYPASS "
          f"spento: {not _bypass_default} (atteso si: il bypass si accende "
          f"solo per scelta esplicita, mai per ordini reali automatici){RESET}")
    ok_d = ok_board and ok_bypass and ok_gate and ok_default

    # --------------------------------------------------------- E. LIQUIDITA' ---
    _head("E. LIQUIDITA' SX — book sottile: ordine RIFIUTATO (no slippage)")
    import execution_engine as ee
    auto_bet._live_fill = _real_live_fill   # codice VERO, non lo stub di C
    _reset_state()
    # Risoluzione mercato forzata (lo scenario misura il guardrail di
    # liquidita', non il matching evento->mercato gia' coperto dai test).
    ee.resolve_match_market = lambda *a, **k: {"market_id": "m-home",
                                              "selection_id": 1}
    pick = {"match_id": "g-valid", "home": "Osasuna", "away": "Getafe",
            "esito_key": "1", "mercato": "1X2", "commence": _start()}
    stake, floor = 5.0, 1.65
    need = auto_bet.required_depth(stake)
    print(f"  {DIM}stake {stake:.2f} USDC -> richiesti "
          f"max({stake:.2f} x {auto_bet.SX_DEPTH_MULTIPLIER:.1f}, "
          f"{auto_bet.MIN_EXEC_DEPTH_USDC:.1f}) = {need:.2f} USDC al floor "
          f"{floor:.2f}{RESET}")

    def _engine(prov):
        eng = type("_Eng", (), {"provider": prov})()
        ee.ExecutionEngine = lambda *a, **k: eng

    # 1) Book sottile (4 USDC < 25 richiesti): l'ordine NON deve partire.
    thin = _LiqProv(depth=4.0)
    _engine(thin)
    mark = len(_RECORDS)
    res_thin = auto_bet._live_fill(pick, stake=stake, floor=floor)
    _print_logs(mark)
    ok_e1 = (res_thin is None and thin.place_calls == [])
    print(f"  {GREEN if ok_e1 else RED}→ book 4.00 USDC → ordini inviati: "
          f"{len(thin.place_calls)}  (atteso 0){RESET}")
    evts = liquidity_monitor.iter_events(days=1)
    if evts:
        e0 = evts[0]
        print(f"  {DIM}scarto registrato: kind={e0.get('kind')} "
              f"reason={e0.get('reason')} depth={e0.get('depth')} "
              f"richiesto={e0.get('threshold')}{RESET}")
    ok_e2 = bool(evts) and evts[0].get("kind") == "order"

    # 2) Controprova: book profondo (>= soglia) -> l'ordine parte.
    deep = _LiqProv(depth=need + 1.0, order=ee.OrderResult(
        True, "0xe2e", "FULLY_FILLED", floor, floor, stake, stake))
    _engine(deep)
    res_deep = auto_bet._live_fill(pick, stake=stake, floor=floor)
    ok_e3 = (res_deep is not None and res_deep.get("ok") is True
             and len(deep.place_calls) == 1)
    print(f"  {DIM}controprova book {need + 1.0:.2f} USDC → ordini inviati: "
          f"{len(deep.place_calls)} (atteso 1, stake {stake:.2f}){RESET}")
    ok_e = ok_e1 and ok_e2 and ok_e3

    # --------------------------------------------------------------- F. LEGA ---
    _head("F. LEGA (STRATEGY_LEAGUES) — campionati non vincenti mai candidati")
    _reset_state()
    auto_bet._execution_mode = lambda allow_sim=True: "sim"
    # Lega vietata (La Liga: ROI negativo nel backtest) con quota, favorito ed
    # EV perfetti: prima del 15/09 questa riga finiva DRITTA in un ordine
    # (candidati senza lega -> gate cieco). Ora deve fermarsi.
    tracker.save_match("gl-bad", "La Liga", "Osasuna", "Getafe", _start())
    tracker.save_prediction("gl-bad", "1X2", "Osasuna", 1.65, 0.62, 0.08,
                            market_prob=0.58, market_edge=0.07, status="value")
    # Controprova: riga IDENTICA in una lega ammessa -> ammessa.
    tracker.save_match("gl-ok", ALLOWED_LEAGUE, "Osasuna", "Getafe", _start())
    tracker.save_prediction("gl-ok", "1X2", "Osasuna", 1.65, 0.62, 0.08,
                            market_prob=0.58, market_edge=0.07, status="value")
    # Lega assente: senza sapere cosa si gioca non si ordina (fail-closed).
    tracker.save_match("gl-vuota", "", "Osasuna", "Getafe", _start())
    tracker.save_prediction("gl-vuota", "1X2", "Osasuna", 1.65, 0.62, 0.08,
                            market_prob=0.58, market_edge=0.07, status="value")
    mark = len(_RECORDS)
    picks = auto_bet._today_value_picks()
    _print_logs(mark)
    ids = sorted(p["match_id"] for p in picks)
    ok_f = ("gl-bad" not in ids and "gl-vuota" not in ids
            and "gl-ok" in ids)
    print(f"  {GREEN if ok_f else RED}→ lega vietata ammessa: "
          f"{'gl-bad' in ids} (atteso no) · lega assente ammessa: "
          f"{'gl-vuota' in ids} (atteso no) · lega ammessa ammessa: "
          f"{'gl-ok' in ids} (atteso si){RESET}")

    # ------------------------------------------- G. T-60 / CIRCUIT BREAKERS ---
    _head("G. STRATEGIA T-60 — finestra esecutiva + circuit breakers")
    _reset_state()
    # L'alert di emergenza del CB2 farebbe un POST Telegram REALE: qui lo
    # intercettiamo (la diagnostica non deve mandare messaggi a nessuno).
    auto_bet._t60_emergency_alert = lambda reason: print(
        f"  {DIM}│ (alert CB2 intercettato) {reason}{RESET}")
    auto_bet._execution_mode = lambda allow_sim=True: "sim"

    # G1 — FINESTRA T-60: il giro esecutivo ordina SOLO dentro la finestra
    # esecutiva (T-120..T-15 dal 30/09: la chiusura e' scesa da T-50 per
    # rendere ordinabili i ritentativi tardivi dell'oracolo eSports).
    _seed("t60-nofin", "Osasuna", 1.65, 0.58, 0.07)   # kickoff a +3h
    auto_bet.T60_EXECUTION_ONLY = True
    mark = len(_RECORDS)
    fuori = auto_bet.run_today_bets(stake_eur=1.0)
    _print_logs(mark)
    ok_g1 = fuori == []
    print(f"  {GREEN if ok_g1 else RED}→ kickoff a +3h con T-60 attivo: "
          f"ordini {len(fuori)} (atteso 0: solo scansione){RESET}")
    # Controprova: la stessa riga ordina se la finestra T-60 e' disattivata.
    # (Si verifica la PRESENZA di t60-nofin, non il totale: il ledger della
    # diagnostica contiene anche le righe degli scenari A-F, anch'esse
    # giocabili a orizzonte aperto.)
    auto_bet.T60_EXECUTION_ONLY = False
    dentro = auto_bet.run_today_bets(stake_eur=1.0)
    ok_g2 = any(p.get("match_id") == "t60-nofin" for p in dentro)
    print(f"  {DIM}controprova con T60_EXECUTION_ONLY=0: t60-nofin ordinata: "
          f"{'si' if ok_g2 else 'NO'} ({len(dentro)} ordini totali nel "
          f"palinsesto della diagnostica){RESET}")
    auto_bet.T60_EXECUTION_ONLY = True

    # G2 — CB1 DINAMICO: nessun calcolo supera il tetto per ordine (12%).
    cap = auto_bet.order_ceiling(10_000.0)
    stake_big = auto_bet.t60_stake(10_000.0, mode="live")
    ok_g3 = (cap > 0 and stake_big <= cap + 1e-9
             and cap < 10_000.0 * 0.30)   # morde lui, non il cap di correlazione
    print(f"  {GREEN if ok_g3 else RED}→ CB1 dinamico: bankroll 10000 USDC -> "
          f"stake {stake_big:.2f} (tetto 12% = {cap:.2f}, Kelly "
          f"sovrascritto){RESET}")
    _ok, _contract, errs = auto_bet.validate_order_payload({
        "signal_id": "s", "record_id": "r", "match_id": "g-cb1",
        "league": ALLOWED_LEAGUE, "market": "1X2", "outcome": "1",
        "home": "Osasuna", "away": "Getafe", "price": 1.65, "stake": 5.0,
        "verdict": "approve", "mode": "live", "provider": "sxbet",
        "kickoff": _start(),
        "created_at": datetime.now(timezone.utc).isoformat()}, bankroll=34.0)
    ok_g4 = (not _ok) and any("circuit breaker" in e for e in errs)
    print(f"  {GREEN if ok_g4 else RED}→ CB3: payload con stake 5.00 "
          f"(tetto 4.08 su 34) -> scartato: "
          f"{'si' if ok_g4 else 'NO'} ({'; '.join(errs)[:80]}){RESET}")

    # G3 — CB2 KILL SWITCH PATRIMONIALE: equity wallet <= 30 USDC = arresto.
    armed = auto_bet.t60_check_wallet_kill(25.0)
    _seed("t60-kill", "Osasuna", 1.65, 0.58, 0.07)
    mark = len(_RECORDS)
    bloccato = auto_bet.run_today_bets(stake_eur=1.0)
    _print_logs(mark)
    ok_g5 = (armed is True and bloccato == []
             and auto_bet.t60_kill_switch_status().get("triggered") is True)
    print(f"  {GREEN if ok_g5 else RED}→ CB2: equity 25.00 <= soglia "
          f"{auto_bet.T60_KILL_WALLET_USDC:.2f} -> flag armato e "
          f"{len(bloccato)} ordini (atteso 0){RESET}")
    auto_bet.t60_clear_kill()
    ok_g = ok_g1 and ok_g2 and ok_g3 and ok_g4 and ok_g5

    # ------------------------------- H. RECINTO DI ESPOSIZIONE APERTA (28/09) ---
    _head("H. RECINTO ESPOSIZIONE 40% — l'Advisor respinge i nuovi piani")
    from agents.advisor_agent import AdvisorAgent   # noqa: E402
    _reset_state()
    conn = tracker._get_conn()
    # Board PULITO: gli scenari precedenti hanno lasciato righe giocabili in
    # Premier League e i cap di portafoglio (correlazione 30% / esposizione
    # 40%) ridurrebbero gli stake, mascherando il recinto che qui si misura.
    conn.execute("DELETE FROM bets")
    conn.execute("DELETE FROM predictions")
    conn.execute("DELETE FROM matches")
    conn.commit()
    conn.close()
    _seed("h-valid", "Osasuna", 1.65, 0.58, 0.07)
    equity_h = 33.55                                  # equity reale del wallet
    # Stake per ordine NEL MONDO NUOVO (04/10): il cap dinamico 12% del
    # bankroll (4.03 su 33.55), non piu' l'importo fisso 1.50 del 28/09.
    per_order_h = round(auto_bet.order_ceiling(equity_h), 2)
    cap_h = round(equity_h * auto_bet.OPEN_EXPOSURE_CAP_PCT, 2)
    # Quanti ordini entrano PRIMA di saturare il 40% (13.42): 3 x 4.03 = 12.09,
    # il quarto porterebbe a 16.12 > 13.42 e va respinto.
    n_pieni = int(cap_h // per_order_h)
    auto_bet._execution_mode = lambda allow_sim=True: "live"
    auto_bet._live_wallet_snapshot = lambda: {
        "available": equity_h, "exposure": 0.0, "equity": equity_h}
    calls_h = {"fill": 0}

    def _fill_h(pick, stake, floor):  # noqa: ANN001
        calls_h["fill"] += 1
        return {"ok": True, "market_id": "m", "selection_id": 1,
                "bet_id": "b", "status": "FULLY_FILLED",
                "price": floor, "stake": stake}

    auto_bet._live_fill = _fill_h
    auto_bet.T60_EXECUTION_ONLY = False
    for i in range(n_pieni):   # ordini aperti finche' il recinto regge
        tracker.save_bet(match_id=f"h{i}", mercato="1X2", esito="1",
                         market_id="0xm", selection_id=1, price=1.65,
                         stake=per_order_h, mode="live", status="FULLY_FILLED",
                         bet_id=f"0xb{i}")
    stato_h = auto_bet.open_exposure_status(equity_h)
    allow_h = auto_bet.exposure_allows(equity_h, per_order_h)
    gate_h = AdvisorAgent().exposure_gate(bankroll=equity_h, stake=per_order_h)
    mark = len(_RECORDS)
    placed_h = auto_bet.run_today_bets(stake_eur=5.0)
    _print_logs(mark)
    ok_h1 = (stato_h["open_stake"] == round(n_pieni * per_order_h, 2)
             and stato_h["count"] == n_pieni and stato_h["cap"] == cap_h)
    ok_h2 = (allow_h["allowed"] is False
             and gate_h.resolved is False
             and gate_h.original_reason == "exposure_cap")
    ok_h3 = placed_h == [] and calls_h["fill"] == 0
    print(f"  {GREEN if ok_h1 else RED}→ ordini aperti: {stato_h['count']} "
          f"per {stato_h['open_stake']:.2f}/{cap_h:.2f} USDC "
          f"({per_order_h:.2f} a ordine, cap dinamico 12%){RESET}")
    print(f"  {GREEN if ok_h2 else RED}→ Advisor: nuovo stake "
          f"{per_order_h:.2f} ammesso: {allow_h['allowed']} (atteso no: "
          f"proiezione {allow_h['projected']:.2f} oltre il tetto "
          f"{cap_h:.2f}){RESET}")
    print(f"  {GREEN if ok_h3 else RED}→ ordini inviati al provider: "
          f"{calls_h['fill']} (atteso 0){RESET}")
    auto_bet.T60_EXECUTION_ONLY = True
    auto_bet._execution_mode = lambda allow_sim=True: "sim"
    ok_h = ok_h1 and ok_h2 and ok_h3

    # ------------------------------------------------------------- ESITO ------
    _head("ESITO")
    all_ok = (ok_a and ok_b and ok_c and ok_d and ok_e and ok_f and ok_g
              and ok_h)
    for name, ok in (("A kill-switch OFF", ok_a),
                     ("B stop-loss 24h", ok_b),
                     ("C Kelly aggressivo k=0.65 / cap 12% / ticket 2.00", ok_c),
                     ("D filtro prezzo/oracolo", ok_d),
                     ("E liquidita' SX", ok_e),
                     ("F lega strategia", ok_f),
                     ("G circuit breakers T-60", ok_g),
                     ("H recinto esposizione 40%", ok_h)):
        print(f"  {GREEN + '✅' if ok else RED + '❌'} {name}{RESET}")
    print(f"\n  {BOLD}{GREEN + 'TUTTI I GUARDRAIL BLOCCANO' if all_ok else RED + 'QUALCOSA NON BLOCCA'}{RESET}\n")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
