"""Test dei contratti MULTI-MERCATO (`decision/market.py`, schema 2.0) — OFFLINE.

Cosa copre questa suite, in breve:

1. **Il registro** (`MARKET_SPECS`): ogni mercato ha regole dichiarate, i type
   id nativi di SX Bet sono quelli della doc ufficiale, i mercati derivati
   dicono da cosa derivano, i tipi SX non modellati restano dichiarati.
2. **Le linee**: obbligatorie su OU/AH, VIETATE altrove, a passi di 0.25,
   dentro il range del mercato, con una chiave canonica stabile ('2.5', '-0.75').
3. **Gli esiti per mercato**: BTTS yes/no, DC 1X/X2/12, Risultato Esatto come
   punteggio ('3:1' -> '3-1'), e il rifiuto incrociato esito/mercato.
4. **Provenienza**: una quota `derived` DEVE dire da dove viene; una `native`
   non puo' dichiarare una derivazione (mai un mercato inventato).
5. **L'identita'**: la LINEA entra nell'identity_key — OU 2.5 e OU 3.5 sono due
   mercati, e confonderli significa saldare un esito con un altro risultato.
6. **Il fixture**: `FixtureQuotes` raggruppa i mercati di UNA partita e
   respinge il gruppo intero se le righe non sono della stessa partita.
7. **La serializzazione**: `as_row()` copre `MARKET_ROW_FIELDS` ed e' JSON-safe.
8. **La purezza**: nessuna rete, nessun DB (verificato in un sottoprocesso).

Nessuna rete, nessun DB, nessun provider: dati finti costruiti a mano, quindi
**zero crediti API** (come il resto della suite `decision`).
"""

import json
import subprocess
import sys

import pytest

from decision.market import (
    MAX_SCORE_GOALS, MARKET_ROW_FIELDS, MARKET_SCHEMA_VERSION, MARKET_SELECTIONS,
    MARKET_SPECS, QUOTE_ORIGINS, SUPPORTED_MARKETS, SX_LINE_BEARING_TYPES,
    SX_QUARTER_LINE_TYPES, SX_TYPE_IDS, SX_TYPES_NOT_MODELLED, FixtureQuotes,
    MarketQuoteError, MarketType, QuoteErrorCode, line_required,
    market_accepts_lines, market_type_of, parse_quote, spec_for,
    validate_fixture_quotes,
)

FIXTURE = "sx-L20067612"


def payload(**overrides):
    """Riga conforme al contratto 2.0 (1X2 casa); gli override la rendono non conforme."""
    base = {
        "schema_version": MARKET_SCHEMA_VERSION,
        "event_id": FIXTURE,
        "market": "1X2",
        "selection": "1",
        "odds": 1.69,
        "timestamp": "2026-09-18T21:20:00+00:00",
        "source": "sxbet",
        "gateway_id": "sxbet-feed",
        "event_name": "Central Cordoba - Defensa y Justicia",
        "league": "Liga Profesional",
        "home": "Central Cordoba",
        "away": "Defensa y Justicia",
        "kickoff": "2026-09-18T23:30:00+00:00",
    }
    base.update(overrides)
    return base


def quote(**overrides):
    return parse_quote(payload(**overrides))


def codes(error: MarketQuoteError) -> list[str]:
    return error.codes()


# ---------------------------------------------------------------------------
# 1. Il registro dei tipi
# ---------------------------------------------------------------------------

