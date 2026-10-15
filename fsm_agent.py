"""
fsm_agent.py - Finite State Machine controller for the Casa Perpetua waiter.

States (the lab handout says "5 distinct states" but names four; the fifth
here is CANCELLED so that order cancellation - a rubric edge case - is a
first-class state instead of a hack):

    GREETING  --order intent / tool call-->  ORDERING
    ORDERING  --"that's all" / Review btn->  REVIEW
    REVIEW    --"confirm" / Confirm btn--->  CONFIRMED   (stock deducted, receipt issued)
    REVIEW    --"add/change/wait"-------->   ORDERING    (edit before confirming)
    any open state --"cancel my order"-->    CANCELLED

Kiosk opening (STATE_GREETING)
  The welcome screen asks "Table for how many?" and the party size is stored on the session.
  The first thing the waiter says - written by code, not the model - ends with "May I take your
  order?". A plain "yes" / "we're ready" moves GREETING -> ORDERING; "not yet" keeps the FSM in GREETING.

Design rules
  * The LLM only *talks* and *calls tools*. Anything that changes money or
    stock (confirm, cancel, checkout) is decided by code, never by the model.
  * Every transition is validated against TRANSITIONS, persisted to
    table_sessions.status and written to logs/<session_id>.json.
"""
import json
import os
import re
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

import db_tools

OLLAMA_CHAT_URL = os.environ.get("OLLAMA_CHAT_URL", "http://localhost:11434/api/chat")
DEFAULT_MODEL = os.environ.get("BISTRO_MODEL", "qwen2.5")
OLLAMA_TIMEOUT = 60          # local models can be slow on first load
HISTORY_WINDOW = 8           # last N chat messages sent to the model (less context = faster)
KEEP_ALIVE = "30m"           # keep the model loaded in memory between turns (no reload delay)
# num_ctx must hold: system prompt + FULL menu table (~2.5k tokens) + 7 tool schemas (~1.5k tokens) + history + cart. 2048 silently truncated the
# system prompt/tools on longer orders, and num_predict 120 could cut a multi-item tool call off mid-JSON.
OLLAMA_OPTIONS = {"num_ctx": 8192, "num_predict": 256, "temperature": 0.2}
MAX_PARTY = 20               # largest party the welcome screen accepts
NUMBER_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
                "ten", "eleven", "twelve"]
LOG_DIR = "logs"

STATE_GREETING = "GREETING"
STATE_TAKING_ORDER = "ORDERING"
STATE_REVIEW = "REVIEW"
STATE_CONFIRMED = "CONFIRMED"
STATE_CANCELLED = "CANCELLED"

TRANSITIONS = {
    STATE_GREETING: {STATE_TAKING_ORDER, STATE_CANCELLED},
    STATE_TAKING_ORDER: {STATE_REVIEW, STATE_CANCELLED},
    STATE_REVIEW: {STATE_TAKING_ORDER, STATE_CONFIRMED, STATE_CANCELLED},
    STATE_CONFIRMED: set(),
    STATE_CANCELLED: set(),
}
FINAL_STATES = {STATE_CONFIRMED, STATE_CANCELLED}

# ---- intent patterns (word-boundary matched so "yes" never hits "yesterday") ----
RE_TABLE = re.compile(r"\btable\s*(?:number|no\.?|#)?\s*(\d{1,2})\b", re.I)
RE_ORDER_INTENT = re.compile(r"\b(order|menu|want|like|food|eat|hungry|drink|show|add|get|have|recommend|craving)\b", re.I)
RE_CHECKOUT = re.compile(
    r"(that'?s (?:all|it)|that is (?:all|it)|(?<!well )\bdone\b|check ?out|\bbill\b|"
    r"check please|ready to pay|nothing else|review (?:my|the) order)", re.I)
RE_CONFIRM = re.compile(r"\b(confirm|send it|send (?:my|the) order|place (?:it|my order|the order)|proceed|yes|go ahead|looks good)\b", re.I)
RE_NEGATION = re.compile(r"\b(don'?t|do not|not yet|wait|hold on|cancel)\b", re.I)
RE_EDIT = re.compile(r"\b(go back|wait|change|edit|add|remove|modify|another|one more|instead)\b", re.I)
RE_CANCEL = re.compile(r"\b(cancel (?:my |the |this )?(?:whole |entire )?(?:order|tab)|start over|never ?mind the order)\b", re.I)
_CAT_WORDS = "|".join(re.escape(w) for w in sorted(
    list(db_tools.CATEGORIES) + list(db_tools.CATEGORY_ALIASES) + list(db_tools.SUBCATEGORY_ALIASES),
    key=len, reverse=True))


def _menu_label(res: Dict[str, Any]) -> str:
    """'Coffee' for Beverages > Coffee, otherwise the category name."""
    return res.get("subcategory") or res["category"]
