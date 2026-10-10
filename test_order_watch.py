"""Test della verifica ordini reali (order_watch.py).

Direttiva 28/09/2026: stake fisso 1.50 USDC per ordine e recinto del 40%
sull'esposizione APERTA (a equity 33.55 il tetto e' 13.42 -> 8 ordini da 1.50).
Questi test sono il controllo che si ripete: gli invarianti sul denaro vanno
verificati da uno strumento, non a mano una volta.

Copre: stake esatto, tetto per-ordine inviolabile, replay dell'esposizione con
rilascio al settlement, tetto non verificabile DICHIARATO (mai spacciato per
verificato), ordine senza id, sorveglianza dei nuovi ordini e le garanzie
(sola lettura, nessuna rete, nessun ordine, nessuna soglia duplicata).

Tutti i test sono OFFLINE: ledger SQLite temporaneo, storico equity nella tmp,
nessuna rete, nessun provider.
"""

import json
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import auto_bet
import order_watch


@pytest.fixture()
def tmp_env(tmp_path, monkeypatch):
    """Ledger temporaneo + storico equity nella tmp (mai il volume)."""
    db = tmp_path / "quotaverace.db"
    hist = tmp_path / "bankroll_history.json"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE bets (
        id INTEGER PRIMARY KEY AUTOINCREMENT, match_id TEXT, mercato TEXT,
        esito TEXT, market_id TEXT, selection_id TEXT, price REAL, stake REAL,
        mode TEXT, status TEXT, bet_id TEXT, esito_finale TEXT, profit REAL,
        created_at TEXT, settled_at TEXT)""")
    conn.commit()
    conn.close()
    monkeypatch.setattr(order_watch, "current_equity", lambda: 33.5535)
    # Lo stake fisso e' ACCESO qui in modo esplicito: il conftest lo isola per
    # i test che misurano altre grandezze (cap, liquidezza, wallet), mentre
    # questo file verifica proprio la direttiva 1.50 USDC.
    monkeypatch.setenv("ORDER_FIXED_STAKE_USDC", "1.50")
    monkeypatch.setattr(auto_bet, "FIXED_STAKE_USDC", 1.50)
    return {"db": db, "hist": hist}


def _bet(db, stake=1.50, mode="live", status="FULLY_FILLED", bet_id="sx-1",
         finale=None, created="2026-09-28T10:00:00", settled=None,
         match="m1", esito="1"):
    conn = sqlite3.connect(db)
    cur = conn.execute(
        "INSERT INTO bets (match_id, mercato, esito, price, stake, mode, status, "
        "bet_id, esito_finale, profit, created_at, settled_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (match, "1X2", esito, 1.65, stake, mode, status, bet_id, finale,
         1.0 if finale == "won" else None, created, settled))
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def _hist(path, samples):
    path.write_text(json.dumps({"basis_key": "live_equity", "samples": samples}))


def _iso(offset_min=0):
    base = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    return (base + timedelta(minutes=offset_min)).isoformat()


# ---------------------------------------------------------------------------
# A. Stake esatto
# ---------------------------------------------------------------------------


class TestStake:
    def test_ledger_vuoto_nessuna_violazione(self, tmp_env):
        data = order_watch.audit(tmp_env["db"], history_path=tmp_env["hist"])
        assert data["verdict"] == "ok"
        assert data["orders"]["total"] == 0
        assert data["expected_stake"] == 1.50

    def test_ordine_con_stake_esatto_e_conforme(self, tmp_env):
        _bet(tmp_env["db"], stake=1.50)
        data = order_watch.audit(tmp_env["db"], history_path=tmp_env["hist"])
        assert data["verdict"] == "ok" and data["violations"] == []
        assert data["orders"]["live_dopo_direttiva"] == 1

    def test_stake_diverso_e_violazione(self, tmp_env):
        _bet(tmp_env["db"], stake=1.49)
        data = order_watch.audit(tmp_env["db"], history_path=tmp_env["hist"])
        assert data["verdict"] == "violazioni"
        assert data["violations"][0]["kind"] == order_watch.VIOL_STAKE_MISMATCH

    def test_stake_oltre_il_tetto_e_violazione_sempre(self, tmp_env):
        # Anche su una riga PRECEDENTE alla direttiva: il tetto per-ordine non
        # e' negoziabile, non e' una regola di strategia. Col cap DINAMICO
        # (12% di 33.5535 = 4.03) serve uno stake sopra quella soglia.
        _bet(tmp_env["db"], stake=5.00, created="2026-09-10T10:00:00")
        data = order_watch.audit(tmp_env["db"], history_path=tmp_env["hist"])
        assert data["violations"][0]["kind"] == order_watch.VIOL_STAKE_OVER_MAX

    def test_stake_predirettiva_dichiarato_non_giudicato(self, tmp_env):
        _bet(tmp_env["db"], stake=1.00, created="2026-09-10T10:00:00")
        data = order_watch.audit(tmp_env["db"], history_path=tmp_env["hist"])
        assert data["verdict"] == "ok"
        assert any(d["kind"] == "stake_predirective" for d in data["declared"])

    def test_sim_non_e_giudicata_sullo_stake(self, tmp_env):
        _bet(tmp_env["db"], stake=0.40, mode="sim")
        data = order_watch.audit(tmp_env["db"], history_path=tmp_env["hist"])
        assert data["verdict"] == "ok"
        assert data["orders"]["sim"] == 1 and data["orders"]["live"] == 0

    def test_ordine_senza_id_e_violazione(self, tmp_env):
        _bet(tmp_env["db"], bet_id=None)
        data = order_watch.audit(tmp_env["db"], history_path=tmp_env["hist"])
        assert data["violations"][0]["kind"] == order_watch.VIOL_MISSING_BET_ID

    def test_kelly_aggressivo_cambia_la_regola_non_sospende_il_controllo(
            self, tmp_env, monkeypatch):
        """Dal 04/10/2026 il default e' il Kelly **dinamico** (k 0.15-0.25):
        con lo stake fisso spento un importo diverso da 1.50 NON e' una
        violazione, ma il controllo NON e' sospeso — valgono il cap dinamico
        (12%) e il ticket minimo del motore (**1.00**). Un ordine sotto il
        ticket e' un percorso che ha aggirato il motore."""
        monkeypatch.setattr(auto_bet, "FIXED_STAKE_USDC", 0.0)
        _bet(tmp_env["db"], stake=3.00)          # entro il cap, sopra il ticket
        data = order_watch.audit(tmp_env["db"], history_path=tmp_env["hist"])
        assert data["fixed_active"] is False
        assert data["verdict"] == "ok"
        _bet(tmp_env["db"], stake=0.50, match="m2")   # sotto il ticket 1.00
        data = order_watch.audit(tmp_env["db"], history_path=tmp_env["hist"])
        kinds = [v["kind"] for v in data["violations"]]
        assert order_watch.VIOL_STAKE_UNDER_TICKET in kinds

    def test_tetto_per_ordine_vale_anche_con_stake_fisso_spento(self, tmp_env,
                                                               monkeypatch):
        monkeypatch.setattr(auto_bet, "FIXED_STAKE_USDC", 0.0)
        _bet(tmp_env["db"], stake=5.00)
        data = order_watch.audit(tmp_env["db"], history_path=tmp_env["hist"])
        assert data["violations"][0]["kind"] == order_watch.VIOL_STAKE_OVER_MAX
        assert "tetto per-ordine" in data["violations"][0]["detail"]

    def test_valore_atteso_viene_da_auto_bet(self, tmp_env):
        assert order_watch.expected_stake() == pytest.approx(1.50)
        assert order_watch.cap_pct() == pytest.approx(auto_bet.OPEN_EXPOSURE_CAP_PCT)


