"""
record_sample_session.py - records sample_conversation_log.json (Lab 6.1 deliverable 5) from a LIVE Qwen run.

Run it on the laptop that has Ollama:
    ollama serve            (separate terminal)
    ollama pull qwen2.5
    python record_sample_session.py

It plays one scripted guest through the REAL agent (fsm_agent + db_tools + Ollama), so every tool call,
FSM transition and database state in the saved log was produced by the model, not replayed.
It works on a temporary COPY of restaurant.db, so your real stock levels are never touched.
It refuses to write the file when Ollama is not reachable, so a fake log can never be saved by accident.
"""
import json
import os
import shutil
import sys
import tempfile

import requests

import db_tools
import fsm_agent

OUT_FILE = "sample_conversation_log.json"

# (what the guest says, what this turn is meant to show)
SCRIPT = [
    ("yes",                                                         "GREETING -> ORDERING"),
    ("Show me the Burgers menu",                                    "tool: get_menu_by_category"),
    ("Add 2 cheeseburgers, no onions",                              "tool: add_item_to_order with special instructions"),
    ("Add 1 Authentic Pad Thai",                                    "tool: add_item_to_order (second dish)"),
    ("Does the Pad Thai have peanuts?",                             "tool: check_item_allergen (no guessing)"),
    ("Please add a Unicorn Burger",                                 "edge case: item not on the menu (no hallucination)"),
    ("Add 500 cheeseburgers",                                       "edge case: more than the stock left"),
    ("Actually make the cheeseburgers no onions and extra cheese",  "tool: update_item_instructions"),
    ("Remove the Pad Thai",                                         "tool: remove_item_from_order"),
    ("Remove the Lasagna",                                          "edge case: removing an item that is not in the cart"),
    ("That's all, check out",                                       "ORDERING -> REVIEW (itemised tab + totals)"),
    ("I'll pay by GCash",                                           "payment captured in REVIEW"),
    ("Confirm order and send to kitchen",                           "REVIEW -> CONFIRMED (stock deducted, receipt)"),
]


def ollama_ready(agent) -> bool:
    base = fsm_agent.OLLAMA_CHAT_URL.rsplit("/api/", 1)[0]
    try:
        names = [m.get("name", "") for m in requests.get(base + "/api/tags", timeout=3).json().get("models", [])]
    except requests.exceptions.RequestException:
        print("Ollama is not reachable at", base, "- run `ollama serve` first.")
        return False
    if not any(n.startswith(agent.model_name) for n in names):
        print(f"Model '{agent.model_name}' is not pulled - run `ollama pull {agent.model_name}`.")
        return False
    return True


def main(agent=None, out_file: str = OUT_FILE) -> int:
    agent = agent or fsm_agent.CasaPerpetuaAgent()
    if not ollama_ready(agent):
        print("Nothing was written.")
        return 1

    tmp = tempfile.mkdtemp(prefix="cp_sample_")
    real_db, real_logs = db_tools.DB_PATH, fsm_agent.LOG_DIR
    shutil.copyfile(real_db, os.path.join(tmp, "restaurant.db"))
    db_tools.DB_PATH = os.path.join(tmp, "restaurant.db")
    fsm_agent.LOG_DIR = os.path.join(tmp, "logs")
    try:
        session = agent.start_session(table_no=4, party_size=2)
        for n, (text, goal) in enumerate(SCRIPT, 1):
            out = agent.process_turn(session, text)
            tools = ", ".join(c["tool"] for c in out["tool_calls"]) or "-"
            print(f"{n:>2}. [{session.current_state:<9}] {text!r}\n      goal: {goal}\n      tools: {tools}\n      waiter: {out['assistant_reply'][:110]}")
        log = agent.export_log(session)
        log["_note"] = "Recorded live: scripted guest messages sent through the real FSM, Ollama/Qwen tool calls and SQLite tools (record_sample_session.py)."
        called = {c["tool"] for t in log["turns"] for c in t["tool_calls"]}
        problems = []
        if log["final_cart_state"]["status"] != "CONFIRMED":
            problems.append(f"session ended in {log['final_cart_state']['status']}, not CONFIRMED")
        for tool in ("get_menu_by_category", "add_item_to_order", "check_item_allergen", "remove_item_from_order"):
            if tool not in called:
                problems.append(f"Qwen never called {tool}")
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(log, f, indent=2, ensure_ascii=False, default=str)
        print(f"\nSaved {out_file} ({len(log['turns'])} turns, tools used: {', '.join(sorted(called))}).")
        if problems:
            print("Check before submitting (Qwen is not deterministic - just run the script again):")
            for p in problems:
                print("  -", p)
        return 0
    finally:
        db_tools.DB_PATH, fsm_agent.LOG_DIR = real_db, real_logs
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
