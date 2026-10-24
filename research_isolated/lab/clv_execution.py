"""CLV sul prezzo ESEGUIBILE, non su quello del segnale.

Perche' esiste (misura del 20/10/2026). Il CLV riportato dai report precedenti
era **+1,70%** (137 osservazioni) e **+3,34%** (40 osservazioni con chiusura
Pinnacle a T-0). Ricalcolato distinguendo la sorgente di chiusura e il prezzo
realmente pagato, il segno si inverte: **-3,33%** (IC95 [-4,45, -2,21], n=58).

Le tre cause, tutte riproducibili con `decompose()`:

1. **ECHO** — `clv_history.closing_quota` e' la quota SX riletta a fine giornata
   dal *nostro* stesso flusso: quando coincide con `signal_quota` il CLV vale 0
   per costruzione ("closing = eco del segnale"). Includerla in una media
   sposta il risultato verso zero **senza misurare nulla**.
2. **DE-VIG DELLA CHIUSURA** — de-vigare la closing line alza la quota di
   ~2pp: confrontarla con una quota d'acquisto che *contiene* il vig/spread
   non e' un confronto omogeneo. Il `clv_vig_free` del progetto serve a
   confrontare due closing fra loro, non una closing con un prezzo pagato.
3. **SEGNALE vs ACQUISTO** — la quota del segnale e' spesso il miglior prezzo
   fra i book; l'ordine parte poi su SX a un prezzo diverso. Qui non e' questa
   la causa principale (differenza misurata +0,02pp), ed e' proprio il dato che
   va guardato per capire quanto pesa il line shopping.

Regole del modulo:
* sola LETTURA (connessione `mode=ro` passata dal chiamante), zero rete,
  zero scritture, nessun ordine;
* le chiusure si pesano per SORGENTE dichiarata, mai mescolate in un numero
  solo senza dirlo;
* nessun CLV senza la sua fonte (`source`) nel record;
* il verdetto e' `machine-readable` e usa soglie DICHIARATE.

Uso:
    venv/bin/python -m research_isolated.lab.clv_execution [--json]
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

from . import stats

logger = logging.getLogger(__name__)

#: Soglie di giudizio (dichiarate, non implicite).
MIN_SAMPLES = 30          # stessa soglia di significance.py / multi_market
BEAT_EPS = 0.005          # +/-0,5%: sotto questa ampiezza il CLV e' piatto

#: Fonti di chiusura, in ordine di autorevolezza. `closing_quota` e' la SX
#: (spesso eco del segnale), `pinnacle_quota` e `closing_odds` sono sharp.
CLOSING_SOURCES = ("closing_odds", "pinnacle_quota", "closing_quota")
SHARP_SOURCES = ("closing_odds", "pinnacle_quota")

STATUS_BEATING = "beating"
STATUS_LAGGING = "lagging"
STATUS_FLAT = "flat"
STATUS_INSUFFICIENT = "insufficient"
STATUS_UNAVAILABLE = "unavailable"


@dataclass
class Sample:
    """Un campione di CLV: la fonte viaggia SEMPRE con il numero."""

    match_id: str
    outcome: str
    source: str
    buy_price: float
    closing_price: float
    signal_quota: Optional[float] = None
    market: Optional[str] = None
    created_at: Optional[str] = None
    profit: Optional[float] = None
    stake: Optional[float] = None

    @property
    def clv(self) -> float:
        return self.closing_price / self.buy_price - 1.0

    @property
    def clv_signal(self) -> Optional[float]:
        if not self.signal_quota or self.signal_quota <= 1:
            return None
        return self.closing_price / self.signal_quota - 1.0

    @property
    def shopping(self) -> Optional[float]:
        """Quanto il prezzo d'acquisto e' meglio della quota del segnale."""
        if not self.signal_quota or self.signal_quota <= 1:
            return None
        return self.buy_price / self.signal_quota - 1.0

    def as_dict(self) -> dict:
        return {
            "match_id": self.match_id, "outcome": self.outcome, "source": self.source,
            "buy_price": round(self.buy_price, 6), "closing_price": round(self.closing_price, 6),
            "clv": round(self.clv, 6), "clv_signal": None if self.clv_signal is None else round(self.clv_signal, 6),
            "shopping": None if self.shopping is None else round(self.shopping, 6),
            "market": self.market, "created_at": self.created_at,
            "profit": self.profit, "stake": self.stake,
        }


@dataclass
class Bucket:
    """Statistiche di un gruppo di campioni."""

    label: str
    samples: List[Sample] = field(default_factory=list)

    def values(self, attr: str = "clv") -> List[float]:
        out = []
        for s in self.samples:
            v = getattr(s, attr)
            if v is not None:
                out.append(float(v))
        return out

    def summary(self, attr: str = "clv") -> dict:
        vals = self.values(attr)
        if not vals:
            return {"n": 0, "mean": None, "lo": None, "hi": None}
        ci = stats.bootstrap_mean_ci(vals, [s.match_id for s in self.samples if getattr(s, attr) is not None])
        return ci

    def roi(self) -> Optional[dict]:
        """P/L, ROI e DRAWDOWN del campione, in ordine cronologico.

        Il drawdown si misura sulla serie ORDINATA per data: e' la metrica che
        dice quanto capitale avrebbe richiesto la sequenza, non solo il saldo
        finale.
        """
        ordered = sorted([s for s in self.samples if s.profit is not None],
                         key=lambda s: s.created_at or "")
        if not ordered:
            return None
        profits = [float(s.profit) for s in ordered]
        stakes = [s.stake for s in ordered if s.stake]
        pnl = float(sum(profits))
        staked = float(sum(stakes)) if stakes else None
        return {
            "n": len(profits),
            "pnl": round(pnl, 4),
            "staked": None if staked is None else round(staked, 4),
            "roi": None if not staked else round(pnl / staked, 6),
            "max_drawdown": round(stats.max_drawdown(profits), 4),
            "worst": round(min(profits), 4),
        }


def _to_float(value) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def is_echo(buy_price: float, closing_price: float, tol: float = 1e-4) -> bool:
    """La chiusura coincide col prezzo d'acquisto: non misura nulla."""
    return abs(closing_price - buy_price) <= tol * max(1.0, buy_price)


