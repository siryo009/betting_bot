"""portfolio_sizing.py — M6 FASE 3+4: promozione dei due gate nel percorso ordini.

Direttiva del proprietario (16/10/2026): i due moduli di rischio di portafoglio
(`risk_parity.py` = FASE 3, `drawdown_rebalancer.py` = FASE 4) sono pronti e
misurati ma **scollegati** dal flusso di esecuzione. Questo modulo e' il punto
UNICO di promozione: li porta nel giro ordini **in shadow prima**, con un
moltiplicatore che puo' solo stare in **[0, 1]**, e con una reversibilita' che
e' una sola variabile d'ambiente.

PERCHE' UN MODULO DI WIRING E NON DUE CHIAMATE IN `auto_bet`
(regola di progetto: "ogni regola vive in UN solo posto" — i tre bug gravi del
progetto sono tutti logica copiata in due posti):
- `auto_bet` ha DUE percorsi che mandano ordini reali (il giro delle 60s della
  FASE 2 e il dispatch T-60): i due devono applicare gli STESSI gate nello
  STESSO ordine, altrimenti il gate e' aggirabile ordinando dall'altro.
- la semantica dello SHADOW deve essere identica nei due percorsi: se ognuno la
  implementasse per conto suo, "shadow" potrebbe voler dire due cose diverse.
Qui la logica sta una volta, e i chiamanti sono due righe.

GLI SHADOW MODE (perche' il modulo ne impone UNO sopra i due esistenti)
I due moduli hanno gia' un `mode` (`enforce`/`shadow`/`off`) ma il loro
"shadow" **applica** comunque la riduzione (il loro `active` e' True con
`mode="shadow"`): un'interfaccia che promette shadow e riduce lo stake e' la
classe di difetto piu' pericolosa in questo progetto (un denaro reale ordinato
in un importo che nessuno ha autorizzato). La promozione quindi NON si affida
al modo dei due moduli: `PORTFOLIO_SIZING` (qui) decide se i risultati vengono
**scritti** sui candidati, e in shadow i due moduli girano su **copie** dei
candidati — cosi' l'originale non puo' essere toccato nemmeno per errore.

  off      -> nessuna valutazione (nessun import, nessuna lettura del ledger)
  shadow   -> i due gate VALUTANO e loggano cosa farebbero; gli stake NON
              cambiano (default: "shadow per primo")
  enforce  -> i due gate riducono davvero gli stake (e bloccano sotto il
              ticket), come ultimo passo di sizing prima del gate di
              dipendenza (copula)

ORDINE DEI DUE GATE (non arbitrario): prima il **drawdown** (una misura di
salute del capitale: "quanto sono disposto a mettere in gioco, adesso"), poi il
**risk parity** (come distribuirlo fra i bucket: si giudicano gli importi che
sono sopravvissuti al taglio di salute). Invertirli farebbe calcolare le quote
di rischio su stake che poi vengono riscalati.

DIREZIONE UNICA: il prodotto finale puo' solo **ridurre**. Lo stake effettivo e'
sempre `min(stake_prima, prodotto_dei_due_moduli)` — se un domani un modulo
tornasse un moltiplicatore > 1 il valore viene riportato indietro e l'evento e'
CONTATO (`clamped`), non nascosto. Un modulo che alza lo stake dopo un drawdown
sarebbe l'opposto di un controllo di rischio.

FAIL-OPEN dove serve, FAIL-CLOSED dove conta (le stesse direzioni dei due
moduli, dichiarate qui perche' e' qui che si decide):
- modulo non importabile, errore di valutazione, copia di lavoro non
  riconoscibile -> candidati **INVARIATI** con `error` dichiarato (un guasto di
  telemetria non e' un rischio misurato e non deve fermare il bot);
- riduzione GIA' calcolata -> non si "dimentica": si applica (in enforce) o si
  dichiara (in shadow);
- stake ridotto sotto il **ticket minimo del MOTORE** -> il candidato viene
  **BLOCCATO** (mai un importo diverso dalla riduzione per farlo passare). Il
  ticket si LEGGE da `auto_bet.aggressive_min_ticket()` (stessa fonte di tutto
  il progetto), non si copia.

CONFINE: nessuna scrittura, nessun ordine, zero rete, zero crediti. Le uniche
letture sono il ledger `bets` (posizioni aperte, per il portafoglio) e le
misure dei due moduli. `import portfolio_sizing` non carica `auto_bet`/`bot`/
`tracker` (import pigri dentro le funzioni).

CLI:
    venv/bin/python portfolio_sizing.py [--json] [--measure]
"""
from __future__ import annotations

