"""Test del contratto di mercato (`decision/market.py`) — tutti OFFLINE.

Nessuna rete, nessun DB, nessun provider: il contratto e' pydantic puro. Il
sink di osservabilita' e' iniettato (`ListSink`), quindi nessun evento finisce
sul volume. I dati sono finti e costruiti a mano: **zero crediti API**.
"""

import io
import json
import logging
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from decision import (
    MARKET_SCHEMA_VERSION, MIN_ODDS, SUPPORTED_MARKETS, SUPPORTED_SCHEMA_VERSIONS,
    MarketQuote, MarketQuoteError, MarketType, QuoteErrorCode, parse_quote,
    prepare_payload, validate_batch,
)
from decision.__main__ import SAMPLE_QUOTES, main
from decision.middleware import ListSink, Observability

#: I campi chiave richiesti dal contratto (evento, mercato, esito, quota,
#: timestamp, fonte, gateway, versione schema).
KEY_FIELDS = ("schema_version", "event_id", "market", "selection", "odds",
              "timestamp", "source", "gateway_id")


def payload(**overrides):
    """Payload conforme al contratto; gli override lo rendono non conforme."""
    base = {
        "schema_version": MARKET_SCHEMA_VERSION,
        "event_id": "sx-L19947936",
        "market": "1X2",
        "selection": "1",
        "odds": 1.69,
        "timestamp": "2026-09-15T17:20:00+00:00",
        "source": "sxbet",
        "gateway_id": "sx-feed",
    }
    base.update(overrides)
    return base


def sink_obs():
    """(sink, osservabilita') con sink in memoria: i test leggono gli eventi."""
    sink = ListSink()
    return sink, Observability(sink=sink)


def codes(error: MarketQuoteError) -> list[str]:
    return error.codes()


# ---------------------------------------------------------------------------
# 1. Il contratto
# ---------------------------------------------------------------------------

class TestContratto:
    def test_campi_chiave_obbligatori(self):
        """I campi del contratto esistono e sono OBBLIGATORI (nessun default)."""
        for field in KEY_FIELDS:
            assert field in MarketQuote.model_fields, field
            assert MarketQuote.model_fields[field].is_required(), field

    def test_quota_minima_dichiarata(self):
        assert MIN_ODDS == 0.1
        quote = parse_quote(payload(odds=MIN_ODDS))
        assert quote.odds == MIN_ODDS

    def test_versione_schema_dichiarata(self):
        assert MARKET_SCHEMA_VERSION in SUPPORTED_SCHEMA_VERSIONS
        # Lo schema 2.0 apre il contratto a TUTTI i mercati del registro:
        # la lista non si scrive a mano (altrimenti diverge dal registro).
        assert SUPPORTED_MARKETS == tuple(m.value for m in MarketType)
        # La famiglia "including overtime" (01/10/2026) fa parte del registro:
        # e' la lista dei mercati che il contratto sa rappresentare.
        assert {"1X2", "OU", "AH", "BTTS", "DC", "CS",
                "OU_OT", "AH_OT", "ML_OT"} == set(SUPPORTED_MARKETS)

    def test_quote_valida_normalizza(self):
        quote = parse_quote(payload(market="h2h", selection="home", odds="1.69",
                                    event_name="CSKA - Rubin"))
        assert quote.market == "1X2"
        assert quote.selection == "1"
        assert quote.odds == 1.69 and isinstance(quote.odds, float)
        assert quote.timestamp.tzinfo is not None
        assert quote.event_name == "CSKA - Rubin"

    def test_quote_id_stabile_e_identity_senza_tempo(self):
        first = parse_quote(payload())
        same = parse_quote(payload())
        later = parse_quote(payload(odds=1.85, timestamp="2026-09-15T18:00:00+00:00"))
        assert first.quote_id == same.quote_id          # stessa rilevazione
        assert len(first.quote_id) == 12
        assert first.identity_key == later.identity_key  # stesso evento/mercato/esito
        other = parse_quote(payload(selection="2"))
        assert other.identity_key != first.identity_key

    def test_campi_fuori_contratto_non_persi(self):
        quote = parse_quote(payload(depth_usdc=120.5, provider_market_id="0xdeadbeef"))
        assert quote.depth_usdc == 120.5
        assert quote.extra_fields == {"provider_market_id": "0xdeadbeef"}

    def test_to_signal_fields(self):
        quote = parse_quote(payload(league="Premier League", odds=1.6))
        fields = quote.to_signal_fields()
        assert fields["match_id"] == "sx-L19947936"
        assert fields["outcome"] == "1"
        assert fields["price"] == 1.6
        assert fields["price_source"] == "sxbet"
        assert fields["kickoff"] == quote.timestamp       # senza kickoff esplicito
        # L'OU non inventa un `outcome` che il Signal non accetta.
        ou = parse_quote(payload(market="OU", selection="over", line=2.5))
        assert "outcome" not in ou.to_signal_fields()

    def test_age_seconds(self):
        moment = datetime.now(timezone.utc) - timedelta(minutes=10)
        quote = parse_quote(payload(timestamp=moment.isoformat()))
        assert 590 <= quote.age_seconds() <= 610