def build_samples(clv_rows: Iterable[dict], bets_rows: Iterable[dict],
                  source: Optional[str] = None) -> List[Sample]:
    """Unisce le chiusure (`clv_history`) con i prezzi REALMENTE pagati (`bets`).

    `source` forza una singola colonna di chiusura (utile per la tabella per
    fonte); senza, vince la piu' autorevole disponibile. Gli ECHI sono
    esclusi: un CLV 0 per costruzione non e' un campione.
    """
    index: Dict[tuple, dict] = {}
    for row in clv_rows:
        key = (row.get("match_id"), str(row.get("esito")))
        index.setdefault(key, row)          # il primo record e' quello originale

    samples: List[Sample] = []
    for bet in bets_rows:
        key = (bet.get("match_id"), str(bet.get("esito")))
        row = index.get(key)
        if row is None:
            continue
        buy = _to_float(bet.get("price"))
        if buy is None or buy <= 1:
            continue
        cols = (source,) if source else CLOSING_SOURCES
        closing = found = None
        for col in cols:
            val = _to_float(row.get(col))
            if val and val > 1:
                closing, found = val, col
                break
        if closing is None or found is None:
            continue
        if is_echo(buy, closing):
            logger.debug("echo ignorato: %s %s (%s)", key[0], key[1], found)
            continue
        samples.append(Sample(
            match_id=str(key[0]), outcome=str(key[1]), source=found,
            buy_price=buy, closing_price=closing,
            signal_quota=_to_float(row.get("signal_quota")),
            market=bet.get("mercato") or bet.get("market"),
            created_at=str(bet.get("created_at") or ""),
            profit=_to_float_zero(bet.get("profit")),
            stake=_to_float(bet.get("stake")),
        ))
    samples.sort(key=lambda s: s.created_at or "")
    return samples