class TestRegistro:
    def test_ogni_tipo_ha_una_spec(self):
        for market in MarketType:
            assert market in MARKET_SPECS, market
            assert MARKET_SPECS[market].market_type is market
            assert MARKET_SPECS[market].label

    def test_supported_markets_derivato_dal_registro(self):
        """La lista dei mercati non si scrive a mano: diverge dal registro."""
        assert SUPPORTED_MARKETS == tuple(m.value for m in MarketType)
        assert set(MARKET_SPECS) == set(MarketType)

    def test_type_id_sx_ufficiali(self):
        """Tripwire sui type id NATIVI di SX Bet (docs.sx.bet).

        La famiglia "including overtime" (28/342/226) e' stata aggiunta il
        01/10/2026 dalla stessa doc e confermata dal probe reale su
        /markets/active di Basketball (sportId 1) e American Football (8).
        """
        assert SX_TYPE_IDS == {1: MarketType.MATCH_RESULT, 2: MarketType.OVER_UNDER,
                               3: MarketType.ASIAN_HANDICAP,
                               17: MarketType.BOTH_TEAMS_TO_SCORE,
                               28: MarketType.OVER_UNDER_OT,
                               226: MarketType.MONEYLINE_OT,
                               342: MarketType.ASIAN_HANDICAP_OT}
        for market, spec in MARKET_SPECS.items():
            ids = dict(spec.source_type_ids)
            if spec.native:
                assert "sxbet" in ids, f"{market} nativo senza type id SX"
                assert SX_TYPE_IDS[ids["sxbet"]] is market
            else:
                assert not ids, f"{market} non nativo con type id SX"

    def test_mercati_derivati_dichiarano_la_provenienza(self):
        for market, spec in MARKET_SPECS.items():
            if spec.native:
                assert not spec.derivable_from, market
            else:
                assert spec.derivable_from, f"{market} derivato senza provenienza"
                for source in spec.derivable_from:
                    assert source in MARKET_SPECS

    def test_double_chance_e_correct_score_non_sono_nativi(self):
        """Su SX non esistono: se diventassero 'nativi' sarebbe un'invenzione."""
        assert not spec_for("DC").native
        assert not spec_for("CS").native
        assert spec_for("DC").derivable_from == (MarketType.MATCH_RESULT,)

    def test_linee_dichiarate_solo_dove_esistono(self):
        assert spec_for("OU").has_lines and spec_for("AH").has_lines
        for market in ("1X2", "BTTS", "DC", "CS"):
            assert not spec_for(market).has_lines, market
        # I quarter-line sono solo dove la doc ufficiale li ammette (2, 3, 28).
        assert spec_for("OU").quarter_line_eligible
        assert spec_for("AH").quarter_line_eligible
        assert set(SX_QUARTER_LINE_TYPES) <= set(SX_LINE_BEARING_TYPES)

    def test_mercati_con_linea_hanno_bounds(self):
        for market, spec in MARKET_SPECS.items():
            if spec.has_lines:
                assert spec.line_bounds, market
                assert spec.line_bounds[0] < spec.line_bounds[1]

    def test_correct_score_validato_da_regola_non_da_elenco(self):
        assert "CS" not in MARKET_SELECTIONS
        assert spec_for("CS").requires_score

    def test_tipi_sx_non_modellati_dichiarati(self):
        """Il tipo 52 e' vivo e liquido su SX: deve restare scritto cosa manca."""
        assert 52 in SX_TYPES_NOT_MODELLED and 835 in SX_TYPES_NOT_MODELLED
        assert 52 not in SX_TYPE_IDS and 17 in SX_TYPE_IDS

    def test_famiglia_including_overtime_modellata(self):
        """28/342/226 non sono piu' "non modellati": sono tipi canonici.

        Il 01/10/2026 il proprietario ha chiesto la telemetria sui mercati non
        calcistici (Basketball/NFL). I tre tipi esistono sulla doc ufficiale
        con forma identica ai mercati base, e su SX sono VIVI: 226 esce da
        SX_TYPES_NOT_MODELLED perche' ora il contratto lo rappresenta.
        """
        for type_id, market in ((28, MarketType.OVER_UNDER_OT),
                                (342, MarketType.ASIAN_HANDICAP_OT),
                                (226, MarketType.MONEYLINE_OT)):
            assert SX_TYPE_IDS[type_id] is market
            assert type_id not in SX_TYPES_NOT_MODELLED, type_id
            assert dict(spec_for(market).source_type_ids)["sxbet"] == type_id

    def test_famiglia_ot_stessa_forma_dei_mercati_base(self):
        """OU_OT/AH_OT ereditano la forma di OU/AH; ML_OT e' 2 esiti senza linea."""
        assert (spec_for("OU_OT").selections
                == spec_for("OU").selections == ("over", "under"))
        assert (spec_for("AH_OT").selections
                == spec_for("AH").selections == ("1", "2"))
        assert spec_for("OU_OT").has_lines and spec_for("AH_OT").has_lines
        assert not spec_for("ML_OT").has_lines
        assert spec_for("ML_OT").selections == ("1", "2")

    def test_bounds_ot_larghi_per_gli_sport_non_calcistici(self):
        """I bounds calcistici (0.5..12) respingerebbero OGNI riga basket/NFL.

        Il basket totalizza 150-260 punti e gli spread arrivano a ~30: se il
        contratto riusasse i limiti di OU/AH la telemetria sarebbe muta per
        costruzione (un dato perso non e' un dato corretto).
        """
        assert spec_for("OU_OT").line_bounds[1] >= 300
        assert spec_for("AH_OT").line_bounds[0] <= -50
        assert spec_for("AH_OT").line_bounds[1] >= 50

    @pytest.mark.parametrize("value,expected", [
        ("AH", MarketType.ASIAN_HANDICAP), ("asian handicap", MarketType.ASIAN_HANDICAP),
        ("spread", MarketType.ASIAN_HANDICAP), ("btts", MarketType.BOTH_TEAMS_TO_SCORE),
        ("Gol Gol", MarketType.BOTH_TEAMS_TO_SCORE), ("double chance", MarketType.DOUBLE_CHANCE),
        ("correct_score", MarketType.CORRECT_SCORE), ("totals", MarketType.OVER_UNDER),
        (MarketType.OVER_UNDER, MarketType.OVER_UNDER),
    ])
    def test_alias_mercati(self, value, expected):
        assert market_type_of(value) is expected
        assert spec_for(value).market_type is expected

    @pytest.mark.parametrize("value", ["corner", "nope", "", None, 12.5])
    def test_valori_non_mappati(self, value):
        assert market_type_of(value) is None
        assert spec_for(value) is None

    def test_helper_linee(self):
        assert line_required("OU") and line_required("asian handicap")
        assert not line_required("1X2") and not line_required("btts")
        assert market_accepts_lines("OU") and not market_accepts_lines("BTTS")


