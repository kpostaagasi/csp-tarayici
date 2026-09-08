"""Turning a scored contract into something a human can act on.

Two layers: a width-adaptive table (numbers, dense) and a four-line panel (the same numbers in
plain Turkish). The table is evidence; the panel is the claim.
"""

import math

from .backtest import backtest, ev
from .sources import known_closes

# (header, cell, drop priority) — the header carries its own width. Priority 0 never drops;
# when the terminal is too narrow the biggest number leaves first, so score/strike/DTE stay.
COLUMNS = [
    (f"{'sym':<5}", lambda r: f"{r.sym:<5}", 0),
    (f"{'spot':>7}", lambda r: f"{r.spot:>7.2f}", 10),
    (f"{'strike':>7}", lambda r: f"{r.strike:>7.2f}", 0),
    (f"{'exp':>10}", lambda r: f"{r.exp!s:>10}", 9),
    (f"{'dte':>4}", lambda r: f"{r.dte:>4}", 0),
    (f"{'delta':>6}", lambda r: f"{r.delta:>6.2f}", 4),
    (f"{'mid':>6}", lambda r: f"{r.mid:>6.2f}", 3),
    (f"{'ROC%y':>6}", lambda r: f"{r.roc * 100:>6.1f}", 0),
    (f"{'IV':>6}", lambda r: f"{r.iv:>6.1f}", 5),
    (f"{'RV30':>6}", lambda r: f"{(r.rv30 or 0):>6.1f}", 7),
    (f"{'cush':>5}", lambda r: f"{r.cushion:>5.2f}", 0),
    (f"{'sprd%':>6}", lambda r: f"{r.spread * 100:>6.1f}", 6),
    (f"{'OI':>7}", lambda r: f"{r.oi:>7.0f}", 8),
    (f"{'$col':>6}", lambda r: f"{r.collat:>6.0f}", 2),
    (f"{'EV$':>6}", lambda r: ev_cell(r), 1),
    (f"{'vrp':>4}", lambda r: f"{r.parts['vrp']:>4.2f}", 11),
    (f"{'liq':>4}", lambda r: f"{r.parts['liq']:>4.2f}", 12),
    (f"{'yld':>4}", lambda r: f"{r.parts['yield']:>4.2f}", 13),
    (f"{'score':>6}", lambda r: f"{r.score:>6}", 0),
]

SORTS = [("score", -1), ("ev", -1), ("roc", -1), ("iv30", -1), ("cushion", -1), ("spread", 1), ("dte", 1)]

HDR_TRADES = (
    f"{'giriş':>10} {'vade':>10} {'dte':>4} {'strike':>7} {'prim':>6} {'spot':>7} "
    f"{'uzlaşma':>8} {'skor':>5} {'P&L$':>7}"
)


def usd(v):
    return f"{'-' if v < 0 else '+'}${abs(v):.0f}"


def ev_cell(row):
    """EV$, or an em dash when there was never enough price history to measure one.

    A printed 0 reads as "this contract breaks even historically", which is a claim; no data is
    not that claim. The explanation panel has always drawn the distinction, the table had not.
    """
    v = ev(row)
    return f"{'—':>6}" if row.ev_n == 0 else f"{v:>6.0f}"


def columns(width):
    """Widest column set that fits: a cut-off score column is worse than a missing spot column."""
    cols = list(COLUMNS)
    while sum(len(h) + 1 for h, _, _ in cols) - 1 > width:
        worst = max(cols, key=lambda c: c[2])
        if worst[2] == 0:
            return cols  # nothing left to sacrifice: let the caller clip it
        cols.remove(worst)
    return cols


def table(rows, width):
    cols = columns(width)
    return (" ".join(h for h, _, _ in cols), [" ".join(cell(r) for _, cell, _ in cols) for r in rows])


def explain(row, px=None):
    """Four lines of plain Turkish: cash flow, risk, distances, and where the score came from."""
    be = row.strike - row.mid  # break-even at expiry
    em = row.iv / 100 * math.sqrt(row.dte / 365) * 100  # expected move to expiry, %
    p = row.parts
    earn = (
        f"kazanç {row.earn}" + (" ⚠ VADE İÇİNDE" if row.earn <= row.exp else ", vadeden sonra")
        if row.earn
        else "bilinen kazanç tarihi yok"
    )
    out = [
        f" {row.sym} {row.strike:.2f} PUT · vade {row.exp} · {row.dte} gün · spot {row.spot:.2f}",
        f" sat: +${row.mid * 100:.0f} prim, ${row.collat:.0f} nakit bloke, %{row.roc * 100:.1f} yıllık"
        f" · atama olasılığı ~%{abs(row.delta) * 100:.0f} · {earn}",
        f" başabaş {be:.2f} (spot'un %{(row.spot - be) / row.spot * 100:.1f} altı) · piyasa vadeye"
        f" ±%{em:.1f} bekliyor · strike {row.cushion:.2f} beklenen hareket uzakta",
        f" skor {row.score}: vrp {p['vrp']:.2f} (IV {row.iv:.1f} vs RV {(row.rv30 or 0):.1f})"
        f" · liq {p['liq']:.2f} (spread %{row.spread * 100:.1f}, OI {row.oi:.0f})"
        f" · yield {p['yield']:.2f} · cushion {p['cushion']:.2f}",
    ]
    bt = backtest(row, px if px is not None else known_closes(row.sym))
    out.append(
        f" geçmiş {bt['years']} yıl / {bt['n']} pencere: bu mesafe %{bt['assign'] * 100:.0f} atama"
        f" (piyasa ~%{abs(row.delta) * 100:.0f}) · %{bt['loss'] * 100:.0f} zarar"
        f" · ortalama {usd(bt['mean'])} · en kötü {usd(bt['worst'])}"
        if bt
        else " geçmiş: bu vade için yeterli fiyat geçmişi yok"
    )
    return out


def print_trades(trades, stats):
    print(HDR_TRADES)
    for t in trades:
        print(
            f"{t['entry']:>10} {t['exp']:>10} {t['dte']:>4} {t['strike']:>7.2f} "
            f"{t['credit']:>6.2f} {t['spot']:>7.2f} {t['settle']:>8.2f} {t['score']:>5} "
            f"{t['pl']:>+7.0f}"
        )
    s = stats
    print(f"\n{s['n']} işlem · {s['first']} → {s['last']} · {s['days']} gün pozisyonda")
    print(f"toplam {usd(s['total'])} · işlem başına {usd(s['mean'])} · en kötü {usd(s['worst'])}")
    print(
        f"kazanan %{s['wins'] * 100:.0f} · atanan %{s['assigned'] * 100:.0f} · "
        f"en kötü seri {usd(s['drawdown'])} · bloke edilen tepe ${s['tied']:.0f}"
    )
    print(f"teminata göre yıllık %{s['ann'] * 100:.1f}  (pozisyonda geçen günlerle ölçekli)")
