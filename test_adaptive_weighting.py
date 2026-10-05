"""Test della ponderazione dinamica per campionato (CLV, module 3).

Coprono: interruttore OFF (default) = nessun effetto, moltiplicatore SOLO verso
il basso, campione minimo, floor, whitelist restrittiva, normalizzazione del
nome lega, fallback su `matches.league`, gestione delle date del ledger
(lezione 17/09: `datetime(...)` sempre), cache/TTL, fail-open e i tripwire
dell'integrazione in `auto_bet` (delega, riduzione dello stake, nessun
percorso che alzi il moltiplicatore).

Tutti i test sono OFFLINE (SQLite temporaneo, zero rete) e non scrivono su
alcun file di produzione.
"""
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import adaptive_weighting as aw
import tracker


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        monkeypatch.setattr(tracker, "DB_PATH", db_path)
        tracker.init_db()
        aw.reset_cache()
        yield db_path
    aw.reset_cache()


@pytest.fixture()
def on(monkeypatch):
    """Accende la ponderazione per il test (env letta a runtime)."""
    monkeypatch.setenv("ADAPTIVE_WEIGHTING", "1")
    monkeypatch.setenv("ADAPTIVE_WEIGHTING_MIN_SAMPLES", "3")
    aw.reset_cache()
    yield
    aw.reset_cache()


def _seed_clv(match_id, esito, sig, clos, league=None, *,
              updated_at=None, with_prediction=True, with_match=False):
    """Semina un campione CLV (+ eventuale prediction/match per la lega)."""
    if with_prediction:
        tracker.save_prediction(match_id, "1X2", esito, sig, 0.5, 0.03,
                                league=league)
    if with_match:
        tracker.save_match(match_id, league or "", "Casa", "Ospite",
                           datetime.now(timezone.utc).isoformat())
    conn = tracker._get_conn()
    # Colonne NOMINATE (non posizionali): dal 04/10/2026 `clv_history` ha la
    # colonna `closing_odds` e un INSERT posizionale si romperebbe a ogni
    # colonna nuova (osservato: "table clv_history has 7 columns but 6 values").
    conn.execute(
        "INSERT OR REPLACE INTO clv_history (match_id, esito, signal_quota, "
        "closing_quota, updated_at, pinnacle_quota) VALUES (?,?,?,?,?,?)",
        (match_id, esito, sig, clos,
         updated_at or datetime.now().isoformat(), None))
    conn.commit()
    conn.close()


def _seed_batch(league, n, sig, clos, prefix="m"):
    for i in range(n):
        _seed_clv(f"{prefix}{league}{i}", "1", sig, clos, league=league)


# ---------------------------------------------------------------------------
# 1. Interruttore: default OFF = nessun effetto
# ---------------------------------------------------------------------------

class TestInterruttore:

    def test_default_off_non_cambia_nulla(self, temp_db, monkeypatch):
        monkeypatch.delenv("ADAPTIVE_WEIGHTING", raising=False)
        _seed_batch("Serie A", 10, 1.70, 1.90)          # CLV molto negativo
        aw.reset_cache()
        assert aw.enabled() is False
        assert aw.league_multiplier("Serie A") == 1.0

    def test_on_con_clv_negativo_riduce(self, temp_db, on):
        _seed_batch("Serie A", 6, 1.70, 1.90)           # CLV -10.5%
        assert aw.enabled() is True
        assert aw.league_multiplier("Serie A") < 1.0

    def test_env_letta_a_runtime_non_all_import(self, temp_db, monkeypatch):
        # Le env sono lette a ogni chiamata: accendere l'interruttore DOPO
        # l'import deve avere effetto (senza reload del modulo).
        monkeypatch.setenv("ADAPTIVE_WEIGHTING", "0")
        _seed_batch("Serie A", 6, 1.70, 1.90)
        assert aw.league_multiplier("Serie A") == 1.0
        monkeypatch.setenv("ADAPTIVE_WEIGHTING", "1")
        monkeypatch.setenv("ADAPTIVE_WEIGHTING_MIN_SAMPLES", "3")
        aw.reset_cache()
        assert aw.league_multiplier("Serie A") < 1.0


# ---------------------------------------------------------------------------
# 2. Solo verso il basso + forma del moltiplicatore
# ---------------------------------------------------------------------------