# ---------------------------------------------------------------------------
# 2. Le linee
# ---------------------------------------------------------------------------

class TestLinee:
    def test_ou_richiede_la_linea(self):
        with pytest.raises(MarketQuoteError) as exc:
            quote(market="OU", selection="over")
        assert codes(exc.value) == [QuoteErrorCode.LINE_REQUIRED.value]

    def test_ah_richiede_la_linea(self):
        with pytest.raises(MarketQuoteError) as exc:
            quote(market="AH", selection="1")
        assert codes(exc.value) == [QuoteErrorCode.LINE_REQUIRED.value]

    @pytest.mark.parametrize("market,selection", [("1X2", "1"), ("BTTS", "yes"),
                                                  ("DC", "1X"), ("CS", "2-1")])
    def test_linea_vietata_sui_mercati_senza_linee(self, market, selection):
        overrides = {"market": market, "selection": selection, "line": 2.5}
        if market == "CS":
            overrides.update(origin="derived", derived_from=["1X2"])
        with pytest.raises(MarketQuoteError) as exc:
            quote(**overrides)
        assert codes(exc.value) == [QuoteErrorCode.LINE_NOT_ALLOWED.value]

    @pytest.mark.parametrize("line", [2.5, 3.25, "2,5", 2, 0.5])
    def test_linee_ammesse_ou(self, line):
        assert quote(market="OU", selection="over", line=line).line is not None

    @pytest.mark.parametrize("line", [0.0, -1.5, 0.75, -0.75, 1.0])
    def test_linee_ammesse_ah(self, line):
        assert quote(market="AH", selection="1", line=line).line == line

    def test_passo_di_quarto(self):
        with pytest.raises(MarketQuoteError) as exc:
            quote(market="OU", selection="over", line=2.3)
        assert codes(exc.value) == [QuoteErrorCode.LINE_INVALID.value]

    def test_fuori_range(self):
        with pytest.raises(MarketQuoteError) as exc:
            quote(market="AH", selection="1", line=30.0)
        assert codes(exc.value) == [QuoteErrorCode.LINE_INVALID.value]

    def test_linea_non_numerica(self):
        with pytest.raises(MarketQuoteError) as exc:
            quote(market="OU", selection="over", line="molto")
        assert codes(exc.value) == [QuoteErrorCode.INVALID_TYPE.value]

    def test_linea_vuota_e_non_finita(self):
        # Una linea vuota non e' una linea: su OU resta l'obbligo, quindi rifiuto.
        with pytest.raises(MarketQuoteError) as exc:
            quote(market="OU", selection="over", line="   ")
        assert codes(exc.value) == [QuoteErrorCode.LINE_REQUIRED.value]
        with pytest.raises(MarketQuoteError):
            quote(market="OU", selection="over", line=float("inf"))

    @pytest.mark.parametrize("line,expected,market", [
        (2.5, "2.5", "OU"), (2.50, "2.5", "OU"), (2, "2", "OU"),
        (3.25, "3.25", "OU"), ("2,5", "2.5", "OU"),
        (-0.75, "-0.75", "AH"), (0, "0", "AH"), ("-0.25", "-0.25", "AH"),
    ])
    def test_line_key_canonica(self, line, expected, market):
        assert quote(market=market, selection="1" if market == "AH" else "over",
                     line=line).line_key == expected

    def test_line_key_vuota_senza_linee(self):
        assert quote(market="1X2", selection="1").line_key == ""

    def test_main_line_vietata_senza_linee(self):
        with pytest.raises(MarketQuoteError) as exc:
            quote(market="1X2", selection="1", main_line=True)
        assert codes(exc.value) == [QuoteErrorCode.LINE_NOT_ALLOWED.value]

    def test_main_line_su_ou(self):
        assert quote(market="OU", selection="over", line=2.5, main_line="true").main_line is True
        with pytest.raises(MarketQuoteError):
            quote(market="OU", selection="over", line=2.5, main_line="boh")