# ---------------------------------------------------------------------------
# 2. Quota: minimo 0.1 e valori non plausibili
# ---------------------------------------------------------------------------

class TestQuota:
    @pytest.mark.parametrize("odds", [0.0999, 0.05, 0.0, -1.5])
    def test_sotto_il_minimo(self, odds):
        with pytest.raises(MarketQuoteError) as exc:
            parse_quote(payload(odds=odds))
        assert codes(exc.value) == [QuoteErrorCode.ODDS_BELOW_MIN.value]
        assert exc.value.issues[0].field == "odds"

    def test_confine_esatto_ammesso(self):
        assert parse_quote(payload(odds=0.1)).odds == 0.1
        assert parse_quote(payload(odds=0.1000001)).odds == 0.1000001

    @pytest.mark.parametrize("odds", [float("inf"), float("-inf"), float("nan")])
    def test_non_finita(self, odds):
        with pytest.raises(MarketQuoteError) as exc:
            parse_quote(payload(odds=odds))
        assert codes(exc.value) == [QuoteErrorCode.ODDS_NOT_FINITE.value]

    @pytest.mark.parametrize("odds", ["1.75", " 1.75 ", "1,75", 2, "2.50"])
    def test_formati_numerici_accettati(self, odds):
        assert parse_quote(payload(odds=odds)).odds == pytest.approx(float(str(odds).replace(",", ".").strip()))

    @pytest.mark.parametrize("odds", ["abc", "", "1.7.5", True, None, [1.7]])
    def test_tipi_non_numerici(self, odds):
        with pytest.raises(MarketQuoteError) as exc:
            parse_quote(payload(odds=odds))
        # `None`/assente lo segnala pydantic, gli altri li intercetta il contratto.
        assert codes(exc.value)[0] in (QuoteErrorCode.INVALID_TYPE.value,
                                       QuoteErrorCode.MISSING_FIELD.value)


# ---------------------------------------------------------------------------
# 3. Timestamp
# ---------------------------------------------------------------------------

