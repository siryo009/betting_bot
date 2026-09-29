"""test_money_decimal.py — Direttiva 29/09/2026: il denaro NON e' un float.

Verifica due cose che vanno tenute insieme:

1. **Comportamento** — `money()`/`Money`/`as_float()` fanno quello che
   promettono: conversione per STRINGA (mai il rumore binario), `Decimal` a
   riposo in python, NUMERO nei JSON/SQLite, coercizione anche sulle
   assegnazioni (`validate_assignment`).
2. **Struttura** — nessun campo di denaro (`stake`, `bankroll`, `price`,
   `floor`) dichiarato `float` nei contratti, e il Finance Agent senza un
   solo `float(...)` sparso: la direttiva vale finche' un tripwire la
   difende, altrimenti torna float alla prima fretta.

Tutti i test sono OFFLINE: nessuna rete, nessun provider, nessun ordine.
"""

from __future__ import annotations

import ast
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from decision.middleware import _json_default
from decision.models import (DataQuality, KillSwitchStatus, Money, ReasonCode,
                             Signal, StakeDecision, T60OrderContract, as_float,
                             money, t60_executable)

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)

ROOT = Path(__file__).resolve().parent

#: Nomi di campo che rappresentano DENARO. Un `float` con uno di questi nomi
#: e' una violazione della direttiva (le probabilita' e le frazioni di Kelly
#: restano float di proposito).
MONEY_NAMES = {"stake", "bankroll", "price", "floor", "balance", "cost",
               "kelly_stake", "amount", "esposizione", "exposure"}

#: Sorgenti sotto la direttiva: i contratti e l'agente che tocca i soldi.
DIRECTIVE_FILES = ("decision/models.py", "decision/commands.py",
                   "decision/stake_engine.py", "agents/finance_agent.py")

#: Campi che DEVONO essere `Money` (annotazione esatta nel sorgente).
REQUIRED_MONEY_FIELDS = ("bankroll: Money", "stake: Money", "price: Money",
                         "floor: Money", "kelly_stake: Money")


def _quality() -> DataQuality:
    """Qualita' dei dati di un segnale APPROVABILE (rating reali su entrambe).

    Un `DataQuality()` vuoto (copertura 0) fa rispondere `review` al Risk Engine
    (`DATA_QUALITY_LOW`: modello cieco) e il piano resta senza stake: i test sul
    denaro misurerebbero il gate, non la tipizzazione.
    """
    return DataQuality(ratings_home_n=12, ratings_away_n=10, model_coverage=1.0,
                       calibrated=True, calibration_samples=60,
                       market_coherent=True, inv_sum=1.02, depth_usdc=500.0)


def _signal(**overrides) -> Signal:
    base = dict(match_id="sx-1", outcome="1", league="Premier League",
                kickoff=NOW + timedelta(hours=3), price=1.65,
                market_prob=0.58, model_prob=0.62, blended_prob=0.65,
                tier="value", confidence=0.80, data_quality=_quality())
    base.update(overrides)
    return Signal(**base)


def _kills(mode: str = "sim") -> KillSwitchStatus:
    """Istantanea di kill switch ESPLICITA: i test non leggono il volume reale."""
    return KillSwitchStatus(mode=mode, provider_ready=True)


def _code_only(relpath: str) -> str:
    """Sorgente SENZA docstring: il tripwire non deve colpire la PROSA.

    Una docstring che nomina `float(`/`Decimal(` per SPIEGARE la direttiva non
    e' una conversione: scandire il file intero renderebbe il tripwire
    rumoroso, e un tripwire rumoroso viene disattivato. Qui resta solo il
    codice eseguibile (`ast`: le docstring sono le prime espressioni costanti
    di moduli, classi e funzioni).
    """
    source = (ROOT / relpath).read_text(encoding="utf-8")
    doc_lines: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", [])
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            doc_lines.update(range(body[0].lineno,
                                  (body[0].end_lineno or body[0].lineno) + 1))
    return "\n".join(line for index, line in enumerate(source.splitlines(), start=1)
                     if index not in doc_lines)


def _without_comments(text: str) -> str:
    """Toglie le righe di solo commento (prosa, non codice)."""
    return "\n".join(line for line in text.splitlines()
                     if not line.strip().startswith("#"))


# ---------------------------------------------------------------------------
# 1. La conversione
# ---------------------------------------------------------------------------