# ---------------------------------------------------------------------------
# 3. Gli esiti per mercato
# ---------------------------------------------------------------------------

class TestEsiti:
    @pytest.mark.parametrize("selection,expected", [
        ("yes", "yes"), ("Yes", "yes"), ("si", "yes"), ("gol gol", "yes"),
        ("no", "no"), ("no goal", "no"),
    ])
    def test_btts(self, selection, expected):
        assert quote(market="BTTS", selection=selection).selection == expected

    @pytest.mark.parametrize("selection,expected", [
        ("1X", "1X"), ("home or draw", "1X"), ("X2", "X2"), ("draw or away", "X2"),
        ("12", "12"), ("home or away", "12"),
    ])
    def test_double_chance(self, selection, expected):
        result = quote(market="DC", selection=selection, origin="derived",
                       derived_from=["1X2"])
        assert result.selection == expected

    @pytest.mark.parametrize("selection,expected", [
        ("3-1", "3-1"), ("3:1", "3-1"), ("0-0", "0-0"), ("10-12", "10-12"),
    ])
    def test_correct_score(self, selection, expected):
        result = quote(market="CS", selection=selection, origin="derived",
                       derived_from=["1X2"])
        assert result.selection == expected

    @pytest.mark.parametrize("selection", ["boh", "3", "3-", "-1", "", "vittoria casa"])
    def test_correct_score_non_valido(self, selection):
        with pytest.raises(MarketQuoteError):
            quote(market="CS", selection=selection, origin="derived",
                  derived_from=["1X2"])

    def test_punteggio_implausibile(self):
        with pytest.raises(MarketQuoteError) as exc:
            quote(market="CS", selection=f"{MAX_SCORE_GOALS + 1}-0", origin="derived",
                  derived_from=["1X2"])
        assert codes(exc.value) == [QuoteErrorCode.INVALID_SCORE.value]

    @pytest.mark.parametrize("market,selection", [
        ("1X2", "yes"), ("BTTS", "1"), ("BTTS", "over"), ("AH", "X"),
        ("AH", "over"), ("DC", "1"), ("DC", "X"), ("1X2", "1X"),
    ])
    def test_rifiuto_incrociato(self, market, selection):
        """L'esito deve appartenere al mercato: e' il bug del 09/09."""
        overrides = {"market": market, "selection": selection}
        if market == "AH":
            overrides["line"] = -0.5
        with pytest.raises(MarketQuoteError) as exc:
            quote(**overrides)
        assert QuoteErrorCode.SELECTION_MARKET_MISMATCH.value in codes(exc.value)


# ---------------------------------------------------------------------------
# 4. Provenienza delle quote
# ---------------------------------------------------------------------------