# ---------------------------------------------------------------------------
# B. Recinto del 40% (replay dell'esposizione aperta)
# ---------------------------------------------------------------------------


class TestRecinto:
    def test_otto_ordini_da_150_entrano(self, tmp_env):
        for i in range(8):
            _bet(tmp_env["db"], match=f"m{i}", created=_iso(i))
        _hist(tmp_env["hist"], [[_iso(-5), 33.5535]])
        data = order_watch.audit(tmp_env["db"], history_path=tmp_env["hist"])
        assert data["verdict"] == "ok"
        assert data["max_open"]["stake"] == pytest.approx(12.00)
        assert data["max_open"]["count"] == 8
        assert data["max_open"]["cap"] == pytest.approx(13.4214)
        assert data["max_open"]["source"] == "sample"

    def test_il_nono_ordine_sfonda_il_recinto(self, tmp_env):
        for i in range(9):
            _bet(tmp_env["db"], match=f"m{i}", created=_iso(i))
        _hist(tmp_env["hist"], [[_iso(-5), 33.5535]])
        data = order_watch.audit(tmp_env["db"], history_path=tmp_env["hist"])
        assert data["verdict"] == "violazioni"
        over = [v for v in data["violations"]
                if v["kind"] == order_watch.VIOL_EXPOSURE_OVER_CAP]
        assert len(over) == 1 and over[0]["id"] == 9
        assert "13.50" in over[0]["detail"] and "13.42" in over[0]["detail"]

    def test_il_settlement_libera_il_posto(self, tmp_env):
        # 8 ordini aperti, il primo si chiude, poi entra il nono: conforme.
        _bet(tmp_env["db"], match="m0", created=_iso(0), finale="won",
             settled=_iso(60))
        for i in range(1, 8):
            _bet(tmp_env["db"], match=f"m{i}", created=_iso(i))
        _bet(tmp_env["db"], match="m9", created=_iso(70))
        _hist(tmp_env["hist"], [[_iso(-5), 33.5535]])
        data = order_watch.audit(tmp_env["db"], history_path=tmp_env["hist"])
        assert data["verdict"] == "ok"
        assert data["max_open"]["stake"] == pytest.approx(12.00)

    def test_compounding_col_capitale_aggiornato(self, tmp_env):
        # Equity 100 -> tetto 40: entrano 26 ordini da 1.50 senza violazioni.
        _hist(tmp_env["hist"], [[_iso(-5), 100.0]])
        for i in range(26):
            _bet(tmp_env["db"], match=f"m{i}", created=_iso(i))
        data = order_watch.audit(tmp_env["db"], history_path=tmp_env["hist"])
        assert data["verdict"] == "ok"
        assert data["max_open"]["cap"] == pytest.approx(40.0)

    def test_tetto_non_verificabile_e_dichiarato(self, tmp_env, monkeypatch):
        monkeypatch.setattr(order_watch, "current_equity", lambda: None)
        _bet(tmp_env["db"], match="m0")
        data = order_watch.audit(tmp_env["db"], history_path=tmp_env["hist"])
        assert data["verdict"] == "ok"
        assert any(d["kind"] == "cap_unverifiable" for d in data["declared"])

    def test_tetto_stimato_dichiarato_non_spacciato_per_verificato(self, tmp_env):
        _bet(tmp_env["db"], match="m0")
        data = order_watch.audit(tmp_env["db"], history_path=tmp_env["hist"])
        assert any(d["kind"] == "cap_estimated" for d in data["declared"])
        assert data["max_open"]["source"] == "now"

    def test_campione_storico_precedente_usato(self, tmp_env):
        _hist(tmp_env["hist"], [[_iso(-120), 50.0], [_iso(-60), 60.0]])
        _bet(tmp_env["db"], match="m0", created=_iso(0))
        data = order_watch.audit(tmp_env["db"], history_path=tmp_env["hist"])
        assert data["max_open"]["cap"] == pytest.approx(60.0 * 0.40)
        assert data["max_open"]["source"] == "sample"

    def test_equity_iniettabile(self, tmp_env):
        _bet(tmp_env["db"], match="m0")
        data = order_watch.audit(tmp_env["db"], history_path=tmp_env["hist"],
                                 equity=250.0)
        assert data["max_open"]["cap"] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# C. Report, CLI e sorveglianza
