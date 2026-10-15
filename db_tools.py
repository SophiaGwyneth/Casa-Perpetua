"""
db_tools.py - SQLite layer + LLM tool functions for the Casa Perpetua kiosk.

Everything the LLM is allowed to do to the restaurant goes through this file,
so the model can never invent a menu item or a price: if a dish is not in
`menu_items`, the tool says so and the waiter has to relay that.
"""
import os
import re
import sqlite3
import unicodedata
from urllib.parse import quote
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

DB_PATH = "restaurant.db"
SCHEMA_PATH = "init_restaurant_db.sql"
SERVICE_FEE_RATE = 0.10

CATEGORIES = [
    "Pasta", "Pastries", "Beverages", "Desserts", "Snacks", "Dairy",
    "Burgers", "Chicken", "Filipino", "Italian", "Japanese", "Korean",
    "Spanish", "Thai", "Chinese",
]

# Beverages is split into three sub-categories (shown as a second row of chips in the kiosk).
BEVERAGE_SUBCATEGORIES = ["Coffee", "Non-Coffee", "Refreshers"]

# Casual names that point to the Beverages category as a whole.
CATEGORY_ALIASES = {"beverage": "Beverages", "drinks": "Beverages", "drink": "Beverages"}

# Casual names that point to one Beverages sub-category.
SUBCATEGORY_ALIASES = {
    "coffee": "Coffee", "coffees": "Coffee",
    "non-coffee": "Non-Coffee", "non coffee": "Non-Coffee", "noncoffee": "Non-Coffee",
    "refreshers": "Refreshers", "refresher": "Refreshers",
}

# Used by _migrate to sort the existing drinks into their sub-category.
_SUB_BY_NAME = {
    "Coffee": ["Cafe Latte", "Americano", "Spanish Latte", "Cappuccino"],
    "Refreshers": ["Iced Tea", "Mango Smoothie", "Lemonade", "Calamansi Juice", "Calamansi Cooler", "Water"],
}


# Drinks the guest can have hot or iced (the kiosk shows a Hot / Iced toggle; the choice is saved
# as the line's note, so "Iced" and "Hot" lattes are separate cart lines).
_HOT_OR_ICED = ["Cafe Latte", "Americano", "Spanish Latte", "Cappuccino",
                "Matcha Tea Latte"]
RE_TEMP = re.compile(r"\b(hot|iced?|cold)\b", re.I)

# New beverages, added once by name (price / stock are starting values - edit them in restaurant.db).
# Name, sub-category, price, stock, allergens
_NEW_BEVERAGES = [
    ("Caramel Crème Frappuccino",   "Non-Coffee", 170, 30, "dairy"),
    ("Strawberry Crème Frappuccino", "Non-Coffee", 170, 30, "dairy"),
    ("Matcha Tea Latte",            "Non-Coffee", 150, 30, "dairy"),
    ("Matcha Crème Frappuccino",    "Non-Coffee", 180, 30, "dairy"),
    ("Vanilla Matcha Frappuccino",  "Non-Coffee", 180, 30, "dairy"),
    ("Iced Green Tea",              "Refreshers",  60, 50, ""),
    ("Iced Matcha Lemonade",        "Refreshers", 110, 40, ""),
    ("Water",                       "Refreshers",   0, 100, ""),      # free of charge
]


# Drink sizes. Hot drinks come in Short or Tall; iced / blended drinks in Tall, Grande or Venti.
# Prices in menu_items are the TALL price; the other sizes add / subtract the amounts below (PHP).
HOT_SIZES = ["Short", "Tall"]
COLD_SIZES = ["Tall", "Grande", "Venti"]
SIZES = ["Short", "Tall", "Grande", "Venti"]
DEFAULT_SIZE = "Tall"
SIZE_PRICE_DELTA = {"Short": -15, "Tall": 0, "Grande": 20, "Venti": 40}
RE_SIZE = re.compile(r"\b(short|tall|grande|venti)\b", re.I)
# Drinks with a single serving: no size picker, no size question, no size surcharge.
NO_SIZE_DRINKS = {"water"}


# Mode of payment. The guest picks one at REVIEW; the order cannot be confirmed without it.
# (No money moves in the kiosk - the choice tells the staff how to settle the bill at the table / counter.)
PAYMENT_METHODS = ["Cash", "GCash", "Maya", "Credit/Debit Card"]
PAYMENT_NOTES = {
    "Cash": "Please pay your waiter or the counter when you are done.",
    "GCash": "Scan the GCash QR at the counter when you are done.",
    "Maya": "Scan the Maya QR at the counter when you are done.",
    "Credit/Debit Card": "Your waiter will bring the card terminal to your table.",
}
# Words a guest might say -> canonical method (longest phrases first so "credit card" never falls through to "card").
_PAYMENT_ALIASES = [
    ("credit/debit card", "Credit/Debit Card"), ("credit card", "Credit/Debit Card"), ("debit card", "Credit/Debit Card"),
    ("credit", "Credit/Debit Card"), ("debit", "Credit/Debit Card"), ("card", "Credit/Debit Card"),
    ("g-cash", "GCash"), ("g cash", "GCash"), ("gcash", "GCash"),
    ("paymaya", "Maya"), ("maya", "Maya"),
    ("cash", "Cash"),
]


def resolve_payment_method(text: str) -> Optional[str]:
    """'gcash' / 'credit card' / 'I'll pay in cash' -> canonical method name, or None."""
    t = (text or "").strip().lower()
    for word, method in _PAYMENT_ALIASES:
        if re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", t):
            return method
    return None