RE_CATEGORY = re.compile(r"\b(" + _CAT_WORDS + r")\b", re.I)
RE_CART_VERB = re.compile(r"\b(add|remove|delete|take off|cancel)\b", re.I)   # these are never "show me the menu"
# "Show me the Chinese menu" / "Thai" / "see the desserts" -> answered instantly by code, no LLM round trip.
RE_MENU_FAST = re.compile(
    r"^\W*(?:(?:please\s+)?(?:show|see|open|view|give|let me see|i(?:'d| would)? like to see|i want to see|can i see|can you show)"
    r"(?:\s+me)?\s+)?(?:the\s+|your\s+|all\s+|some\s+)?(" + _CAT_WORDS + r")"
    r"(?:\s+(?:menu|dishes|items|food|options|selection|section|category))?\W*$", re.I)

# Short answers to the opening question "May I take your order?"
RE_READY_YES = re.compile(
    r"^\s*(?:yes|yeah|yep|yup|sure|sure thing|okay|ok|please|yes please|go ahead|of course|ready|"
    r"we(?:'re| are) ready|i(?:'m| am) ready)\W*$", re.I)
RE_READY_NO = re.compile(
    r"^\s*(?:no|nope|not yet|not ready|wait|hold on|later|give us (?:a )?(?:minute|moment|second|sec)|"
    r"one (?:moment|minute|second|sec))\W*$", re.I)

# "I'll pay by GCash", "cash please", "put it on my card" - only checked at REVIEW, and never when the guest is editing the cart.
RE_PAYMENT = re.compile(r"\b(pay|payment|paying|charge|cash|g-?cash|maya|paymaya|credit|debit|card)\b", re.I)

# "Add a cheeseburger, that's all" = add first, THEN go to review (checking out first lost the add).
RE_ADD_INTENT = re.compile(r"\b(add|i'?ll (?:have|take|get)|i'?d like (?:a|an|\d|one|two|three|four|some)|i want (?:a|an|\d|one|two|three|four|some)|"
                           r"give me|get me|can i (?:get|have)|one more|another)\b", re.I)

MODIFYING_TOOLS = {"add_item_to_order", "remove_item_from_order", "update_item_instructions"}


class SessionContext:
    def __init__(self, session_id: str, table_no: int, party_size: int = 1):
        self.session_id = session_id
        self.table_no = table_no
        self.party_size = party_size
        self.greeting = ""                                # what the kiosk said first (code-written)
        self.current_state = STATE_GREETING
        self.messages: List[Dict[str, Any]] = []          # LLM-visible memory
        self.transitions: List[Dict[str, Any]] = []       # FSM audit trail
        self.turns: List[Dict[str, Any]] = []             # full log for JSON export
        self.receipt: Optional[Dict[str, Any]] = None
        self.last_menu: List[str] = []                    # dishes currently on screen, so "the first one" can be resolved
        self.pending_review = False                       # "...that's all" arrived together with an order
        self.created_at = datetime.now().isoformat(timespec="seconds")