# ---------------------------------------------------------------------------


class TestReportESorveglianza:
    def test_report_conforme(self, tmp_env):
        _bet(tmp_env["db"], stake=1.50)
        text = order_watch.format_report(order_watch.audit(
            tmp_env["db"], history_path=tmp_env["hist"]))
        assert "VERIFICA ORDINI REALI" in text and "✅" in text
        assert "1.5 USDC" in text

    def test_report_con_violazioni(self, tmp_env):
        _bet(tmp_env["db"], stake=5.0)      # oltre il cap dinamico (4.03)
        text = order_watch.format_report(order_watch.audit(
            tmp_env["db"], history_path=tmp_env["hist"]))
        assert "❌" in text and "tetto" in text

    def test_cli_json_esce_1_su_violazioni(self, tmp_env, capsys, monkeypatch):
        _bet(tmp_env["db"], stake=2.5)
        monkeypatch.setattr(order_watch, "bankroll_history", lambda p=None: [])
        rc = order_watch.main(["--json", "--db", str(tmp_env["db"]), "--equity", "33.55"])
        assert rc == 1
        assert json.loads(capsys.readouterr().out)["verdict"] == "violazioni"

    def test_cli_testo_esce_0_se_conforme(self, tmp_env, capsys, monkeypatch):
        monkeypatch.setattr(order_watch, "bankroll_history", lambda p=None: [])
        assert order_watch.main(["--db", str(tmp_env["db"]), "--equity", "33.55"]) == 0
        assert "VERIFICA ORDINI REALI" in capsys.readouterr().out

    def test_last_live_id_su_db_assente(self, tmp_path):
        assert order_watch.last_live_id(tmp_path / "non-esiste.db") == 0

    def test_watch_rileva_il_primo_ordine(self, tmp_env, capsys):
        seen = []

        def fake_sleep(_seconds):
            if not seen:
                _bet(tmp_env["db"], stake=1.50, match="nuovo")
        res = order_watch.watch(0.02, 1, tmp_env["db"], sleep=fake_sleep,
                                on_new=lambda rows, rep: seen.append(rows))
        assert len(seen) == 1 and seen[0][0]["match_id"] == "nuovo"
        assert res["new_orders"][0]["stake"] == pytest.approx(1.50)

    def test_watch_non_riporta_i_vecchi(self, tmp_env):
        _bet(tmp_env["db"], stake=1.50, match="vecchio")
        res = order_watch.watch(0.0, 1, tmp_env["db"], sleep=lambda s: None)
        assert res["new_orders"] == []


