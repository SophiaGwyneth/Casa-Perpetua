# Casa Perpetua Virtual Waiter - Pair Collaboration Log
**Date:** October 10, 2026 (design and build sessions: Oct 10 and Oct 15; final QA before submission)
**Class:** AI Lab - Lesson 6 (Conversational State Machines), Lab Activity 6.1
**Pair:** Student A = Sophia Gwyneth T. Dichoso   |   Student B = Jocelyn B. Parane

## 1. Role Rotation & Task Distribution

| Phase | Duration | Driver (Keyboard/Code) | Navigator (Logic/Prompt review) | Task Focus |
| :--- | :--- | :--- | :--- | :--- |
| **Phase 1: DB & Tool Binding** | 45m | Sophia | Jocelyn | `init_restaurant_db.sql`, `db_tools.py`: schema, 15 categories, CRUD execution, Ollama tool schemas. |
| **Phase 2: Visual Kiosk Integrations** | 50m | Sophia | Jocelyn | Dynamic HTML rendering of food images and the cart panel in `virtual_waiter_prototype.py`. |
| *Role Rotation (halfway)* | - | *Roles swapped* | *Roles swapped* | - |
| **Phase 3: FSM & Agent Loop** | 45m | Jocelyn | Sophia | `fsm_agent.py`: Qwen 2.5 tool-call parsing, multi-turn state transitions, menu guardrails. |
| **Phase 4: Manual Overrides** | 30m | Jocelyn | Sophia | `/api/manual_override` for instant cart (+/-) adjustments; receipt modal. |

**Balance check:** Sophia drove 95 min and navigated 75 min; Jocelyn drove 75 min and navigated 95 min. Each partner therefore spent 170 min in total, split between the two roles.

## 2. What the Navigator Reviewed (logic / prompts)

| Phase | Navigator's review | Result |
| :--- | :--- | :--- |
| 1 | Could the model invent a dish or price? | Every add/remove/allergen call goes through SQLite; an unknown dish returns "not on our menu" with "Did you mean ...?" suggestions. |
| 1 | Allergy safety: guests say "nuts" or "milk", the DB says `peanut` / `dairy`. | Added `ALLERGEN_SYNONYMS` so "does it have nuts?" never wrongly answers "does not list". |
| 3 | Does the prompt change with the FSM state? | `_get_system_prompt` gives Qwen a different instruction in GREETING, ORDERING and REVIEW, and the full menu table is re-read from the DB on every turn. |
| 3 | Model copies a note from one dish onto another ("no onions" on every line). | `_ground_note` / `_guard_tool_args` only keep a note if the guest actually said it for that dish. |
| 3 | Long orders silently lost the system prompt. | `num_ctx` raised to 8192 (menu table + 7 tool schemas + history), `num_predict` raised so a multi-item tool call is never cut off mid-JSON. |
| 3 | Model answers in prose instead of calling the tool. | `_rescue_from_menu`: if the guest named real dishes, code runs the add or the allergen check, and the DB decides, not the model. |
| 4 | Can a stale or double click confirm twice or oversell? | `confirm_and_deduct_inventory` runs in one `BEGIN IMMEDIATE` transaction, re-checks stock and refuses to confirm without a payment method. |

## 3. Key Architectural Decisions
1. **Visual Kiosk UI (handout Option 1):** a scrollable category ribbon calls `get_menu_by_category`, so the guest can browse 15 categories visually (including the Chinese and Thai additions). Left panel = conversation and voice; right panel = live cart synced to SQLite; bottom bar = FSM progress, Review / Confirm and the receipt modal.
2. **Human-in-the-Loop Cart Overrides:** a dedicated REST endpoint (`/api/manual_override`) bypasses Qwen so the (+ / -) buttons change quantities instantly; the FSM is kept in step (`note_manual_change`).
3. **The LLM talks and calls tools; code decides money and stock.** Confirm, cancel, payment and the stock deduction are never decided by the model.
4. **Five states.** The handout asks for 5 states but names four, so we added `CANCELLED` as a real state (order cancellation is a rubric edge case). Every move is validated against a `TRANSITIONS` table, saved to `table_sessions.status` and written to `logs/<session_id>.json`.
5. **Terminal mode as well as the dashboard.** `python virtual_waiter_prototype.py --cli` runs the same agent, FSM and SQLite tools in a plain terminal chat, matching the handout's "Python CLI" expected outcome.

## 4. Testing & Edge Cases
Edge cases we test on every run (`record_sample_session.py` replays them against live Qwen on a temporary copy of the database):
- Item not on the menu ("Unicorn Burger"): refused, no invented dish.
- Quantity above the stock left ("500 cheeseburgers"): refused with the remaining stock.
- Removing an item that is not in the cart ("Remove the Lasagna"): polite message, no crash.
- Changing a note on a dish already in the cart; allergen questions answered from the DB.
- Final QA fix: quantities with three digits are now parsed correctly in the tool-rescue path.

## 5. Deliverables Checklist
`init_restaurant_db.sql`, `db_tools.py`, `fsm_agent.py`, `virtual_waiter_prototype.py`, `pair_collaboration_log.md` (this file), `sample_conversation_log.json` (recorded live with `record_sample_session.py`).

## 6. How to Run
1. Install Python 3 and the two packages: `pip install flask requests`
2. Install [Ollama](https://ollama.com), then in a terminal: `ollama pull qwen2.5` and `ollama serve` (leave it running).
3. From this folder, run `python virtual_waiter_prototype.py` and open http://localhost:5000 (web dashboard).
   For the plain terminal version, run `python virtual_waiter_prototype.py --cli`.
4. `restaurant.db` is created automatically from `init_restaurant_db.sql` if it is missing. Keep the food image folders and `aeris.png` next to the `.py` files so the photos and welcome screen show.