class TestTimestamp:
    def test_naive_respinto(self):
        with pytest.raises(MarketQuoteError) as exc:
            parse_quote(payload(timestamp="2026-09-15T17:20:00"))
        assert codes(exc.value) == [QuoteErrorCode.TIMESTAMP_NAIVE.value]
        assert "assume_utc" in exc.value.issues[0].detail

    def test_assume_utc_esplicito(self):
        quote = parse_quote(payload(timestamp="2026-09-15T17:20:00"), assume_utc=True)
        assert quote.timestamp == datetime(2026, 9, 15, 17, 20, tzinfo=timezone.utc)

    def test_fuso_convertito_in_utc(self):
        quote = parse_quote(payload(timestamp="2026-09-15T19:20:00+02:00"))
        assert quote.timestamp == datetime(2026, 9, 15, 17, 20, tzinfo=timezone.utc)

    def test_suffisso_z(self):
        assert parse_quote(payload(timestamp="2026-09-15T17:20:00Z")).timestamp.hour == 17

    def test_datetime_oggetto(self):
        aware = datetime.now(timezone.utc)
        assert parse_quote(payload(timestamp=aware)).timestamp == aware

    def test_kickoff_naive_respinto(self):
        with pytest.raises(MarketQuoteError) as exc:
            parse_quote(payload(kickoff="2026-09-15T19:45:00"))
        issue = exc.value.issues[0]
        assert issue.code is QuoteErrorCode.TIMESTAMP_NAIVE and issue.field == "kickoff"

    def test_kickoff_facoltativo(self):
        assert parse_quote(payload()).kickoff is None

    def test_data_non_iso(self):
        with pytest.raises(MarketQuoteError) as exc:
            parse_quote(payload(timestamp="ieri sera"))
        assert codes(exc.value) == [QuoteErrorCode.INVALID_TYPE.value]

    def test_epoch_non_accettato(self):
        """Numeri epoch (s o ms): ambigui per definizione -> rifiuto, mai indovinare."""
        with pytest.raises(MarketQuoteError) as exc:
            parse_quote(payload(timestamp=1757950000))
        assert codes(exc.value) == [QuoteErrorCode.INVALID_TYPE.value]


# ---------------------------------------------------------------------------
# 4. Versione dello schema
# ---------------------------------------------------------------------------

class TestSchemaVersion:
    def test_mancante_e_respinta(self):
        row = payload()
        row.pop("schema_version")
        with pytest.raises(MarketQuoteError) as exc:
            parse_quote(row)
        assert codes(exc.value) == [QuoteErrorCode.MISSING_FIELD.value]

    def test_versione_non_supportata(self):
        with pytest.raises(MarketQuoteError) as exc:
            parse_quote(payload(schema_version="9.9"))
        assert codes(exc.value) == [QuoteErrorCode.UNSUPPORTED_SCHEMA.value]

    def test_schema_1_0_ancora_supportato(self):
        """Retrocompatibilita': un produttore vecchio (1.0) resta valido."""
        quote = parse_quote(payload(schema_version="1.0"))
        assert quote.schema_version == "1.0"
        assert quote.market_type is MarketType.MATCH_RESULT

    def test_default_di_feed_solo_se_assente(self):
        row = payload()
        row.pop("schema_version")
        assert parse_quote(row, schema_version=MARKET_SCHEMA_VERSION).schema_version == MARKET_SCHEMA_VERSION
        # Il valore della riga vince: il default non sovrascrive mai.
        assert parse_quote(payload(schema_version="1.0"),
                           schema_version="9.9").schema_version == "1.0"


# ---------------------------------------------------------------------------
# 5. Campi obbligatori ed errori multipli
# ---------------------------------------------------------------------------