def _to_float_zero(value) -> Optional[float]:
    """Come `_to_float` ma conserva lo 0.0 (un push e' un dato, non un vuoto)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def decompose(samples: Sequence[Sample]) -> dict:
    """CLV eseguibile vs CLV del segnale, e quanto pesa il line shopping."""
    overall = Bucket("eseguibile", list(samples))
    sharp = Bucket("sharp", [s for s in samples if s.source in SHARP_SOURCES])
    by_source: Dict[str, dict] = {}
    for src in sorted({s.source for s in samples}):
        by_source[src] = Bucket(src, [s for s in samples if s.source == src]).summary()

    shopping = [s.shopping for s in samples if s.shopping is not None]
    sig = [s.clv_signal for s in samples if s.clv_signal is not None]
    return {
        "n": len(samples),
        "executable": overall.summary(),
        "signal": {
            "n": len(sig),
            "mean": round(float(sum(sig) / len(sig)), 6) if sig else None,
        },
        "shopping": {
            "n": len(shopping),
            "mean": round(float(sum(shopping) / len(shopping)), 6) if shopping else None,
        },
        "sharp_only": sharp.summary(),
        "by_source": by_source,
        "roi": overall.roi(),
    }


def stability(samples: Sequence[Sample], parts: int = 2) -> List[dict]:
    """Il segno resta su sotto-finestre cronologiche CONTIGUE?

    Serve a distinguere un edge persistente da un periodo fortunato: e' la
    lezione del 22/09 e del 25/09, dove un sottogruppo storico positivo e' stato
    letto come edge. Nessuna ottimizzazione: solo finestre contigue ordinate.
    """
    ordered = sorted(samples, key=lambda s: s.created_at or "")
    if not ordered:
        return []
    parts = max(1, int(parts))
    size = max(1, len(ordered) // parts)
    out: List[dict] = []
    for i in range(parts):
        chunk = ordered[i * size:(i + 1) * size] if i < parts - 1 else ordered[i * size:]
        if not chunk:
            continue
        b = Bucket(f"{i + 1}/{parts}", chunk)
        summ = b.summary()
        summ["label"] = f"{i + 1}/{parts}"
        summ["from"] = chunk[0].created_at
        summ["to"] = chunk[-1].created_at
        out.append(summ)
    return out


def verdict(summ: dict, n_min: int = MIN_SAMPLES) -> dict:
    """Verdetto dichiarativo sul CLV eseguibile (IC95, non la media)."""
    n = int((summ or {}).get("n") or 0)
    if n == 0:
        return {"status": STATUS_UNAVAILABLE, "reason": "nessun campione con chiusura utile"}
    if n < n_min:
        return {"status": STATUS_INSUFFICIENT,
                "reason": f"campione {n} < {n_min}: nessuna conclusione"}
    mean, lo, hi = summ.get("mean"), summ.get("lo"), summ.get("hi")
    if None in (mean, lo, hi):
        return {"status": STATUS_UNAVAILABLE, "reason": "intervallo non calcolabile"}
    if lo > BEAT_EPS:
        return {"status": STATUS_BEATING,
                "reason": f"CLV {mean:+.2%} con IC95 interamente sopra +{BEAT_EPS:.1%}"}
    if hi < -BEAT_EPS:
        return {"status": STATUS_LAGGING,
                "reason": f"CLV {mean:+.2%} con IC95 interamente sotto -{BEAT_EPS:.1%}"}
    return {"status": STATUS_FLAT,
            "reason": f"CLV {mean:+.2%} non distinguibile da zero (IC95 [{lo:+.2%}, {hi:+.2%}])"}


def report(samples: Sequence[Sample]) -> dict:
    """Pacchetto completo per il JSON del report."""
    dec = decompose(samples)
    return {
        "decomposition": dec,
        "stability": stability(samples),
        "verdict": verdict(dec["executable"]),
        "verdict_sharp_only": verdict(dec["sharp_only"]),
        "thresholds": {"min_samples": MIN_SAMPLES, "beat_eps": BEAT_EPS},
        "sources": list(CLOSING_SOURCES),
        "sharp_sources": list(SHARP_SOURCES),
        "samples": [s.as_dict() for s in samples],
    }


def format_report(data: dict) -> str:
    """Riepilogo testuale (Telegram/CLI friendly)."""
    dec = data.get("decomposition") or {}
    ex = dec.get("executable") or {}
    lines = ["📉 CLV SUL PREZZO ESEGUIBILE", ""]
    if not ex.get("n"):
        lines.append("nessun campione con chiusura utile (echi esclusi)")
        return "\n".join(lines)
    lines.append(f"campioni: {ex['n']}  (cluster: {ex.get('clusters')})")
    lines.append(f"CLV eseguibile: {ex['mean']:+.2%}  IC95 [{ex['lo']:+.2%}, {ex['hi']:+.2%}]")
    sig = dec.get("signal") or {}
    if sig.get("mean") is not None:
        lines.append(f"CLV del segnale: {sig['mean']:+.2%}  (n={sig['n']})")
    shop = dec.get("shopping") or {}
    if shop.get("mean") is not None:
        lines.append(f"line shopping (acquisto vs segnale): {shop['mean']:+.2%}  (n={shop['n']})")
    sharp = dec.get("sharp_only") or {}
    if sharp.get("n"):
        lines.append(f"solo sharp: {sharp['mean']:+.2%}  IC95 [{sharp['lo']:+.2%}, {sharp['hi']:+.2%}]  (n={sharp['n']})")
    for src, s in (dec.get("by_source") or {}).items():
        if s.get("n"):
            lines.append(f"  · fonte {src}: {s['mean']:+.2%} (n={s['n']})")
    roi = dec.get("roi") or {}
    if roi.get("roi") is not None:
        lines.append(f"P/L sui campioni: {roi['pnl']:+.2f} su {roi['staked']:.2f} di stake → ROI {roi['roi']:+.2%}")
        lines.append(f"drawdown massimo della serie: {roi['max_drawdown']:.2f} (peggior esito {roi['worst']:+.2f})")
    v = data.get("verdict") or {}
    lines.append(f"\nverdetto ({v.get('status')}): {v.get('reason')}")
    for s in data.get("stability") or []:
        if s.get("mean") is not None:
            lines.append(f"  stabilità {s['label']}: {s['mean']:+.2%} (n={s['n']})")
    return "\n".join(lines)


def _load_rows():
    from . import loader

    conn = loader.connect()
    try:
        clv_rows = [dict(r) for r in conn.execute(
            "SELECT match_id, esito, signal_quota, closing_quota, pinnacle_quota, "
            "closing_odds, closing_source FROM clv_history")]
        bets_rows = [dict(r) for r in conn.execute(
            "SELECT match_id, esito, mercato, price, stake, profit, created_at "
            "FROM bets WHERE mode='live'")]
    finally:
        conn.close()
    return clv_rows, bets_rows


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CLV sul prezzo eseguibile")
    parser.add_argument("--json", action="store_true", help="output JSON completo")
    parser.add_argument("--out", help="scrive il JSON in questo file")
    args = parser.parse_args(argv)

    try:
        clv_rows, bets_rows = _load_rows()
    except FileNotFoundError as exc:
        print(f"ERRORE: {exc}")
        return 2
    samples = build_samples(clv_rows, bets_rows)
    data = report(samples)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
    print(json.dumps(data, indent=2, ensure_ascii=False) if args.json else format_report(data))
    return 0


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    raise SystemExit(main())