import json
import logging
import math
import os
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configurazione (env letta a RUNTIME: invertire non richiede un redeploy)
# ---------------------------------------------------------------------------
MODE_ENV = "PORTFOLIO_SIZING"
DEFAULT_MODE = "shadow"          # "shadow per primo": la promozione si misura
MODES = ("off", "shadow", "enforce")

# Chiave privata che lega ogni copia di lavoro al SUO candidato: se un modulo
# riordinasse o ricreasse le voci, l'allineamento per indice sarebbe perso in
# silenzio (e si applicherebbe la riduzione al candidato sbagliato). Con la
# chiave l'accoppiamento e' esplicito e verificabile.
_PSIZE_IDX = "_portfolio_sizing_idx"


def mode() -> str:
    """Modo efficace della promozione (env `PORTFOLIO_SIZING`)."""
    raw = (os.getenv(MODE_ENV) or DEFAULT_MODE).strip().lower()
    if raw not in MODES:
        logger.warning("portfolio_sizing: modo sconosciuto %r: uso %s",
                       raw, DEFAULT_MODE)
        return DEFAULT_MODE
    return raw


def enabled() -> bool:
    """La promozione e' attiva (shadow o enforce)?"""
    return mode() != "off"


def applies() -> bool:
    """I risultati vengono SCRITTI sugli stake (modo enforce)?"""
    return mode() == "enforce"


def config() -> Dict[str, Any]:
    """Configurazione efficace (una sola lettura per decisione)."""
    m = mode()
    return {
        "mode": m,
        "enabled": m != "off",
        "applies": m == "enforce",
        "shadow": m == "shadow",
        "env": MODE_ENV,
    }


def reset_caches() -> None:
    """Azzera le memorie dei due moduli (test e diagnostica).

    Entrambe sono memorie a livello di MODULO e sopravvivono fra i test dello
    stesso processo: senza reset un caso misurato per un test deciderebbe i
    successivi (la stessa lezione della cache di prontezza OU del 26/09).
    Fail-safe: un modulo assente non e' un errore da propagare.
    """
    for name in ("drawdown_rebalancer", "risk_parity"):
        try:
            mod = __import__(name)
            mod.reset_cache()
        except Exception:                                     # pragma: no cover
            logger.debug("portfolio_sizing: reset di %s non riuscito", name)


