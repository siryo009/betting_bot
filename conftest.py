"""Configurazione della suite di test.

Un principio: **i test non scrivono nei file di produzione**. La catena di
decisione ha due sink sul volume (eventi di osservabilita' e registro shadow) e
il job `auto_bet` li usa davvero: senza isolamento, una sessione di test lascia
centinaia di righe in `data/decision/` (osservato il 15/09/2026: 1051 eventi in
pochi minuti). Qui entrambi i percorsi vengono spostati su una directory
temporanea per test.

I test che verificano il sink lo fanno con un oggetto iniettato: un sink
esplicito vince sull'ambiente (`Observability(sink=...)`), quindi questa
configurazione non li disturba.

Stesso principio per il **feed di mercato** (`decision/feeds.py`): lo stato
della validazione finisce in `data/decision/feed_state.json` e il feed primario
chiama l'API pubblica di SX Bet. In test entrambe le cose sono spente: lo stato
va in una directory temporanea e `DECISION_FEED_ENABLED=0` fa valutare la
catena senza il gate di mercato — cosi' nessun test tocca la rete per sbaglio
(i test del feed lo riaccendono esplicitamente, con sorgenti finte).

Terzo sink: lo **store dei callback** delle revisioni
(`decision/review_telegram.py`), che registra i callback risolti e i prompt
inviati. Anche quello va nella tmp: senza isolamento un test scriverebbe sul
volume la memoria dell'idempotenza — e il test successivo la troverebbe.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolated_decision_io(tmp_path, monkeypatch):
    """Log spenti, registro shadow nella tmp e feed di mercato disattivato."""
    monkeypatch.setenv("DECISION_LOG_SINK", "off")
    monkeypatch.setenv("DECISION_SHADOW_LOG", str(tmp_path / "shadow_commands.jsonl"))
    monkeypatch.setenv("DECISION_FEED_STATE", str(tmp_path / "feed_state.json"))
    monkeypatch.setenv("DECISION_FEED_ENABLED", "0")
    monkeypatch.setenv("DECISION_CALLBACK_STORE", str(tmp_path / "review_callbacks.json"))
    monkeypatch.setenv("DECISION_REVIEW_QUEUE", str(tmp_path / "reviews.json"))
    # Nessun token/ destinatario Telegram in test: la catena non deve poter
    # incidere su un canale reale nemmeno per sbaglio.
    monkeypatch.delenv("QUOTAVERACE_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ADMIN_CHAT_ID", raising=False)
    # Circuit breakers T-60 (17/09): il flag CB2 (kill switch patrimoniale) e'
    # PERSISTENTE sul volume e molti test usano wallet finti SOTTO la soglia
    # (es. 12.28 USDC): senza isolamento un solo test lo arma sul percorso
    # reale e TUTTI i test successivi (stesso processo) trovano il sistema
    # "arrestato". Il flag vive nella tmp come gli altri stati su volume.
    monkeypatch.setattr("auto_bet.T60_KILL_FILE",
                        tmp_path / "t60_kill.json")
    monkeypatch.setattr("auto_bet.DAILY_STOP_FILE",
                        tmp_path / "daily_stop.json")
    # Circuit breaker settimanale (26/09): stato e storico del bankroll sono
    # persistenti sul volume e li scrive ogni giro `check_weekly_stop`.
    # Senza isolamento un test che simula un drawdown armerebbe il blocco sul
    # percorso REALE e tutti i test successivi (stesso processo) troverebbero
    # le puntate ferme.
    monkeypatch.setattr("auto_bet.WEEKLY_STOP_FILE",
                        tmp_path / "weekly_stop.json")
    monkeypatch.setattr("auto_bet.BANKROLL_HISTORY_FILE",
                        tmp_path / "bankroll_history.json")
    # Il gate della FINESTRA T-60 e' OFF nei test: la maggior parte semina
    # partite a +3h e verifica la semantica di staking/cap/esposizione, non
    # il timing (in produzione vale il default ON). I test della strategia
    # T-60 (test_t60_breakers) lo accendono esplicitamente.
    monkeypatch.setattr("auto_bet.T60_EXECUTION_ONLY", False)
    # Flusso dell'order book SX (26/09): stato e registro vivono sul volume e
    # li scrivono `sx_signals.scan` e `multi_market.ingest` — che i test
    # esercitano con provider finti. Senza isolamento la suite lascia
    # `data/execution/book_flow_state.json` nel data dir VERO (osservato il
    # 26/09: chiavi `mkt1|1`... scritte da test_sx_signals). Stessa lezione
    # degli altri stati su volume: si spostano nella tmp del test.
    monkeypatch.setattr("book_flow.STATE_PATH",
                        tmp_path / "book_flow_state.json")
    monkeypatch.setattr("book_flow.LOG_PATH",
                        tmp_path / "book_flow_events.jsonl")
    # Copertura intelligente (26/09): anche il suo JSONL vive sul volume. I
    # test iniettano `price_lookup`/`fill`, quindi non toccano l'exchange, ma
    # senza isolamento lascerebbero `data/execution/hedge_events.jsonl` reale.
    monkeypatch.setenv("HEDGE_LOG", str(tmp_path / "hedge_events.jsonl"))
    # Gate di prontezza dell'Over/Under (26/09): la memoria vive a livello di
    # MODULO e sopravvive fra i test dello stesso processo, mentre il ledger
    # no (ogni test ha il suo DB temporaneo). Senza reset un caso che semina
    # un campione "pronto" lascerebbe l'OU pronto per TUTTI i successivi, e
    # `live_markets()` risponderebbe in base all'ultimo test eseguito.
    import multi_market as _mm
    _mm.reset_ou_ready_cache()
    yield
    _mm.reset_ou_ready_cache()
