"""Test `oracle_skips` + hook del gate top-down in `auto_bet` (03/10/2026).

Tutti OFFLINE: nessuna rete, nessun ordine, nessuna scrittura nel ledger. Il
log va nella tmp (conftest isola `ORACLE_SKIP_LOG` per tutta la suite).
"""
import json
import time
from pathlib import Path

import pytest

import oracle_skips as osk


@pytest.fixture(autouse=True)
def _clean():
    osk.reset_dedup()
    yield
    osk.reset_dedup()


def _pick(**kw):
    base = {"match_id": "sx-L1", "esito_key": "Over 2.5", "mercato": "OU",
            "league": "Serie A", "quota": 1.55}
    base.update(kw)
    return base


# ------------------------------------------------------------------ scrittura

def test_record_skip_scrive(tmp_path, monkeypatch):
    p = tmp_path / "skips.jsonl"
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(p))
    evt = osk.record_skip(_pick(), "linea", detail="serve fetch")
    assert evt and evt["reason"] == "linea" and evt["mercato"] == "OU"
    row = json.loads(p.read_text().splitlines()[0])
    assert row["league"] == "Serie A" and row["detail"] == "serve fetch"


def test_dedup_per_giorno_pick_e_motivo(tmp_path, monkeypatch):
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(tmp_path / "s.jsonl"))
    assert osk.record_skip(_pick(), "linea") is not None
    assert osk.record_skip(_pick(), "linea") is None        # duplicato
    assert osk.record_skip(_pick(esito_key="Home +1.5"), "linea") is not None
    assert osk.record_skip(_pick(), "no_oracle") is not None  # motivo diverso


# ----------------------------------------- causa del rifiuto (06/10/2026)

def test_rifiuti_diversi_restano_distinti(tmp_path, monkeypatch):
    """La CAUSA del rifiuto entra nella chiave di dedup.

    Prima due rifiuti DIVERSI dello stesso pick collassavano in una riga: i due
    rifiuti per budget di AFCON non sono mai comparsi nel log e i conteggi
    mostravano il motivo vecchio ("kickoff oltre la finestra di fetch").
    """
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(tmp_path / "s.jsonl"))
    osk.record_skip(_pick(), "no_oracle", action="refused",
                    refusal="budget oracolo esaurito (2/2 oggi)")
    assert osk.record_skip(_pick(), "no_oracle", action="refused",
                           refusal="checkpoint T-70 gia' onorato") is not None
    s = osk.summary(days=1)
    assert s["events"] == 2
    assert s["by_refusal_class"] == {"budget oracolo esaurito": 1,
                                     "checkpoint T-70 gia' onorato": 1}


def test_stessa_causa_non_duplica(tmp_path, monkeypatch):
    """Il TESTO cambia di secondo in secondo: la chiave usa la CAUSA.

    `dedup (73s < 120s)` -> `dedup (133s < 120s)` sono lo STESSO evento; se
    finisse nella chiave si scriverebbe una riga per ciclo di 60s (il flood
    che il dedup esiste per evitare).
    """
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(tmp_path / "s.jsonl"))
    osk.record_skip(_pick(), "no_oracle", action="refused",
                    refusal="dedup (0s < 120s)")
    assert osk.record_skip(_pick(), "no_oracle", action="refused",
                           refusal="dedup (73s < 120s)") is None
    assert osk.summary(days=1)["events"] == 1


def test_refusal_class_stabile():
    assert osk._refusal_class("dedup (73s < 120s)") == "dedup"
    assert osk._refusal_class(
        "budget oracolo esaurito (2/2 oggi)") == "budget oracolo esaurito"
    assert osk._refusal_class("tetto per lega raggiunto (1/1 oggi)") == \
        "tetto per lega raggiunto"
    assert osk._refusal_class("hard-stop crediti") == "hard-stop crediti"
    assert osk._refusal_class(None) == ""
    assert osk._refusal_class("  ") == ""


def test_evento_porta_la_causa(tmp_path, monkeypatch):
    p = tmp_path / "s.jsonl"
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(p))
    evt = osk.record_skip(_pick(), "no_oracle", action="refused",
                          refusal="budget oracolo esaurito (2/2 oggi)")
    assert evt["refusal"] == "budget oracolo esaurito (2/2 oggi)"
    assert json.loads(p.read_text().splitlines()[0])["refusal"].startswith(
        "budget oracolo")


