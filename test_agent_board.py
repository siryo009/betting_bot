"""Test della TABELLA DI COMANDO (`agent_board.py`) e della rotta `/api/agents`.

TUTTO OFFLINE: registro dei cicli in un file temporaneo, `auto_bet` / `tracker` /
`order_watch` sostituiti da funzioni finte dove servono, nessuna rete, nessun
credito, nessun ordine, zero scritture sul volume.

Cosa difendono questi test (il perche' di ognuno):
  - la tabella mostra lo stato del ciclo REALE (ok / saltato / in attesa) e non
    un numero ricopiato: se un agente non e' stato chiamato perche' il ciclo era
    fermo, deve dirlo ("skipped"), non sparire;
  - sicurezza e capitale vengono dalle LORO fonti (auto_bet / order_watch):
    cambiarne una non puo' lasciare la tabella indietro;
  - la lettura e' sola-lettura e leggera: niente ordini, niente rete, e
    `import agent_board` non tira dentro il denaro;
  - un registro rotto o assente e' un `error` DICHIARATO, mai un'eccezione.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import agent_board
import auto_bet
import web_api

def _ago(minutes: float = 5) -> str:
    """Timestamp RELATIVO a `now`.

    Le date fisse sono scadute in silenzio gia' due volte (15/09 e 17/09): qui
    un record "recente" deve restare dentro la finestra di 1 giorno qualunque
    sia il giorno in cui gira la suite.
    """
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def _record(ts: str = "", *, ok: bool = True, blocked: str = "", **over):
    """Un ciclo del Capo nella forma scritta da `chief_shadow_cycle`."""
    base = (datetime.fromisoformat(ts) if ts
            else datetime.now(timezone.utc) - timedelta(minutes=5))
    ts = base.isoformat()
    rec = {
        "ts": ts,
        "kind": "chief_cycle",
        "bankroll": 21.1,
        "mode": "live",
        "ok": ok,
        "blocked_reason": blocked,
        "started_at": ts,
        "finished_at": (base + timedelta(seconds=5)).isoformat(),
        "market": {
            "gate": {"allowed": True, "reason": "OK", "detail": "feed validato"},
            "signals": 3,
            "snapshot": {"accepted": 42, "rejected": 0},
            "intel": [{"match_id": "m1"}],
        },
        "strategy": {"total": 3, "playable": 2, "rejected": 1, "unclassified": 0},
        "analysis": {"signals": 2, "steam_moves": 1, "juice_anomalies": 0,
                     "stale": 0},
        "brain": {"trades": 1, "validated": 1, "rejected": 0, "scaled": 0,
                  "blocked": 0},
        "finance": {"plans": 3, "approved": 1, "review": 1, "rejected": 1},
        "sizing": {"trades": 1, "sized": 1, "executable": 1, "skipped": 0},
        "advisor": [],
        "execution": {"dispatched": 2, "executed": 1, "shadow": True,
                      "resting_open": 0, "resting_stake": 0.0},
        "exposure": {"open_stake": 2.0, "cap": 10.0},
        "resting": {},
    }
    rec.update(over)
    return rec


def _write(path: Path, records) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for rec in records:
            handle.write(json.dumps(rec) + "\n")
    return path


@pytest.fixture
def log(tmp_path) -> Path:
    return tmp_path / "chief_cycles.jsonl"


@pytest.fixture
def quiet_board(monkeypatch):
    """Neutralizza le tre letture laterali (wallet/ledger) nei test di struttura."""
    monkeypatch.setattr(agent_board, "safety_snapshot",
                        lambda: {"checks": [], "blocked_by": [], "precedence": []})
    monkeypatch.setattr(agent_board, "capital_snapshot",
                        lambda: {"available": False})
    monkeypatch.setattr(agent_board, "flow_snapshot", lambda: {})


# ---------------------------------------------------------------------------
# 1) Lettura del registro
# ---------------------------------------------------------------------------
class TestLetturaRegistro:
    def test_legge_e_ordina(self, log):
        _write(log, [_record(), _record(blocked="x", ok=False)])
        out = agent_board.read_cycles(7, path=log)
        assert len(out["records"]) == 2
        assert out["records"][-1]["ok"] is False
        assert out["error"] == ""

    def test_filtra_per_finestra(self, log):
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        _write(log, [_record(old), _record(_ago(60))])
        out = agent_board.read_cycles(2, path=log)
        assert len(out["records"]) == 1

    def test_riga_corrotta_ignorata(self, log):
        log.write_text('{"ok": true}\nNON JSON\n', encoding="utf-8")
        out = agent_board.read_cycles(1, path=log)
        assert len(out["records"]) == 1
        assert out["error"] == ""

    def test_file_assente_non_e_errore(self, tmp_path):
        out = agent_board.read_cycles(1, path=tmp_path / "manca.jsonl")
        assert out["records"] == []
        assert out["total"] == 0

    def test_record_senza_data_resta_incluso(self, log):
        """Una data rotta non deve rendere la tabella vuota (fail-open)."""
        _write(log, [{"ok": True, "market": {"signals": 1}}])
        out = agent_board.read_cycles(1, path=log)
        assert len(out["records"]) == 1

    def test_window_days_dall_env(self, monkeypatch):
        monkeypatch.setenv("AGENT_BOARD_DAYS", "3")
        assert agent_board.window_days() == 3.0
        monkeypatch.setenv("AGENT_BOARD_DAYS", "0")
        assert agent_board.window_days() == agent_board.DEFAULT_DAYS
        monkeypatch.setenv("AGENT_BOARD_DAYS", "abc")
        assert agent_board.window_days() == agent_board.DEFAULT_DAYS


# ---------------------------------------------------------------------------
# 2) Le card degli agenti
# ---------------------------------------------------------------------------
class TestCard:
    def test_ordine_della_catena(self, log):
        _write(log, [_record()])
        cards = agent_board.agent_cards(agent_board.read_cycles(1, path=log)["records"])
        assert [c["id"] for c in cards] == list(agent_board.PIPELINE)
        assert len(cards) == 9

    def test_ogni_agente_dichiara_il_contratto(self, log):
        _write(log, [_record()])
        cards = agent_board.agent_cards(agent_board.read_cycles(1, path=log)["records"])
        for card in cards:
            assert card["name"] and card["role"] and card["isolation"]
            assert card["consumes"] and card["produces"]

    def test_status_ok_su_chi_ha_prodotto(self, log):
        _write(log, [_record()])
        records = agent_board.read_cycles(1, path=log)["records"]
        cards = {c["id"]: c for c in agent_board.agent_cards(records)}
        assert cards["data"]["status"] == "ok"
        assert cards["finance"]["status"] == "ok"
        assert cards["execution"]["status"] == "ok"

    def test_ciclo_fermo_rende_skipped_i_valle(self, log):
        rec = _record(ok=False, blocked="market_gate: FEED_STALE")
        rec["market"] = {}
        rec["strategy"] = {}
        _write(log, [rec])
        records = agent_board.read_cycles(1, path=log)["records"]
        cards = {c["id"]: c for c in agent_board.agent_cards(records)}
        assert cards["data"]["status"] == "skipped"
        assert "market_gate" in cards["strategy"]["detail"]

    def test_advisor_vuoto_e_in_attesa(self, log):
        _write(log, [_record()])
        records = agent_board.read_cycles(1, path=log)["records"]
        cards = {c["id"]: c for c in agent_board.agent_cards(records)}
        assert cards["advisor"]["status"] == "idle"
        assert "non interpellato" in cards["advisor"]["detail"]

    def test_advisor_con_consigli(self, log):
        rec = _record(advisor=[{"override_kind": "reduced_stake",
                                "escalate_review": False},
                               {"override_kind": "context", "escalate_review": True}])
        _write(log, [rec])
        records = agent_board.read_cycles(1, path=log)["records"]
        card = {c["id"]: c for c in agent_board.agent_cards(records)}["advisor"]
        assert card["status"] == "ok"
        metrics = {m["key"]: m for m in card["metrics"]}
        assert metrics["advice"]["value"] == 2
        assert metrics["escalations"]["value"] == 1
        assert metrics["kinds"]["value"] == {"reduced_stake": 1, "context": 1}

    def test_sezione_con_errore(self, log):
        rec = _record()
        rec["brain"] = {"error": "esploso"}
        _write(log, [rec])
        records = agent_board.read_cycles(1, path=log)["records"]
        card = {c["id"]: c for c in agent_board.agent_cards(records)}["brain"]
        assert card["status"] == "error"
        assert "esploso" in card["detail"]

    def test_nessun_ciclo_tutti_sconosciuti(self, log):
        cards = agent_board.agent_cards([])
        assert {c["status"] for c in cards} == {"unknown"}
        assert all(c["detail"] for c in cards)
        assert all(c["window"]["cycles"] == 0 for c in cards)

    def test_conteggio_finestra(self, log):
        _write(log, [_record(), _record(), _record(blocked="stop", ok=False)])
        records = agent_board.read_cycles(1, path=log)["records"]
        cards = {c["id"]: c for c in agent_board.agent_cards(records)}
        assert cards["data"]["window"]["cycles"] == 3
        assert cards["data"]["window"]["with_output"] == 3


# ---------------------------------------------------------------------------
# 3) Indicatori (dalle chiavi vere dei contratti)
# ---------------------------------------------------------------------------
class TestMetriche:
    def _metrics(self, records, agent_id):
        cards = {c["id"]: c for c in agent_board.agent_cards(records)}
        return {m["key"]: m for m in cards[agent_id]["metrics"]}

    def test_chief_metriche_con_durata(self, log):
        _write(log, [_record()])
        m = self._metrics(agent_board.read_cycles(1, path=log)["records"], "chief")
        assert m["bankroll"]["value"] == 21.1
        assert m["mode"]["value"] == "live"
        assert m["duration_ms"]["value"] == 5000.0
        assert m["ok"]["tone"] == "good"

    def test_data_feed_e_quote(self, log):
        _write(log, [_record()])
        m = self._metrics(agent_board.read_cycles(1, path=log)["records"], "data")
        assert m["signals"]["value"] == 3
        assert m["feed"]["value"] == "validato"
        assert m["feed"]["tone"] == "good"
        assert m["accepted"]["value"] == 42
        assert m["intel"]["value"] == 1

    def test_data_feed_bloccato_e_bad(self, log):
        rec = _record()
        rec["market"]["gate"] = {"allowed": False, "reason": "FEED_STALE"}
        _write(log, [rec])
        m = self._metrics(agent_board.read_cycles(1, path=log)["records"], "data")
        assert m["feed"]["value"] == "FEED_STALE"
        assert m["feed"]["tone"] == "bad"

    def test_brain_bloccato_e_warn(self, log):
        rec = _record()
        rec["brain"]["blocked"] = 2
        _write(log, [rec])
        m = self._metrics(agent_board.read_cycles(1, path=log)["records"], "brain")
        assert m["blocked"]["value"] == 2
        assert m["blocked"]["tone"] == "warn"

    def test_execution_shadow(self, log):
        _write(log, [_record()])
        m = self._metrics(agent_board.read_cycles(1, path=log)["records"], "execution")
        assert m["shadow"]["value"] == "shadow"
        assert m["executed"]["tone"] == "good"

    def test_valore_non_numerico_diventa_none(self, log):
        rec = _record()
        rec["market"]["signals"] = "molti"
        _write(log, [rec])
        m = self._metrics(agent_board.read_cycles(1, path=log)["records"], "data")
        assert m["signals"]["value"] is None

    def test_booleano_non_e_un_conteggio(self, log):
        # `_num` esclude i bool: una spunta non e' un numero in tabella.
        assert agent_board._num(True) is None
        assert agent_board._num(3) == 3.0
        assert agent_board._num("3") is None


# ---------------------------------------------------------------------------
# 4) Sicurezza (dalle funzioni che la governano)
# ---------------------------------------------------------------------------
class TestSicurezza:
    @pytest.fixture
    def clear_all(self, monkeypatch):
        monkeypatch.setattr(auto_bet, "kill_switch_status",
                            lambda: {"effective": "live", "provider_ready": True})
        monkeypatch.setattr(auto_bet, "daily_stop_status",
                            lambda: {"stopped": False, "loss_pct": 5.0})
        monkeypatch.setattr(auto_bet, "weekly_stop_status",
                            lambda: {"stopped": False, "reason": ""})
        monkeypatch.setattr(auto_bet, "market_gate_status",
                            lambda: {"allowed": True, "reason": "OK"})
        monkeypatch.setattr("tracker.settlement_paused", lambda: False)

    def test_nessun_blocco(self, clear_all):
        out = agent_board.safety_snapshot()
        assert out["blocked_by"] == []
        assert {c["id"] for c in out["checks"]} == set(agent_board.BLOCK_PRECEDENCE)

    def test_kill_switch_off_blocca(self, clear_all, monkeypatch):
        monkeypatch.setattr(auto_bet, "kill_switch_status",
                            lambda: {"effective": "off", "provider_ready": True})
        out = agent_board.safety_snapshot()
        assert out["blocked_by"] == ["kill_switch"]
        check = {c["id"]: c for c in out["checks"]}["kill_switch"]
        assert check["status"] == "blocked"

    def test_precedenza_rispettata(self, clear_all, monkeypatch):
        monkeypatch.setattr(auto_bet, "kill_switch_status",
                            lambda: {"effective": "off", "provider_ready": True})
        monkeypatch.setattr(auto_bet, "weekly_stop_status",
                            lambda: {"stopped": True, "reason": "-15%"})
        out = agent_board.safety_snapshot()
        assert out["blocked_by"] == ["kill_switch", "weekly_stop"]

    def test_gate_di_mercato_riporta_il_motivo(self, clear_all, monkeypatch):
        monkeypatch.setattr(auto_bet, "market_gate_status",
                            lambda: {"allowed": False, "reason": "FEED_STALE",
                                     "detail": "cache vecchia"})
        out = agent_board.safety_snapshot()
        assert out["blocked_by"] == ["market_gate"]
        check = {c["id"]: c for c in out["checks"]}["market_gate"]
        assert "cache vecchia" in check["detail"]

    def test_auto_bet_non_importabile(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "auto_bet", None)
        out = agent_board.safety_snapshot()
        assert out["blocked_by"] == []
        assert "auto_bet" in out["error"]


# ---------------------------------------------------------------------------
# 5) Capitale
# ---------------------------------------------------------------------------
class TestCapitale:
    def test_equity_e_recinto(self, monkeypatch):
        monkeypatch.setattr("order_watch.current_equity", lambda: 21.1)
        monkeypatch.setattr(auto_bet, "open_exposure_status",
                            lambda eq, **k: {"open_stake": 2.0, "count": 1,
                                             "cap": 10.55, "pct": 0.095,
                                             "blocked": False})
        monkeypatch.setattr(auto_bet, "order_ceiling", lambda eq=None: 1.0)
        out = agent_board.capital_snapshot()
        assert out["available"] is True
        assert out["equity"] == 21.1
        assert out["open_stake"] == 2.0
        assert out["order_ceiling"] == 1.0
        assert out["hard_cap_per_order"] == auto_bet.ORDER_MAX_STAKE_USDC

    def test_equity_non_leggibile(self, monkeypatch):
        monkeypatch.setattr("order_watch.current_equity", lambda: None)
        out = agent_board.capital_snapshot()
        assert out["available"] is False
        assert out["error"]

    def test_lettura_che_esplode(self, monkeypatch):
        def boom():
            raise RuntimeError("wallet giu'")
        monkeypatch.setattr("order_watch.current_equity", boom)
        out = agent_board.capital_snapshot()
        assert out["available"] is False
        assert "wallet giu'" in out["error"]

    def test_recinto_che_esplode_non_nega_l_equity(self, monkeypatch):
        monkeypatch.setattr("order_watch.current_equity", lambda: 21.1)

        def boom(*a, **k):
            raise RuntimeError("ledger rotto")
        monkeypatch.setattr(auto_bet, "open_exposure_status", boom)
        out = agent_board.capital_snapshot()
        assert out["available"] is True and out["equity"] == 21.1
        assert "ledger rotto" in out["error"]


# ---------------------------------------------------------------------------
# 6) Flusso
# ---------------------------------------------------------------------------
class TestFlusso:
    def test_decisioni_e_referto(self, monkeypatch):
        monkeypatch.setattr("tracker.decision_stats",
                            lambda: {"n": 5, "open": 1, "with_order": 2,
                                     "by_verdict": {"approve": 2},
                                     "by_status": {"validated": 1},
                                     "stake_total": 4.0})
        monkeypatch.setattr("tracker.settlement_residue",
                            lambda **k: {"open": {"bets": 0, "predictions": 12},
                                         "overdue_orphans": 0,
                                         "estimated_credits": 2})
        out = agent_board.flow_snapshot()
        assert out["decisions"]["n"] == 5
        assert out["settlement"]["predictions_open"] == 12
        assert out["settlement"]["overdue_orphans"] == 0

    def test_tracker_che_esplode(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("db rotto")
        monkeypatch.setattr("tracker.decision_stats", boom)
        monkeypatch.setattr("tracker.settlement_residue", boom)
        out = agent_board.flow_snapshot()
        assert "db rotto" in out["decisions"]["error"]
        assert "db rotto" in out["settlement"]["error"]


# ---------------------------------------------------------------------------
# 7) Payload completo + cache
# ---------------------------------------------------------------------------
class TestBoard:
    def test_payload_completo(self, log, quiet_board):
        _write(log, [_record()])
        out = agent_board.board(1, path=log)
        for key in ("generated_at", "window_days", "pipeline", "composer",
                    "last_cycle", "cycles", "agents", "safety", "capital",
                    "flow"):
            assert key in out
        assert out["last_cycle"]["ok"] is True
        assert out["composer"]["name"] == "Chief Orchestrator"
        assert (out["cycles"]["window"]) == 1
        assert out["cached"] is False

    def test_cache_riusa_lo_stesso_payload(self, log, quiet_board):
        _write(log, [_record()])
        agent_board.reset_cache()
        first = agent_board.board(1, path=log)
        second = agent_board.board(1, path=log)
        assert first["cached"] is False
        assert second["cached"] is True
        third = agent_board.board(1, path=log, refresh=True)
        assert third["cached"] is False

    def test_cache_disattivabile(self, log, quiet_board, monkeypatch):
        monkeypatch.setenv("AGENT_BOARD_TTL_S", "0")
        _write(log, [_record()])
        agent_board.reset_cache()
        agent_board.board(1, path=log)
        assert agent_board.board(1, path=log)["cached"] is False

    def test_registro_assente_non_esplode(self, tmp_path, quiet_board):
        out = agent_board.board(1, path=tmp_path / "manca.jsonl")
        assert out["cycles"]["window"] == 0
        assert {c["status"] for c in out["agents"]} == {"unknown"}

    def test_format_report(self, log, quiet_board):
        _write(log, [_record()])
        text = agent_board.format_report(agent_board.board(1, path=log))
        for spec in agent_board.AGENT_SPECS:
            assert spec.name in text
        assert "TABELLA DI COMANDO" in text

    def test_format_report_dati_parziali(self):
        """Anche con un payload vuoto il testo si produce (mai KeyError)."""
        text = agent_board.format_report({"agents": [], "last_cycle": {},
                                          "safety": {}, "capital": {}})
        assert "TABELLA DI COMANDO" in text

    def test_cli_json(self, log, quiet_board, capsys):
        _write(log, [_record()])
        assert agent_board.main(["--days", "1", "--json", "--refresh"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert len(payload["agents"]) == 9


# ---------------------------------------------------------------------------
# 8) Tripwire: sola lettura, zero rete, import leggero, cablaggio
# ---------------------------------------------------------------------------
class TestTripwire:
    def _source(self) -> str:
        return Path(agent_board.__file__).read_text(encoding="utf-8")

    def test_nessun_percorso_d_ordine_o_di_rete(self):
        src = self._source()
        for forbidden in ("place_limit_order", "resolve_market_for", "requests.get",
                          "httpx.", "import sqlite3", "save_bet", "save_prediction",
                          "INSERT INTO", "DELETE FROM"):
            assert forbidden not in src, f"agent_board non deve contenere {forbidden}"

    def test_import_leggero(self):
        """Importare la tabella non deve tirare dentro il denaro."""
        code = (
            "import agent_board, sys\n"
            "heavy = [m for m in ('tracker','auto_bet','bot','odds_api',"
            "'execution_engine') if m in sys.modules]\n"
            "print(','.join(heavy))\n"
        )
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, timeout=120, cwd=str(Path(__file__).parent))
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == ""

    def test_rotta_esposta(self):
        assert "/api/agents" in web_api.ROUTES

    def test_handler_fail_safe(self, monkeypatch):
        monkeypatch.setattr(agent_board, "board", lambda *a, **k: {"agents": []})
        assert web_api._agents_json({}) == {"agents": []}

    def test_handler_assorbe_gli_errori(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("registro rotto")
        monkeypatch.setattr(agent_board, "board", boom)
        out = web_api._agents_json({})
        assert out["agents"] == []
        assert "registro rotto" in out["error"]

    def test_handler_accetta_la_finestra(self, monkeypatch):
        seen = {}

        def fake(days=None, *a, **k):
            seen["days"] = days
            return {"agents": []}
        monkeypatch.setattr(agent_board, "board", fake)
        web_api._agents_json({"days": "3"})
        assert seen["days"] == 3.0
        web_api._agents_json({"days": "boh"})
        assert seen["days"] is None

    def test_comando_telegram_registrato(self):
        import bot
        src = Path(bot.__file__).read_text(encoding="utf-8")
        assert 'CommandHandler("agenti", cmd_agenti)' in src
        assert "async def cmd_agenti" in src
        assert callable(getattr(bot, "cmd_agenti"))
