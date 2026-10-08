"""Test della ghigliottina PRE-MATCH (`auto_bet.prematch_guillotine`).

Direttiva del proprietario (08/10/2026): un pick di una partita GIA' INIZIATA
non e' un candidato. Oltre `PREMATCH_MAX_AGE_H` ore dal kickoff (default 5) il
pick esce dal board **a prescindere** da qualunque interruttore — prima del
gate oracolo, dell'harvesting e dell'esecuzione — e quindi non viene piu'
valutato ne' tenuto in memoria come candidato.

Il caso che l'ha motivata: **Botafogo RJ-CR Vasco da Gama** (`sx-L20175875`,
Brasileirao, kickoff 07/10 23:30 UTC) con 6 righe di previsione ancora aperte
22 ore dopo il fischio d'inizio. La finestra esecutiva (`t60_window`,
T-180..T-2) lo scarta quando e' ATTIVA, ma e' una guardia CONFIGURABILE
(`T60_EXECUTION_ONLY=0` la spegne per diagnostica) e dipende dalla banda: la
ghigliottina e' la regola INDIPENDENTE che sopravvive a un cambio di finestra,
a un interruttore e a una corsia nuova che dimenticasse il filtro.

Note di metodo (lezioni del 15/09, 17/09, 30/09):
  * date SEMPRE relative a `now` — un test che scade col calendario arriva
    sempre nel momento peggiore;
  * bordi BRACKETTATI (±30 s), perche' `prematch_age_hours` ricalcola il suo
    `now`: con l'istante esatto il test sarebbe deterministicamente flaky.
"""
import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import auto_bet


def _pick(hours_ago: float, mid: str = "m1", key: str = "commence") -> dict:
    """Pick con kickoff a `hours_ago` da adesso (positivo = passato)."""
    ko = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return {"match_id": mid, key: ko.isoformat().replace("+00:00", "Z")}


class TestEtaDelPick:
    """`prematch_age_hours`: ore dal kickoff, None quando non e' leggibile."""

    def test_partita_futura_ha_eta_negativa(self):
        assert auto_bet.prematch_age_hours(_pick(-3)) < 0

    def test_partita_passata_ha_eta_positiva(self):
        assert auto_bet.prematch_age_hours(_pick(22)) == pytest.approx(
            22.0, abs=0.01)

    def test_kickoff_illeggibile_non_e_un_eta(self):
        """Assente o non parsabile -> None, MAI un numero inventato: la
        differenza conta, perche' la ghigliottina non scarta su un dato che
        non ha letto."""
        assert auto_bet.prematch_age_hours({"match_id": "m1"}) is None
        assert auto_bet.prematch_age_hours(
            {"match_id": "m1", "commence": "non-una-data"}) is None
        assert auto_bet.prematch_age_hours(
            {"match_id": "m1", "commence": None}) is None

    def test_legge_anche_la_chiave_kickoff(self):
        """Il piano del Capo usa `kickoff`: la ghigliottina vale anche li'."""
        assert auto_bet.prematch_age_hours(_pick(22, key="kickoff")) > 5

    def test_confronto_sul_datetime_non_sulla_stringa(self):
        """Un ISO con OFFSET non e' confrontabile per stringa col formato del
        ledger (lezione del 17/09 su `datetime('now')`): '2026-10-08T23:52+02:00'
        e' PIU' RECENTE di '2026-10-08T21:52Z' come stringa, ma e' 22 ore
        prima nel tempo reale. L'eta' si calcola sul datetime, quindi il pick
        e' vecchio — nessun falso negativo da formato."""
        ko = datetime.now(timezone.utc) - timedelta(hours=22)
        roma = ko.astimezone(timezone(timedelta(hours=2)))
        assert auto_bet.prematch_age_hours(
            {"match_id": "m1", "commence": roma.isoformat()}) > 21