# Words guests actually say -> the allergen labels stored in menu_items.allergens.
# Without this, "does it have nuts / milk / shrimp?" wrongly answers "does not list ..." (unsafe for allergy guests).
ALLERGEN_SYNONYMS = {
    "dairy": {"dairy", "milk", "lactose", "cheese", "butter", "cream"},
    "gluten": {"gluten", "wheat", "flour"},
    "egg": {"egg", "eggs"},
    "peanut": {"peanut", "peanuts", "nut", "nuts", "groundnut"},
    "tree nut": {"tree nut", "tree nuts", "nut", "nuts", "almond", "cashew", "walnut", "pecan", "pistachio", "hazelnut"},
    "shellfish": {"shellfish", "shrimp", "prawn", "prawns", "crab", "lobster", "squid", "seafood"},
    "fish": {"fish", "seafood", "salmon", "tuna", "anchovy", "anchovies"},
    "soy": {"soy", "soya", "soybean", "soybeans"},
    "sesame": {"sesame"},
}


def _allergen_labels(term: str) -> set:
    """'nuts' -> {'peanut', 'tree nut'}; 'milk' -> {'dairy'}; unknown words map to themselves."""
    t = re.sub(r"\b(allerg(?:y|ic|ies|en)|free|intolerance)\b", "", (term or "").lower()).strip(" -")
    labels = {label for label, words in ALLERGEN_SYNONYMS.items() if t in words or t == label}
    return labels or {t}


def _temps(item) -> List[str]:
    return [o.strip() for o in (item["temp_options"] or "").split(",") if o.strip()]


def size_menu(item) -> Dict[str, List[str]]:
    """Which sizes each temperature offers for this drink. '' = drinks with no hot/iced choice (always cold)."""
    return {"Hot": HOT_SIZES, "Iced": COLD_SIZES} if len(_temps(item)) > 1 else {"": COLD_SIZES}