class TestProvenienza:
    def test_default_nativa(self):
        assert quote().origin == "native" and quote().is_derived is False

    def test_derivata_senza_provenienza_rifiutata(self):
        with pytest.raises(MarketQuoteError) as exc:
            quote(market="DC", selection="X2", origin="derived")
        assert codes(exc.value) == [QuoteErrorCode.DERIVED_MISSING_SOURCE.value]

    def test_nativa_con_derivazione_rifiutata(self):
        with pytest.raises(MarketQuoteError) as exc:
            quote(derived_from=["OU"])
        assert codes(exc.value) == [QuoteErrorCode.UNKNOWN_ORIGIN.value]

    def test_origine_ignota(self):
        with pytest.raises(MarketQuoteError) as exc:
            quote(origin="magic")
        assert codes(exc.value) == [QuoteErrorCode.UNKNOWN_ORIGIN.value]

    def test_derived_from_normalizzato(self):
        result = quote(market="DC", selection="12", origin="derived",
                       derived_from=["h2h", MarketType.MATCH_RESULT, "totals"])
        assert result.derived_from == ("1X2", "OU")     # dedup + canonico

    def test_derived_from_ignoto(self):
        with pytest.raises(MarketQuoteError) as exc:
            quote(market="DC", selection="12", origin="derived", derived_from=["corner"])
        assert codes(exc.value) == [QuoteErrorCode.UNKNOWN_MARKET_TYPE.value]

    def test_origini_ammesse(self):
        assert QUOTE_ORIGINS == ("native", "derived")


# ---------------------------------------------------------------------------
# 5. market / market_type: una cosa sola
# ---------------------------------------------------------------------------

class TestTipoMercato:
    def test_market_riempie_market_type(self):
        result = quote(market="h2h", selection="1")
        assert result.market == "1X2" and result.market_type is MarketType.MATCH_RESULT

    def test_market_type_riempie_market(self):
        result = parse_quote({k: v for k, v in payload().items() if k != "market"}
                             | {"market_type": "asian handicap", "selection": "2", "line": -0.5})
        assert result.market == "AH" and result.market_type is MarketType.ASIAN_HANDICAP

    def test_contraddizione_rifiutata(self):
        with pytest.raises(MarketQuoteError) as exc:
            quote(market="1X2", market_type="AH", line=-0.5)
        assert codes(exc.value) == [QuoteErrorCode.MARKET_TYPE_MISMATCH.value]

    def test_concordanza_accettata(self):
        assert quote(market="btts", market_type="BTTS", selection="yes").market == "BTTS"

    def test_market_type_ignoto(self):
        row = {k: v for k, v in payload().items() if k != "market"}
        with pytest.raises(MarketQuoteError) as exc:
            parse_quote(row | {"market_type": "corners"})
        assert QuoteErrorCode.UNKNOWN_MARKET_TYPE.value in codes(exc.value)

    def test_alias_di_chiave_del_feed(self):
        """Una riga col vocabolario del feed (marketType) non passa dal prepare."""
        result = parse_quote({"schema_version": MARKET_SCHEMA_VERSION,
                              "sportXeventId": FIXTURE, "marketType": "AH",
                              "selection": "2", "line": "-0.25", "price": 1.92,
                              "timestamp": "2026-09-18T21:20:00Z", "provider": "sxbet",
                              "gateway": "sxbet-feed"})
        assert result.event_id == FIXTURE and result.market == "AH" and result.line == -0.25


# ---------------------------------------------------------------------------
# 6. Identita' e rese leggibili
# ---------------------------------------------------------------------------

