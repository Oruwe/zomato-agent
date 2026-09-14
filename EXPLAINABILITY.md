# Explainability

How this agent decides, what it reads, and — the part that matters most — what it
cannot do. Every claim below is enforced by code in this repository and covered by the
test suite; nothing here is aspirational.

### Decision Reasoning

The agent places an order autonomously by clearing a **deterministic policy engine**,
not by consulting the language model. The model is asked exactly one question — which
restaurant and which dishes — and its answer is re-grounded against the real catalogue
before anything acts on it. Everything after that is Python:

1. **Schedule** — find the meal window, or use the plan the user stated explicitly.
2. **Candidates** — search Zomato, drop anything below the rating floor, and drop
   anything whose ETA cannot beat a stated deadline. A restaurant that cannot arrive in
   time is not a candidate, however good it is.
3. **Selection** — the model ranks what survives, judging the *dish* rather than the
   restaurant average. Its choice is checked back against the fetched menu; an item it
   invented does not exist and cannot be ordered.
4. **Authorisation** — the wallet reserves the cart total against a pre-authorised
   envelope (per-order, daily and monthly caps). Reserve → commit on success, release on
   failure, journalled with `fsync` before the call returns.
5. **Escalation** — above the approval threshold the run stops and waits for a human.
   The cart is staged, not bought.
6. **Checkout** — only if dry-run is off, autonomous checkout is on, and the payment
   type is one Zomato actually accepts.

A refusal at any step is reported in plain language ("Better options exist but cannot
arrive by 13:00"), never as a policy code.

### Inputs and Data Sources

- **Schedule** — a Calendar MCP client supplies busy/free intervals, from which meal
  gaps are computed. Runs against local fixtures by default; set `CALENDAR_MCP_URL` to
  attach a live calendar.
- **Restaurants, menus, prices, ETAs** — the Zomato MCP, keyed by the `address_id` of a
  saved address on the user's own linked account. The agent does not use raw coordinates;
  it uses the address the user already chose in Zomato.
- **Preferences** — a local append-only journal of what the user ordered before, what
  they rejected, and hard dietary constraints. Dietary rules are enforced as *policy*,
  not as a hint to the model.

All three are treated as untrusted input. Calendar text in particular is attacker-writable
(Gmail auto-creates events from inbound mail), so intent is extracted through a closed
vocabulary of food words: the calendar can name a dish and cannot express an instruction.

### Known Limits

**It does not hold or spend a Zomato Money balance.** Zomato is the merchant of record
and its MCP exposes no balance or wallet tool — checkout accepts `upi` or
`cash_on_delivery` and nothing else. The internal wallet is a spend *envelope* that
authorises an amount; it does not store value and does not move money.

**Therefore there is no zero-click checkout on UPI.** A UPI order returns
`payment_pending` with a `upi://pay` intent the user approves in their own UPI app. Only
cash on delivery completes without a human touch. An agent that could silently debit a
bank account is not a feature this system withholds — it is a rail that does not exist
for third parties. NPCI's Unified Agent Protocol and UPI Circle are the mechanisms being
built for it, and they cap delegation at ₹15,000/month, which is where the monthly cap
default comes from.

**Injection detection is best-effort.** Pattern scoring is tuned to a 26-case corpus and
a novel phrasing may score clean. This is why the policy engine, not the scorer, is what
bounds spend, tools and payment type — a prompt that gets through still cannot buy
anything outside the envelope.

**Single worker, single tenant.** The wallet ledger and run store live in-process, so two
replicas would each see a full envelope. The deployment pins one worker for exactly this
reason. Auth is one password and a signed cookie.

**State is only as durable as its disk.** On an ephemeral filesystem the ledger resets on
restart, which would reset spend caps with it. Live money requires a persistent volume.

**The model is optional, and often absent.** With no API key the deterministic planner
runs instead. Selection quality drops; nothing else changes.
