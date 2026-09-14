"""The orchestration loop: schedule -> selection -> cart -> policy -> wallet -> order.

Control flow is ordinary Python, not model-driven. The LLM is consulted at exactly one
point (choosing a restaurant and dishes) and its output is re-grounded against the real
catalogue before anything else happens. Every other step -- reading the schedule, finding
gaps, validating the cart, reserving budget, deciding whether checkout may proceed -- is
deterministic code.

This is the core security property: an attacker who fully controls the model's output
still cannot spend more than the wallet allows, pay a different merchant, or skip the
approval threshold, because none of those decisions are the model's to make.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from app.config import Settings
from app.core.memory import UserMemory
from app.core.planner import Selection, make_planner
from app.core.state import AgentRun, OrderState
from app.integrations.calendar_mcp import MEAL_WINDOWS, ScheduleGap, ScheduleReader
from app.integrations.zomato_mcp import MenuItem, Restaurant, ZomatoClient, ZomatoError
from app.observability.latency import now_ns
from app.observability.logger import get_logger, new_trace_id, trace_id_var
from app.payments.base import Money, PaymentIntent
from app.payments.wallet import Wallet, WalletDenied
from app.security.guardrails import make_canary, new_nonce
from app.security.policy import Decision, PolicyEngine

log = get_logger(__name__)

__all__ = ["FoodOrderingAgent", "AgentDeps"]

IST = timezone(timedelta(hours=5, minutes=30))


@dataclass(slots=True)
class AgentDeps:
    settings: Settings
    zomato: ZomatoClient
    schedule: ScheduleReader
    wallet: Wallet
    policy: PolicyEngine
    memory: UserMemory
    rail: object | None = None
    # Run history, consulted to avoid ordering the same meal twice.
    runs: object | None = None
    # The address the user picked in the dashboard. None falls back to their Zomato
    # default, which is what a single-address account will have anyway.
    address_id: str | None = None
    # Standing authorisation to debit without asking. Absent means the agent may not
    # move money on its own, however much envelope the wallet still has.
    mandates: object | None = None


class FoodOrderingAgent:
    def __init__(self, deps: AgentDeps) -> None:
        self.d = deps

    async def run(
        self,
        *,
        user_id: str | None = None,
        day: date | None = None,
        slot: str | None = None,
        now: datetime | None = None,
        force: bool = False,
    ) -> AgentRun:
        s = self.d.settings
        trace_id = new_trace_id()
        run = AgentRun(
            run_id=uuid.uuid4().hex[:12],
            user_id=user_id or self.d.memory.user_id,
            trace_id=trace_id,
            dry_run=s.dry_run,
        )
        canary, nonce = make_canary(), new_nonce()
        current = now or datetime.now(IST)
        log.info("agent run started", extra={"run_id": run.run_id, "dry_run": s.dry_run})

        try:
            address_id = await self._step_address(run)
            gap = await self._step_schedule(run, day, slot, current)
            if gap is None:
                return run
            if not force and self._already_ordered(run, gap, current, day):
                return run
            budget_paise = self._budget(run)
            candidates = await self._step_candidates(run, address_id, gap, budget_paise)
            if not candidates:
                return run
            selection = await self._step_select(run, gap, candidates, budget_paise, canary, nonce)
            if selection is None:
                return run
            cart = await self._step_cart(run, selection, address_id)
            if cart is None:
                return run
            await self._step_checkout(run, cart, selection, gap, current)
        except Exception as exc:  # noqa: BLE001 - a run must always return a record
            log.exception("agent run failed", extra={"run_id": run.run_id})
            run.error = str(exc)
            if run.state not in (OrderState.ORDER_PLACED, OrderState.SIMULATED):
                run.state = OrderState.FAILED
        finally:
            trace_id_var.set("-")
        log.info(
            "agent run finished",
            extra={"run_id": run.run_id, "state": run.state.value,
                   "amount_paise": run.amount_paise, "order_id": run.order_id},
        )
        return run

    # -- steps ------------------------------------------------------------------
    async def _step_address(self, run: AgentRun) -> str:
        t = now_ns()
        address_id = self.d.address_id or await self.d.zomato.default_address_id()
        run.record("resolve_address", True, {"address_id": address_id},
                   (now_ns() - t) / 1000.0)
        return address_id

    async def _step_schedule(
        self, run: AgentRun, day: date | None, slot: str | None, current: datetime
    ) -> ScheduleGap | None:
        t = now_ns()
        events = await self.d.schedule.read_day(day)
        for ev in events:
            if ev.is_risky:
                run.flag_injection("calendar", ev.risk_reasons, ev.risk_score)
        run.transition(OrderState.SCHEDULE_READ)
        gaps = self.d.schedule.find_gaps(events, day)
        run.record("read_schedule", True,
                   {"events": len(events), "gaps": [g.describe() for g in gaps],
                    "injection_events": len(run.injection_events)},
                   (now_ns() - t) / 1000.0)

        chosen: ScheduleGap | None
        if slot:
            chosen = next((g for g in gaps if g.slot == slot), None)
            if chosen is None:
                # Slot explicitly requested but fully booked: still order, targeting the
                # window itself, since the user asked for it.
                window = next((w for w in MEAL_WINDOWS if w.name == slot), None)
                if window and day is None:
                    base = current.date()
                    chosen = ScheduleGap(
                        slot=slot,
                        start=datetime.combine(base, window.start, tzinfo=IST),
                        end=datetime.combine(base, window.end, tzinfo=IST),
                        keyword=window.default_keyword,
                    )
        else:
            chosen = self.d.schedule.next_gap(gaps, current)

        if chosen is None:
            run.state = OrderState.REJECTED
            run.escalation_reason = "no suitable meal window found in the schedule"
            run.record("select_slot", False, {"reason": run.escalation_reason})
            return None

        run.transition(OrderState.SLOT_SELECTED)
        run.slot = chosen.slot
        run.order_date = chosen.start.date().isoformat()
        run.record("select_slot", True, {"slot": chosen.slot, "gap": chosen.describe()})
        return chosen

    def _already_ordered(
        self, run: AgentRun, gap: ScheduleGap, current: datetime, day: date | None
    ) -> bool:
        """Stop a second order for a meal the user has already handled today.

        A double-clicked button, a retried webhook and a cron tick landing on top of a
        manual run all produce this. The wallet correctly allows each one individually --
        they are all inside the caps -- so the guard has to live here, in the domain.
        """
        store = self.d.runs
        if store is None:
            return False
        on_date = run.order_date or (day or current.date()).isoformat()
        existing = store.existing_order_for_slot(gap.slot, on_date)
        if existing is None:
            return False

        run.state = OrderState.REJECTED
        run.escalation_reason = (
            f"{gap.slot} was already ordered today from "
            f"{existing.restaurant or 'a restaurant'} "
            f"(₹{existing.amount_paise / 100:.2f}). Use force to order it again."
        )
        run.record("duplicate_guard", False,
                   {"existing_run_id": existing.run_id, "slot": gap.slot,
                    "existing_state": existing.state})
        log.info("duplicate order prevented",
                 extra={"run_id": run.run_id, "slot": gap.slot,
                        "existing_run_id": existing.run_id})
        return True

    def _mandate_for(self, run: AgentRun, amount_paise: int):
        """Resolve the standing authorisation covering this debit.

        Returns ``(mandate, reason)``; mandate is None when the agent may not pay. The
        reason is returned rather than recorded here so the caller records exactly one
        audit entry with the true cause -- recording in both places overwrote
        "out of headroom" with "no mandate", which is a different and wrong story.

        Checked in code rather than trusted to the planner: a mandate is the user's
        consent, and its ceiling is enforced by the rail too, so an over-ceiling debit
        would be declined anyway. Failing here is clearer than failing there.
        """
        store = self.d.mandates
        if store is None:
            return None, "no_mandate_store"
        mandate = store.active_for(run.user_id)
        if mandate is None:
            existing = store.get(run.user_id)
            return None, (f"mandate_{existing.status}" if existing else "no_active_mandate")
        if amount_paise > mandate.remaining_paise:
            return None, "mandate_headroom_exhausted"
        return mandate, "ok"

    def _budget(self, run: AgentRun) -> int:
        snap = self.d.wallet.snapshot()
        budget = min(
            int(snap["per_order_cap_paise"]),
            int(snap["daily_remaining_paise"]),
        )
        run.record("compute_budget", budget > 0,
                   {"budget_paise": budget, "wallet": snap})
        return budget

    async def _step_candidates(
        self, run: AgentRun, address_id: str, gap: ScheduleGap, budget_paise: int
    ) -> list[tuple[Restaurant, list[MenuItem]]]:
        t = now_ns()
        profile = self.d.memory.recall(slot=gap.slot)
        keyword = gap.keyword
        if profile.top_cuisines:
            keyword = f"{profile.top_cuisines[0][0]} {gap.keyword}"

        min_rating = self.d.settings.min_restaurant_rating
        restaurants = await self.d.zomato.search_restaurants(
            address_id=address_id, keyword=keyword,
            max_price=budget_paise / 100.0, min_rating=min_rating, page_size=8,
        )
        # Re-check locally: the search backend is free to ignore the filter, and a rating
        # floor the user set is a requirement, not a hint.
        restaurants = [r for r in restaurants if r.rating >= min_rating]
        for r in restaurants:
            if r.risk_score:
                run.flag_injection(f"zomato:{r.res_id}", r.risk_reasons, r.risk_score)

        # Menus fetched concurrently: 5 sequential ~300ms calls become one ~300ms wait.
        menus = await asyncio.gather(
            *(self.d.zomato.get_menu(res_id=r.res_id, address_id=address_id)
              for r in restaurants[:5]),
            return_exceptions=True,
        )
        candidates: list[tuple[Restaurant, list[MenuItem]]] = []
        for restaurant, menu in zip(restaurants[:5], menus, strict=False):
            if isinstance(menu, BaseException):
                log.warning("menu fetch failed",
                            extra={"res_id": restaurant.res_id, "error": str(menu)})
                continue
            if menu:
                candidates.append((restaurant, menu))

        if candidates:
            run.transition(OrderState.CANDIDATES_FETCHED)
        else:
            run.state = OrderState.REJECTED
            run.escalation_reason = "no restaurants matched the schedule slot and budget"
        run.record("fetch_candidates", bool(candidates),
                   {"keyword": keyword, "restaurants": len(restaurants),
                    "with_menus": len(candidates)},
                   (now_ns() - t) / 1000.0)
        return candidates

    async def _step_select(
        self, run: AgentRun, gap: ScheduleGap,
        candidates: list[tuple[Restaurant, list[MenuItem]]],
        budget_paise: int, canary: str, nonce: str,
    ) -> Selection | None:
        t = now_ns()
        planner = make_planner(self.d.settings, canary=canary, nonce=nonce)
        selection = await planner.select(
            gap=gap, profile=self.d.memory.recall(slot=gap.slot),
            candidates=candidates, budget_paise=budget_paise,
        )
        if selection is None:
            run.state = OrderState.REJECTED
            run.escalation_reason = "no menu combination fit the budget and constraints"
            run.record("select_items", False, {"reason": run.escalation_reason})
            return None

        run.transition(OrderState.ITEMS_SELECTED)
        run.restaurant = selection.restaurant_name
        run.res_id = selection.res_id
        run.dishes = selection.dish_names
        run.record("select_items", True,
                   {"backend": selection.backend, "restaurant": selection.restaurant_name,
                    "dishes": selection.dish_names,
                    "estimated_paise": selection.estimated_total_paise,
                    "reasoning": selection.reasoning,
                    "injection_detected": selection.injection_detected},
                   (now_ns() - t) / 1000.0)
        return selection

    async def _step_cart(self, run: AgentRun, selection: Selection, address_id: str):
        s = self.d.settings
        cart_args = {
            "res_id": selection.res_id,
            "items": selection.items,
            "payment_type": s.zomato_settlement_type,
        }
        verdict = self.d.policy.validate("create_cart", cart_args)
        if verdict.decision is Decision.DENY:
            run.state = OrderState.REJECTED
            run.escalation_reason = "; ".join(verdict.reasons)
            run.record("policy_cart", False, {"reasons": verdict.reasons})
            return None
        run.record("policy_cart", True, {"decision": verdict.decision.value,
                                         "reasons": verdict.reasons})

        # Strip planner-internal keys before they reach the API.
        wire_items = [
            {k: v for k, v in item.items() if not k.startswith("_")}
            for item in selection.items
        ]
        try:
            cart = await self.d.zomato.create_cart(
                res_id=selection.res_id, items=wire_items,
                address_id=address_id, payment_type=s.zomato_settlement_type,
            )
        except ZomatoError as exc:
            run.state = OrderState.FAILED
            run.error = str(exc)
            run.record("create_cart", False, {"error": str(exc)})
            return None

        run.transition(OrderState.CART_CREATED)
        run.cart_id = cart.cart_id
        run.amount_paise = cart.total_paise
        run.record("create_cart", True,
                   {"cart_id": cart.cart_id, "total_paise": cart.total_paise})
        return cart

    async def _step_checkout(
        self, run: AgentRun, cart, selection: Selection, gap: ScheduleGap, current: datetime
    ) -> None:
        s = self.d.settings
        # Reserve against the real envelope before asking whether checkout may proceed.
        try:
            hold = self.d.wallet.authorize(cart.total_paise)
        except WalletDenied as exc:
            run.state = OrderState.REJECTED
            run.escalation_reason = f"wallet denied: {exc.reason}"
            run.record("wallet_authorize", False,
                       {"reason": exc.reason, "requested": exc.requested,
                        "remaining": exc.remaining})
            return
        run.transition(OrderState.WALLET_AUTHORIZED)
        run.record("wallet_authorize", True,
                   {"hold_id": hold.hold_id, "amount_paise": hold.amount_paise})

        verdict = self.d.policy.validate(
            "checkout",
            {"cart_id": cart.cart_id, "amount_paise": cart.total_paise,
             "payment_method_type": s.zomato_settlement_type},
            local_now=current,
            held_paise=hold.amount_paise,
        )
        run.record("policy_checkout", verdict.allowed,
                   {"decision": verdict.decision.value, "reasons": verdict.reasons})

        if verdict.decision is Decision.DENY:
            self.d.wallet.release(hold.hold_id)
            run.state = OrderState.REJECTED
            run.escalation_reason = "; ".join(verdict.reasons)
            return

        if verdict.decision is Decision.ESCALATE or s.dry_run:
            self.d.wallet.release(hold.hold_id)
            run.state = (
                OrderState.SIMULATED if s.dry_run else OrderState.AWAITING_APPROVAL
            )
            run.escalation_reason = (
                "dry-run: no order placed" if s.dry_run else "; ".join(verdict.reasons)
            )
            self.d.memory.record_order(
                restaurant=selection.restaurant_name, dishes=selection.dish_names,
                cuisines=selection.cuisines, amount_paise=cart.total_paise,
                meal_slot=gap.slot, simulated=True,
            )
            run.record("checkout", True,
                       {"simulated": True, "reason": run.escalation_reason})
            return

        # Cash on delivery moves no money now -- the user pays the rider -- so there is
        # no rail leg at all. This is the only genuinely zero-touch settlement available
        # through Zomato's MCP today.
        cod = s.zomato_settlement_type == "cash_on_delivery"

        # Live path. Charge the rail first so a wallet commit always has a payment behind
        # it, then place the order, then make the spend permanent.
        if self.d.rail is not None and not cod:
            mandate, reason = self._mandate_for(run, cart.total_paise)
            if mandate is None:
                # No standing authorisation covering this amount: the agent may not move
                # money on its own, however much wallet envelope is left.
                self.d.wallet.release(hold.hold_id)
                run.state = OrderState.AWAITING_APPROVAL
                run.escalation_reason = (
                    "This order needs more headroom than your payment authorisation has "
                    "left. Raise it, or approve this one by hand."
                    if reason == "mandate_headroom_exhausted"
                    else "No active payment authorisation. Authorise autonomous payment "
                         "once, or switch settlement to cash on delivery."
                )
                run.record("mandate_check", False, {"reason": reason})
                return
            run.record("mandate_check", True,
                       {"mandate_id": mandate.mandate_id,
                        "remaining_paise": mandate.remaining_paise})
            intent = PaymentIntent(
                amount=Money(cart.total_paise),
                idempotency_key=f"{run.run_id}:{cart.cart_id}",
                description=f"{selection.restaurant_name} ({gap.slot})",
                order_ref=cart.cart_id,
                mandate=mandate.as_ref(),
            )
            result = await self.d.rail.charge(intent)
            run.record("rail_charge", result.ok,
                       {"rail": result.rail, "status": result.status.value,
                        "provider_ref": result.provider_ref, "error": result.error})
            if not result.ok:
                self.d.wallet.release(hold.hold_id)
                run.state = OrderState.FAILED
                run.error = result.error or "payment rail declined"
                if self.d.mandates is not None:
                    self.d.mandates.fail(run.user_id, run.error)
                return
            if self.d.mandates is not None:
                self.d.mandates.record_debit(run.user_id, cart.total_paise)

        try:
            order = await self.d.zomato.checkout(
                cart_id=cart.cart_id, payment_method_type=s.zomato_settlement_type
            )
        except ZomatoError as exc:
            self.d.wallet.release(hold.hold_id)
            run.state = OrderState.FAILED
            run.error = str(exc)
            run.record("checkout", False, {"error": str(exc)})
            return

        self.d.wallet.commit(hold.hold_id)
        run.transition(OrderState.ORDER_PLACED)
        run.order_id = str(order.get("order_id", ""))
        self.d.memory.record_order(
            restaurant=selection.restaurant_name, dishes=selection.dish_names,
            cuisines=selection.cuisines, amount_paise=cart.total_paise,
            meal_slot=gap.slot, simulated=False,
        )
        run.record("checkout", True,
                   {"order_id": run.order_id, "amount_paise": cart.total_paise})