class TestGhigliottina:
    """`prematch_guillotine`: una regola sola, applicata al board."""

    def test_scarta_i_pick_di_ieri_e_tiene_i_futuri(self):
        ghost = _pick(22, mid="sx-L20175875")      # Botafogo-CR Vasco
        fresh = _pick(-3, mid="sx-FRESCO")
        kept, dropped = auto_bet.prematch_guillotine([ghost, fresh])
        assert kept == [fresh]
        assert [p for p, _ in dropped] == [ghost]
        # l'eta' viaggia con lo scarto: un taglio senza il numero non si
        # puo' verificare a posteriori dal log
        assert dropped[0][1] == pytest.approx(22.0, abs=0.05)

    def test_bordo_delle_5_ore_brackettato(self):
        """Soglia di PRODUZIONE (5.0 h) con il bordo brackettato: 4.99 h
        resta, 5.01 h esce. Asserire l'istante esatto sarebbe flaky perche'
        la funzione ricalcola il suo `now`."""
        sotto = _pick(4.99)
        sopra = _pick(5.01)
        kept, dropped = auto_bet.prematch_guillotine([sotto, sopra])
        assert kept == [sotto]
        assert [p for p, _ in dropped] == [sopra]

    def test_la_soglia_di_produzione_e_5_ore(self):
        assert auto_bet.PREMATCH_MAX_AGE_H == 5.0

    def test_soglia_esplicita_vince_sul_default(self):
        p = _pick(22)
        assert auto_bet.prematch_guillotine([p], max_age_h=48)[0] == [p]
        assert auto_bet.prematch_guillotine([p], max_age_h=1)[1]

    def test_soglia_illeggibile_ricade_sul_default(self):
        """Un env sbagliato non spegne la guardia: si torna alla produzione."""
        kept, dropped = auto_bet.prematch_guillotine([_pick(22)],
                                                     max_age_h="boh")
        assert kept == [] and len(dropped) == 1

    def test_kickoff_illeggibile_non_viene_scartato(self):
        """Non si nasconde un dato mancante: la riga resta al chiamante, che
        ha le sue guardie fail-closed (`MIN_MINUTES_TO_START`)."""
        p = {"match_id": "m1"}
        assert auto_bet.prematch_guillotine([p]) == ([p], [])

    def test_lista_vuota_e_un_no_op(self):
        assert auto_bet.prematch_guillotine([]) == ([], [])
        assert auto_bet.prematch_guillotine(None) == ([], [])

    def test_i_tenuti_sono_gli_stessi_oggetti(self):
        """Nessuna copia silenziosa: il board e' il medesimo (le corsie si
        riconoscono il proprio pick)."""
        p = _pick(-3)
        kept, _ = auto_bet.prematch_guillotine([p])
        assert kept[0] is p

    def test_ordine_di_arrivo_preservato(self):
        picks = [_pick(-5, mid="a"), _pick(30, mid="ghost"),
                 _pick(-1, mid="b"), _pick(9, mid="ghost2")]
        kept, dropped = auto_bet.prematch_guillotine(picks)
        assert [p["match_id"] for p in kept] == ["a", "b"]
        assert [p["match_id"] for p, _ in dropped] == ["ghost", "ghost2"]


class TestCablaggioNelGiroOrdini:
    """Tripwire sul PERCORSO REALE (`auto_bet.run_today_bets`).

    Il taglio e' verificato in modo strutturale e non comportamentale: TUTTE
    le corsie filtrano gia' per kickoff futuro (l'ho misurato: 0 candidati con
    kickoff passato su tutte le lane) e `_too_close_to_start` /
    `t60_window` scartano comunque la partita, quindi un test di flusso NON
    potrebbe distinguere la ghigliottina dai suoi vicini. Cio' che va difeso
    qui e' che la chiamata esista, sia UNA sola e avvenga PRIMA del dedup
    cross-corsia, del gate oracolo e dell'harvesting — cioe' prima di ogni
    valutazione.
    """

    @staticmethod
    def _src() -> str:
        return Path(auto_bet.__file__).read_text(encoding="utf-8")

    def test_chiamata_presente_e_unica(self):
        src = self._src()
        assert src.count("prematch_guillotine(") == 2   # def + chiamata
        assert src.count("prematch_guillotine(board)") == 1

    def test_chiamata_prima_del_dedup_e_del_gate(self):
        src = self._src()
        call = src.index("prematch_guillotine(board)")
        assert call < src.index("_seen_pick: set[tuple] = set()"), \
            "il dedup cross-corsia verrebbe alimentato coi fantasmi"
        # la DEFINIZIONE della funzione sta prima nel file: si cerca la
        # chiamata dentro `run_today_bets`, cioe' DOPO la ghigliottina
        assert call < src.index("_harvest_oracle_board(", call), \
            "l'harvesting pagherebbe per una partita gia' iniziata"

    def test_il_log_dichiara_quanti_e_quali(self):
        src = self._src()
        assert "ghigliottina pre-match" in src
        # il warning porta il numero di scarti e l'eta' di ognuno: senza,
        # dal log non si capirebbe cosa e' stato tolto ne' perche'
        assert "len(_ghosts)" in src
        assert "PREMATCH_MAX_AGE_H" in src

    def test_la_costante_e_letta_dall_env(self):
        """`PREMATCH_MAX_AGE_H` e' tarabile senza redeploy."""
        tree = ast.parse(self._src())
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "PREMATCH_MAX_AGE_H"
                    for t in node.targets):
                found = True
                assert isinstance(node.value, ast.Call)
                seg = ast.unparse(node.value)
                assert "getenv" in seg and "PREMATCH_MAX_AGE_H" in seg
        assert found, "PREMATCH_MAX_AGE_H non piu' dichiarata"

    def test_soglia_dichiarata_nella_iac(self):
        """Senza `preserve()` un `railway config apply` distrugge la soglia
        tarata a mano (lezione del 28/09)."""
        iac = Path(".railway/railway.ts").read_text(encoding="utf-8")
        assert "PREMATCH_MAX_AGE_H: preserve()" in iac