class TestMoney:
    def test_da_float_via_stringa_senza_rumore_binario(self):
        """`Decimal(0.1)` darebbe 0.1000...0555: la via e' la stringa."""
        assert money(0.1) == Decimal("0.1")
        assert money(1.5) == Decimal("1.5")
        assert money(1.65) == Decimal("1.65")
        # Prova del nove: il percorso diretto sarebbe impreciso.
        assert Decimal(0.1) != Decimal("0.1")

    def test_stringa_e_interi(self):
        assert money("1.50") == Decimal("1.50")
        assert money("0.000001") == Decimal("0.000001")
        assert money(2) == Decimal(2)

    def test_decimal_passato_per_identita(self):
        value = Decimal("3.33")
        assert money(value) is value

    def test_valore_ostile_non_diventa_zero_in_silenzio(self):
        for bad in ("", "abc", None, object()):
            with pytest.raises(ValueError):
                money(bad)

    def test_as_float_e_la_conversione_degli_estremi(self):
        assert as_float(Decimal("1.5")) == 1.5
        assert as_float("2.5") == 2.5
        assert as_float(None) == 0.0

    def test_accumulo_esatto_la_ragione_della_direttiva(self):
        """Sette stake da 1.50 su un conto: in float si accumula errore."""
        exact = sum((money("1.50") for _ in range(7)), Decimal("0"))
        assert exact == Decimal("10.50")
        # controprova: in binario il totale non e' esatto
        naive = sum(0.1 for _ in range(7))
        assert naive != 0.7                      # 0.7000000000000001
        assert naive == pytest.approx(0.7)       # ...ma vicino quanto basta a mentire


# ---------------------------------------------------------------------------
# 2. Signal: la quota e' denaro
# ---------------------------------------------------------------------------

class TestSignalMoney:
    def test_price_e_decimal(self):
        signal = _signal()
        assert isinstance(signal.price, Decimal)
        assert signal.price == Decimal("1.65")

    def test_ev_calcolato_sulla_conversione_all_estremo(self):
        """`ev` e' statistica (float) ma la formula non deve esplodere."""
        signal = _signal(price=2.0, blended_prob=0.6)
        assert signal.ev == pytest.approx(0.6 * 2.0 - 1.0)
        assert isinstance(signal.ev, float)

    def test_le_probabilita_restano_float(self):
        signal = _signal()
        for value in (signal.market_prob, signal.model_prob,
                      signal.blended_prob, signal.edge):
            assert isinstance(value, float)

    def test_assegnazione_coercita(self):
        """`signal.price = 2.6` non deve poter mettere un float nel campo."""
        signal = _signal()
        signal.price = 2.6
        assert isinstance(signal.price, Decimal)
        assert signal.price == Decimal("2.6")

    def test_tier_invalido_rifiutato_all_assegnazione(self):
        """Il buco che la direttiva chiude: prima `signal.tier = 'x'` passava."""
        signal = _signal()
        with pytest.raises(Exception):
            signal.tier = "rejected"

    def test_json_esce_numero_non_stringa(self):
        """Dashboard e report devono poter sommare: niente `"1.65"`."""
        payload = json.loads(_signal().model_dump_json())
        assert payload["price"] == 1.65
        assert isinstance(payload["price"], float)

    def test_il_gate_converte_la_quota_all_estremo(self):
        """La quota e' `Decimal`: il gate la converte, quindi 1.30 passa."""
        assert as_float(_signal(price=1.30).price) >= 1.30
        assert as_float(_signal(price=1.79).price) < 1.80
        assert as_float(_signal(price=1.81).price) > 1.80

    def test_il_confronto_grezzo_col_float_e_una_trappola(self):
        """Il perche' della conversione: `Decimal('1.3') >= 1.30` e' FALSO.

        Il float 1.30 vale 1.30000000000000004: un confronto diretto
        rifiuterebbe un segnale esattamente sul confine della fascia, per un
        bit. Il Risk Engine non lo fa — converte prima, una volta.
        """
        assert not (_signal(price=1.30).price >= 1.30)

        from decision.limits import RiskLimits
        from decision.risk_engine import evaluate

        risk = evaluate(_signal(price=1.30, market_prob=0.70, blended_prob=0.72),
                        kills=_kills(), limits=RiskLimits.from_env())
        assert risk.reason is not ReasonCode.ODDS_TOO_LOW


# ---------------------------------------------------------------------------
# 3. StakeDecision / T60OrderContract
# ---------------------------------------------------------------------------