class TestCampiObbligatori:
    @pytest.mark.parametrize("field", ("event_id", "source", "gateway_id"))
    def test_mancante(self, field):
        row = payload()
        row.pop(field)
        with pytest.raises(MarketQuoteError) as exc:
            parse_quote(row)
        issue = exc.value.issues[0]
        assert issue.code is QuoteErrorCode.MISSING_FIELD and issue.field == field
        assert "obbligatorio" in issue.detail           # messaggio leggibile nei log

    @pytest.mark.parametrize("field", ("event_id", "source", "gateway_id"))
    @pytest.mark.parametrize("value", ["", "   "])
    def test_vuoto(self, field, value):
        with pytest.raises(MarketQuoteError) as exc:
            parse_quote(payload(**{field: value}))
        issue = exc.value.issues[0]
        assert issue.code is QuoteErrorCode.EMPTY_FIELD and issue.field == field

    def test_tutti_i_problemi_insieme(self):
        """Una sola passata: il rifiuto dice TUTTO cio' che non va, non solo il primo."""
        with pytest.raises(MarketQuoteError) as exc:
            parse_quote({})
        assert len(exc.value.issues) == len(KEY_FIELDS)
        assert set(codes(exc.value)) == {QuoteErrorCode.MISSING_FIELD.value}
        assert "8 problemi" in str(exc.value)

    def test_payload_non_oggetto(self):
        with pytest.raises(MarketQuoteError) as exc:
            parse_quote("non un oggetto")
        assert codes(exc.value) == [QuoteErrorCode.INVALID_TYPE.value]

    def test_alias_di_chiave_ma_canonico_vince(self):
        row = payload()
        row.pop("event_id")
        row["match_id"] = "sx-ALIAS"
        assert parse_quote(row).event_id == "sx-ALIAS"
        row["event_id"] = "sx-CANONICO"
        assert parse_quote(row).event_id == "sx-CANONICO"

    def test_default_di_feed_gateway(self):
        row = payload()
        row.pop("gateway_id")
        assert parse_quote(row, gateway_id="sx-feed").gateway_id == "sx-feed"
        assert parse_quote(row, gateway_id="feed-generico").gateway_id == "feed-generico"

    def test_prepare_payload_non_corregge_valori(self):
        """La preparazione normalizza le chiavi, NON aggiusta i valori sbagliati."""
        prepared, issues = prepare_payload({"match_id": "x", "odd": "0.02",
                                            "timestamp": "2026-09-15T10:00:00"})
        assert issues == []
        assert prepared["event_id"] == "x" and prepared["odds"] == "0.02"
        assert prepared["timestamp"] == "2026-09-15T10:00:00"      # resta naive
        naive, _ = prepare_payload({"timestamp": "2026-09-15T10:00:00"}, assume_utc=True)
        assert naive["timestamp"].tzinfo is not None


# ---------------------------------------------------------------------------
# 6. Mercato e selezione (alias + coerenza incrociata)
# ---------------------------------------------------------------------------

class TestMercatoSelezione:
    @pytest.mark.parametrize("market,selection,expected", [
        ("1X2", "1", "1X2"), ("1x2", "1", "1X2"), ("h2h", "1", "1X2"),
        ("Match Odds", "1", "1X2"), ("match_odds", "1", "1X2"),
        ("Money Line", "1", "1X2"), ("12", "1", "1X2"),
        ("OU", "over", "OU"), ("ou", "over", "OU"),
        ("Over/Under", "over", "OU"), ("totals", "under", "OU"),
        ("total", "over", "OU"), (" O/U ", "over", "OU"),
    ])
    def test_alias_mercato(self, market, selection, expected):
        # Dal 18/09 (schema 2.0) l'OU comporta una LINEA: senza non e' eseguibile.
        extra = {"line": 2.5} if expected == "OU" else {}
        assert parse_quote(payload(market=market, selection=selection,
                                   **extra)).market == expected

    @pytest.mark.parametrize("selection,expected", [
        ("1", "1"), ("home", "1"), ("Home", "1"), ("casa", "1"), (1, "1"),
        ("X", "X"), ("draw", "X"), ("tie", "X"),
        ("2", "2"), ("away", "2"), (2, "2"),
    ])
    def test_alias_selezione(self, selection, expected):
        assert parse_quote(payload(selection=selection)).selection == expected

    def test_alias_selezione_ou(self):
        """Dal 18/09 un OU ESIGE la linea: senza, l'esito non e' eseguibile."""
        assert parse_quote(payload(market="OU", selection="over", line=2.5)).selection == "over"
        assert parse_quote(payload(market="OU", selection="U", line=2.5)).selection == "under"

    @pytest.mark.parametrize("market", ["corner", "nope", "risultato finale"])
    def test_mercato_sconosciuto(self, market):
        with pytest.raises(MarketQuoteError) as exc:
            parse_quote(payload(market=market))
        assert codes(exc.value) == [QuoteErrorCode.UNKNOWN_MARKET.value]

    @pytest.mark.parametrize("market,expected", [
        ("asian handicap", "AH"), ("spread", "AH"), ("btts", "BTTS"),
        ("gol gol", "BTTS"), ("dc", "DC"), ("correct score", "CS"),
    ])
    def test_mercati_del_registro_riconosciuti(self, market, expected):
        """I mercati aggiunti dallo schema 2.0 non sono piu' 'sconosciuti'."""
        from decision import spec_for
        assert spec_for(market).market_type.value == expected

    def test_selezione_sconosciuta(self):
        with pytest.raises(MarketQuoteError) as exc:
            parse_quote(payload(selection="nessuno"))
        assert codes(exc.value) == [QuoteErrorCode.UNKNOWN_SELECTION.value]

    @pytest.mark.parametrize("market,selection", [("1X2", "over"), ("1X2", "under"),
                                                  ("OU", "1"), ("OU", "X"), ("OU", "2")])
    def test_selezione_fuori_mercato(self, market, selection):
        """E' il controllo che il 09/09 mancava: un `over` saldato su un 1X2."""
        with pytest.raises(MarketQuoteError) as exc:
            parse_quote(payload(market=market, selection=selection, line=2.5))
        assert QuoteErrorCode.SELECTION_MARKET_MISMATCH.value in codes(exc.value)
        issue = exc.value.issues[0]
        assert issue.code is QuoteErrorCode.SELECTION_MARKET_MISMATCH
        assert issue.field == "selection"

    def test_mercato_non_testuale(self):
        with pytest.raises(MarketQuoteError) as exc:
            parse_quote(payload(market={"name": "1X2"}))
        assert codes(exc.value) == [QuoteErrorCode.INVALID_TYPE.value]


