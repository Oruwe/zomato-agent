"""Which rail settles this order, and whether a human has to touch it.

The priority is a human-less payment: the agent orders, Zomato Money pays, nobody taps
anything. The constraint is that Zomato exposes no balance tool and no wallet value in
its checkout enum -- both `create_cart` and `checkout_cart` accept exactly
``upi`` and ``cash_on_delivery``.

The wallet is still reachable, just not addressable. Zomato applies a user's Zomato
Money balance server-side during a ``upi`` checkout: if the balance covers the bill it
debits it and raises no collect request, and the response comes back paid with no action
url. That is the zero-touch path, and ``app/integrations/payment_status.py`` already
detects it.

So the ladder cannot ask "what is the balance". It has to *expect* a balance and then
find out whether it was right:

    balance >= bill   -> upi, expecting Zomato Money to absorb it        (no human)
    balance <  bill   -> cash_on_delivery, placed without a tap          (no human now)
    cash unavailable  -> upi, and the user approves a collect request    (human)

The expectation is a declared figure the user gives us once, corrected by what actually
happens: an order that comes back zero-touch spent from it, and one that raises a collect
request when we expected the wallet to cover it proves the figure was too high, so it is
written down to below that bill. An estimate that learns beats a number nobody updates.

Nothing here is a decision the language model participates in.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "SettlementChoice", "choose_settlement", "WALLET", "UPI", "CASH",
    "NO_SETTLEMENT_AVAILABLE",
]

WALLET = "zomato_money"          # not a wire value: applied by Zomato during a upi checkout
UPI = "upi"
CASH = "cash_on_delivery"

NO_SETTLEMENT_AVAILABLE = "no permitted settlement method"


@dataclass(frozen=True, slots=True)
class SettlementChoice:
    """The rail to use, why, and what it implies for the user."""

    wire_type: str | None          # what goes to Zomato; None when nothing is permitted
    expect_wallet: bool = False    # do we expect Zomato Money to absorb the bill
    human_less: bool = False       # will this complete with no action from the user
    reason: str = ""               # plain language, safe to show
    considered: tuple[dict, ...] = field(default_factory=tuple)  # the ladder walk

    @property
    def ok(self) -> bool:
        return self.wire_type is not None

    def to_dict(self) -> dict:
        return {
            "wire_type": self.wire_type,
            "expect_wallet": self.expect_wallet,
            "human_less": self.human_less,
            "reason": self.reason,
            "considered": list(self.considered),
        }


def _rupees(paise: int) -> str:
    return f"₹{paise / 100:,.2f}"


def choose_settlement(
    amount_paise: int,
    *,
    balance_paise: int | None,
    allowed: frozenset[str] | set[str],
    prefer_cash_when_short: bool = True,
) -> SettlementChoice:
    """Pick the rail for one order. Deterministic, and never asks the model.

    ``balance_paise`` is the declared Zomato Money balance, or None when the user has
    never told us. Unknown is treated as "cannot be relied on", not as zero: the agent
    does not claim a human-less payment it has no reason to expect.
    """
    walk: list[dict] = []

    def note(method: str, used: bool, why: str) -> None:
        walk.append({"method": method, "used": used, "why": why})

    # 1. Zomato Money, via a upi checkout. The only genuinely hands-off *payment*.
    if UPI not in allowed:
        note(WALLET, False, "upi settlement is not permitted by policy")
    elif balance_paise is None:
        note(WALLET, False, "no Zomato Money balance on record, so it cannot be relied on")
    elif balance_paise < amount_paise:
        note(WALLET, False,
             f"balance {_rupees(balance_paise)} does not cover {_rupees(amount_paise)}")
    else:
        note(WALLET, True, f"balance {_rupees(balance_paise)} covers {_rupees(amount_paise)}")
        return SettlementChoice(
            wire_type=UPI, expect_wallet=True, human_less=True,
            reason=(f"Paying from Zomato Money ({_rupees(balance_paise)} available). "
                    "Nothing to approve."),
            considered=tuple(walk),
        )

    # 2. Cash on delivery. The payment is not hands-off -- someone hands over money at the
    #    door -- but the *order* is placed with no tap, which is what unblocks the run.
    if prefer_cash_when_short and CASH in allowed:
        note(CASH, True, "order can be placed without an approval step")
        short = (
            "Zomato Money is not set up"
            if balance_paise is None
            else f"Zomato Money is short by {_rupees(amount_paise - balance_paise)}"
        )
        return SettlementChoice(
            wire_type=CASH, expect_wallet=False, human_less=True,
            reason=f"{short}, so this is going out as cash on delivery. Pay the rider.",
            considered=tuple(walk),
        )
    note(CASH, False,
         "cash on delivery is not permitted by policy" if CASH not in allowed
         else "cash fallback is switched off")

    # 3. UPI collect. The order is placed, the user approves the request.
    if UPI in allowed:
        note(UPI, True, "falls back to a collect request the user approves")
        return SettlementChoice(
            wire_type=UPI, expect_wallet=False, human_less=False,
            reason=("Zomato Money will not cover this, so you will get a UPI request "
                    "to approve."),
            considered=tuple(walk),
        )
    note(UPI, False, "upi settlement is not permitted by policy")

    return SettlementChoice(wire_type=None, reason=NO_SETTLEMENT_AVAILABLE,
                            considered=tuple(walk))