# ---------------------------------------------------------------------------
# Utilità
# ---------------------------------------------------------------------------
def _num(value: Any) -> float:
    """Numero finito >= 0 (input ostili -> 0.0, mai un'eccezione)."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(out) or out < 0:
        return 0.0
    return out


def min_ticket() -> Optional[float]:
    """Ticket minimo del MOTORE (letto, mai copiato). None se non leggibile."""
    try:
        import auto_bet
        return float(auto_bet.aggressive_min_ticket())
    except Exception as exc:
        logger.debug("portfolio_sizing: ticket minimo non leggibile (%s)", exc)
        return None


def read_open_positions(reader: Optional[Callable[[], Sequence[Dict[str, Any]]]] = None
                        ) -> List[Dict[str, Any]]:
    """Posizioni LIVE aperte nel formato atteso da `risk_parity` (bucket incluso).

    Perche' le posizioni APERTE entrano nel calcolo: il risk parity e' una
    proprieta' del PORTAFOGLIO, non di un pick isolato — senza il capitale gia'
    impegnato il modulo giudicherebbe i candidati solo fra loro e non vedrebbe
    la concentrazione che sta gia' rischiando il bankroll.

    La lettura delega ad `auto_bet._open_live_rows` (l'UNICO punto del progetto
    che sa cosa e' aperto: stessa tabella e stessa condizione di
    `_open_live_snapshot`). Fail-open: su errore la lista e' vuota — una
    telemetria rotta non deve fermare il giro puntate.
    """
    if reader is None:
        try:
            import auto_bet
            reader = auto_bet._open_live_rows          # noqa: SLF001
        except Exception as exc:                          # pragma: no cover
            logger.debug("portfolio_sizing: lettore posizioni non disponibile (%s)",
                         exc)
            return []
    try:
        rows = reader() or []
    except Exception as exc:                              # pragma: no cover
        logger.warning("portfolio_sizing: posizioni aperte non leggibili (%s)", exc)
        return []
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        stake = _num(row.get("stake"))
        if stake <= 0:
            continue
        out.append({"match_id": row.get("match_id"),
                    "mercato": str(row.get("mercato") or ""),
                    "stake": stake})
    return out


# ---------------------------------------------------------------------------
# Applicazione ai candidati
# ---------------------------------------------------------------------------
def _sources(item: Dict[str, Any]) -> List[str]:
    """Da quali dei due gate arriva la riduzione (machine-readable, per log/audit)."""
    out: List[str] = []
    dd = item.get("drawdown_rebalance") or {}
    rp = item.get("risk_parity") or {}
    if dd.get("decision") in ("scaled", "blocked"):
        out.append("drawdown")
    if rp.get("decision") in ("scaled", "blocked"):
        out.append("risk_parity")
    return out


def _steps(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Valore dello stake dopo OGNI gate che ha ridotto (diagnosi per passo).

    `stake_after` della riga e' il valore FINALE (cio' che l'ordine userebbe):
    senza i passi intermedi un audit non potrebbe dire quale dei due gate ha
    tagliato quanto — e in shadow e' l'unica cosa che si puo' leggere, perche'
    i verbi dei moduli non vengono scritti sui candidati veri.
    """
    out: List[Dict[str, Any]] = []
    for gate, block in (("drawdown", item.get("drawdown_rebalance")),
                        ("risk_parity", item.get("risk_parity"))):
        if isinstance(block, dict) and block.get("decision") in ("scaled", "blocked"):
            out.append({"gate": gate,
                        "stake_after": _num(block.get("stake_after")),
                        "multiplier": block.get("multiplier")})
    return out


def _log(info: Dict[str, Any]) -> None:
    """Log: una riga per riduzione/blocco + il riepilogo del giro.

    In modo SHADOW ogni riga dichiara che NON e' applicata: un log che dicesse
    "stake ridotto" mentre l'ordine parte a stake pieno e' la classe di difetto
    dell'etichetta del 13/09. A giro in salute (nessuna riduzione) si scende a
    DEBUG: il gate gira ogni 60s e un riepilogo vuoto sarebbe solo rumore.
    """
    tag = info["mode"].upper()
    suffix = "" if info["applied"] else " — NON applicato (modo shadow)"
    reductions = info.get("reductions") or []
    blocks = info.get("blocks") or []
    if not (reductions or blocks or info.get("clamped")):
        logger.debug("portfolio_sizing [%s]: nessuna riduzione su %d candidati",
                     tag, int(info.get("passed") or 0))
        return
    for row in reductions:
        logger.info("portfolio_sizing [%s]: %s (%s) stake %.2f -> %.2f USDC "
                    "[%s]%s", tag, row.get("match_id"), row.get("esito"),
                    row.get("stake_before"), row.get("stake_after"),
                    row.get("source"), suffix)
    for row in blocks:
        logger.info("portfolio_sizing [%s]: %s (%s) BLOCCATO [%s]: %s%s",
                    tag, row.get("match_id"), row.get("esito"),
                    row.get("reason"), row.get("detail") or "", suffix)
    dd = info.get("drawdown") or {}
    rp = info.get("risk_parity") or {}
    logger.info("portfolio_sizing [%s]: %d ridotti, %d bloccati su %d candidati "
                "| drawdown %s (x%.2f) | risk parity %d ridotti / %d bloccati / "
                "%d invariati | ticket %s%s",
                tag, len(reductions), len(blocks),
                int(info.get("passed") or 0),
                ("%.2f%%" % float(dd["drawdown_pct"]))
                if dd.get("drawdown_pct") is not None else dd.get("reason"),
                float(dd.get("multiplier") or 1.0), int(rp.get("scaled") or 0),
                int(rp.get("blocked") or 0), int(rp.get("clear") or 0),
                ("%.2f" % info["ticket"]) if info.get("ticket") is not None
                else "n/d", suffix)