def test_report_mostra_le_cause(tmp_path, monkeypatch):
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(tmp_path / "s.jsonl"))
    osk.record_skip(_pick(), "no_oracle", action="refused",
                    refusal="budget oracolo esaurito (2/2 oggi)")
    txt = osk.format_report(days=1)
    assert "cause dei rifiuti" in txt and "budget oracolo esaurito" in txt


def test_reset_dedup_riabilita(tmp_path, monkeypatch):
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(tmp_path / "s.jsonl"))
    osk.record_skip(_pick(), "linea")
    osk.reset_dedup()
    assert osk.record_skip(_pick(), "linea") is not None


def test_scrittura_impossibile_non_propaga(tmp_path, monkeypatch):
    # Il path punta a una DIRECTORY: `open("a")` solleva e la telemetria deve
    # degradare a None senza propagare (una scrittura rotta non ferma il giro).
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(tmp_path))
    assert osk.record_skip(_pick(), "no_oracle") is None


def test_path_letto_a_runtime(tmp_path, monkeypatch):
    """Il path NON e' una costante di import (bug reale di `tennis_quant`)."""
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(a))
    osk.record_skip(_pick(), "linea")
    osk.reset_dedup()
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(b))
    osk.record_skip(_pick(match_id="sx-L2"), "linea")
    assert a.exists() and b.exists()
    assert json.loads(b.read_text().splitlines()[0])["match_id"] == "sx-L2"


# --------------------------------------------------------------------- report

def test_summary_raggruppa(tmp_path, monkeypatch):
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(tmp_path / "s.jsonl"))
    osk.record_skip(_pick(), "linea")
    osk.record_skip(_pick(esito_key="Under 2.5"), "no_oracle")
    osk.record_skip(_pick(match_id="sx-L9", mercato="AH", esito_key="Home +1",
                          league="Liga MX"), "no_oracle")
    s = osk.summary(days=1)
    assert s["events"] == 3
    assert s["by_reason"] == {"linea": 1, "no_oracle": 2}
    assert s["by_market"] == {"OU": 2, "AH": 1}
    assert s["by_league"]["Liga MX"] == 1


def test_summary_vuoto(tmp_path, monkeypatch):
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(tmp_path / "s.jsonl"))
    s = osk.summary(days=1)
    assert s["events"] == 0 and s["by_reason"] == {}


def test_finestra_temporale(tmp_path, monkeypatch):
    p = tmp_path / "s.jsonl"
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(p))
    p.write_text(json.dumps({"reason": "linea", "ts_epoch": time.time()}) + "\n"
                 + json.dumps({"reason": "linea",
                               "ts_epoch": time.time() - 10 * 86400}) + "\n")
    assert osk.summary(days=1)["events"] == 1


def test_format_report(tmp_path, monkeypatch):
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(tmp_path / "s.jsonl"))
    assert "nessuno scarto" in osk.format_report(days=1)
    osk.record_skip(_pick(), "linea")
    txt = osk.format_report(days=1)
    assert "linea" in txt and "OU" in txt


# ------------------------------------------------- hook nel gate top-down

def test_note_top_down_skip_scrive(tmp_path, monkeypatch):
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(tmp_path / "s.jsonl"))
    import auto_bet
    auto_bet._note_top_down_skip(_pick(), "no_oracle", detail="d")
    assert osk.summary(days=1)["by_reason"] == {"no_oracle": 1}