class TestIdentita:
    def test_la_linea_entra_nell_identita(self):
        due_punti = quote(market="OU", selection="over", line=2.5)
        tre_punti = quote(market="OU", selection="over", line=3.5)
        assert due_punti.identity_key != tre_punti.identity_key

    def test_identita_stabile_nel_tempo(self):
        first = quote(market="OU", selection="over", line=2.5)
        later = quote(market="OU", selection="over", line=2.5, odds=1.55,
                      timestamp="2026-09-18T22:00:00+00:00")
        assert first.identity_key == later.identity_key
        assert first.quote_id != later.quote_id

    def test_quote_id_cambia_con_la_linea(self):
        assert (quote(market="OU", selection="over", line=2.5).quote_id
                != quote(market="OU", selection="over", line=3.5).quote_id)

    def test_fixture_id_e_event_id(self):
        assert quote().fixture_id == quote().event_id == FIXTURE

    @pytest.mark.parametrize("market,selection,line,expected", [
        ("1X2", "1", None, "1"),
        ("1X2", "X", None, "X"),
        ("OU", "over", 2.5, "Over 2.5"),
        ("OU", "under", 3.25, "Under 3.25"),
        ("AH", "1", -0.75, "Home -0.75"),
        ("AH", "2", -0.75, "Away +0.75"),
        ("AH", "1", 0.25, "Home +0.25"),
        ("AH", "2", 0, "Away 0"),
        ("BTTS", "yes", None, "Yes"),
        ("BTTS", "no", None, "No"),
        ("DC", "1X", None, "1X"),
        ("CS", "3-1", None, "3-1"),
    ])
    def test_ledger_esito(self, market, selection, line, expected):
        """Il formato che `tracker`/`ml_audit` gia' scrivono per AH/OU/BTTS."""
        overrides = {"market": market, "selection": selection, "line": line}
        if market in ("DC", "CS"):
            overrides.update(origin="derived", derived_from=["1X2"])
        assert quote(**overrides).ledger_esito == expected

    def test_handicap_della_selezione(self):
        casa = quote(market="AH", selection="1", line=-0.75)
        ospite = quote(market="AH", selection="2", line=-0.75)
        assert casa.handicap_for_selection == -0.75
        assert ospite.handicap_for_selection == 0.75
        assert quote(market="OU", selection="over", line=2.5).handicap_for_selection is None

    def test_to_signal_fields_outcome_solo_per_1x2(self):
        uno = quote(market="1X2", selection="2").to_signal_fields()
        assert uno["outcome"] == "2" and uno["market_type"] == "1X2"
        ou = quote(market="OU", selection="over", line=2.5).to_signal_fields()
        assert "outcome" not in ou
        assert ou["market_type"] == "OU" and ou["line"] == 2.5


# ---------------------------------------------------------------------------
# 7. Serializzazione (contratto verso il gateway SQLite)
# ---------------------------------------------------------------------------

class TestSerializzazione:
    def test_riga_copre_i_campi_dichiarati(self):
        row = quote(market="AH", selection="1", line=-0.75).as_row()
        assert set(row) == set(MARKET_ROW_FIELDS)

    def test_riga_json_safe(self):
        row = quote(market="CS", selection="3-1", origin="derived",
                    derived_from=["1X2"], provider_market_id="0xabc").as_row()
        text = json.dumps(row)                      # nessun datetime, nessun enum
        assert json.loads(text)["market_type"] == "CS"
        assert row["extra"] == {"provider_market_id": "0xabc"}

    def test_riga_chiave_composta(self):
        row = quote(market="OU", selection="over", line=2.5).as_row()
        assert (row["fixture_id"], row["market_type"], row["line_key"],
                row["selection"]) == (FIXTURE, "OU", "2.5", "over")

    def test_riga_di_una_derivata_dichiara_la_provenienza(self):
        row = quote(market="DC", selection="X2", origin="derived",
                    derived_from=["1X2"]).as_row()
        assert row["origin"] == "derived" and row["derived_from"] == ["1X2"]

    def test_selection_label_di_default_e_leggibile(self):
        assert quote(market="OU", selection="under", line=3.5).as_row()["selection_label"] == "Under 3.5"


# ---------------------------------------------------------------------------
# 8. Un fixture, molti mercati
# ---------------------------------------------------------------------------

def fixture_rows(**overrides):
    """Le quote tipiche di un fixture: 1X2, OU su due linee, AH, BTTS (senza DC)."""
    rows = [
        payload(market="1X2", selection="1", odds=2.10),
        payload(market="1X2", selection="X", odds=3.40),
        payload(market="1X2", selection="2", odds=3.60),
        payload(market="OU", selection="over", odds=1.95, line=2.5, main_line=True),
        payload(market="OU", selection="under", odds=1.90, line=2.5),
        payload(market="OU", selection="over", odds=2.60, line=3.5),
        payload(market="AH", selection="1", odds=1.85, line=-0.25),
        payload(market="AH", selection="2", odds=2.00, line=-0.25),
        payload(market="BTTS", selection="yes", odds=1.72),
    ]
    for row in rows:
        row.update(overrides)
    return rows