class TestStakeDecision:
    def test_denaro_decimal_rapporti_float(self):
        stake = StakeDecision(bankroll=33.5535, stake=1.5, floor=1.0,
                              kelly_fraction=0.25, cap_pct=0.01)
        assert isinstance(stake.bankroll, Decimal)
        assert isinstance(stake.stake, Decimal)
        assert isinstance(stake.floor, Decimal)
        # Rapporti e percentuali: float, perche' non sono importi.
        assert isinstance(stake.kelly_fraction, float)
        assert isinstance(stake.cap_pct, float)

    def test_default_decimal_zero(self):
        stake = StakeDecision(bankroll=100)
        assert stake.stake == Decimal("0")
        assert isinstance(stake.stake, Decimal)

    def test_assegnazione_coercita_motore_di_stake(self):
        """Lo Stake Engine assegna dopo la costruzione (`base.stake = ...`)."""
        stake = StakeDecision(bankroll=100)
        stake.stake = 1.5
        stake.floor = 1.0
        assert isinstance(stake.stake, Decimal)
        assert stake.stake == Decimal("1.5")

    def test_json_numerico(self):
        payload = json.loads(StakeDecision(bankroll=33.5, stake=1.5).model_dump_json())
        assert payload["stake"] == 1.5
        assert payload["bankroll"] == 33.5


class TestT60OrderContract:
    def _contract(self, **overrides):
        base = dict(signal_id="s", record_id="r", match_id="sx-1",
                    league="Premier League", outcome="1", price=1.75, stake=1.0,
                    mode="live", provider="sxbet",
                    kickoff=NOW + timedelta(hours=1), created_at=NOW)
        base.update(overrides)
        return T60OrderContract(**base)

    def test_denaro_decimal(self):
        contract = self._contract()
        assert isinstance(contract.price, Decimal)
        assert isinstance(contract.stake, Decimal)

    def test_vincoli_numerici_ancora_attivi(self):
        with pytest.raises(Exception):
            self._contract(price=1.0)
        with pytest.raises(Exception):
            self._contract(stake=0)

    def test_t60_executable_accetta_decimal(self):
        """CB1/CB4 usano `float()` all'estremo: con Decimal devono funzionare."""
        assert t60_executable(Decimal("1.00"), Decimal("1.75")) is True
        assert t60_executable(Decimal("1.50"), Decimal("1.75")) is False  # cap 1.00

    def test_json_numerico(self):
        payload = json.loads(self._contract().model_dump_json())
        assert payload["price"] == 1.75
        assert isinstance(payload["price"], float)


class TestPayloadDOrdine:
    def test_payload_serializzato_numerico(self):
        """Il gateway fa `float(payload.get('stake'))`: deve trovare un numero."""
        from decision.commands import PlaceOrderPayload

        payload = PlaceOrderPayload(match_id="sx-1", outcome="1", kickoff=NOW,
                                    price=1.75, stake=1.5)
        assert isinstance(payload.stake, Decimal)
        dumped = payload.model_dump(mode="json")
        assert dumped["stake"] == 1.5
        assert isinstance(dumped["stake"], float)
        assert float(dumped["stake"]) == 1.5


# ---------------------------------------------------------------------------
# 4. Estremi: DB, JSON, osservabilita'
# ---------------------------------------------------------------------------

class TestEstremi:
    def test_as_row_pronto_per_il_ledger(self):
        """La colonna REAL non accetta un Decimal: `as_row` converte."""
        from decision.models import DecisionRecord, KillSwitchStatus, RiskDecision

        record = DecisionRecord(
            signal=_signal(),
            kill_switch=KillSwitchStatus(mode="live", provider_ready=True),
            risk=RiskDecision(verdict="approve", reason="ok"),
            stake=StakeDecision(bankroll=33.5, stake=1.5, executable=True),
            mode="live",
        )
        row = record.as_row()
        assert isinstance(row["price"], float)
        assert isinstance(row["stake"], float)

    def test_binding_sqlite_reale(self, tmp_path):
        """Prova del campo: le stesse conversioni vanno scritte su un DB vero."""
        from decision.models import DecisionRecord, KillSwitchStatus, RiskDecision

        record = DecisionRecord(
            signal=_signal(),
            kill_switch=KillSwitchStatus(mode="live", provider_ready=True),
            risk=RiskDecision(verdict="approve", reason="ok"),
            stake=StakeDecision(bankroll=33.5, stake=1.5, executable=True),
        )
        row = record.as_row()
        conn = sqlite3.connect(tmp_path / "t.db")
        conn.execute("CREATE TABLE r (price REAL, stake REAL)")
        conn.execute("INSERT INTO r VALUES (?, ?)", (row["price"], row["stake"]))
        conn.commit()
        stored = conn.execute("SELECT price, stake FROM r").fetchone()
        conn.close()
        assert stored == (1.65, 1.5)

    def test_middleware_scrive_decimal_come_numero(self):
        assert _json_default(Decimal("1.5")) == 1.5
        # Il resto dei tipi non serializzabili ricade su str, come prima.
        assert _json_default(NOW).startswith("2026-09-29")

    def test_evento_di_osservabilita_con_denaro(self):
        from decision.middleware import ListSink, Observability

        sink = ListSink()
        obs = Observability(sink=sink)
        with obs.span("t") as scope:
            obs.event("e", ctx=scope, stake=Decimal("1.5"))
        # `events[-1]` sarebbe lo `span.end`: l'evento col denaro e' quello "e".
        event = sink.of("e")[-1]
        line = json.dumps(event, default=_json_default)
        assert json.loads(line)["stake"] == 1.5