def test_note_top_down_skip_fail_safe(monkeypatch):
    """Un guasto della telemetria non deve MAI fermare un giro puntate."""
    import auto_bet
    monkeypatch.setattr(osk, "record_skip",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    auto_bet._note_top_down_skip(_pick(), "linea")      # nessuna eccezione


def test_hook_presente_nei_rami_di_scarto():
    """Tripwire: i due rami di scarto del gate devono registrare la misura."""
    src = (Path(__file__).parent / "auto_bet.py").read_text()
    assert src.count("_note_top_down_skip(") >= 3     # def + 2 chiamate
    seg = src.split("if not verdict.get(\"ok\"):")[1][:600]
    assert "_note_top_down_skip(" in seg
    seg2 = src.split("verdict[\"ev_min\"] * 100.0,")[1][:400]
    assert "_note_top_down_skip(" in seg2


def test_job_credit_watchdog_usa_la_diagnosi():
    src = (Path(__file__).parent / "bot.py").read_text()
    assert "credit_diagnose" in src and "attribuzione" in src


def test_rotazione_agganciata_al_backup():
    src = (Path(__file__).parent / "bot.py").read_text()
    assert "rotate_jsonl_logs" in src


def test_env_dichiarate_in_iac():
    iac = (Path(__file__).parent / ".railway" / "railway.ts").read_text()
    assert "ORACLE_SKIP_LOG" in iac and "TELEMETRY_ROTATE_ENABLED" in iac


# -------------------------------- finestra esecutiva: ordini BLOCCATI o no

def test_scarto_in_finestra_e_ordine_bloccato(tmp_path, monkeypatch):
    """FIX 04/10/2026: il gate gira PRIMA del controllo T-60, su TUTTI i
    candidati. Senza distinguere la finestra, un pick a 20 ore dal kickoff che
    salta per `linea` sembrava un ordine perso (e i conteggi erano gonfiati).
    Il numero che conta e' `orders_blocked`."""
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(tmp_path / "s.jsonl"))
    osk.reset_dedup()
    osk.record_skip(_pick(), "linea", in_window=False)
    osk.record_skip(_pick(), "linea", in_window=True)
    s = osk.summary(days=1)
    assert s["events"] == 2              # due bucket distinti, nessun dedup
    assert s["orders_blocked"] == 1
    assert s["outside_window"] == 1
    assert s["by_reason_in_window"] == {"linea": 1}


def test_dedup_dentro_lo_stesso_bucket(tmp_path, monkeypatch):
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(tmp_path / "s.jsonl"))
    osk.reset_dedup()
    for _ in range(5):
        osk.record_skip(_pick(), "linea", in_window=True)
    assert osk.summary(days=1)["events"] == 1


def test_scarto_senza_finestra_non_conta_come_bloccato(tmp_path, monkeypatch):
    """Finestra non valutabile (kickoff illeggibile) -> bucket '?', mai
    contato come ordine bloccato."""
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(tmp_path / "s.jsonl"))
    osk.reset_dedup()
    osk.record_skip(_pick(), "linea")          # in_window resta None
    s = osk.summary(days=1)
    assert s["orders_blocked"] == 0 and s["window_unknown"] == 1
    assert "ordini bloccati" in osk.format_report(days=1)


def test_hook_valuta_finestra_col_verdetto_di_produzione():
    """Il hook usa `pick_window` (che delega a `t60_window`), non una copia.

    E la regola vive in UN SOLO posto: `t60_window` compare una volta sola
    nel sorgente (dentro `pick_window`), cosi' gate e telemetria non possono
    divergere — e nessun percorso puo' aggirare la finestra.
    """
    src = (Path(__file__).parent / "auto_bet.py").read_text()
    seg = src.split("def _note_top_down_skip")[1][:1600]
    assert "pick_window(pick) == \"within\"" in seg
    assert src.count('t60_window(_parse_iso_utc(pick.get("commence")))') == 1


def test_hook_scrive_la_finestra(tmp_path, monkeypatch):
    """End-to-end: un pick con kickoff fra 60' finisce nel bucket 'in'."""
    from datetime import datetime, timedelta, timezone
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(tmp_path / "s.jsonl"))
    monkeypatch.setenv("T60_EXECUTION_ONLY", "1")
    osk.reset_dedup()
    import auto_bet
    soon = (datetime.now(timezone.utc) + timedelta(minutes=60)).isoformat()
    p = dict(_pick(), commence=soon)
    auto_bet._note_top_down_skip(p, "linea")
    assert osk.summary(days=1)["orders_blocked"] == 1


def test_hook_fuori_finestra_non_blocca(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(tmp_path / "s.jsonl"))
    osk.reset_dedup()
    import auto_bet
    far = (datetime.now(timezone.utc) + timedelta(hours=20)).isoformat()
    auto_bet._note_top_down_skip(dict(_pick(), commence=far), "linea")
    s = osk.summary(days=1)
    assert s["orders_blocked"] == 0 and s["outside_window"] == 1


def test_etichetta_finestra_deriva_dalle_costanti(tmp_path, monkeypatch):
    """Il report NON dichiara una banda hardcoded.

    Il 03/10 la chiusura e' scesa a T-5: l'etichetta era `T-120..T-15` fissa e
    avrebbe dichiarato una finestra che non esiste piu' (classe di bug dei
    "testi derivati dalle costanti", 13/09 e 21/09). L'etichetta si costruisce
    dai valori di PRODUZIONE di `auto_bet`.
    """
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(tmp_path / "s.jsonl"))
    osk.reset_dedup()
    osk.record_skip(_pick(), "linea", in_window=False)
    import auto_bet
    lab = osk.window_label()
    assert lab == f"T-{auto_bet.T60_WINDOW_MIN_MIN:g}..T-{auto_bet.T60_WINDOW_MAX_MIN:g}"
    assert lab in osk.format_report(days=1)
    assert "T-120..T-15" not in osk.format_report(days=1)