def apply(candidates: List[Dict[str, Any]], *, bankroll: Optional[float] = None,
          open_positions: Optional[Sequence[Dict[str, Any]]] = None,
          cfg: Optional[Dict[str, Any]] = None,
          ) -> Tuple[List[Dict], Dict[str, Any]]:
    """Applica i due gate M6. Ritorna `(candidati, info)` — non solleva MAI.

    In `shadow` i candidati tornano **identici** (stesse chiavi, stesso stake):
    l'unica cosa che cambia e' il log e il blocco `info` con quello che sarebbe
    successo. In `enforce` lo stake viene ridotto (mai alzato) e i candidati
    ridotti sotto il ticket minimo vengono SCARTATI: un candidato a stake 0 non
    deve arrivare al percorso d'ordine (li' non c'e' nessuna guardia sul
    sotto-ticket: `_live_fill` costruirebbe un ordine da zero).
    """
    conf = dict(cfg) if cfg else config()
    m = str(conf.get("mode") or mode()).strip().lower()
    if m not in MODES:
        m = DEFAULT_MODE
    conf["mode"] = m
    # Derivati coerenti col modo: un `cfg` parziale non deve poter far
    # applicare una riduzione perche' mancava una chiave (fail-closed sul
    # DEFAULT: senza `enforce` esplicito si resta in shadow).
    conf.setdefault("enabled", m != "off")
    conf.setdefault("applies", m == "enforce")
    conf.setdefault("shadow", m == "shadow")
    real: List[Dict[str, Any]] = list(candidates or [])
    info: Dict[str, Any] = {
        "mode": conf["mode"], "enabled": bool(conf["enabled"]),
        "applied": bool(conf.get("applies")), "passed": len(real),
        "kept": len(real), "reductions": [], "blocks": [], "clamped": 0,
        "dropped": 0, "drawdown": {}, "risk_parity": {}, "ticket": None,
        "error": None, "open_positions": 0,
    }
    if not conf["enabled"]:
        return real, info
    try:
        import drawdown_rebalancer as _dd
        import risk_parity as _rp
    except Exception as exc:                              # pragma: no cover
        info["error"] = "moduli_non_disponibili"
        logger.warning("portfolio_sizing: moduli non disponibili (%s): candidati "
                       "invariati", exc)
        return real, info

    ticket = min_ticket()
    info["ticket"] = ticket
    if ticket is None:
        # Fail-open dichiarato: senza il ticket non si blocca nulla sotto
        # soglia. Non si inventa un valore (il ticket e' una soglia del motore,
        # non una costante di questo modulo).
        logger.warning("portfolio_sizing: ticket minimo non leggibile: nessun "
                       "blocco sotto-ticket in questo giro")

    # Copie di lavoro: in shadow i due moduli non devono poter toccare gli
    # oggetti veri nemmeno per sbaglio.
    probe: List[Dict[str, Any]] = []
    for i, cand in enumerate(real):
        if not isinstance(cand, dict):
            info["error"] = "candidato_non_valido"
            logger.warning("portfolio_sizing: candidato non valido in posizione "
                           "%d: candidati invariati", i)
            return real, info
        work = dict(cand)
        work[_PSIZE_IDX] = i
        probe.append(work)

    try:
        positions = (list(open_positions) if open_positions is not None
                     else read_open_positions())
        info["open_positions"] = len(positions)
        probe, dd_info = _dd.apply(probe, bankroll=bankroll)
        probe, rp_info = _rp.apply(probe, bankroll=float(bankroll or 0.0),
                                   open_positions=positions)
    except Exception as exc:                              # pragma: no cover
        info["error"] = "errore_valutazione"
        logger.warning("portfolio_sizing: valutazione non riuscita (%s): "
                       "candidati invariati", exc)
        return real, info

    info["drawdown"] = {k: dd_info.get(k) for k in
                        ("mode", "multiplier", "drawdown_pct", "factor_legacy",
                         "reason", "error", "scaled", "blocked")}
    info["risk_parity"] = {k: rp_info.get(k) for k in
                           ("mode", "scaled", "blocked", "clear",
                            "skipped_no_sample", "by_reason", "error",
                            "max_share_ratio", "min_multiplier")}

    aligned: List[Tuple[int, Dict[str, Any]]] = []
    for item in probe:
        if not isinstance(item, dict) or _PSIZE_IDX not in item:
            # L'accoppiamento e' l'UNICA cosa che impedisce di applicare una
            # riduzione al candidato sbagliato: se si perde, non si applica
            # nulla (fail-open) e lo si dichiara.
            info["error"] = "allineamento_perso"
            logger.warning("portfolio_sizing: copia di lavoro non riconosciuta "
                           "(%s): candidati invariati", _PSIZE_IDX)
            return real, info
        aligned.append((int(item[_PSIZE_IDX]), item))

    effective: Dict[int, float] = {}
    blocked_idx: set = set()
    for i, item in aligned:
        if i < 0 or i >= len(real):                       # pragma: no cover
            info["error"] = "indice_fuori_range"
            return real, info
        before = _num(real[i].get("stake"))
        after = _num(item.get("stake"))
        if after > before + 1e-9:
            # Difesa in profondita': il contratto dei due moduli e' [0, 1]; se
            # un domani non lo fosse, qui lo stake NON sale e l'evento e' contato.
            info["clamped"] = int(info["clamped"]) + 1
            after = before
        sources = _sources(item)
        detail = ((item.get("drawdown_rebalance") or {}).get("detail")
                  or (item.get("risk_parity") or {}).get("detail") or "")
        if before > 0 and after <= 0:
            blocked_idx.add(i)
            info["blocks"].append({
                "match_id": item.get("match_id"),
                "esito": item.get("esito_key") or item.get("esito"),
                "reason": "blocked_by_gate", "stake_before": before,
                "source": "|".join(sources) or "gate", "detail": detail,
                "steps": _steps(item)})
            continue
        if ticket is not None and 0 < after < float(ticket):
            blocked_idx.add(i)
            info["blocks"].append({
                "match_id": item.get("match_id"),
                "esito": item.get("esito_key") or item.get("esito"),
                "reason": "below_ticket_after_reduction",
                "stake_before": before, "stake_after": after,
                "source": "|".join(sources) or "gate",
                "steps": _steps(item),
                "detail": ("stake ridotto a %.2f USDC, sotto il ticket %.2f "
                           "— ordine bloccato (fail-closed)" % (after, float(ticket)))})
            continue
        effective[i] = after
        if after < before - 1e-9:
            info["reductions"].append({
                "match_id": item.get("match_id"),
                "esito": item.get("esito_key") or item.get("esito"),
                "stake_before": before, "stake_after": after,
                "source": "|".join(sources) or "gate", "steps": _steps(item)})

    if not conf.get("applies"):
        # SHADOW: gli oggetti veri non vengono toccati (nemmeno per aggiungere
        # un campo: un `risk_parity` sui candidati veri verrebbe letto a valle
        # come se il gate fosse stato applicato).
        info["kept"] = len(real)
        _log(info)
        return real, info

    for i, item in aligned:
        if i in blocked_idx or i not in effective:
            continue
        real[i]["stake"] = effective[i]
        for key in ("drawdown_rebalance", "risk_parity"):
            if isinstance(item.get(key), dict):
                real[i][key] = item[key]
    out = [cand for i, cand in enumerate(real) if i not in blocked_idx]
    info["dropped"] = len(blocked_idx)
    info["kept"] = len(out)
    _log(info)
    return out, info