_QTY_WORDS = {"a": 1, "an": 1, "one": 1, "single": 1, "two": 2, "couple": 2, "three": 3, "four": 4, "five": 5,
              "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "dozen": 12}


def _to_qty(qty, default: int = 1) -> int:
    """Models sometimes send qty as 'two' or '2'; never silently turn that into 1."""
    if isinstance(qty, str):
        w = qty.strip().lower()
        if w in _QTY_WORDS:
            return _QTY_WORDS[w]
    try:
        return int(qty)
    except (TypeError, ValueError):
        return default


def _strip_sizes(text: str) -> str:
    parts = [RE_SIZE.sub("", p).strip() for p in (text or "").split(",")]
    return ", ".join(p for p in parts if p)


def resolve_category(category: str):
    """'coffee' -> ('Beverages', 'Coffee'); 'Drinks' -> ('Beverages', ''); 'Thai' -> ('Thai', '')."""
    c = (category or "").strip()
    key = c.lower()
    if key in SUBCATEGORY_ALIASES:
        return "Beverages", SUBCATEGORY_ALIASES[key]
    return CATEGORY_ALIASES.get(key, c), ""


def normalize_category(category: str) -> str:
    return resolve_category(category)[0]


# --------------------------------------------------------------------------
# Connection helpers
# --------------------------------------------------------------------------
def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    """Commit on success, roll back on error, ALWAYS close (sqlite3's own
    `with conn:` never closes the connection)."""
    conn = get_db_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_database() -> None:
    """Create tables (and seed the menu if empty) from init_restaurant_db.sql."""
    if not os.path.exists(SCHEMA_PATH):
        print(f"[db_tools] WARNING: {SCHEMA_PATH} not found - database not initialised.")
        return
    conn = get_db_connection()
    try:
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            conn.executescript(f.read())
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def _slug(text: str) -> str:
    # strip accents first so "Crème" -> "creme" and matches a file called "Creme"
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


_PHOTO_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def _photo_index() -> Dict[str, str]:
    """slug of file name -> file name, for every image in the folders next to this script
    (pasta/, pastries/, beverages/, static/menu/ ...). 'Hot Cafe Latte.png' -> 'hot-cafe-latte'."""
    base = os.path.dirname(os.path.abspath(__file__))
    files: Dict[str, str] = {}
    for root, dirs, names in os.walk(base):
        dirs[:] = [d for d in dirs if not d.startswith((".", "_")) and d not in ("__pycache__", "node_modules", "venv")]
        for n in sorted(names):
            stem, ext = os.path.splitext(n)
            if ext.lower() in _PHOTO_EXT:
                files.setdefault(_slug(stem), n)
    return files


def dish_photos(name: str, category: str = "", subcategory: str = "", files: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Photo URLs for one dish: {'default': ..., 'hot': ..., 'iced': ...} (missing ones are '').
    Looks for  <category>-<dish>, <subcategory>-<dish>, <dish>  and, for drinks, hot-<dish> / iced-<dish>
    (e.g. beverages/Hot Cafe Latte.png and beverages/Iced Cafe Latte.png)."""
    files = _photo_index() if files is None else files
    d = _slug(name)
    url = lambda key: ("/menu-img/" + quote(files[key])) if key in files else ""
    plain = next((url(k) for k in (f"{_slug(category)}-{d}", f"{_slug(subcategory)}-{d}", d) if url(k)), "")
    hot, iced = url(f"hot-{d}"), url(f"iced-{d}")
    return {"default": plain or iced or hot, "hot": hot or plain, "iced": iced or plain}


def _link_dish_photos(conn: sqlite3.Connection) -> None:
    """Fill an EMPTY image_url from photo files in the folders next to this script (pasta/, pastries/, beverages/, static/menu/ ...).
    File name = <category>-<dish>.png/.jpg  e.g. pastries-butter-croissant.png, pasta-carbonara.png
    (a plain <dish>.png also works, and drinks may be 'Hot <dish>.png' / 'Iced <dish>.png').
    Photos you linked yourself are never overwritten."""
    files = _photo_index()
    for row in conn.execute("SELECT id, name, category, subcategory FROM menu_items WHERE COALESCE(image_url, '') = '';").fetchall():
        url = dish_photos(row["name"], row["category"], row["subcategory"], files)["default"]
        if url:
            conn.execute("UPDATE menu_items SET image_url = ? WHERE id = ?;", (url, row["id"]))


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an older restaurant.db up to date (CREATE TABLE IF NOT EXISTS never alters existing tables)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(table_sessions);")}
    if "party_size" not in cols:
        conn.execute("ALTER TABLE table_sessions ADD COLUMN party_size INTEGER NOT NULL DEFAULT 1;")
    mcols = {r["name"] for r in conn.execute("PRAGMA table_info(menu_items);")}
    if "subcategory" not in mcols:
        conn.execute("ALTER TABLE menu_items ADD COLUMN subcategory TEXT NOT NULL DEFAULT '';")
    if "payment_method" not in cols:
        conn.execute("ALTER TABLE table_sessions ADD COLUMN payment_method TEXT NOT NULL DEFAULT '';")
    lcols = {r["name"] for r in conn.execute("PRAGMA table_info(order_line_items);")}
    if "size" not in lcols:
        conn.execute("ALTER TABLE order_line_items ADD COLUMN size TEXT NOT NULL DEFAULT '';")
    if "temp_options" not in mcols:
        conn.execute("ALTER TABLE menu_items ADD COLUMN temp_options TEXT NOT NULL DEFAULT '';")
    # Sub-category from the old category names (matters on a freshly seeded DB) ...
    conn.execute("UPDATE menu_items SET subcategory = 'Coffee' WHERE subcategory = '' AND LOWER(category) = 'coffee';")
    conn.execute("UPDATE menu_items SET subcategory = 'Refreshers' WHERE subcategory = '' AND LOWER(category) = 'refreshers';")
    # Merge drink categories into "Beverages" (idempotent; prices/stock/allergens untouched).
    conn.execute("UPDATE menu_items SET category = 'Beverages' WHERE LOWER(category) IN ('coffee', 'refreshers');")
    # ... and from known drink names (DBs that were already merged). Anything not listed keeps ''
    # and still shows under Beverages > All.
    for sub, names in _SUB_BY_NAME.items():
        marks = ",".join("?" * len(names))
        conn.execute(
            f"UPDATE menu_items SET subcategory = ? WHERE category = 'Beverages' AND subcategory = '' "
            f"AND LOWER(name) IN ({marks});", [sub] + [n.lower() for n in names])
    # New beverages (only inserted when no dish with that name exists, so edits are never overwritten).
    for name, sub, price, stock, allergens in _NEW_BEVERAGES:
        conn.execute(
            "INSERT INTO menu_items (name, category, subcategory, price, stock_qty, allergens) "
            "SELECT ?, 'Beverages', ?, ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM menu_items WHERE LOWER(name) = LOWER(?));",
            (name, sub, price, stock, allergens, name))
    # Water is free of charge (only touches the old default price, so a price you set later is kept).
    conn.execute("UPDATE menu_items SET price = 0 WHERE LOWER(name) = 'water' AND price = 30;")
    # Duplicate of Cafe Latte: remove it (only when no order refers to it).
    conn.execute("DELETE FROM menu_items WHERE LOWER(name) = 'caffe latte' "
                 "AND id NOT IN (SELECT item_id FROM order_line_items);")
    # Merge "Iced Americano" into "Americano" (which already offers Hot / Iced); old order lines move over as "Iced".
    old = conn.execute("SELECT id FROM menu_items WHERE LOWER(name) = 'iced americano';").fetchone()
    keep = conn.execute("SELECT id FROM menu_items WHERE LOWER(name) = 'americano';").fetchone()
    if old and keep:
        conn.execute("UPDATE order_line_items SET item_id = ?, special_instructions = CASE WHEN special_instructions = '' "
                     "THEN 'Iced' ELSE special_instructions END WHERE item_id = ?;", (keep["id"], old["id"]))
        conn.execute("DELETE FROM menu_items WHERE id = ?;", (old["id"],))
    # Fresh Milk was removed from the menu completely (old order lines for it go too, so the delete is allowed).
    conn.execute("DELETE FROM order_line_items WHERE item_id IN (SELECT id FROM menu_items WHERE LOWER(name) = 'fresh milk');")
    conn.execute("DELETE FROM menu_items WHERE LOWER(name) = 'fresh milk';")
    # Strawberry Milk was removed from the menu completely as well.
    conn.execute("DELETE FROM order_line_items WHERE item_id IN (SELECT id FROM menu_items WHERE LOWER(name) = 'strawberry milk');")
    conn.execute("DELETE FROM menu_items WHERE LOWER(name) = 'strawberry milk';")
    _link_dish_photos(conn)
    marks = ",".join("?" * len(_HOT_OR_ICED))
    conn.execute(f"UPDATE menu_items SET temp_options = 'hot,iced' WHERE temp_options = '' "
                 f"AND LOWER(name) IN ({marks});", [n.lower() for n in _HOT_OR_ICED])


# --------------------------------------------------------------------------
# Session helpers
# --------------------------------------------------------------------------
def set_session_status(session_id: str, status: str) -> None:
    with db() as conn:
        conn.execute("UPDATE table_sessions SET status = ? WHERE session_id = ?;", (status, session_id))


def set_table_number(session_id: str, table_no: int) -> None:
    with db() as conn:
        conn.execute("UPDATE table_sessions SET table_no = ? WHERE session_id = ?;", (table_no, session_id))


def set_payment_method(session_id: str, method: str) -> Dict[str, Any]:
    """Save the guest's mode of payment on the session (only while the order is still open)."""
    canonical = resolve_payment_method(method)
    if canonical is None:
        return {"success": False, "message": "Please choose one of: " + ", ".join(PAYMENT_METHODS) + "."}
    with db() as conn:
        locked = _order_is_locked(conn, session_id)
        if locked:
            return {"success": False, "message": locked}
        conn.execute("UPDATE table_sessions SET payment_method = ? WHERE session_id = ?;", (canonical, session_id))
    return {"success": True, "payment_method": canonical, "message": f"Payment: {canonical}."}


def get_payment_method(session_id: str) -> str:
    with db() as conn:
        row = conn.execute("SELECT payment_method FROM table_sessions WHERE session_id = ?;", (session_id,)).fetchone()
    return (row["payment_method"] if row else "") or ""


def _session_status(conn: sqlite3.Connection, session_id: str) -> Optional[str]:
    row = conn.execute("SELECT status FROM table_sessions WHERE session_id = ?;", (session_id,)).fetchone()
    return row["status"] if row else None


def _order_is_locked(conn: sqlite3.Connection, session_id: str) -> Optional[str]:
    """Return a user-facing message if this tab can no longer be edited."""
    status = _session_status(conn, session_id)
    if status == "CONFIRMED":
        return "This order was already sent to the kitchen. Please start a new order to add more."
    if status == "CANCELLED":
        return "This order was cancelled. Please start a new order."
    return None


def _name_pattern(name: str):
    """Regex that finds a menu name inside free text: tolerates plurals ('cheeseburgers'),
    spacing/hyphens ('cheese burger') and ignores case."""
    chars = [re.escape(ch) for ch in re.sub(r"[^a-z0-9]", "", name.lower())]
    return re.compile(r"(?<![a-z0-9])" + r"[\s\-]*".join(chars) + r"(?:e?s)?(?![a-z0-9])", re.I)


def match_menu_items(conn: sqlite3.Connection, text: str):
    """Every menu dish mentioned in `text`, in order: [(row, start, end)]. Longest names win, so
    'kimchi fried rice' is never also counted as 'Kimchi'."""
    rows = conn.execute("SELECT * FROM menu_items;").fetchall()
    cands = []
    for r in rows:
        for m in _name_pattern(r["name"]).finditer(text or ""):
            cands.append((m.end() - m.start(), r, m.start(), m.end()))
    cands.sort(key=lambda c: -c[0])
    taken, hits = [], []
    for _, r, a, b in cands:
        if all(b <= x or a >= y for x, y in taken):
            taken.append((a, b))
            hits.append((r, a, b))
    return sorted(hits, key=lambda h: h[1])


def menu_catalog_text(only_in_stock: bool = False) -> str:
    """The WHOLE menu_items table as compact text for the system prompt, grouped by category:
    name | price PHP | stock | allergens | hot/iced. Read fresh from the DB on every turn,
    so stock and price edits show up immediately."""
    with db() as conn:
        rows = conn.execute("SELECT name, category, subcategory, price, stock_qty, allergens, temp_options "
                            "FROM menu_items ORDER BY category, subcategory, name;").fetchall()
    by: Dict[str, List[str]] = {}
    for r in rows:
        if only_in_stock and r["stock_qty"] <= 0:
            continue
        label = r["category"] + (f"/{r['subcategory']}" if r["subcategory"] else "")
        stock = "SOLD OUT" if r["stock_qty"] <= 0 else f"{r['stock_qty']} left"
        extra = " | hot or iced" if len([t for t in (r["temp_options"] or "").split(",") if t]) > 1 else ""
        by.setdefault(label, []).append(
            f"{r['name']} | {r['price']:.0f} PHP | {stock} | allergens: {r['allergens'] or 'none'}{extra}")
    return "\n".join(f"[{k}]\n" + "\n".join(v) for k, v in by.items())


def _find_item(conn: sqlite3.Connection, name: str):
    """Exact (case-insensitive) match first, then a unique partial match
    ('dim sum' -> 'Dim Sum Platter'). Returns (row | None, suggestions)."""
    name = (name or "").strip()
    if not name:
        return None, []
    row = conn.execute("SELECT * FROM menu_items WHERE LOWER(name) = LOWER(?);", (name,)).fetchone()
    if row:
        return row, []
    like = conn.execute(
        "SELECT * FROM menu_items WHERE LOWER(name) LIKE '%' || LOWER(?) || '%';", (name,)
    ).fetchall()
    if len(like) == 1:
        return like[0], []
    # plurals / spacing: 'cheeseburgers', 'Cheese Burger', 'two pretzels'
    hits = match_menu_items(conn, name)
    if len(hits) == 1:
        return hits[0][0], []
    sugg = [r["name"] for r in like[:4]] or [h[0]["name"] for h in hits[:4]]
    if not sugg:
        import difflib
        names = [r["name"] for r in conn.execute("SELECT name FROM menu_items;")]
        sugg = difflib.get_close_matches(name, names, n=3, cutoff=0.6)
    return None, sugg


def _not_found(item_name: str, suggestions: List[str]) -> Dict[str, Any]:
    msg = f"'{item_name}' is not on our menu."
    if suggestions:
        msg += " Did you mean: " + ", ".join(suggestions) + "?"
    return {"success": False, "message": msg}


# --------------------------------------------------------------------------
# Tool 1: get_menu_by_category
# --------------------------------------------------------------------------
def get_menu_by_category(category: str, subcategory: str = "") -> Dict[str, Any]:
    """category may also be a sub-category word ('coffee', 'non-coffee', 'refreshers'),
    or category='Beverages' + subcategory='Coffee'."""
    cat, sub = resolve_category(category)
    if subcategory:
        sub = SUBCATEGORY_ALIASES.get(subcategory.strip().lower(), subcategory.strip())
    sql = ("SELECT id, name, category, subcategory, temp_options, price, stock_qty, allergens, image_url "
           "FROM menu_items WHERE LOWER(category) = LOWER(?)")
    params: List[Any] = [cat]
    if sub:
        sql += " AND LOWER(subcategory) = LOWER(?)"
        params.append(sub)
    sql += (" ORDER BY CASE subcategory WHEN 'Coffee' THEN 0 WHEN 'Non-Coffee' THEN 1 "
            "WHEN 'Refreshers' THEN 2 ELSE 3 END, name;")
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
        if not rows:
            cats = [r["category"] for r in conn.execute("SELECT DISTINCT category FROM menu_items;")]
            return {
                "success": False,
                "message": f"No items found for '{category}'. Available categories: {', '.join(cats)}"
                           f" (Beverages has: {', '.join(BEVERAGE_SUBCATEGORIES)})",
                "items": [],
            }
        items = []
        files = _photo_index()
        for r in rows:
            d = dict(r)
            ph = dish_photos(d["name"], d["category"], d["subcategory"], files)
            if not d["image_url"] and ph["default"]:
                d["image_url"] = ph["default"]          # photo added after the DB was created
            if d["category"] == "Beverages" and d["name"].lower() not in NO_SIZE_DRINKS:
                d["size_menu"], d["size_deltas"], d["default_size"] = size_menu(r), SIZE_PRICE_DELTA, DEFAULT_SIZE
                d["image_hot"], d["image_iced"] = ph["hot"], ph["iced"]
            items.append(d)
        return {"success": True, "category": rows[0]["category"], "subcategory": sub,
                "subcategories": BEVERAGE_SUBCATEGORIES if rows[0]["category"] == "Beverages" else [],
                "items": items}


# --------------------------------------------------------------------------
# Tool 2: check_item_allergen
# --------------------------------------------------------------------------
def check_item_allergen(item_id: Optional[int] = None, allergen: str = "",
                        item_name: Optional[str] = None) -> Dict[str, Any]:
    """Does a dish contain an allergen? Accepts an id (lab spec) or a name
    (what the LLM actually knows)."""
    allergen = (allergen or "").strip().lower()
    with db() as conn:
        item = None
        suggestions: List[str] = []
        if item_id not in (None, ""):
            try:
                item = conn.execute("SELECT * FROM menu_items WHERE id = ?;", (int(item_id),)).fetchone()
            except (TypeError, ValueError):
                item = None
        if item is None and item_name:
            item, suggestions = _find_item(conn, item_name)
        if item is None:
            return _not_found(item_name or str(item_id), suggestions)

        listed = [a.strip().lower() for a in (item["allergens"] or "").split(",") if a.strip()]
        if not allergen:
            return {"success": True, "item": item["name"], "allergens": listed,
                    "message": f"{item['name']} contains: {', '.join(listed) if listed else 'no listed allergens'}."}
        wanted = _allergen_labels(allergen)
        matched = [a for a in listed if a in wanted or any(w in a or a in w for w in wanted)]
        hit = bool(matched)
        return {
            "success": True, "item": item["name"], "allergen": allergen, "contains_allergen": hit,
            "allergens": listed,
            "message": f"{item['name']} {'CONTAINS' if hit else 'does not list'} {allergen}"
                       + (f" (listed as: {', '.join(matched)})." if hit and matched != [allergen] else ".")
                       + (f" Full allergen list: {', '.join(listed)}." if listed else ""),
        }


# --------------------------------------------------------------------------
# Tool 3: add_item_to_order
# --------------------------------------------------------------------------
def add_item_to_order(session_id: str, item_name: str, qty: int = 1, instructions: str = "",
                      size: str = "") -> Dict[str, Any]:
    qty = _to_qty(qty)
    if qty < 1:
        return {"success": False, "message": "Quantity must be at least 1."}
    instructions = (instructions or "").strip()
    size = (size or "").strip().title()
    if not size:                      # the model sometimes puts the size in the note
        m = RE_SIZE.search(instructions)
        if m:
            size = m.group(1).title()
    if size:
        instructions = _strip_sizes(instructions)
    try:
        with db() as conn:
            locked = _order_is_locked(conn, session_id)
            if locked:
                return {"success": False, "message": locked}
            item, suggestions = _find_item(conn, item_name)
            if not item:
                return _not_found(item_name, suggestions)
            if item["category"] == "Beverages" and item["name"].lower() not in NO_SIZE_DRINKS:
                # Drinks need a temperature (when they come hot or iced) and a size before they go in the cart.
                temps = _temps(item)
                tm = RE_TEMP.search(instructions)
                temp = None if (len(temps) > 1 and not tm) else ("hot" if tm and tm.group(1).lower() == "hot" else "iced")
                name = item["name"]
                if temp is None and not size:
                    return {"success": False, "needs_choice": "temperature,size",
                            "message": f"Would you like the {name} hot or iced, and what size: Short, Tall, Grande or Venti?"}
                if temp is None:
                    return {"success": False, "needs_choice": "temperature",
                            "message": f"Would you like the {name} hot or iced?"}
                valid = HOT_SIZES if temp == "hot" else COLD_SIZES
                if not size:
                    return {"success": False, "needs_choice": "size",
                            "message": f"What size would you like for your {temp} {name}: {', '.join(valid[:-1])} or {valid[-1]}?"}
                if size not in valid:
                    return {"success": False, "needs_choice": "size",
                            "message": (f"Hot drinks come in {' or '.join(HOT_SIZES)}." if temp == "hot"
                                        else f"Iced drinks come in {', '.join(COLD_SIZES[:-1])} or {COLD_SIZES[-1]}.")
                                       + " Which size would you like?"}
            else:
                size = ""

            in_cart = conn.execute(
                "SELECT COALESCE(SUM(quantity), 0) AS n FROM order_line_items WHERE session_id = ? AND item_id = ?;",
                (session_id, item["id"]),
            ).fetchone()["n"]
            if item["stock_qty"] <= 0:
                return {"success": False, "message": f"Sorry, {item['name']} is sold out."}
            if in_cart + qty > item["stock_qty"]:
                left = item["stock_qty"] - in_cart
                return {"success": False,
                        "message": f"Only {item['stock_qty']} of {item['name']} remaining"
                                   + (f" ({left} more can be added)." if left > 0 else " and they are all in your cart.")}

            existing = conn.execute(
                "SELECT id FROM order_line_items WHERE session_id = ? AND item_id = ? AND special_instructions = ? AND size = ?;",
                (session_id, item["id"], instructions, size),
            ).fetchone()
            if existing:
                conn.execute("UPDATE order_line_items SET quantity = quantity + ? WHERE id = ?;", (qty, existing["id"]))
            else:
                conn.execute(
                    "INSERT INTO order_line_items (session_id, item_id, quantity, special_instructions, size) VALUES (?, ?, ?, ?, ?);",
                    (session_id, item["id"], qty, instructions, size),
                )
            label = f"{item['name']} ({size})" if size else item["name"]
            return {"success": True, "message": f"Added {qty}x {label} to the cart.", "item": dict(item)}
    except sqlite3.Error as e:
        return {"success": False, "message": f"Database error: {e}"}


# --------------------------------------------------------------------------
# Tool 4: remove_item_from_order  (name based - for the LLM)
# --------------------------------------------------------------------------
def remove_item_from_order(session_id: str, item_name: str, qty: int = 1) -> Dict[str, Any]:
    qty = max(1, _to_qty(qty))
    with db() as conn:
        locked = _order_is_locked(conn, session_id)
        if locked:
            return {"success": False, "message": locked}
        item, suggestions = _find_item(conn, item_name)
        if not item:
            return _not_found(item_name, suggestions)
        lines = conn.execute(
            "SELECT id, quantity FROM order_line_items WHERE session_id = ? AND item_id = ? ORDER BY id DESC;",
            (session_id, item["id"]),
        ).fetchall()
        if not lines:
            return {"success": False, "message": f"{item['name']} is not in the cart, so there is nothing to remove."}
        remaining = qty
        removed = 0
        for line in lines:
            if remaining <= 0:
                break
            take = min(remaining, line["quantity"])
            if take == line["quantity"]:
                conn.execute("DELETE FROM order_line_items WHERE id = ?;", (line["id"],))
            else:
                conn.execute("UPDATE order_line_items SET quantity = quantity - ? WHERE id = ?;", (take, line["id"]))
            remaining -= take
            removed += take
        return {"success": True, "message": f"Removed {removed}x {item['name']} from the cart."}


# --------------------------------------------------------------------------
# Tool 5: update_item_instructions
# --------------------------------------------------------------------------
def update_item_instructions(session_id: str, item_name: str, instructions: str) -> Dict[str, Any]:
    """Change the prep note on a dish already in the cart ('no onions')."""
    instructions = (instructions or "").strip()
    with db() as conn:
        locked = _order_is_locked(conn, session_id)
        if locked:
            return {"success": False, "message": locked}
        item, suggestions = _find_item(conn, item_name)
        if not item:
            return _not_found(item_name, suggestions)
        line = conn.execute(
            "SELECT id FROM order_line_items WHERE session_id = ? AND item_id = ? ORDER BY id DESC LIMIT 1;",
            (session_id, item["id"]),
        ).fetchone()
        if not line:
            return {"success": False, "message": f"{item['name']} is not in the cart yet. Add it first."}
        return _set_line_note(conn, session_id, line["id"], instructions, item["name"])


def _set_line_note(conn, session_id, line_id, instructions, display_name=None) -> Dict[str, Any]:
    line = conn.execute(
        "SELECT id, item_id, quantity, size FROM order_line_items WHERE id = ? AND session_id = ?;", (line_id, session_id)
    ).fetchone()
    if not line:
        return {"success": False, "message": "That cart line no longer exists."}
    # merge into an identical line if one exists, so the cart never shows duplicates
    twin = conn.execute(
        "SELECT id FROM order_line_items WHERE session_id = ? AND item_id = ? AND special_instructions = ? AND size = ? AND id != ?;",
        (session_id, line["item_id"], instructions, line["size"], line_id),
    ).fetchone()
    if twin:
        conn.execute("UPDATE order_line_items SET quantity = quantity + ? WHERE id = ?;", (line["quantity"], twin["id"]))
        conn.execute("DELETE FROM order_line_items WHERE id = ?;", (line_id,))
    else:
        conn.execute("UPDATE order_line_items SET special_instructions = ? WHERE id = ?;", (instructions, line_id))
    label = display_name or "the item"
    return {"success": True, "message": f"Note for {label}: {instructions}" if instructions else f"Cleared the note on {label}."}


# --------------------------------------------------------------------------
# Manual (+ / - / edit / delete) overrides used by the kiosk buttons.
# These bypass the LLM so the cart responds instantly.
# --------------------------------------------------------------------------
def adjust_line_quantity(session_id: str, line_id: int, delta: int) -> Dict[str, Any]:
    with db() as conn:
        locked = _order_is_locked(conn, session_id)
        if locked:
            return {"success": False, "message": locked}
        line = conn.execute(
            "SELECT oli.id, oli.quantity, m.name, m.stock_qty, "
            "(SELECT COALESCE(SUM(quantity),0) FROM order_line_items WHERE session_id = oli.session_id AND item_id = oli.item_id) AS total_in_cart "
            "FROM order_line_items oli JOIN menu_items m ON m.id = oli.item_id WHERE oli.id = ? AND oli.session_id = ?;",
            (line_id, session_id),
        ).fetchone()
        if not line:
            return {"success": False, "message": "That cart line no longer exists."}
        if delta > 0:
            if line["total_in_cart"] + delta > line["stock_qty"]:
                return {"success": False, "message": f"Only {line['stock_qty']} of {line['name']} available."}
            conn.execute("UPDATE order_line_items SET quantity = quantity + ? WHERE id = ?;", (delta, line_id))
        else:
            new_qty = line["quantity"] + delta
            if new_qty <= 0:
                conn.execute("DELETE FROM order_line_items WHERE id = ?;", (line_id,))
            else:
                conn.execute("UPDATE order_line_items SET quantity = ? WHERE id = ?;", (new_qty, line_id))
        return {"success": True}


def remove_line(session_id: str, line_id: int) -> Dict[str, Any]:
    with db() as conn:
        locked = _order_is_locked(conn, session_id)
        if locked:
            return {"success": False, "message": locked}
        cur = conn.execute("DELETE FROM order_line_items WHERE id = ? AND session_id = ?;", (line_id, session_id))
        return {"success": cur.rowcount > 0}


def set_line_note(session_id: str, line_id: int, note: str) -> Dict[str, Any]:
    with db() as conn:
        locked = _order_is_locked(conn, session_id)
        if locked:
            return {"success": False, "message": locked}
        return _set_line_note(conn, session_id, line_id, (note or "").strip()[:120])


def decrement_or_remove_item(session_id: str, item_id: int) -> Dict[str, Any]:
    """Kept for backwards compatibility with the first prototype."""
    with db() as conn:
        row = conn.execute(
            "SELECT id, quantity FROM order_line_items WHERE session_id = ? AND item_id = ? ORDER BY id DESC LIMIT 1;",
            (session_id, item_id),
        ).fetchone()
    return adjust_line_quantity(session_id, row["id"], -1) if row else {"success": False}


def clear_order(session_id: str) -> Dict[str, Any]:
    with db() as conn:
        locked = _order_is_locked(conn, session_id)
        if locked:
            return {"success": False, "message": locked}
        conn.execute("DELETE FROM order_line_items WHERE session_id = ?;", (session_id,))
        return {"success": True, "message": "Cart cleared."}


# --------------------------------------------------------------------------
# Tool 6: view_current_order
# --------------------------------------------------------------------------
def view_current_order(session_id: str) -> Dict[str, Any]:
    with db() as conn:
        items = [dict(r) for r in conn.execute(
            """
            SELECT oli.id AS line_id, m.id AS item_id, m.name, m.image_url, m.category, m.allergens,
                   oli.quantity, oli.special_instructions, oli.size, m.price AS base_price
            FROM order_line_items oli JOIN menu_items m ON oli.item_id = m.id
            WHERE oli.session_id = ? ORDER BY oli.id;
            """,
            (session_id,),
        ).fetchall()]
    files = _photo_index()
    for i in items:   # drink size adjusts the unit price; display_name carries the size
        if i["category"] == "Beverages":
            ph = dish_photos(i["name"], i["category"], "", files)
            m = RE_TEMP.search(i["special_instructions"] or "")
            hot = bool(m) and m.group(1).lower() == "hot"
            i["image_url"] = (ph["hot"] if hot else ph["iced"]) or i["image_url"] or ph["default"]
        i["price"] = round(i["base_price"] + SIZE_PRICE_DELTA.get(i["size"], 0), 2)
        i["line_total"] = round(i["quantity"] * i["price"], 2)
        i["display_name"] = f"{i['name']} ({i['size']})" if i["size"] else i["name"]
    subtotal = round(sum(i["line_total"] for i in items), 2)
    fee = round(subtotal * SERVICE_FEE_RATE, 2)
    pay = get_payment_method(session_id)
    return {
        "session_id": session_id,
        "payment_method": pay,
        "payment_methods": PAYMENT_METHODS,
        "payment_note": PAYMENT_NOTES.get(pay, ""),
        "items": items,
        "item_count": sum(i["quantity"] for i in items),
        "subtotal": subtotal,
        "service_fee_10_pct": fee,
        "grand_total": round(subtotal + fee, 2),
    }


# --------------------------------------------------------------------------
# STATE_CONFIRMED: commit the order + deduct stock atomically
# --------------------------------------------------------------------------
def confirm_and_deduct_inventory(session_id: str) -> Dict[str, Any]:
    conn = get_db_connection()
    conn.isolation_level = None  # manual transaction control
    try:
        conn.execute("BEGIN IMMEDIATE;")
        status = _session_status(conn, session_id)
        if status == "CONFIRMED":
            conn.execute("ROLLBACK;")
            return {"success": False, "message": "This order was already sent to the kitchen."}
        pay = conn.execute("SELECT payment_method FROM table_sessions WHERE session_id = ?;", (session_id,)).fetchone()
        if not pay or not pay["payment_method"]:
            conn.execute("ROLLBACK;")
            return {"success": False, "needs_payment": True,
                    "message": "Please choose a mode of payment first: " + ", ".join(PAYMENT_METHODS) + "."}
        lines = conn.execute(
            "SELECT item_id, SUM(quantity) AS qty FROM order_line_items WHERE session_id = ? GROUP BY item_id;",
            (session_id,),
        ).fetchall()
        if not lines:
            conn.execute("ROLLBACK;")
            return {"success": False, "message": "Your cart is empty - add something before confirming."}
        for line in lines:
            stock = conn.execute("SELECT stock_qty, name FROM menu_items WHERE id = ?;", (line["item_id"],)).fetchone()
            if stock["stock_qty"] < line["qty"]:
                conn.execute("ROLLBACK;")
                return {"success": False, "sold_out_item": stock["name"],
                        "message": f"{stock['name']} just sold out ({stock['stock_qty']} left). Please adjust your cart."}
            conn.execute("UPDATE menu_items SET stock_qty = stock_qty - ? WHERE id = ?;", (line["qty"], line["item_id"]))
        conn.execute("UPDATE table_sessions SET status = 'CONFIRMED' WHERE session_id = ?;", (session_id,))
        conn.execute("COMMIT;")
        return {"success": True, "message": "Order committed to kitchen."}
    except sqlite3.Error as e:
        try:
            conn.execute("ROLLBACK;")
        except sqlite3.Error:
            pass
        return {"success": False, "message": f"Database error: {e}"}
    finally:
        conn.close()


# ==================== Kiosk-optimised Ollama tool schemas ====================
_CATEGORY_HELP = ("The exact category name. Must be exactly one of: " + ", ".join(CATEGORIES) + ". "
                  "Drinks live under Beverages, which has sub-categories: " + ", ".join(BEVERAGE_SUBCATEGORIES) + ".")

OLLAMA_TOOLS = [
    {"type": "function", "function": {
        "name": "get_menu_by_category",
        "description": "Fetch the dishes in a category and show their pictures and prices on the kiosk screen.",
        "parameters": {"type": "object", "properties": {
            "category": {"type": "string", "description": _CATEGORY_HELP},
            "subcategory": {"type": "string", "enum": BEVERAGE_SUBCATEGORIES,
                            "description": "Only for category 'Beverages': Coffee, Non-Coffee (milk and other "
                                           "non-coffee drinks) or Refreshers. Omit to show all beverages."}},
            "required": ["category"]}}},
    {"type": "function", "function": {
        "name": "check_item_allergen",
        "description": "Check whether a menu item contains an allergen (e.g. peanut, dairy, gluten, shellfish). "
                       "Always use this instead of guessing about ingredients.",
        "parameters": {"type": "object", "properties": {
            "item_name": {"type": "string", "description": "Name of the dish, e.g. 'Authentic Pad Thai'"},
            "allergen": {"type": "string", "description": "Allergen to check, e.g. 'peanut'. Leave empty to list all."}},
            "required": ["item_name"]}}},
    {"type": "function", "function": {
        "name": "add_item_to_order",
        "description": "Add a dish to the customer's tab, with optional preparation notes. ALWAYS call this when the guest orders a dish; the tool itself checks the menu, so never decide a dish is unavailable yourself. Use the singular menu name ('cheeseburgers' -> 'Cheeseburger'). For drinks that come hot "
                       "or iced (Coffee and Non-Coffee), put 'Hot' or 'Iced' in instructions.",
        "parameters": {"type": "object", "properties": {
            "item_name": {"type": "string", "description": "Name of the dish (e.g., 'Sweet and Sour Pork')"},
            "qty": {"type": "integer", "description": "Quantity to add", "default": 1},
            "instructions": {"type": "string", "description": "Special requests the guest EXPLICITLY made for THIS dish in their latest message, "
                                                        "in their own words. Leave empty (\"\") if they made none. Never copy a "
                                                        "note from another dish or from the cart."},
            "size": {"type": "string", "enum": SIZES,
                     "description": "Drinks only. Hot drinks: Short or Tall. Iced and blended drinks: Tall, Grande or Venti."}},
            "required": ["item_name"]}}},
    {"type": "function", "function": {
        "name": "remove_item_from_order",
        "description": "Remove a dish (or reduce its quantity) on the customer's tab.",
        "parameters": {"type": "object", "properties": {
            "item_name": {"type": "string", "description": "Name of the dish to remove"},
            "qty": {"type": "integer", "description": "How many to remove", "default": 1}},
            "required": ["item_name"]}}},
    {"type": "function", "function": {
        "name": "update_item_instructions",
        "description": "Change the preparation note on a dish that is already in the cart.",
        "parameters": {"type": "object", "properties": {
            "item_name": {"type": "string", "description": "Dish already in the cart"},
            "instructions": {"type": "string", "description": "The new full note, exactly what the guest just asked for on this dish."}},
            "required": ["item_name", "instructions"]}}},
    {"type": "function", "function": {
        "name": "view_current_order",
        "description": "Retrieve the active order items, subtotal and grand total for checkout.",
        "parameters": {"type": "object", "properties": {}}}},
]


def execute_tool_call(tool_name: str, args: Dict[str, Any], session_id: str) -> Dict[str, Any]:
    args = args or {}
    if tool_name == "get_menu_by_category":
        return get_menu_by_category(args.get("category", ""), args.get("subcategory", "") or "")
    if tool_name == "check_item_allergen":
        return check_item_allergen(args.get("item_id"), args.get("allergen", ""), args.get("item_name"))
    if tool_name == "add_item_to_order":
        return add_item_to_order(session_id, args.get("item_name", ""), args.get("qty", 1), args.get("instructions", ""),
                                 args.get("size", ""))
    if tool_name == "remove_item_from_order":
        return remove_item_from_order(session_id, args.get("item_name", ""), args.get("qty", 1))
    if tool_name == "update_item_instructions":
        return update_item_instructions(session_id, args.get("item_name", ""), args.get("instructions", ""))
    if tool_name == "view_current_order":
        return view_current_order(session_id)
    return {"success": False, "message": f"Unknown tool: {tool_name}"}