# ---------------------------------------------------------------------------
# D. Garanzie
# ---------------------------------------------------------------------------


class TestGaranzie:
    def _src(self):
        return Path("order_watch.py").read_text()

    def test_nessuna_scrittura(self):
        src = self._src()
        for banned in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP TABLE"):
            assert banned not in src

    def test_connessione_solo_lettura(self):
        src = self._src()
        assert "mode=ro" in src
        assert src.count("sqlite3.connect") == src.count("sqlite3.connect(f\"file:")

    def test_nessuna_rete_ne_ordini(self):
        src = self._src()
        for banned in ("import requests", "import aiohttp", "place_limit_order",
                       "execution_engine", "_live_fill", "resolve_market_for"):
            assert banned not in src

    def test_job_e_comando_registrati_nel_bot(self):
        """La sorveglianza deve esistere in PRODUZIONE, non solo in locale."""
        import asyncio
        src = Path("bot.py").read_text()
        assert "order_watch_job, interval=1800" in src
        assert 'CommandHandler("ordini", cmd_ordini)' in src
        import bot
        assert asyncio.iscoroutinefunction(bot.order_watch_job)
        assert asyncio.iscoroutinefunction(bot.cmd_ordini)

    def test_job_logga_sempre_e_notifica_solo_su_violazioni(self, tmp_env,
                                                           monkeypatch):
        import asyncio
        import bot

        monkeypatch.setattr(order_watch, "audit", lambda *a, **k: {
            "verdict": "ok", "orders": {"live": 1, "live_dopo_direttiva": 1,
                                        "aperte": 1}, "esposizione_corrente": 1.5,
            "violations": [], "declared": []})
        sent = []

        async def fake_send(ctx, text):
            sent.append(text)

        monkeypatch.setattr(bot, "_send_report_to_recipients", fake_send)
        # Contesto non-None: e' il percorso del job vero (con `None` il job
        # logga soltanto, ed e' un altro ramo).
        ctx = object()
        asyncio.run(bot.order_watch_job(ctx))
        assert sent == []                       # nessuna violazione: silenzio

        monkeypatch.setattr(order_watch, "audit", lambda *a, **k: {
            "verdict": "violazioni", "orders": {"live": 2},
            "esposizione_corrente": 3.0, "declared": [],
            "violations": [{"id": 2, "label": "esposizione aperta oltre il 40%",
                            "detail": "13.50 > 13.42"}]})
        monkeypatch.setattr("tracker.is_notified", lambda *a, **k: False)
        monkeypatch.setattr("tracker.mark_notified", lambda *a, **k: None)
        asyncio.run(bot.order_watch_job(ctx))
        assert len(sent) == 1 and "🚨" in sent[0]
        assert "esposizione aperta oltre il 40%" in sent[0]

    def test_job_senza_contesto_logga_e_basta(self, tmp_env, monkeypatch, caplog):
        """Con `context=None` il job non prova a notificare (ramo diagnostico)."""
        import asyncio
        import bot
        monkeypatch.setattr(order_watch, "audit", lambda *a, **k: {
            "verdict": "violazioni", "orders": {"live": 1},
            "esposizione_corrente": 3.0, "declared": [],
            "violations": [{"id": 1, "label": "stake diverso", "detail": "x"}]})
        asyncio.run(bot.order_watch_job(None))       # nessuna eccezione

    def test_job_non_esplode_se_il_ledger_e_rotto(self, monkeypatch, caplog):
        import asyncio
        import bot

        def boom(*a, **k):
            raise RuntimeError("ledger rotto")
        monkeypatch.setattr(order_watch, "audit", boom)
        asyncio.run(bot.order_watch_job(None))     # nessuna eccezione

    def test_nessuna_soglia_duplicata(self, monkeypatch):
        """Stake e cap si LEGGONO da `auto_bet`: nessun numero copiato a mano.

        Il controllo e' COMPORTAMENTALE, non testuale: se il valore atteso
        seguisse `auto_bet` al variare della configurazione, allora non esiste
        una seconda copia della soglia nel modulo.
        """
        src = self._src()
        assert "fixed_order_stake()" in src and "OPEN_EXPOSURE_CAP_PCT" in src
        monkeypatch.setattr(auto_bet, "FIXED_STAKE_USDC", 0.75)
        assert order_watch.expected_stake() == pytest.approx(0.75)
        monkeypatch.setattr(auto_bet, "FIXED_STAKE_USDC", 1.50)
        assert order_watch.expected_stake() == pytest.approx(1.50)
        monkeypatch.setattr(auto_bet, "OPEN_EXPOSURE_CAP_PCT", 0.25)
        assert order_watch.cap_pct() == pytest.approx(0.25)