# ---------------------------------------------------------------------------
# Report e CLI
# ---------------------------------------------------------------------------
def format_report(info: Optional[Dict[str, Any]] = None, *,
                  cfg: Optional[Dict[str, Any]] = None) -> str:
    """Report testuale: modo, ticket e (se disponibile) l'ultimo giro."""
    conf = cfg or config()
    _ticket = min_ticket()
    lines = ["⚖️ PORTFOLIO SIZING (M6 Fase 3+4) — risk parity + drawdown",
             "  modo %s (%s) | ticket minimo %s | gate: drawdown -> risk parity"
             % (conf["mode"], conf["env"],
                ("%.2f" % _ticket) if _ticket is not None else "n/d")]
    if conf["shadow"]:
        lines.append("  ⚠️ SHADOW: i gate MISURANO e loggano, gli stake NON "
                     "cambiano (per applicarli: %s=enforce)" % conf["env"])
    if not info:
        return "\n".join(lines)
    if info.get("error"):
        lines.append("  ⚠️ ultimo giro non disponibile: %s" % info["error"])
    lines.append("  candidati %d | ridotti %d | bloccati %d | invariati %d"
                 % (int(info.get("passed") or 0),
                    len(info.get("reductions") or []),
                    len(info.get("blocks") or []),
                    int(info.get("kept") or 0) - len(info.get("reductions") or [])))
    dd = info.get("drawdown") or {}
    if dd:
        lines.append("  drawdown: %s (x%.2f) | curva storica %.2f"
                     % ((" %.2f%%" % float(dd["drawdown_pct"]))
                        if dd.get("drawdown_pct") is not None
                        else (dd.get("reason") or "n/d"),
                        float(dd.get("multiplier") or 1.0),
                        float(dd.get("factor_legacy") or 1.0)))
    rp = info.get("risk_parity") or {}
    if rp:
        lines.append("  risk parity: %d ridotti, %d bloccati, %d invariati, "
                     "%d senza campione" % (int(rp.get("scaled") or 0),
                                            int(rp.get("blocked") or 0),
                                            int(rp.get("clear") or 0),
                                            int(rp.get("skipped_no_sample") or 0)))
    for row in (info.get("reductions") or [])[:10]:
        lines.append("  · %s (%s) %.2f -> %.2f USDC [%s]"
                     % (row.get("match_id"), row.get("esito"),
                        float(row.get("stake_before") or 0.0),
                        float(row.get("stake_after") or 0.0), row.get("source")))
    for row in (info.get("blocks") or [])[:10]:
        lines.append("  · %s (%s) BLOCCATO [%s]"
                     % (row.get("match_id"), row.get("esito"), row.get("reason")))
    return "\n".join(lines)