# ---------------------------------------------------------------------------
# 7. Validazione all'ingresso: log ed eventi
# ---------------------------------------------------------------------------

class TestIngressoLogging:
    def test_rifiuto_loggato_e_riprodotto_come_evento(self, caplog):
        sink, obs = sink_obs()
        with caplog.at_level(logging.ERROR, logger="decision.market"):
            with pytest.raises(MarketQuoteError):
                parse_quote(payload(odds=0.05), obs=obs)
        lines = [record.getMessage() for record in caplog.records]
        assert any("odds_below_min" in line and "odds" in line for line in lines)
        events = sink.of("market.quote_rejected")
        assert len(events) == 1
        event = events[0]
        assert event["error_code"] == "odds_below_min"
        assert event["field"] == "odds"
        assert event["gateway_id"] == "sx-feed"
        assert event["source"] == "sxbet"
        assert event["event_id"] == "sx-L19947936"
        assert event["outcome"] == "rejected"

    def test_ogni_problema_ha_il_suo_evento(self):
        sink, obs = sink_obs()
        row = payload()
        with pytest.raises(MarketQuoteError):
            parse_quote({**row, "odds": 0.02}, obs=obs)
        with pytest.raises(MarketQuoteError):
            parse_quote({**row, "schema_version": "9.9", "selection": ""}, obs=obs)
        emitted = [e["error_code"] for e in sink.of("market.quote_rejected")]
        assert emitted == ["odds_below_min", "unsupported_schema", "empty_field"]

    def test_nessun_evento_senza_sink(self, caplog):
        """Il contratto logga sempre; l'evento strutturato solo se c'e' il middleware."""
        with caplog.at_level(logging.ERROR, logger="decision.market"):
            with pytest.raises(MarketQuoteError):
                parse_quote(payload(odds=0.02))
        assert any("odds_below_min" in r.getMessage() for r in caplog.records)

    def test_accettazione_non_loggata_di_default(self):
        sink, obs = sink_obs()
        parse_quote(payload(), obs=obs)
        assert sink.events == []                     # niente flood di eventi

    def test_accettazione_loggata_su_richiesta(self):
        sink, obs = sink_obs()
        quote = parse_quote(payload(), obs=obs, log_accepted=True)
        accepted = sink.of("market.quote_accepted")
        assert len(accepted) == 1
        assert accepted[0]["quote_id"] == quote.quote_id
        assert accepted[0]["odds"] == 1.69

    def test_valore_lungo_troncato_nei_log(self, caplog):
        with caplog.at_level(logging.ERROR, logger="decision.market"):
            with pytest.raises(MarketQuoteError):
                parse_quote(payload(odds="x" * 500))
        line = " ".join(r.getMessage() for r in caplog.records)
        assert len(line) < 400                       # mai il payload intero

    def test_errori_non_bloccano_il_canale_di_log(self):
        class BrokenSink:
            name = "broken"

            def write(self, event):
                raise RuntimeError("disco pieno")

        obs = Observability(sink=BrokenSink())
        with pytest.raises(MarketQuoteError):
            parse_quote(payload(odds=0.02), obs=obs)   # l'eccezione resta quella giusta