class TestMoltiplicatore:

    def test_clv_positivo_non_promuove(self, temp_db, on):
        _seed_batch("Serie A", 6, 1.90, 1.70)           # CLV POSITIVO
        assert aw.league_multiplier("Serie A") == 1.0

    def test_clv_zero_neutro(self, temp_db, on):
        _seed_batch("Serie A", 6, 1.80, 1.80)
        assert aw.league_multiplier("Serie A") == 1.0

    def test_mai_sopra_uno_su_griglia(self, temp_db, on, monkeypatch):
        # Tripwire: nessuna combinazione (n, CLV) puo' superare 1.0.
        monkeypatch.setenv("ADAPTIVE_WEIGHTING_MIN_SAMPLES", "1")
        for n in (0, 1, 5, 50):
            for clv in (-0.5, -0.1, -0.04, -0.01, 0.0, 0.05, 1.0):
                m = aw._multiplier_from_clv(n, clv)
                assert 0.0 <= m <= 1.0, (n, clv, m)

    def test_scala_linearmente_al_floor(self, temp_db, monkeypatch):
        monkeypatch.setenv("ADAPTIVE_WEIGHTING_MIN_SAMPLES", "1")
        monkeypatch.setenv("ADAPTIVE_WEIGHTING_FLOOR", "0.5")
        monkeypatch.setenv("ADAPTIVE_WEIGHTING_CLV_FLOOR_THRESHOLD", "-0.02")
        # meta' strada (-1%) -> meta' della riduzione (0.75)
        assert aw._multiplier_from_clv(10, -0.01) == pytest.approx(0.75)
        # alla soglia -> floor
        assert aw._multiplier_from_clv(10, -0.02) == pytest.approx(0.5)
        # oltre la soglia resta al floor (mai sotto)
        assert aw._multiplier_from_clv(10, -0.9) == pytest.approx(0.5)

    def test_floor_da_env(self, temp_db, monkeypatch):
        monkeypatch.setenv("ADAPTIVE_WEIGHTING_MIN_SAMPLES", "1")
        monkeypatch.setenv("ADAPTIVE_WEIGHTING_FLOOR", "0.2")
        assert aw._multiplier_from_clv(10, -1.0) == pytest.approx(0.2)

    def test_campione_minimo(self, temp_db, on):
        # 2 campioni con il minimo a 3 -> nessuna conclusione
        _seed_batch("Serie A", 2, 1.70, 1.90)
        assert aw.league_multiplier("Serie A") == 1.0

    def test_soglia_campione_da_env(self, temp_db, monkeypatch):
        monkeypatch.setenv("ADAPTIVE_WEIGHTING", "1")
        monkeypatch.setenv("ADAPTIVE_WEIGHTING_MIN_SAMPLES", "10")
        _seed_batch("Serie A", 5, 1.70, 1.90)
        aw.reset_cache()
        assert aw.league_multiplier("Serie A") == 1.0
        _seed_batch("Serie A", 10, 1.70, 1.90, prefix="x")
        aw.reset_cache()
        assert aw.league_multiplier("Serie A") < 1.0


# ---------------------------------------------------------------------------
# 3. Whitelist restrittiva
# ---------------------------------------------------------------------------

class TestWhitelist:

    def test_restricted_sotto_la_soglia(self, temp_db, on):
        _seed_batch("Grecia", 6, 1.40, 2.20)            # CLV ~ -36%
        tab = aw.table(use_cache=False)
        assert tab["Grecia"]["restricted"] is True
        assert tab["Grecia"]["multiplier"] <= aw.floor() + 1e-9

    def test_non_restricted_sopra_la_soglia(self, temp_db, on):
        _seed_batch("Serie A", 6, 1.95, 2.00)           # CLV ~ -2.5%
        tab = aw.table(use_cache=False)
        assert tab["Serie A"]["restricted"] is False

    def test_soglia_restrict_da_env(self, temp_db, monkeypatch):
        monkeypatch.setenv("ADAPTIVE_WEIGHTING", "1")
        monkeypatch.setenv("ADAPTIVE_WEIGHTING_MIN_SAMPLES", "3")
        monkeypatch.setenv("ADAPTIVE_WEIGHTING_RESTRICT_THRESHOLD", "-0.005")
        _seed_batch("Serie A", 6, 1.95, 2.00)           # -2.5% < -0.5%
        aw.reset_cache()
        assert aw.table(use_cache=False)["Serie A"]["restricted"] is True


# ---------------------------------------------------------------------------
# 4. Lega: normalizzazione, fallback, ignota
# ---------------------------------------------------------------------------

