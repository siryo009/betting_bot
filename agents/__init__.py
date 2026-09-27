"""agents/ — i 4 Lead Agent del modello piramidale (FASE 1, shadow).

Ogni agente e' una **facciata sottile** su un modulo di produzione gia'
testato: NON duplica logica, espone solo il metodo `process(input)` che
restituisce un contratto serializzabile (`agents/contracts.py`). Il Capo
(`chief_orchestrator.py`) incatena Data -> Strategy -> Finance -> Execution
e passa all'esecuzione SOLO i piani con verdetto `approve`.

Regole d'oro (dalla storia di progetto):
- delega, mai duplicazione: ogni formula vive nel suo modulo originale
  (`decision/`, `value_filter`, `adaptive_staking`...);
- un solo esecutore del denaro: in Fase 1 l'esecuzione reale resta in
  `auto_bet.run_today_bets` — questa catena gira SOLO in shadow (gateway
  che NON eseguono); nessun agente chiama `execution_engine`;
- `import agents` resta leggero: nessun import di `tracker`/`auto_bet`/`bot`
  a livello di modulo (tripwire in test_agent_hierarchy.py).
"""