# ---------------------------------------------------------------------------
# 8. Lotti (`validate_batch`)
# ---------------------------------------------------------------------------

class TestBatch:
    def test_lotto_misto(self):
        sink, obs = sink_obs()
        batch = validate_batch([payload(), payload(odds=0.02), "nope"],
                              gateway_id="sx-feed", obs=obs)
        assert batch.total == 3
        assert len(batch.accepted) == 1 and len(batch.rejected) == 2
        assert batch.ok is False
        assert batch.by_code()["odds_below_min"] == 1
        by_index = {rejection.index: rejection for rejection in batch.rejected}
        assert set(by_index) == {1, 2}
        assert by_index[1].code is QuoteErrorCode.ODDS_BELOW_MIN
        assert by_index[2].raw_keys == []                 # riga non mappabile
        assert by_index[1].raw_keys[0] == "event_id"       # ...l'altra si'

    def test_lotto_pulito(self):
        batch = validate_batch([payload(), payload(selection="2")])
        assert batch.ok is True and len(batch.accepted) == 2
        assert batch.as_dict()["rejected"] == 0

    def test_lotto_vuoto(self):
        batch = validate_batch(None)
        assert batch.total == 0 and batch.ok is True and batch.accepted == []

    def test_lotto_non_iterabile(self):
        batch = validate_batch(42)
        assert batch.rejected[0].code is QuoteErrorCode.INVALID_TYPE
        assert "non iterabile" in batch.rejected[0].detail

    def test_non_solleva_mai(self):
        """Nemmeno con righe assurde: il lotto e' fail-safe per contratto."""
        class Esplode(dict):
            def get(self, *_args, **_kwargs):
                raise RuntimeError("riga ostile")

        batch = validate_batch([Esplode({"event_id": "x"}), {"strano": True}, 3.14])
        assert len(batch.rejected) >= 2 and batch.accepted == []

    def test_riepilogo_loggato_e_evento(self, caplog):
        sink, obs = sink_obs()
        with caplog.at_level(logging.WARNING, logger="decision.market"):
            batch = validate_batch([payload(), payload(odds=0.01, market="nope")],
                                   gateway_id="sx-feed", obs=obs)
        assert any("quote respinte" in r.getMessage() for r in caplog.records)
        summary = sink.of("market.batch_validated")
        assert len(summary) == 1
        assert summary[0]["accepted"] == 1
        assert summary[0]["rejected_rows"] == 1        # una riga...
        assert summary[0]["issues"] == 2               # ...due problemi
        assert summary[0]["by_code"]["odds_below_min"] == 1
        assert summary[0]["outcome"] == "rejected"
        assert len(batch.rejected) == 2
        assert batch.rejected_rows == 1 and batch.issues == 2
        assert "1/2 quote respinte, 2 problemi" in " ".join(r.getMessage()
                                                             for r in caplog.records)

    def test_event_id_recuperato_dall_alias(self):
        batch = validate_batch([{"match_id": "sx-999", "odds": 0.01}])
        assert batch.rejected[0].event_id == "sx-999"
        assert "match_id" in batch.rejected[0].raw_keys

    def test_anti_flood_degli_eventi(self):
        sink, obs = sink_obs()
        rows = [payload(odds=0.01) for _ in range(30)]
        batch = validate_batch(rows, obs=obs, max_events=1)
        emitted = len(sink.of("market.quote_rejected"))
        assert len(batch.rejected) == 30
        assert 0 < emitted < 30                       # il primo problema passa...
        assert batch.suppressed_events == 30 - emitted  # ...gli altri non inondano
        assert len(sink.of("market.batch_validated")) == 1

    def test_batch_as_dict(self):
        batch = validate_batch([payload(odds=0.01)], gateway_id="feed", source="sx")
        data = json.loads(json.dumps(batch.as_dict()))    # serializzabile
        assert data["gateway_id"] == "feed" and data["source"] == "sx"