class TestLega:

    def test_alias_canonical(self, temp_db, on):
        # Il nome grezzo del provider deve contare come la chiave della
        # strategia (stessa normalizzazione del gate di lega).
        _seed_batch("Major League Soccer", 6, 1.70, 1.90)
        tab = aw.table(use_cache=False)
        assert "MLS" in tab, tab
        assert aw.league_multiplier("MLS") < 1.0
        # Anche la variante con prefisso paese passa dal canonical
        assert aw.league_multiplier("USA MLS") < 1.0

    def test_fallback_su_matches_league(self, temp_db, on):
        # Nessuna riga in `predictions` (match non piu' joinabile): la lega
        # arriva dalla riga `matches`.
        _seed_clv("no-pred", "1", 1.70, 1.90, with_prediction=False,
                  with_match=True, league="Serie A")
        for i in range(5):
            _seed_clv(f"no-pred{i}", "1", 1.70, 1.90, with_prediction=False,
                      with_match=True, league="Serie A")
        assert aw.league_multiplier("Serie A") < 1.0

    def test_lega_assente_o_ignota_neutra(self, temp_db, on):
        assert aw.league_multiplier(None) == 1.0
        assert aw.league_multiplier("") == 1.0
        assert aw.league_multiplier("Lega Mai Vista") == 1.0

    def test_righe_senza_lega_ignorate(self, temp_db, on):
        _seed_batch("Serie A", 6, 1.70, 1.90)
        # righe senza lega non devono entrare in nessun bucket
        _seed_clv("orfana", "1", 1.70, 1.90, league=None)
        tab = aw.table(use_cache=False)
        assert "" not in tab


# ---------------------------------------------------------------------------
# 5. Date del ledger (lezione 17/09)
# ---------------------------------------------------------------------------

class TestDate:

    def test_formati_di_data_tutti_dentro_la_finestra(self, temp_db, on):
        now = datetime.now(timezone.utc)
        for i, ts in enumerate([
                now.isoformat(),                             # +00:00
                now.isoformat().replace("+00:00", "Z"),      # Z
                now.strftime("%Y-%m-%d %H:%M:%S"),           # spazio (SQLite)
        ]):
            _seed_clv(f"d{i}", "1", 1.70, 1.90, league="Serie A",
                      updated_at=ts)
        tab = aw.table(use_cache=False)
        assert tab["Serie A"]["n"] == 3

    def test_fuori_finestra_escluso(self, temp_db, on):
        old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        _seed_clv("old", "1", 1.70, 1.90, league="Serie A", updated_at=old)
        assert "Serie A" not in aw.table(use_cache=False)

    def test_finestra_da_env(self, temp_db, on, monkeypatch):
        mid = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        _seed_clv("mid", "1", 1.70, 1.90, league="Serie A", updated_at=mid)
        assert "Serie A" not in aw.table(use_cache=False, days=5)
        assert aw.table(use_cache=False, days=30)["Serie A"]["n"] == 1

    def test_quote_non_valide_scartate(self, temp_db, on):
        _seed_clv("bad", "1", 1.0, 1.90, league="Serie A")
        _seed_clv("bad2", "1", 0.5, 1.90, league="Serie A")
        assert "Serie A" not in aw.table(use_cache=False)


# ---------------------------------------------------------------------------
# 6. Cache, fail-open, report
# ---------------------------------------------------------------------------

