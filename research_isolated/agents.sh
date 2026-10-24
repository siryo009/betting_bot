#!/usr/bin/env bash
# Batteria di AGENTI della ricerca (struttura riavviabile, zero ordini).
#
# Perche' uno script e non comandi a mano: una sessione tmux muore col
# workspace, e senza un launcher "gli agenti" sparivano al primo riavvio
# (osservato il 21/10/2026: server tmux assente, nessun pane). Ogni agente gira
# su DATI LOCALI (snapshot) o in SOLA LETTURA sul ledger: nessuno tocca il
# percorso del denaro.
#
#   ./research_isolated/agents.sh          # (ri)crea la sessione "qv"
#   ./research_isolated/agents.sh status   # stato dei pane
#   ./research_isolated/agents.sh kill     # chiude tutto
#
# Il pane 0 e' l'unico che guarda la produzione: streamma i log del container
# (nessuna scrittura, nessun ordine). Tutti gli altri scrivono in
# research_isolated/out/agents/.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
PY="$ROOT/venv/bin/python"
SNAP="$ROOT/research_isolated/snapshot/quotaverace_prod.db"
LOGS="$ROOT/research_isolated/out/agents"
SESSION=qv
mkdir -p "$LOGS"
# Tutti gli agenti leggono lo SNAPSHOT (loader.py: QV_RESEARCH_DB), non il DB
# locale ne' quello di produzione: le misure devono essere riproducibili e il
# laboratorio non puo' scrivere sul ledger che regola denaro reale.
export QV_RESEARCH_DB="$SNAP"

SESSION_CMD="start"
if [ $# -ge 1 ]; then SESSION_CMD="$1"; fi

if [ "$SESSION_CMD" = "kill" ]; then
    tmux kill-session -t "$SESSION" 2>/dev/null && echo "sessione $SESSION chiusa"
    exit 0
fi

if [ "$SESSION_CMD" = "status" ]; then
    tmux ls 2>/dev/null || { echo "nessuna sessione tmux"; exit 1; }
    for p in $(tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index}' 2>/dev/null); do
        echo "== $p"
        tmux capture-pane -p -t "$p" 2>/dev/null | grep -v '^$' | tail -5
    done
    exit 0
fi

if [ ! -f "$SNAP" ]; then
    echo "snapshot assente: $SNAP (copiarlo dal container prima di avviare)" >&2
    exit 1
fi
tmux kill-session -t "$SESSION" 2>/dev/null

# --- 0) LIVE: stream dei log di produzione (sola lettura) ------------------
# Loop di POLLING e non `railway logs` in stream: lo stream esce quando il
# container si riavvia (redeploy) e il pane moriva in silenzio — osservato il
# 21/10. `--lines N` e' non bloccante (regola: mai comandi che troncano il
# turno) e il filtro tiene solo le righe che riguardano il DENARO.
tmux new-session -d -s "$SESSION" -n live \
    "cd $ROOT && echo '[AGENTE 0] log produzione (sola lettura)'; \
     while true; do \
       railway logs --service betting_bot --lines 80 2>&1 \
         | grep -aiE 'auto_bet:|puntat|FULLY_FILLED|STOP-LOSS|esposizione|ERROR|Traceback' \
         | tail -n 12; \
       date -u '+[AGENTE 0] poll %H:%M:%S UTC'; sleep 45; \
     done"
tmux set-option -t "$SESSION" remain-on-exit on 2>/dev/null

# --- 1) CLV: verifica indipendente sul campione fresco ----------------------
tmux new-window -t "$SESSION" -n clv \
    "cd $ROOT && export QV_RESEARCH_DB='$SNAP' && echo '[AGENTE 1] validazione CLV'; \
     $PY -m research_isolated.lab.clv_validation --db '$SNAP' --out research_isolated/out 2>&1 \
       | tee '$LOGS/clv_validation.log' | tail -n 40; \
     echo '--- shadow status ---'; \
     $PY -m research_isolated.lab.clv_shadow status --db '$SNAP' 2>&1 | tee '$LOGS/clv_shadow_status.log'; \
     echo '[AGENTE 1] finito'; sleep 86400"

# --- 2) SHADOW: raccolta PROSPETTICA (registra i nuovi, poi analizza) ------
tmux new-window -t "$SESSION" -n shadow \
    "cd $ROOT && export QV_RESEARCH_DB='$SNAP' && echo '[AGENTE 2] raccolta prospettica'; \
     while true; do \
       $PY -m research_isolated.lab.clv_shadow record --db '$SNAP' 2>&1 | tee -a '$LOGS/clv_shadow_record.log' | tail -n 3; \
       $PY -m research_isolated.lab.clv_shadow analyse --db '$SNAP' 2>&1 | tee '$LOGS/clv_shadow_analyse.log' | tail -n 12; \
       date -u '+[AGENTE 2] giro %H:%M UTC'; sleep 900; \
     done"

# --- 3) EV: calibrazione del gate -----------------------------------------
tmux new-window -t "$SESSION" -n ev \
    "cd $ROOT && export QV_RESEARCH_DB='$SNAP' && echo '[AGENTE 3] calibrazione EV'; \
     $PY -m research_isolated.lab.ev_calibration 2>&1 | tee '$LOGS/ev_calibration.log' | tail -n 40; \
     echo '[AGENTE 3] finito'; sleep 86400"

# --- 4) NEXT PHASE: protocollo e candidati (report-only, non riscrive) -----
tmux new-window -t "$SESSION" -n next \
    "cd $ROOT && export QV_RESEARCH_DB='$SNAP' && echo '[AGENTE 4] next phase (report-only)'; \
     $PY -m research_isolated.lab.next_phase --report-only 2>&1 | tee '$LOGS/next_phase.log' | tail -n 40; \
     echo '[AGENTE 4] finito'; sleep 86400"

# --- 5) EDGE HUNT: edge strutturali sul campione ---------------------------
tmux new-window -t "$SESSION" -n edge \
    "cd $ROOT && export QV_RESEARCH_DB='$SNAP' && echo '[AGENTE 5] edge hunt'; \
     $PY -m research_isolated.lab.edge_hunt --db '$SNAP' --out research_isolated/out/EDGE_HUNT.md 2>&1 \
       | tee '$LOGS/edge_hunt.log' | tail -n 40; \
     echo '[AGENTE 5] finito'; sleep 86400"

tmux select-window -t "$SESSION":live 2>/dev/null
echo "sessione '$SESSION' avviata con 6 pane:"
tmux list-windows -t "$SESSION" -F '  #{window_index} #{window_name}'