def measure() -> Dict[str, Any]:
    """Misura corrente dei due moduli (diagnostica, sola lettura).

    Serve alla CLI e a `test_portfolio_sizing`: mostra cosa i gate direbbero
    ADESSO, senza toccare nessun candidato.
    """
    out: Dict[str, Any] = {"drawdown": None, "risk_parity": None, "error": None}
    try:
        import drawdown_rebalancer as _dd
        out["drawdown"] = _dd.drawdown_state()
    except Exception as exc:                              # pragma: no cover
        out["error"] = "drawdown_non_misurabile"
        logger.debug("portfolio_sizing: drawdown non misurabile (%s)", exc)
    try:
        import risk_parity as _rp
        snap = _rp.variance_snapshot()
        out["risk_parity"] = {"buckets": snap.get("buckets"),
                              "stats": snap.get("stats"),
                              "days": snap.get("days"),
                              "error": snap.get("error")}
    except Exception as exc:                              # pragma: no cover
        out["error"] = "varianza_non_misurabile"
        logger.debug("portfolio_sizing: varianza non misurabile (%s)", exc)
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in args
    conf = config()
    info = measure() if "--measure" in args else None
    if as_json:
        print(json.dumps({"config": conf, "ticket": min_ticket(),
                          "measure": info}, indent=2, default=str))
        return 0
    print(format_report(info, cfg=conf))
    return 0


if __name__ == "__main__":                                # pragma: no cover
    raise SystemExit(main())