class TestRobustezza:

    def test_cache_e_reset(self, temp_db, on):
        _seed_batch("Serie A", 6, 1.70, 1.90)
        first = aw.table()          # popola la cache
        assert "Serie A" in first
        # nuovi campioni NON visibili finche' la cache e' valida
        _seed_batch("Bundesliga", 6, 1.70, 1.90)
        assert "Bundesliga" not in aw.table()
        aw.reset_cache()
        assert "Bundesliga" in aw.table(use_cache=False)

    def test_ttl_zero_disattiva_la_cache(self, temp_db, on, monkeypatch):
        monkeypatch.setenv("ADAPTIVE_WEIGHTING_TTL", "0")
        _seed_batch("Serie A", 6, 1.70, 1.90)
        aw.table()
        _seed_batch("Bundesliga", 6, 1.70, 1.90)
        assert "Bundesliga" in aw.table()

    def test_db_non_leggibile_fail_open(self, monkeypatch):
        monkeypatch.setenv("ADAPTIVE_WEIGHTING", "1")
        monkeypatch.setattr(tracker, "DB_PATH",
                            Path("/proc/1/non-esiste/db.sqlite"))
        aw.reset_cache()
        # Nessuna eccezione: solo "nessun dato".
        assert aw.table(use_cache=False) == {}
        assert aw.league_multiplier("Serie A") == 1.0

    def test_lega_multiplier_mai_solleva(self, monkeypatch):
        monkeypatch.setenv("ADAPTIVE_WEIGHTING", "1")
        monkeypatch.setattr(aw, "table", lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("boom")))
        assert aw.league_multiplier("Serie A") == 1.0

    def test_report_e_format(self, temp_db, on):
        _seed_batch("Serie A", 6, 1.70, 1.90)
        _seed_batch("Grecia", 6, 1.30, 2.30)
        res = aw.report()
        assert res["enabled"] is True
        assert res["n_leagues"] == 2
        assert "Grecia" in res["restricted"]
        txt = aw.format_report(res)
        assert "Ponderazione per campionato" in txt
        assert "Grecia" in txt

    def test_format_report_senza_dati(self, temp_db, monkeypatch):
        monkeypatch.setenv("ADAPTIVE_WEIGHTING", "0")
        txt = aw.format_report(aw.report())
        assert "OFF" in txt
        assert "Nessun dato" in txt

    def test_cli_json(self, temp_db, on, capsys):
        _seed_batch("Serie A", 6, 1.70, 1.90)
        assert aw.main(["--json"]) == 0
        out = capsys.readouterr().out
        assert "Serie A" in out

    def test_sola_lettura_nel_sorgente(self):
        src = Path("adaptive_weighting.py").read_text(encoding="utf-8")
        for forbidden in ("INSERT ", "UPDATE ", "DELETE ", "save_bet",
                          "save_prediction", "sqlite3"):
            assert forbidden not in src, forbidden

    def test_datetime_avvolge_le_date_nel_sql(self):
        # Tripwire della lezione permanente: il confronto su `updated_at`
        # DEVE essere avvolto in datetime(...).
        src = Path("adaptive_weighting.py").read_text(encoding="utf-8")
        assert "datetime(c.updated_at) >= datetime(?)" in src


# ---------------------------------------------------------------------------
# 7. Integrazione con auto_bet (delega + solo riduzione)
# ---------------------------------------------------------------------------

class TestIntegrazioneAutoBet:

    def test_delega_e_fail_open(self, monkeypatch):
        import auto_bet
        monkeypatch.setenv("ADAPTIVE_WEIGHTING", "0")
        aw.reset_cache()
        assert auto_bet._league_multiplier("Serie A") == 1.0
        # modulo indisponibile -> 1.0 (mai un'eccezione)
        monkeypatch.setitem(__import__("sys").modules, "adaptive_weighting",
                            None)
        assert auto_bet._league_multiplier("Serie A") == 1.0
        monkeypatch.undo()

    def test_riduce_lo_stake_della_lega(self, temp_db, on, monkeypatch):
        import auto_bet
        _seed_batch("Serie A", 6, 1.70, 1.90)
        m = auto_bet._league_multiplier("Serie A")
        assert 0.0 < m < 1.0
        assert 10.0 * m < 10.0

    def test_tripwire_chiamata_nel_percorso_staking(self):
        src = Path("auto_bet.py").read_text(encoding="utf-8")
        assert '_league_multiplier(pick.get("league"))' in src
        # Solo riduzione: nel percorso ordini la variabile si moltiplica
        # sempre (nessun percorso che la usi come divisore).
        assert "pick_stake *= _lm" in src

    def test_env_dichiarate_nella_iac(self):
        src = Path(".railway/railway.ts").read_text(encoding="utf-8")
        for env in ("ADAPTIVE_WEIGHTING", "ADAPTIVE_WEIGHTING_WINDOW_DAYS",
                    "ADAPTIVE_WEIGHTING_MIN_SAMPLES",
                    "ADAPTIVE_WEIGHTING_FLOOR",
                    "ADAPTIVE_WEIGHTING_CLV_FLOOR_THRESHOLD",
                    "ADAPTIVE_WEIGHTING_RESTRICT_THRESHOLD",
                    "ADAPTIVE_WEIGHTING_TTL"):
            assert f"{env}: preserve()" in src, env

    def test_default_spento_non_altera_lo_stake(self, temp_db, monkeypatch):
        # I default di codice sono "telemetria": con l'env assente il modulo
        # non altera lo stake NEMMENO con CLV molto negativo nel DB.
        monkeypatch.delenv("ADAPTIVE_WEIGHTING", raising=False)
        monkeypatch.setenv("ADAPTIVE_WEIGHTING_MIN_SAMPLES", "1")
        _seed_batch("Grecia", 20, 1.30, 2.30)
        aw.reset_cache()
        assert aw.enabled() is False
        assert aw.league_multiplier("Grecia") == 1.0