# ---------------------------------------------------------------------------
# 5. FinanceAgent: la direttiva sul denaro
# ---------------------------------------------------------------------------

class TestFinanceAgent:
    def test_bankroll_e_decimal(self):
        from agents.finance_agent import FinanceAgent

        agent = FinanceAgent(bankroll=33.5535, mode="sim")
        assert isinstance(agent.bankroll, Decimal)
        assert agent.bankroll == Decimal("33.5535")

    def test_override_resta_decimal(self):
        from agents.finance_agent import FinanceAgent

        agent = FinanceAgent(bankroll=100, mode="sim", kills=_kills())
        plan = agent.process(_signal(), bankroll_override="50.00")
        assert plan.record.stake is not None
        # Il motore ha ricevuto il bankroll ridotto, in Decimal a monte.
        assert plan.record.stake.bankroll == Decimal("50.00")

    def test_parita_con_il_motore(self):
        """Il wrapper non cambia nulla: stesso stake di `build_plan` diretto."""
        from agents.finance_agent import FinanceAgent
        from decision.engine import build_plan

        signal = _signal()
        kills = _kills()
        direct = build_plan(signal, bankroll=100.0, mode="sim", kills=kills)
        wrapped = FinanceAgent(bankroll=100.0, mode="sim", kills=kills).process(signal)
        assert direct.record.stake is not None
        assert direct.record.stake.stake == wrapped.record.stake.stake
        assert direct.record.risk.verdict == wrapped.record.risk.verdict

    def test_stake_del_piano_restando_decimal(self):
        from agents.finance_agent import FinanceAgent

        plan = FinanceAgent(bankroll=1000.0, mode="sim",
                            kills=_kills()).process(_signal())
        assert plan.record.stake is not None
        assert isinstance(plan.record.stake.stake, Decimal)


# ---------------------------------------------------------------------------
# 6. Tripwire di struttura (la direttiva vale finche' qualcosa la difende)
# ---------------------------------------------------------------------------

class TestTripwireSorgenti:
    @pytest.mark.parametrize("relpath", DIRECTIVE_FILES)
    def test_nessun_campo_di_denaro_in_float(self, relpath):
        """`stake: float`, `bankroll: float`, `price: float` = direttiva violata."""
        text = _code_only(relpath)
        offenders = []
        for match in re.finditer(r"^\s*(\w+)\s*:\s*(?:Optional\[)?float", text,
                                 flags=re.MULTILINE):
            if match.group(1) in MONEY_NAMES:
                offenders.append(match.group(0).strip())
        assert not offenders, f"{relpath}: denaro in float -> {offenders}"

    def test_i_contratti_dichiarano_money(self):
        text = (ROOT / "decision/models.py").read_text(encoding="utf-8")
        for field in REQUIRED_MONEY_FIELDS:
            assert field in text, f"campo di denaro non tipizzato Money: {field}"

    def test_finance_agent_senza_float_sparsi(self):
        """L'unica conversione passa da `as_float`, non da un `float()` locale."""
        body = _without_comments(_code_only("agents/finance_agent.py"))
        assert "float(" not in body.replace("as_float(", "")
        assert "as_float(" in body

    def test_finance_agent_usa_il_tipo_condiviso(self):
        """Nessuna formula di conversione copiata nell'agente."""
        text = _without_comments(_code_only("agents/finance_agent.py"))
        assert "from decision.models import" in text
        assert "money" in text and "as_float" in text
        assert "Decimal(" not in text        # nessuna conversione fai-da-te

    def test_money_e_esportato_dai_contratti(self):
        """Gli altri moduli devono importare il tipo, non ridefinirlo."""
        import decision

        assert "Money" in decision.models.__all__
        assert "money" in decision.models.__all__
        assert "as_float" in decision.models.__all__

    def test_isinstance_del_tipo_annotato(self):
        """`Money` non e' un alias decorativo: il core e' Decimal."""
        from typing import get_args

        assert get_args(Money)[0] is Decimal