class TestFixtureQuotes:
    def test_raggruppa_i_mercati_della_stessa_partita(self):
        batch = validate_fixture_quotes(fixture_rows())
        assert batch.ok and len(batch.fixtures) == 1
        fixture = batch.fixture(FIXTURE)
        assert fixture is not None and len(fixture.quotes) == 9
        assert [m.value for m in fixture.market_types()] == ["1X2", "OU", "AH", "BTTS"]

    def test_quote_di_un_altro_fixture_respinte_in_blocco(self):
        """Mescolare due partite e' il bug piu' costoso: il gruppo e' respinto."""
        from decision import FixtureQuotes
        good = validate_fixture_quotes(fixture_rows()).fixtures[0]
        intruder = quote(event_id="sx-ALTRA", market="OU", selection="over", line=2.5)
        with pytest.raises(Exception) as exc:
            FixtureQuotes(fixture_id=FIXTURE, kickoff=good.kickoff,
                          quotes=list(good.quotes) + [intruder])
        assert QuoteErrorCode.FIXTURE_MISMATCH.value in str(exc.value)

    def test_kickoff_incoerente(self):
        """L'incoerenza si vede anche se il contenitore non dichiara il kickoff."""
        from decision import FixtureQuotes
        quotes = [quote(), quote(selection="2", kickoff="2026-09-18T20:00:00+00:00")]
        for container_kickoff in (None, "2026-09-18T23:30:00+00:00"):
            with pytest.raises(Exception) as exc:
                FixtureQuotes(fixture_id=FIXTURE, kickoff=container_kickoff, quotes=quotes)
            assert QuoteErrorCode.FIXTURE_MISMATCH.value in str(exc.value)

    def test_fixture_vuoto_rifiutato(self):
        from decision import FixtureQuotes
        with pytest.raises(Exception):
            FixtureQuotes(fixture_id=FIXTURE, quotes=[])

    def test_linee_disponibili(self):
        fixture = validate_fixture_quotes(fixture_rows()).fixtures[0]
        assert fixture.lines_for("OU") == [2.5, 3.5]
        assert fixture.lines_for("AH") == [-0.25]
        assert fixture.lines_for("1X2") == []

    def test_quotes_for_con_linea(self):
        fixture = validate_fixture_quotes(fixture_rows()).fixtures[0]
        assert len(fixture.quotes_for("OU")) == 3
        assert len(fixture.quotes_for("OU", 2.5)) == 2
        assert {q.selection for q in fixture.quotes_for("OU", 3.5)} == {"over"}

    def test_main_line_dichiarata_dalla_fonte(self):
        fixture = validate_fixture_quotes(fixture_rows()).fixtures[0]
        assert fixture.main_line("OU") == 2.5
        assert fixture.main_line("AH") is None       # la fonte non la dichiara

    def test_completezza_per_mercato_e_linea(self):
        fixture = validate_fixture_quotes(fixture_rows()).fixtures[0]
        assert fixture.is_complete("1X2") is True
        assert fixture.is_complete("OU", 2.5) is True
        assert fixture.is_complete("OU", 3.5) is False   # manca l'under
        assert fixture.is_complete("BTTS") is False      # manca il no

    def test_completezza_non_decidibile_per_il_risultato_esatto(self):
        rows = fixture_rows() + [payload(market="CS", selection="1-0", odds=7.5,
                                        origin="derived", derived_from=["1X2"])]
        fixture = validate_fixture_quotes(rows).fixtures[0]
        assert fixture.is_complete("CS") is None     # non enumerabile: mai un falso

    def test_report_dei_mercati_incompleti(self):
        fixture = validate_fixture_quotes(fixture_rows()).fixtures[0]
        incomplete = fixture.incomplete()
        by_market = {(item["market_type"], item["line_key"]): item for item in incomplete}
        assert by_market[("BTTS", "")]["missing"] == ["no"]
        assert by_market[("OU", "3.5")]["missing"] == ["under"]
        assert ("1X2", "") not in by_market          # completo
        assert by_market[("OU", "3.5")]["present"] == 1

    def test_as_rows_ordinate_e_json_safe(self):
        fixture = validate_fixture_quotes(fixture_rows()).fixtures[0]
        rows = fixture.as_rows()
        assert len(rows) == 9
        assert set(rows[0]) == set(MARKET_ROW_FIELDS)
        json.dumps(rows)
        keys = [(r["market_type"], r["line_key"], r["selection"]) for r in rows]
        assert keys == sorted(keys)

    def test_summary(self):
        fixture = validate_fixture_quotes(fixture_rows()).fixtures[0]
        summary = fixture.summary()
        assert summary["fixture_id"] == FIXTURE and summary["quotes"] == 9
        assert summary["markets"] == ["1X2", "OU", "AH", "BTTS"]
        assert summary["lines"]["OU"] == [2.5, 3.5]
        assert summary["incomplete"]