class TestEquityRiconciliata:
    """Fix 10/10/2026: l'audit misura lo STESSO capitale del sizing.

    Il cap per-ordine e il recinto si calcolano sull'equity: se il bot
    dimensiona sull'equity RICONCILIATA (`auto_bet.sizing_equity`) e l'audit
    leggesse quella GREZZA, la verifica segnalerebbe violazioni su ordini
    dimensionati correttamente — e, peggio, NON le segnalerebbe quando il
    capitale e' gonfiato dalla finestra payout/settlement (l'anomalia vera,
    misurata in produzione il 10/10: +5.95 USDC su ~34.8, con tre ordini
    sopra il cap).
    """

    def test_usa_la_riconciliazione(self, monkeypatch):
        monkeypatch.setattr(auto_bet, "_live_wallet_snapshot",
                            lambda: {"available": 40.75, "exposure": 0.0,
                                     "equity": 40.75})
        monkeypatch.setattr(auto_bet, "reconciled_equity",
                            lambda equity, now=None: (30.0, {"applied": True}))
        assert order_watch.current_equity() == pytest.approx(30.0)

    def test_senza_correzione_resta_il_grezzo(self, monkeypatch):
        monkeypatch.setattr(auto_bet, "_live_wallet_snapshot",
                            lambda: {"available": 30.0, "exposure": 0.0,
                                     "equity": 30.0})
        monkeypatch.setattr(auto_bet, "reconciled_equity",
                            lambda equity, now=None: (30.0,
                                                      {"applied": False}))
        assert order_watch.current_equity() == pytest.approx(30.0)

    def test_wallet_illeggibile_resta_none(self, monkeypatch):
        monkeypatch.setattr(auto_bet, "_live_wallet_snapshot", lambda: None)
        assert order_watch.current_equity() is None

    def test_equity_esattamente_zero_e_una_lettura_valida(self, monkeypatch):
        """Regressione: la vecchia `return float(eq) if eq else None` trattava
        lo 0.0 come 'illeggibile' (capitalizzato a zero = wallet prosciugato,
        che e' proprio il caso in cui l'audit serve)."""
        monkeypatch.setattr(auto_bet, "_live_wallet_snapshot",
                            lambda: {"available": 0.0, "exposure": 0.0,
                                     "equity": 0.0})
        monkeypatch.setattr(auto_bet, "reconciled_equity",
                            lambda equity, now=None: (0.0, {"applied": False}))
        assert order_watch.current_equity() == pytest.approx(0.0)