class CasaPerpetuaAgent:
    def __init__(self, model_name: str = DEFAULT_MODEL):
        self.model_name = model_name
        self.active_sessions: Dict[str, SessionContext] = {}

    # ------------------------------------------------------------------ sessions
    def start_session(self, table_no: int = 4, party_size: int = 2) -> SessionContext:
        try:
            table_no = min(99, max(1, int(table_no)))
        except (TypeError, ValueError):
            table_no = 4
        try:
            party_size = min(MAX_PARTY, max(1, int(party_size)))
        except (TypeError, ValueError):
            party_size = 2
        session_id = f"sess_{uuid.uuid4().hex[:8]}"
        session = SessionContext(session_id, table_no, party_size)
        with db_tools.db() as conn:
            conn.execute("INSERT INTO table_sessions (session_id, table_no, party_size, status) VALUES (?, ?, ?, ?);",
                         (session_id, table_no, party_size, STATE_GREETING))
        session.greeting = self.greeting_text(session)
        # The opening question is part of the conversation, so a later "yes" has something to answer.
        session.messages.append({"role": "assistant", "content": session.greeting})
        self.active_sessions[session_id] = session
        self._log_turn(session, f"[kiosk] Table for {party_size}", session.greeting, [], source="ui")
        return session

    def greeting_text(self, session: SessionContext) -> str:
        """STATE_GREETING opener. Written by code so it is instant and always worded the same."""
        n = session.party_size
        count = NUMBER_WORDS[n] if n < len(NUMBER_WORDS) else str(n)
        return (f"Welcome to Casa Perpetua! Table {session.table_no} is all set, for {count}. "
                "May I take your order?")

    def get_session(self, session_id: Optional[str]) -> Optional[SessionContext]:
        return self.active_sessions.get(session_id or "")

    def snapshot(self, session: SessionContext) -> Dict[str, Any]:
        return {
            "session_id": session.session_id,
            "table_no": session.table_no,
            "party_size": session.party_size,
            "current_state": session.current_state,
            "order_snapshot": db_tools.view_current_order(session.session_id),
            "receipt": session.receipt,
        }

    # --------------------------------------------------------------- FSM plumbing
    def transition(self, session: SessionContext, new_state: str, trigger: str) -> bool:
        """Validated, persisted state change. Returns True if the state changed."""
        old = session.current_state
        if new_state == old:
            return False
        if new_state not in TRANSITIONS.get(old, set()):
            return False
        session.current_state = new_state
        db_tools.set_session_status(session.session_id, new_state)
        session.transitions.append({"from": old, "to": new_state, "trigger": trigger,
                                    "at": datetime.now().isoformat(timespec="seconds")})
        return True

    def build_receipt(self, session: SessionContext) -> Dict[str, Any]:
        order = db_tools.view_current_order(session.session_id)
        return {
            "receipt_no": "CP-" + session.session_id[-6:].upper(),
            "issued_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "table_no": session.table_no,
            "items": order["items"],
            "subtotal": order["subtotal"],
            "service_fee": order["service_fee_10_pct"],
            "grand_total": order["grand_total"],
            "payment_method": order["payment_method"],
            "payment_note": order["payment_note"],
        }

    # Public actions used by both chat intents and the kiosk buttons ------------
    def request_review(self, session: SessionContext, trigger: str = "ui: Review order") -> Dict[str, Any]:
        if session.current_state in FINAL_STATES:
            return {"ok": False, "message": "This order is already closed."}
        if db_tools.view_current_order(session.session_id)["item_count"] == 0:
            return {"ok": False, "message": "Your cart is empty - pick something from the menu first."}
        if session.current_state == STATE_GREETING:
            self.transition(session, STATE_TAKING_ORDER, trigger)
        self.transition(session, STATE_REVIEW, trigger)
        return {"ok": True, "message": "Please check your order and confirm when you are ready."}

    def back_to_ordering(self, session: SessionContext, trigger: str = "ui: Keep ordering") -> Dict[str, Any]:
        self.transition(session, STATE_TAKING_ORDER, trigger)
        return {"ok": session.current_state == STATE_TAKING_ORDER}

    def confirm_order(self, session: SessionContext, trigger: str = "ui: Confirm order") -> Dict[str, Any]:
        if session.current_state == STATE_CONFIRMED:
            return {"ok": False, "message": "This order has already been sent to the kitchen.", "receipt": session.receipt}
        if session.current_state != STATE_REVIEW:
            return {"ok": False, "message": "Please review your order first."}
        if not db_tools.get_payment_method(session.session_id):      # stay in REVIEW: the guest only has to pick a payment
            return {"ok": False, "needs_payment": True,
                    "message": "Please choose a mode of payment first: " + ", ".join(db_tools.PAYMENT_METHODS) + "."}
        res = db_tools.confirm_and_deduct_inventory(session.session_id)
        if not res["success"]:
            self.transition(session, STATE_TAKING_ORDER, trigger + " (blocked: " + res["message"] + ")")
            return {"ok": False, "message": res["message"]}
        self.transition(session, STATE_CONFIRMED, trigger)
        session.receipt = self.build_receipt(session)
        return {"ok": True, "message": f"Your order is on its way to the kitchen. Receipt {session.receipt['receipt_no']}.",
                "receipt": session.receipt}

    def set_payment(self, session: SessionContext, method: str, trigger: str = "ui: Mode of payment") -> Dict[str, Any]:
        """Pick (or change) the mode of payment. Allowed while the order is still open; not an FSM transition."""
        if session.current_state in FINAL_STATES:
            return {"ok": False, "message": "This order is already closed."}
        res = db_tools.set_payment_method(session.session_id, method)
        if not res["success"]:
            return {"ok": False, "message": res["message"]}
        return {"ok": True, "payment_method": res["payment_method"],
                "message": f"Got it, you'll pay by {res['payment_method']}. {db_tools.PAYMENT_NOTES[res['payment_method']]}"}

    def cancel_order(self, session: SessionContext, trigger: str = "ui: Cancel order") -> Dict[str, Any]:
        if session.current_state in FINAL_STATES:
            return {"ok": False, "message": "This order is already closed."}
        db_tools.clear_order(session.session_id)
        self.transition(session, STATE_CANCELLED, trigger)
        return {"ok": True, "message": "Your order has been cancelled."}

    def note_manual_change(self, session: SessionContext) -> None:
        """A +/-/add button was used: keep the FSM in step with what the customer is doing."""
        if session.current_state in (STATE_GREETING, STATE_REVIEW):
            self.transition(session, STATE_TAKING_ORDER, "ui: cart edited")

    def log_ui_event(self, session: SessionContext, label: str, result: Optional[Dict[str, Any]] = None) -> None:
        self._log_turn(session, f"[button] {label}", (result or {}).get("message", ""), [], source="ui")

    # --------------------------------------------------------------- prompt
    def _cart_text(self, session: SessionContext) -> str:
        order = db_tools.view_current_order(session.session_id)
        if not order["items"]:
            return "empty"
        parts = []
        for i in order["items"]:
            note = f" (note for this dish only: {i['special_instructions']})" if i["special_instructions"] else ""
            parts.append(f"{i['quantity']}x {i['display_name']}{note} @ {i['price']:.0f}")
        return "; ".join(parts) + f". Subtotal {order['subtotal']:.2f} PHP."

    def _get_system_prompt(self, session: SessionContext) -> str:
        guidance = {
            STATE_GREETING: "The guests were just asked 'May I take your order?'. Invite them to pick a category or say what they are craving.",
            STATE_TAKING_ORDER: ("Help them choose dishes. When they name a dish, call add_item_to_order and put any "
                                 "modifications the guest asked for in `instructions` (leave it empty if they asked for none). For ingredient or "
                                 "allergy questions call check_item_allergen. To remove or reduce a dish call "
                                 "remove_item_from_order. To change a note on a dish already in the cart call update_item_instructions."),
            STATE_REVIEW: ("The customer is reviewing their order. Call view_current_order, read the items and grand total "
                           "back briefly. If no mode of payment is chosen yet, ask how they would like to pay (" +
                           ", ".join(db_tools.PAYMENT_METHODS) + "); once it is chosen, ask them to press Confirm order "
                           "(or say 'confirm'). Only change the cart if they ask."),
        }.get(session.current_state, "The order is closed.")
        return (
            f"You are the waiter of Casa Perpetua, speaking through the interactive kiosk at Table {session.table_no} "
            f"(party of {session.party_size}). The kiosk already welcomed the guests and asked 'May I take your order?', "
            "so never greet them again. Your replies are read aloud: be warm, brief (1-2 short sentences) with natural "
            "commas. All prices are in PHP.\n"
            "MENU CATEGORIES: " + ", ".join(db_tools.CATEGORIES) + ".\n"
            "FULL MENU DATABASE (read live from menu_items: name | price | stock | allergens. This is every dish we serve; answer price, stock and allergen questions from it):\n"
            + db_tools.menu_catalog_text() + "\n"
            "Drinks are under Beverages, split into: " + ", ".join(db_tools.BEVERAGE_SUBCATEGORIES) + ". For a specific kind of "
            "drink call get_menu_by_category with category 'Beverages' and that subcategory.\n"
            "DRINKS ONLY (items under Beverages, never food): drinks marked 'hot or iced' in the menu need a temperature - if the "
            "guest did not say which, ask before adding; otherwise put 'Hot' or 'Iced' in the instructions. Every drink also "
            "needs a size (the size argument): hot drinks come in Short or Tall, iced and blended drinks in Tall, Grande or "
            "Venti. Ask if the guest did not say. Water has no size or temperature choice, so just add it.\n"
            "FOOD (every category except Beverages: burgers, pasta, pastries, chicken, etc.) has NO temperature, NO size and NO "
            "extra options. NEVER ask a guest whether food is hot or iced or what size it is, and do not ask about onions or "
            "other modifications - just call add_item_to_order, using `instructions` only if the guest already gave a note.\n"
            "RULES:\n"
            "1. Never invent dishes, prices, ingredients or allergens. Only talk about items returned by your tools.\n"
            "2. When the customer picks a category, asks what is available, or wants to see the menu, call get_menu_by_category "
            "immediately - the kiosk shows the photo cards, so do not list every dish in text.\n"
            "3. If a tool says an item is unavailable, sold out or not on the menu, say so politely and offer an alternative "
            "from the same category.\n"
            "3b. NEVER say a dish or category is missing from the menu unless it is absent from the FULL MENU above. "
            "Plurals and spacing do not matter ('cheeseburgers' = Cheeseburger). When the guest orders anything that is in "
            "the FULL MENU, call add_item_to_order right away instead of answering in text; never suggest a different dish "
            "unless the tool says the dish is unavailable.\n"
            "4. Never claim an order was sent to the kitchen - the Confirm order button does that.\n"
            "5. `instructions` is ONLY for requests the guest made in their latest message, for that dish. If they made none, "
            "leave it empty. Notes shown in CURRENT CART belong to those dishes only: never copy them onto another dish.\n"
            f"CURRENT STATE: {session.current_state}. {guidance}\n"
            + (("MENU ON SCREEN (in order; 'the first one' / 'the second' means these): " + "; ".join(session.last_menu) + ".\n")
               if session.last_menu else "") +
            f"MODE OF PAYMENT: {db_tools.get_payment_method(session.session_id) or 'not chosen yet'}.\n"
            f"CURRENT CART: {self._cart_text(session)}"
        )


    # --------------------------------------------------------------- note guardrail
    _NOTE_STOP = {"no", "not", "without", "with", "and", "or", "the", "a", "an", "it", "make", "please", "extra", "less",
                  "more", "add", "hold", "skip", "on", "in", "of", "to", "for", "my", "me", "i", "want", "have", "can",
                  "side", "only", "just", "some", "very", "little", "bit"}
    _TEMP_WORDS = {"hot", "iced", "ice", "cold"}

    @classmethod
    def _words(cls, text: str) -> List[str]:
        return re.findall(r"[a-z']+", (text or "").lower())

    @classmethod
    def _ground_note(cls, note: str, current: str, recent: str) -> str:
        """Keep only the parts of a model-written note the guest really asked for.
        Small models copy examples from the prompt or notes of other cart lines (e.g. 'no onions' on
        a pretzel), so every comma/'and' separated part must be backed by the guest's own words.
        Hot/Iced may also come from the last few guest messages (answer to 'hot or iced?')."""
        cur = cls._words(current)
        rec = cls._words(recent)
        kept = []

        def seen(w, pool):
            return any(p == w or (len(w) >= 4 and p[:4] == w[:4]) for p in pool)

        for part in re.split(r",|;|\band\b|\+", note or ""):
            toks = [w for w in cls._words(part) if w not in cls._NOTE_STOP]
            if toks and all(seen(w, rec if w in cls._TEMP_WORDS else cur) for w in toks):
                kept.append(part.strip())
        return ", ".join(kept)

    def _guard_tool_args(self, session: SessionContext, name: str, args: Dict[str, Any], user_text: str) -> Dict[str, Any]:
        """Strip ungrounded notes from add_item_to_order / update_item_instructions before they touch the DB."""
        if name not in ("add_item_to_order", "update_item_instructions") or not args.get("instructions"):
            return args
        recent = " ".join(m["content"] for m in session.messages[-6:] if m["role"] == "user")
        return {**args, "instructions": self._ground_note(str(args["instructions"]), user_text, recent)}

    # --------------------------------------------------------------- LLM helpers
    def _call_ollama(self, messages: List[Dict[str, Any]], tools: Optional[list] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {"model": self.model_name, "messages": messages, "stream": False,
                                "keep_alive": KEEP_ALIVE, "options": OLLAMA_OPTIONS}
        if tools:
            body["tools"] = tools
        resp = requests.post(OLLAMA_CHAT_URL, json=body, timeout=OLLAMA_TIMEOUT)
        resp.raise_for_status()
        return resp.json().get("message", {}) or {}

    def warm_up(self) -> None:
        """Load the model into memory in the background so the first real question is fast."""
        def _go():
            try:
                requests.post(OLLAMA_CHAT_URL, timeout=120, json={
                    "model": self.model_name, "stream": False, "keep_alive": KEEP_ALIVE,
                    "messages": [{"role": "user", "content": "hi"}], "options": {"num_predict": 1}})
            except requests.exceptions.RequestException:
                pass
        threading.Thread(target=_go, daemon=True).start()

    @staticmethod
    def _sentence(text: str) -> str:
        text = (text or "").strip()
        return text if not text or text[-1] in ".!?" else text + "."

    def _compose_reply(self, records: List[Dict[str, Any]], state: str) -> str:
        """Build the waiter's spoken/written reply from tool results in code, so the turn
        needs only ONE model call (the second 'summarise the tool result' call was the slowest part)."""
        parts: List[str] = []
        added: List[str] = []
        for r in records:
            res, tool = r["result"], r["tool"]
            if tool == "get_menu_by_category" and res.get("success"):
                parts.append(f"Here is our {_menu_label(res)} menu. Tap Add to order on anything you would like.")
            elif tool == "add_item_to_order" and res.get("success"):
                added.append(res["message"].replace("Added ", "").replace(" to the cart.", "").replace("x ", " ", 1))
            elif tool == "view_current_order":
                n = res.get("item_count", 0)
                if n:
                    pay = res.get("payment_method")
                    parts.append(f"You have {n} item{'s' if n != 1 else ''}, {res['grand_total']:.2f} pesos with the service fee. "
                                 + (f"Paying by {pay}. Press Confirm order when you are ready." if pay else
                                    "How would you like to pay: cash, GCash, Maya, or card?"))
                else:
                    parts.append("Your cart is empty.")
            elif res.get("message"):
                parts.append(res["message"])
        if added:
            parts.insert(0, "Added " + ", ".join(added) + " to your order.")
            if state != STATE_REVIEW:
                parts.append("Anything else?")
        return " ".join(self._sentence(p) for p in parts).strip()

    @staticmethod
    def _normalise_tool_calls(message: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Return [{'name','args'}]. Also rescues the common qwen quirk of
        printing the tool call as JSON text instead of using tool_calls."""
        calls = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function", {})
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            if fn.get("name"):
                calls.append({"name": fn["name"], "args": args or {}})
        if calls:
            return calls
        content = (message.get("content") or "").strip()
        if content.startswith("{") or content.startswith("["):
            try:
                data = json.loads(content)
                for d in data if isinstance(data, list) else [data]:
                    name = d.get("name")
                    args = d.get("arguments") or d.get("parameters") or {}
                    if name in {t["function"]["name"] for t in db_tools.OLLAMA_TOOLS}:
                        calls.append({"name": name, "args": args if isinstance(args, dict) else {}})
            except Exception:
                pass
        return calls

    @staticmethod
    def _fallback_text(records: List[Dict[str, Any]]) -> str:
        for r in reversed(records):
            res = r["result"]
            if r["tool"] == "get_menu_by_category" and res.get("success"):
                return f"Here is our {_menu_label(res)} menu. Tap Add to order on anything you would like."
            if res.get("message"):
                return res["message"]
        return "How can I help you next?"

    def _run_llm(self, session: SessionContext) -> Tuple[str, List[Dict[str, Any]], Optional[str]]:
        records: List[Dict[str, Any]] = []
        payload = [{"role": "system", "content": self._get_system_prompt(session)}] + session.messages[-HISTORY_WINDOW:]
        user_text = session.messages[-1]["content"]

        # FAST PATH: a plain "show me the X menu" never needs the model.
        fast = RE_MENU_FAST.match(user_text)
        if fast:
            category, sub = db_tools.resolve_category(fast.group(1))
            category = next((c for c in db_tools.CATEGORIES if c.lower() == category.lower()), category)
            args = {"category": category, **({"subcategory": sub} if sub else {})}
            result = db_tools.execute_tool_call("get_menu_by_category", args, session.session_id)
            records.append({"tool": "get_menu_by_category", "args": args, "result": result, "fast_path": True})
            return self._compose_reply(records, session.current_state), records, None

        try:
            message = self._call_ollama(payload, db_tools.OLLAMA_TOOLS)
            calls = self._normalise_tool_calls(message)

            if calls:
                for c in calls:
                    raw_note = c["args"].get("instructions")
                    c["args"] = self._guard_tool_args(session, c["name"], c["args"], user_text)
                    if c["name"] == "update_item_instructions" and raw_note and not c["args"].get("instructions"):
                        records.append({"tool": c["name"], "args": c["args"], "result": {
                            "success": False, "message": f"What note would you like on the {c['args'].get('item_name', 'dish')}?"}})
                        continue
                    result = db_tools.execute_tool_call(c["name"], c["args"], session.session_id)
                    records.append({"tool": c["name"], "args": c["args"], "result": result})
                # one model call per turn: the reply is composed from the tool results in code
                text = self._compose_reply(records, session.current_state) or self._fallback_text(records)
                return text, records, None

            text = (message.get("content") or "").strip()
            rescued = self._rescue_from_menu(session, user_text, text, records)
            if rescued:
                return rescued, records, None
            # Guardrail: the model skipped the tool for a category request -> do it for it.
            m = None if RE_CART_VERB.search(user_text) else RE_CATEGORY.search(user_text)
            if m and not text.startswith("{"):
                result = db_tools.execute_tool_call("get_menu_by_category", {"category": m.group(1)}, session.session_id)
                records.append({"tool": "get_menu_by_category", "args": {"category": m.group(1)}, "result": result,
                                "guardrail": True})
                return self._fallback_text(records), records, None
            return text or "What can I prepare for you today?", records, None

        except requests.exceptions.RequestException as e:
            return self._offline(user_text, records, session), records, f"ollama: {e.__class__.__name__}"
        except Exception as e:  # never crash the kiosk on a model hiccup
            return self._offline(user_text, records, session), records, f"agent: {e.__class__.__name__}: {e}"

    RE_CLAIMS_MISSING = re.compile(r"\b(don'?t have|do not have|not on (?:our|the) menu|isn'?t on (?:our|the) menu|"
                                   r"not available|no longer (?:have|available)|we (?:don'?t|do not) (?:serve|offer))\b", re.I)
    _FILLER = {"please", "pls", "and", "a", "an", "the", "some", "of", "x", "also", "plus", "then", "with", "me", "i", "we",
               "ll", "d", "like", "have", "get", "want", "take", "would", "can", "could", "give", "add", "order", "to", "my",
               "for", "us", "just", "only", "thanks", "thank", "you", "more", "another", "one"}
    RE_QTY_BEFORE = re.compile(r"(\d{1,2}|\b(?:a|an|one|two|three|four|five|six|seven|eight|nine|ten|couple of|dozen)\b)\s*(?:x\s*|of\s+)?$", re.I)
    RE_NOTE = re.compile(r"\b(?:no|without|extra|less|hold the|easy on the)\s+[a-z]+(?:\s+[a-z]+)?", re.I)

    def _rescue_from_menu(self, session: SessionContext, user_text: str, model_text: str, records: list) -> Optional[str]:
        """The model answered in prose without a tool call. If the guest named real dishes, the DB decides, not the
        model: run the add in code (add intent), or state the facts (the model wrongly claimed the dish is missing)."""
        with db_tools.db() as conn:
            hits = db_tools.match_menu_items(conn, user_text)
        if not hits:
            return None
        if "?" in user_text and re.search(r"\b(allerg\w*|contain\w*|ingredient\w*|has|have|with)\b", user_text, re.I) \
                and not RE_ADD_INTENT.search(user_text):
            words = {w for ws in db_tools.ALLERGEN_SYNONYMS.values() for w in ws}
            found = next((w for w in re.findall(r"[a-z]+", user_text.lower()) if w in words), "")
            if found:                                   # "does the cheeseburger contain dairy?" -> answered from the DB
                args = {"item_name": hits[0][0]["name"], "allergen": found}
                res = db_tools.execute_tool_call("check_item_allergen", args, session.session_id)
                records.append({"tool": "check_item_allergen", "args": args, "result": res, "guardrail": True})
                return res["message"]
        # "cheeseburger please" / "2 pretzels" / "bacon burger and veggie burger": nothing but dish names + filler words
        rest = user_text
        for _, a, b in reversed(hits):
            rest = rest[:a] + " " + rest[b:]
        leftover = [w for w in re.findall(r"[a-z']+", rest.lower()) if w not in self._FILLER and w not in db_tools._QTY_WORDS]
        bare_order = not leftover and "?" not in user_text
        only_food = all(r["category"] != "Beverages" for r, _, _ in hits)
        asks_drink_q = bool(re.search(r"\b(hot|iced|size|short|tall|grande|venti)\b", model_text or "", re.I)) and only_food
        wants_add = bool(RE_ADD_INTENT.search(user_text)) or bare_order
        claims_missing = bool(self.RE_CLAIMS_MISSING.search(model_text or ""))
        if not (wants_add or claims_missing or asks_drink_q):
            return None
        if asks_drink_q and not wants_add and "?" in user_text and re.search(r"\b(allerg|contain|ingredient|price|cost|how much)\b", user_text, re.I):
            return None
        if not wants_add:                       # "do you have cheeseburgers?" -> answer from the DB
            names = ", ".join(f"{r['name']} (PHP {r['price']:.0f})" for r, _, _ in hits if r["stock_qty"] > 0)
            return (f"Yes, we have {names}. Would you like to add it?" if names else "Sorry, that dish is sold out right now.")
        for i, (row, a, b) in enumerate(hits):
            qty_m = self.RE_QTY_BEFORE.search(user_text[:a].strip())
            qty = db_tools._to_qty(qty_m.group(1).replace("couple of", "couple")) if qty_m else 1
            tail = user_text[b:(hits[i + 1][1] if i + 1 < len(hits) else len(user_text))]
            note = ", ".join(m.group(0).strip() for m in self.RE_NOTE.finditer(tail))
            args = {"item_name": row["name"], "qty": qty, "instructions": note}
            result = db_tools.execute_tool_call("add_item_to_order", args, session.session_id)
            records.append({"tool": "add_item_to_order", "args": args, "result": result, "guardrail": True})
        return self._compose_reply(records, session.current_state) or self._fallback_text(records)

    def _offline(self, user_text: str, records: list, session: SessionContext) -> str:
        m = None if RE_CART_VERB.search(user_text) else RE_CATEGORY.search(user_text)
        if m and not records:
            result = db_tools.execute_tool_call("get_menu_by_category", {"category": m.group(1)}, session.session_id)
            records.append({"tool": "get_menu_by_category", "args": {"category": m.group(1)}, "result": result,
                            "guardrail": True})
            return f"My voice assistant is offline, but here is our {m.group(1).title()} menu. Tap Add to order to pick dishes."
        return ("My assistant is offline right now (is Ollama running?). You can still browse the categories above "
                "and use the + and - buttons to build your order.")

    # --------------------------------------------------------------- FSM evaluation
    def _capture_table(self, session: SessionContext, text: str) -> None:
        m = RE_TABLE.search(text)
        if m and session.current_state == STATE_GREETING:
            n = int(m.group(1))
            if 1 <= n <= 99:
                session.table_no = n
                db_tools.set_table_number(session.session_id, n)

    def _pre_transition(self, session: SessionContext, text: str) -> Optional[str]:
        """Intent-driven transitions decided BEFORE the LLM answers (so the prompt
        carries the right state). Returns a canned reply when code, not the model,
        must speak (confirm / cancel / empty-cart checkout)."""
        state = session.current_state
        if state in FINAL_STATES:
            return ("Your order is complete - tap 'New order' to start another."
                    if state == STATE_CONFIRMED else "That order was cancelled - tap 'New order' to start again.")

        if RE_CANCEL.search(text):
            return self.cancel_order(session, f"chat: {text}")["message"]

        if state == STATE_GREETING:                    # answers to "May I take your order?"
            if RE_READY_YES.match(text):
                self.transition(session, STATE_TAKING_ORDER, f"chat: {text}")
                return "Wonderful! What can I get started for you? You can tap a category above, or just tell me what you're craving."
            if RE_READY_NO.match(text):
                return "Of course, take your time. Just say the word, when you're ready to order."

        if state in (STATE_GREETING, STATE_TAKING_ORDER) and RE_CHECKOUT.search(text) and self._mentions_item(text):
            session.pending_review = True              # let the model add the dish(es) first; review follows in process_turn

        elif state in (STATE_GREETING, STATE_TAKING_ORDER) and RE_CHECKOUT.search(text):
            res = self.request_review(session, f"chat: {text}")
            if not res["ok"]:
                return res["message"]          # e.g. empty cart
            # read the bill back from code - instant, and no model call needed
            order = db_tools.view_current_order(session.session_id)
            return self._compose_reply([{"tool": "view_current_order", "result": order}], STATE_REVIEW)

        if state == STATE_GREETING and (RE_ORDER_INTENT.search(text) or RE_CATEGORY.search(text)):
            self.transition(session, STATE_TAKING_ORDER, f"chat: {text}")

        if state == STATE_REVIEW and RE_PAYMENT.search(text) and not RE_CART_VERB.search(text):
            method = db_tools.resolve_payment_method(text)
            if method:
                reply = self.set_payment(session, method, f"chat: {text}")["message"]
                if RE_CONFIRM.search(text) and not RE_NEGATION.search(text) and not RE_EDIT.search(text):
                    return self.confirm_order(session, f"chat: {text}")["message"]      # "GCash, go ahead"
                return reply + " Press Confirm order when you are ready."
            if re.search(r"\b(pay|payment|paying)\b", text, re.I):                      # "how can I pay?"
                return "You can pay by " + ", ".join(db_tools.PAYMENT_METHODS[:-1]) + " or " + db_tools.PAYMENT_METHODS[-1] + ". Which would you like?"

        if state == STATE_REVIEW:
            if RE_CONFIRM.search(text) and not RE_NEGATION.search(text) and not RE_EDIT.search(text):
                return self.confirm_order(session, f"chat: {text}")["message"]
            if RE_EDIT.search(text) or RE_NEGATION.search(text):
                self.transition(session, STATE_TAKING_ORDER, f"chat: {text}")
        return None

    @staticmethod
    def _mentions_item(text: str) -> bool:
        """True when a checkout phrase comes with something to order (a dish name or 'add / I'll have ...')."""
        t = text.lower()
        with db_tools.db() as conn:
            names = [r["name"].lower() for r in conn.execute("SELECT name FROM menu_items;")]
        return any(n in t for n in names) or bool(RE_ADD_INTENT.search(text))

    def _post_transition(self, session: SessionContext, records: List[Dict[str, Any]], trigger: str) -> None:
        """Tool-driven transitions decided AFTER the LLM acted."""
        touched = any(r["tool"] in MODIFYING_TOOLS and r["result"].get("success") for r in records)
        browsed = any(r["tool"] == "get_menu_by_category" for r in records)
        if session.current_state == STATE_GREETING and (touched or browsed):
            self.transition(session, STATE_TAKING_ORDER, f"tool: {trigger}")
        elif session.current_state == STATE_REVIEW and touched:
            self.transition(session, STATE_TAKING_ORDER, f"tool: cart changed during review")

    # --------------------------------------------------------------- the turn loop
    def process_turn(self, session: SessionContext, user_input: str) -> Dict[str, Any]:
        user_input = (user_input or "").strip()
        if not user_input:
            return {**self.snapshot(session), "assistant_reply": "I'm listening - what would you like?",
                    "tool_calls": [], "error": None}

        session.messages.append({"role": "user", "content": user_input})
        self._capture_table(session, user_input)

        canned = self._pre_transition(session, user_input)
        if canned is not None:
            assistant_text, records, error = canned, [], None
        else:
            assistant_text, records, error = self._run_llm(session)
            self._post_transition(session, records, user_input)
            for r in records:                                   # remember what the guest is looking at
                if r["tool"] == "get_menu_by_category" and r["result"].get("success"):
                    session.last_menu = [f"{i['name']} ({i['price']:.0f})" for i in r["result"]["items"]]
            if session.pending_review:
                session.pending_review = False
                if session.current_state not in FINAL_STATES and self.request_review(session, f"chat: {user_input}")["ok"]:
                    order = db_tools.view_current_order(session.session_id)
                    bill = self._compose_reply([{"tool": "view_current_order", "result": order}], STATE_REVIEW)
                    assistant_text = re.sub(r"\s*Anything else\?\s*$", "", assistant_text).strip()
                    assistant_text = (assistant_text + " " + bill).strip()

        session.messages.append({"role": "assistant", "content": assistant_text})
        self._log_turn(session, user_input, assistant_text, records, error=error)

        return {**self.snapshot(session), "assistant_reply": assistant_text, "tool_calls": records, "error": error}

    # --------------------------------------------------------------- logging
    def _db_state(self, session: SessionContext) -> Dict[str, Any]:
        order = db_tools.view_current_order(session.session_id)
        with db_tools.db() as conn:
            row = conn.execute("SELECT status FROM table_sessions WHERE session_id = ?;", (session.session_id,)).fetchone()
        return {
            "table_sessions.status": row["status"] if row else None,
            "order_line_items": [{"item": i["display_name"], "qty": i["quantity"], "notes": i["special_instructions"]}
                                 for i in order["items"]],
            "grand_total_php": order["grand_total"],
            "payment_method": order["payment_method"] or None,
        }

    def _log_turn(self, session, user_text, agent_text, records, source="chat", error=None) -> None:
        trimmed = []
        for r in records:
            res = dict(r["result"])
            if isinstance(res.get("items"), list) and r["tool"] == "get_menu_by_category":
                res["items"] = [{"name": i["name"], "price": i["price"], "stock_qty": i["stock_qty"]} for i in res["items"]]
            trimmed.append({"tool": r["tool"], "args": r["args"], "result": res,
                            **({"guardrail": True} if r.get("guardrail") else {}),
                            **({"fast_path": True} if r.get("fast_path") else {})})
        session.turns.append({
            "turn": len(session.turns) + 1,
            "source": source,
            "user_transcript": user_text,
            "fsm_state": session.current_state,
            "tool_calls": trimmed,
            "agent_response": agent_text,
            "db_state": self._db_state(session),
            **({"error": error} if error else {}),
        })
        self.save_log(session)

    def export_log(self, session: SessionContext) -> Dict[str, Any]:
        state = self._db_state(session)
        order = db_tools.view_current_order(session.session_id)
        return {
            "session_id": session.session_id,
            "table_number": session.table_no,
            "party_size": session.party_size,
            "model": self.model_name,
            "started_at": session.created_at,
            "fsm_state_transitions": session.transitions,
            "turns": session.turns,
            "final_cart_state": {
                "status": state["table_sessions.status"],
                "grand_total_php": order["grand_total"],
                "payment_method": order["payment_method"] or None,
                "items_purchased": [i["name"] for i in order["items"]],
            },
            "receipt": session.receipt,
        }

    def save_log(self, session: SessionContext) -> Optional[str]:
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            path = os.path.join(LOG_DIR, f"{session.session_id}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.export_log(session), f, indent=2, ensure_ascii=False, default=str)
            return path
        except OSError:
            return None