# ---------------------------------------------------------------------------
# 9. Purezza e CLI
# ---------------------------------------------------------------------------

class TestPurezza:
    def test_nessun_import_di_produzione(self):
        source = Path("decision/market.py").read_text(encoding="utf-8")
        for banned in ("import tracker", "from tracker", "import auto_bet",
                       "from auto_bet", "import bot", "from bot", "import requests"):
            assert banned not in source, banned

    def test_import_decision_non_carica_la_produzione(self):
        code = ("import decision, sys;"
                "print(any(m in sys.modules for m in ('auto_bet', 'bot', 'tracker')))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert out.stdout.strip() == "False", out.stdout + out.stderr


class TestCLI:
    def test_esempi_integrati(self, capsys):
        assert main(["market"]) == 1                  # l'esempio respinto fa uscire 1
        out = capsys.readouterr().out
        assert "Contratto di mercato" in out
        assert "odds_below_min" in out and "timestamp_naive" in out
        assert "accettate 1/2" in out

    def test_json(self, capsys):
        assert main(["market", "--json"]) == 1
        data = json.loads(capsys.readouterr().out)
        assert data["schema_version"] == MARKET_SCHEMA_VERSION
        assert data["summary"]["accepted"] == 1
        assert data["rejected"][0]["code"] == "unsupported_schema"

    def test_file_di_quote_tutte_valide(self, tmp_path, capsys):
        path = tmp_path / "quotes.json"
        path.write_text(json.dumps([payload()]), encoding="utf-8")
        assert main(["market", "--file", str(path)]) == 0
        assert "accettate 1/1" in capsys.readouterr().out

    def test_gateway_di_default_dalla_cli(self, monkeypatch, capsys):
        row = payload()
        row.pop("gateway_id")
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(row)))
        assert main(["market", "--gateway", "sx-feed-cli", "--stdin", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["accepted"][0]["gateway_id"] == "sx-feed-cli"
        assert data["summary"]["gateway_id"] == "sx-feed-cli"

    def test_input_non_json_non_esplode(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin", io.StringIO("non json"))
        assert main(["market", "--stdin"]) == 2
        assert "non leggibile come JSON" in capsys.readouterr().err

    def test_cli_non_scrive_eventi_sul_volume(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("DECISION_LOG_SINK", str(tmp_path / "events.jsonl"))
        main(["market"])
        capsys.readouterr()
        assert not (tmp_path / "events.jsonl").exists()

    def test_esempi_coerenti_col_contratto(self):
        """Gli esempi della CLI: il primo conforme, il secondo respinto apposta."""
        assert parse_quote(dict(SAMPLE_QUOTES[0])).market == "1X2"
        with pytest.raises(MarketQuoteError) as exc:
            parse_quote(dict(SAMPLE_QUOTES[1]))
        assert set(codes(exc.value)) == {"unsupported_schema",
                                         "selection_market_mismatch",
                                         "odds_below_min",
                                         "timestamp_naive"}