def test_etichetta_finestra_fail_safe_senza_costanti(monkeypatch):
    """Se i valori non sono leggibili l'etichetta resta generica (mai un
    numero inventato, mai un'eccezione dentro un report)."""
    import builtins
    real_import = builtins.__import__

    def _boom(name, *a, **k):
        if name == "auto_bet":
            raise ImportError("simulato")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _boom)
    assert osk.window_label() == "finestra esecutiva"


# ------------------------------- esito del FETCH ON-DEMAND (06/10/2026)
#
# Prima l'esito del fetch viveva solo nel TESTO di `detail`: leggibile a
# occhio, non contabile. La domanda "quante fetch ha pagato l'oracolo e quante
# ne ha rifiutate il tetto crediti o il tiering?" non aveva risposta numerica.

def test_action_e_refusal_sono_persistiti(tmp_path, monkeypatch):
    p = tmp_path / "s.jsonl"
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(p))
    osk.reset_dedup()
    osk.record_skip(_pick(), "no_oracle/EXPIRED_CACHE", action="refused",
                    refusal="budget oracolo esaurito (2/2 oggi)")
    row = json.loads(p.read_text().splitlines()[0])
    assert row["action"] == "refused"
    assert "budget" in row["refusal"]


def test_righe_senza_action_non_inventano_un_azione(tmp_path, monkeypatch):
    """Le righe scritte prima del 06/10 non hanno il campo: restano 'assenti'."""
    p = tmp_path / "s.jsonl"
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(p))
    p.write_text(json.dumps({"reason": "linea", "ts_epoch": time.time()}) + "\n")
    assert osk.summary(days=1)["by_action"] == {"assenti": 1}


def test_summary_aggrega_per_azione_e_rifiuto(tmp_path, monkeypatch):
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(tmp_path / "s.jsonl"))
    osk.reset_dedup()
    osk.record_skip(_pick(match_id="a"), "linea",
                    action="fetched")
    osk.record_skip(_pick(match_id="b"), "linea", action="refused",
                    refusal="checkpoint non aperto (150')")
    osk.record_skip(_pick(match_id="c"), "linea", action="refused",
                    refusal="checkpoint non aperto (140')")
    osk.record_skip(_pick(match_id="d"), "no_oracle/EXPIRED_CACHE",
                    action="tier_not_paid")
    s = osk.summary(days=1)
    assert s["by_action"] == {"refused": 2, "fetched": 1, "tier_not_paid": 1}
    assert s["by_refusal"] == {"checkpoint non aperto (150')": 1,
                              "checkpoint non aperto (140')": 1}
    txt = osk.format_report(days=1)
    assert "fetch on-demand:" in txt and "tier_not_paid 1" in txt
    assert "rifiuti dichiarati:" in txt


def test_la_transizione_di_azione_non_e_un_duplicato(tmp_path, monkeypatch):
    """Lo stesso pick puo' essere rifiutato per tier e poi PAGATO: due righe."""
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(tmp_path / "s.jsonl"))
    osk.reset_dedup()
    assert osk.record_skip(_pick(), "no_oracle/EXPIRED_CACHE",
                           action="tier_not_paid") is not None
    assert osk.record_skip(_pick(), "no_oracle/EXPIRED_CACHE",
                           action="tier_not_paid") is None      # duplicato
    assert osk.record_skip(_pick(), "no_oracle/EXPIRED_CACHE",
                           action="fetched") is not None
    assert osk.summary(days=1)["events"] == 2


def test_note_top_down_skip_propaga_l_azione(tmp_path, monkeypatch):
    monkeypatch.setenv("ORACLE_SKIP_LOG", str(tmp_path / "s.jsonl"))
    import auto_bet
    auto_bet._note_top_down_skip(_pick(), "no_oracle/EXPIRED_CACHE",
                                 detail="d", action="tier_not_paid")
    row = osk.iter_events(days=1)[0]
    assert row["action"] == "tier_not_paid"