# ---------------------------------------------------------------------------
# 9. Ingresso a lotti multi-mercato
# ---------------------------------------------------------------------------

class TestValidateFixtureQuotes:
    def test_una_riga_rotta_non_ferma_le_altre(self):
        rows = fixture_rows() + [payload(market="AH", selection="1")]   # senza linea
        batch = validate_fixture_quotes(rows)
        assert batch.quotes == 9 and not batch.ok
        assert batch.by_code() == {QuoteErrorCode.LINE_REQUIRED.value: 1}
        assert batch.total == 10 and batch.rejected_rows == 1

    def test_conta_per_fixture(self):
        rows = fixture_rows() + [payload(event_id="sx-ALTRA", market="1X2", selection="1")]
        batch = validate_fixture_quotes(rows)
        assert len(batch.fixtures) == 2 and batch.quotes == 10

    def test_riepilogo(self):
        batch = validate_fixture_quotes(fixture_rows(), gateway_id="sxbet-feed")
        data = batch.as_dict()
        assert data["fixtures"] == 1 and data["accepted"] == 9
        assert data["rejected"] == 0 and data["by_code"] == {}

    def test_anti_flood(self):
        rows = [payload(market="AH", selection="1") for _ in range(50)]
        batch = validate_fixture_quotes(rows, max_events=5)
        assert len(batch.rejected) == 5 and batch.suppressed_events == 45

    def test_riga_ostile_non_esplode(self):
        class Ostile(dict):
            def get(self, key, default=None):
                raise RuntimeError("riga ostile")

        batch = validate_fixture_quotes([Ostile(), payload()])
        assert batch.quotes == 1 and batch.rejected

    def test_lotto_vuoto(self):
        batch = validate_fixture_quotes([])
        assert batch.total == 0 and batch.quotes == 0 and batch.ok

    def test_legacy_1_0_continua_a_funzionare(self):
        """Retrocompatibilita': le righe 1.0 (1X2 senza linea) restano valide."""
        batch = validate_fixture_quotes([payload(schema_version="1.0")])
        assert batch.ok and batch.fixtures[0].quotes[0].market_type is MarketType.MATCH_RESULT

    def test_ou_1_0_senza_linea_ora_e_respinto(self):
        """Cambio di contratto DICHIARATO: un OU senza linea non e' eseguibile."""
        batch = validate_fixture_quotes([payload(schema_version="1.0", market="OU",
                                                 selection="over")])
        assert batch.by_code() == {QuoteErrorCode.LINE_REQUIRED.value: 1}


# ---------------------------------------------------------------------------
# 10. Purezza del modulo (nessuna rete, nessun DB)
# ---------------------------------------------------------------------------

class TestPurezza:
    def test_import_non_carica_la_produzione(self):
        """Il contratto e' un tipo: non deve tirare dentro DB, bot o provider."""
        code = ("import sys; import decision.market as m; "
                "proibiti = [n for n in ('tracker', 'auto_bet', 'bot', 'requests', "
                "'odds_api', 'sx_signals', 'psycopg2') if n in sys.modules]; "
                "print('|'.join(proibiti))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "", out.stdout

    def test_nessun_import_di_rete_nel_sorgente(self):
        from pathlib import Path
        text = Path("decision/market.py").read_text(encoding="utf-8")
        for proibito in ("import requests", "import urllib", "socket.",
                         "from tracker", "import tracker"):
            assert proibito not in text, proibito
