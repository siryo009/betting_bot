"""
Test unitari per QuotaVerace Bot (integrato con Poisson Engine).
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from bot import (
    compute_ev,
    format_segnale_pronto,
    prob_1x2,
    prob_over_under,
    cmd_test_segnale,
    cmd_segnale,
    cmd_help,
)


class TestInvioDirettoTelegram:
    """Il messaggio di avvio non veniva MAI consegnato in produzione.

    `send_telegram_message_direct` cercava il destinatario solo in
    TELEGRAM_CHAT_ID / TELEGRAM_CHAT_ID_FALLBACK (variabili del vecchio
    "signals-mvp" locale): su Railway esiste ADMIN_CHAT_ID, quindi a ogni
    deploy il warning "Token o chat_id Telegram mancanti" e il messaggio
    perso (21/09/2026).
    """

    def _capture(self, monkeypatch):
        import requests
        inviati = {}

        class _Resp:
            ok = True
            status_code = 200
            text = ""

        def _post(url, json=None, timeout=None):
            inviati["url"] = url
            inviati["payload"] = json or {}
            return _Resp()

        monkeypatch.setattr(requests, "post", _post)
        return inviati

    def test_fallback_su_admin_chat_id(self, monkeypatch):
        import bot
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID_FALLBACK", raising=False)
        monkeypatch.setenv("ADMIN_CHAT_ID", "7718157436")
        monkeypatch.setenv("QUOTAVERACE_BOT_TOKEN", "fake/test-token")
        inviati = self._capture(monkeypatch)
        bot.send_telegram_message_direct("avvio")
        assert inviati.get("payload", {}).get("chat_id") == 7718157436
        assert "fake/test-token" in inviati.get("url", "")

    def test_admin_chat_id_con_virgole_usa_il_primo(self, monkeypatch):
        import bot
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID_FALLBACK", raising=False)
        monkeypatch.setenv("ADMIN_CHAT_ID", "111, 222")
        monkeypatch.setenv("QUOTAVERACE_BOT_TOKEN", "fake/test-token")
        inviati = self._capture(monkeypatch)
        bot.send_telegram_message_direct("avvio")
        assert inviati.get("payload", {}).get("chat_id") == 111

    def test_env_locali_hanno_precedenza(self, monkeypatch):
        import bot
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
        monkeypatch.setenv("ADMIN_CHAT_ID", "7718157436")
        monkeypatch.setenv("QUOTAVERACE_BOT_TOKEN", "fake/test-token")
        inviati = self._capture(monkeypatch)
        bot.send_telegram_message_direct("avvio")
        assert inviati.get("payload", {}).get("chat_id") == "999"

    def test_senza_destinatario_non_invia(self, monkeypatch):
        import bot
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID_FALLBACK", raising=False)
        monkeypatch.delenv("ADMIN_CHAT_ID", raising=False)
        monkeypatch.setenv("QUOTAVERACE_BOT_TOKEN", "fake/test-token")
        inviati = self._capture(monkeypatch)
        bot.send_telegram_message_direct("avvio")
        assert inviati == {}


class TestComputeEv:
    def test_ev_positivo(self):
        assert compute_ev(0.50, 2.20) == pytest.approx(0.10)

    def test_ev_negativo(self):
        assert compute_ev(0.30, 2.00) == pytest.approx(-0.40)

    def test_ev_zero(self):
        assert compute_ev(0.50, 2.00) == pytest.approx(0.0)


class TestProbabilitaPoisson:
    def test_prob_1x2_somma_a_1(self):
        p1, px, p2 = prob_1x2(1.5, 1.0)
        assert abs((p1 + px + p2) - 1.0) < 0.01

    def test_prob_over_under_somma_a_1(self):
        p_over, p_under = prob_over_under(1.5, 1.0)
        assert abs((p_over + p_under) - 1.0) < 0.01


class TestFormatSchedina:
    """Regressione: format_schedina con picks non deve mai crashare
    (mancava get_pro_stake importato in fixture_engine → la schedina
    delle 08:00 non sarebbe mai partita con picks presenti)."""

    def test_con_picks_non_crasha(self):
        from fixture_engine import format_schedina
        picks = [
            {"evento": "Serie A – Inter vs Napoli", "esito": "1", "quota": 2.21,
             "bookmaker": "Pinnacle", "ev": 0.061, "market_edge": 0.10},
            {"evento": "Serie A – Milan vs Juventus", "esito": "Over 2.5", "quota": 2.38,
             "bookmaker": "Bet365", "ev": 0.095, "market_edge": 0.12},
        ]
        msg = format_schedina(picks, 100.0)
        assert "SCHEDINA DEL GIORNO" in msg
        assert "Inter" in msg and "Over 2.5" in msg
        assert "MULTIPLA" in msg and "EV" in msg

    def test_senza_picks(self):
        from fixture_engine import format_schedina
        msg = format_schedina([], 100.0)
        assert "Nessuna partita" in msg


class TestFormatBetVerdicts:
    """Verdetti puntate a fine partita: notifica Telegram vinta/persa/push."""

    def test_formatta_verdetti(self):
        from bot import format_bet_verdicts
        sets = [
            {"home": "Inter", "away": "Napoli", "league": "Serie A",
             "mercato": "1X2", "esito": "1", "price": 2.10, "stake": 5.0,
             "mode": "dry-run", "outcome": "won", "profit": 5.5},
            {"home": "Milan", "away": "Juventus", "league": "Serie A",
             "mercato": "OU", "esito": "Over 2.5", "price": 1.90, "stake": 5.0,
             "mode": "dry-run", "outcome": "lost", "profit": -5.0},
        ]
        text = format_bet_verdicts(sets)
        assert "ESITO PUNTATE AUTOMATICHE" in text and "DRY-RUN" in text
        assert "✅ *VINTA*" in text and "Inter vs Napoli" in text and "+€5.50" in text
        assert "❌ *PERSA*" in text and "Over 2.5" in text and "-€5.00" in text

    def test_vuoto(self):
        from bot import format_bet_verdicts
        assert format_bet_verdicts([]) == ""

    def test_favorito_casa_con_lambda_maggiore(self):
        p1, px, p2 = prob_1x2(2.5, 0.8)
        assert p1 > p2


class TestFormatSegnalePronto:
    def test_contiene_expected_goals(self):
        text = format_segnale_pronto("Roma", "Empoli", 2.28, 0.63)
        assert "Expected Goals" in text
        assert "Roma" in text
        assert "Empoli" in text

    def test_contiene_prob_1x2(self):
        text = format_segnale_pronto("Roma", "Empoli", 2.28, 0.63)
        assert "1:" in text
        assert "X:" in text
        assert "2:" in text

    def test_escluso_over_under(self):
        """OU2.5 escluso definitivamente (06/09): il formatter segnale non
        deve MAI proporre Over/Under, coerentemente con test_ou_exclusion."""
        text = format_segnale_pronto("Roma", "Empoli", 2.28, 0.63)
        assert "Over 2.5" not in text
        assert "Under 2.5" not in text

    def test_contiene_header_segnale(self):
        text = format_segnale_pronto("Roma", "Empoli", 2.28, 0.63)
        assert "SEGNALE PRONTO" in text

    def test_contiene_disclaimer(self):
        text = format_segnale_pronto("Roma", "Empoli", 2.28, 0.63)
        assert "Gioca responsabilmente" in text
        assert "www.adm.gov.it" in text


class TestHandlerTelegram:
    def _make_update(self, text="/test_segnale"):
        update = MagicMock()
        update.message = MagicMock()
        update.message.reply_text = AsyncMock()
        update.message.text = text
        return update

    @pytest.mark.asyncio
    async def test_cmd_test_segnale_invia_messaggio_corretto(self):
        update = self._make_update()
        context = MagicMock()

        await cmd_test_segnale(update, context)

        update.message.reply_text.assert_awaited_once()
        args, kwargs = update.message.reply_text.await_args
        text = args[0] if args else kwargs.get("text")
        parse_mode = kwargs.get("parse_mode")

        assert "SEGNALE PRONTO" in text
        assert "Inter" in text
        assert "Expected Goals" in text
        assert "Gioca responsabilmente" in text
        assert "www.adm.gov.it" in text
        assert parse_mode == "Markdown"

    @pytest.mark.asyncio
    async def test_cmd_segnale_partita_valida(self):
        update = self._make_update("/segnale Roma Empoli")
        context = MagicMock()
        context.args = ["Roma", "Empoli"]

        await cmd_segnale(update, context)

        update.message.reply_text.assert_awaited_once()
        args, kwargs = update.message.reply_text.await_args
        text = args[0] if args else kwargs.get("text")

        assert "Roma" in text
        assert "Empoli" in text
        assert "SEGNALE PRONTO" in text
        assert "Gioca responsabilmente" in text
        assert kwargs.get("parse_mode") == "Markdown"

    @pytest.mark.asyncio
    async def test_cmd_segnale_squadra_non_trovata(self):
        update = self._make_update("/segnale SquadraInventata Altra")
        context = MagicMock()
        context.args = ["SquadraInventata", "Altra"]

        await cmd_segnale(update, context)

        update.message.reply_text.assert_awaited_once()
        args, kwargs = update.message.reply_text.await_args
        text = args[0] if args else kwargs.get("text")

        assert "❌" in text
        assert "SquadraInventata" in text
        assert kwargs.get("parse_mode") == "Markdown"

    @pytest.mark.asyncio
    async def test_cmd_segnale_argomenti_mancanti(self):
        update = self._make_update("/segnale")
        context = MagicMock()
        context.args = []

        await cmd_segnale(update, context)

        update.message.reply_text.assert_awaited_once()
        args, kwargs = update.message.reply_text.await_args
        text = args[0] if args else kwargs.get("text")

        assert "❌" in text
        assert "Errore" in text
        assert kwargs.get("parse_mode") == "Markdown"

    @pytest.mark.asyncio
    async def test_cmd_help_mostra_comandi(self):
        update = self._make_update("/help")
        context = MagicMock()

        await cmd_help(update, context)

        update.message.reply_text.assert_awaited_once()
        args, kwargs = update.message.reply_text.await_args
        text = args[0] if args else kwargs.get("text")

        assert "QuotaVerace Pro" in text
        assert "Comandi" in text
        assert "/segnale" in text
        assert "/scan" not in text  # rimosso dal 04/09 (Betfair fuori architettura)
        assert kwargs.get("parse_mode") == "Markdown"




class TestBankrollAvvio:
    """Tripwire del crash-loop del 19/09/2026.

    `bot.main()` chiamava `get_bankroll()` senza argomento mentre la firma
    richiedeva `chat_id`: OGNI avvio del container moriva con TypeError e la
    produzione restava in 502. Il test blocca il ritorno di quella forma.
    """

    def test_get_bankroll_senza_argomento_usa_il_default(self):
        import bot
        assert bot.get_bankroll() == bot.BANKROLL_DEFAULT
        assert bot.get_bankroll(None) == bot.BANKROLL_DEFAULT

    def test_main_non_chiama_get_bankroll_senza_argomenti(self):
        import ast
        import pathlib
        import bot
        tree = ast.parse(pathlib.Path(bot.__file__).read_text(encoding="utf-8"))
        offese = [node.lineno for node in ast.walk(tree)
                  if isinstance(node, ast.Call)
                  and getattr(node.func, "id", "") == "get_bankroll"
                  and not node.args and not node.keywords]
        assert offese == [], f"get_bankroll() senza chat_id alle righe {offese}"


class TestOrarioInizioPartita:
    """Direttiva 02/10/2026: orario di inizio in ora ITALIANA.

    `format_match_start` converte il timestamp UTC in Europe/Rome. Mai un
    orario INVENTATO: un input assente o non parsabile deve dare None, cosi'
    il chiamante omette la riga invece di stampare un valore falso.
    """

    def test_timestamp_z_convertito_in_ora_italiana(self):
        import bot
        assert (bot.format_match_start("2026-10-02T18:45:00Z")
                == "🕒 Inizio: 20:45 (IT)")

    def test_prefix_personalizzabile(self):
        import bot
        assert (bot.format_match_start("2026-10-02T18:45:00+00:00",
                                       prefix="Kickoff:")
                == "Kickoff: 20:45 (IT)")

    def test_timestamp_naive_trattato_come_utc(self):
        import bot
        assert (bot.format_match_start("2026-10-02T18:45:00")
                == "🕒 Inizio: 20:45 (IT)")

    def test_input_assente_o_non_parsabile_da_none(self):
        import bot
        assert bot.format_match_start(None) is None
        assert bot.format_match_start("") is None
        assert bot.format_match_start("non-una-data") is None

    def test_messaggio_fully_filled_contiene_la_riga_orario(self):
        """Tripwire sul sorgente: la riga orario sta sotto il nome del match."""
        import pathlib
        import bot
        src = pathlib.Path(bot.__file__).read_text(encoding="utf-8")
        idx = src.index('filled = [p for p in placed')
        blocco = src[idx:idx + 1200]
        assert 'format_match_start(p.get("commence"))' in blocco
        assert "_start_line" in blocco
        assert "ORDINE FULLY_FILLED" in blocco


class TestBankrollReale:
    """Direttiva 02/10/2026: bankroll REALE nel messaggio di avvio.

    Il vecchio fallback fisso `BANKROLL_DEFAULT` (100.00) faceva leggere un
    patrimonio inesistente. La lettura preferisce l'EQUITY del wallet SX
    (la stessa base che governa Kelly/stop/recinto), poi la cassa ledger;
    se nessuna risponde il chiamante DEVE dichiararlo (valore None).
    """

    def test_usa_l_equity_del_wallet_sx(self, monkeypatch):
        import bot
        import auto_bet
        monkeypatch.setattr(auto_bet, "_live_wallet_snapshot",
                            lambda: {"available": 26.5, "exposure": 1.5,
                                     "equity": 28.0})
        val, basis = bot.real_bankroll_usdc()
        assert val == 28.0
        assert "wallet SX" in basis and "equity" in basis

    def test_fallback_sulla_cassa_ledger(self, monkeypatch):
        import bot
        import auto_bet
        import adaptive_staking
        monkeypatch.setattr(auto_bet, "_live_wallet_snapshot", lambda: None)
        monkeypatch.setattr(adaptive_staking, "bankroll_stats",
                            lambda: {"current": 27.5})
        val, basis = bot.real_bankroll_usdc()
        assert val == 27.5
        assert basis == "cassa ledger"

    def test_entrambe_le_fonti_falliscono_dichiara_l_errore(self, monkeypatch):
        import bot
        import auto_bet
        import adaptive_staking
        monkeypatch.setattr(auto_bet, "_live_wallet_snapshot", lambda: None)
        monkeypatch.setattr(adaptive_staking, "bankroll_stats",
                            lambda: {"current": 0.0})
        val, basis = bot.real_bankroll_usdc()
        assert val is None
        assert "NON leggibile" in basis
        # Mai il vecchio fallback fisso.
        assert val != bot.BANKROLL_DEFAULT

    def test_main_usa_il_bankroll_reale_non_il_default(self):
        """Tripwire sul sorgente: `main()` legge il bankroll reale."""
        import pathlib
        import bot
        src = pathlib.Path(bot.__file__).read_text(encoding="utf-8")
        idx = src.index("def main()")
        corpo = src[idx:idx + 2000]
        assert "real_bankroll_usdc()" in corpo
        assert "BOT - QUANT BETTING - SX BET" in corpo


class TestNotificheDisattivate:
    """Direttiva 02/10/2026: sandbox tennis e "Riepilogo di ieri" NON
    inviano piu' messaggi su Telegram (log/DB restano). Riattivabili con
    l'env corrispondente a 1.
    """

    def test_notify_enabled_default_spento(self, monkeypatch):
        import bot
        monkeypatch.delenv("MORNING_REPORT_NOTIFY", raising=False)
        assert bot._notify_enabled("MORNING_REPORT_NOTIFY") is False

    def test_notify_enabled_con_env_uno(self, monkeypatch):
        import bot
        for v in ("1", "true", "yes", "on", "ON"):
            monkeypatch.setenv("TENNIS_SANDBOX_NOTIFY", v)
            assert bot._notify_enabled("TENNIS_SANDBOX_NOTIFY") is True
        monkeypatch.setenv("TENNIS_SANDBOX_NOTIFY", "0")
        assert bot._notify_enabled("TENNIS_SANDBOX_NOTIFY") is False

    def test_riepilogo_ieri_non_invia_di_default(self, monkeypatch):
        import asyncio
        import bot
        monkeypatch.delenv("MORNING_REPORT_NOTIFY", raising=False)
        monkeypatch.setattr(bot, "format_daily_report",
                            lambda d, t: "riepilogo di test")
        inviati = []

        async def _fake(context, text):
            inviati.append(text)

        monkeypatch.setattr(bot, "_send_report_to_recipients", _fake)
        asyncio.run(bot.report_morning_job(None))
        assert inviati == []

    def test_riepilogo_ieri_invia_con_env(self, monkeypatch):
        import asyncio
        import bot
        monkeypatch.setenv("MORNING_REPORT_NOTIFY", "1")
        monkeypatch.setattr(bot, "format_daily_report",
                            lambda d, t: "riepilogo di test")
        inviati = []

        async def _fake(context, text):
            inviati.append(text)

        monkeypatch.setattr(bot, "_send_report_to_recipients", _fake)
        asyncio.run(bot.report_morning_job(None))
        assert inviati == ["riepilogo di test"]

    def test_tennis_report_non_invia_di_default(self, monkeypatch):
        import asyncio
        import bot
        monkeypatch.setenv("TENNIS_SANDBOX_ENABLED", "1")
        monkeypatch.delenv("TENNIS_SANDBOX_NOTIFY", raising=False)
        monkeypatch.setattr(bot, "_tennis_report_text", lambda: "report tennis")
        inviati = []

        async def _fake(context, text):
            inviati.append(text)

        monkeypatch.setattr(bot, "_send_report_to_recipients", _fake)
        asyncio.run(bot.tennis_sandbox_report_job(None))
        assert inviati == []

    def test_tennis_report_invia_con_env(self, monkeypatch):
        import asyncio
        import bot
        monkeypatch.setenv("TENNIS_SANDBOX_ENABLED", "1")
        monkeypatch.setenv("TENNIS_SANDBOX_NOTIFY", "1")
        monkeypatch.setattr(bot, "_tennis_report_text", lambda: "report tennis")
        inviati = []

        async def _fake(context, text):
            inviati.append(text)

        monkeypatch.setattr(bot, "_send_report_to_recipients", _fake)
        asyncio.run(bot.tennis_sandbox_report_job(None))
        assert inviati == ["report tennis"]

    def test_tennis_scan_job_non_invia_mai(self):
        """Tripwire sul sorgente: il job di scansione non invia su Telegram."""
        import ast
        import pathlib
        import bot
        tree = ast.parse(pathlib.Path(bot.__file__).read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.AsyncFunctionDef)
                  and n.name == "tennis_sandbox_job")
        corpo = ast.get_source_segment(
            pathlib.Path(bot.__file__).read_text(encoding="utf-8"), fn) or ""
        assert "_send_report_to_recipients" not in corpo
