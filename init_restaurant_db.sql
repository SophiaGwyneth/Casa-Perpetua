-- ============================================================
-- Casa Perpetua - restaurant.db schema + seed data
-- Safe to run on every start: tables use IF NOT EXISTS and the
-- menu is only seeded when menu_items is completely empty.
-- Put your own photo links in menu_items.image_url; items without
-- one get an illustrated placeholder tile in the kiosk.
-- ============================================================

CREATE TABLE IF NOT EXISTS menu_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    category   TEXT NOT NULL,
    price      REAL NOT NULL CHECK (price >= 0),
    stock_qty  INTEGER NOT NULL DEFAULT 0 CHECK (stock_qty >= 0),
    allergens  TEXT NOT NULL DEFAULT '',      -- comma separated, lowercase
    image_url  TEXT,
    subcategory TEXT NOT NULL DEFAULT ''      -- only used by Beverages: Coffee / Non-Coffee / Refreshers (filled by db_tools._migrate)
    ,temp_options TEXT NOT NULL DEFAULT ''    -- 'hot,iced' when the guest can choose; '' otherwise (filled by db_tools._migrate)
);

CREATE TABLE IF NOT EXISTS table_sessions (
    session_id TEXT PRIMARY KEY,
    table_no   INTEGER NOT NULL,
    party_size INTEGER NOT NULL DEFAULT 1,    -- answer to "Table for how many?" on the welcome screen
    status     TEXT NOT NULL DEFAULT 'GREETING',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    payment_method TEXT NOT NULL DEFAULT ''   -- mode of payment picked at REVIEW: Cash / GCash / Maya / Credit/Debit Card (added to older DBs by db_tools._migrate)
);

CREATE TABLE IF NOT EXISTS order_line_items (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id           TEXT NOT NULL REFERENCES table_sessions(session_id),
    item_id              INTEGER NOT NULL REFERENCES menu_items(id),
    quantity             INTEGER NOT NULL CHECK (quantity > 0),
    special_instructions TEXT NOT NULL DEFAULT '',
    size                 TEXT NOT NULL DEFAULT ''   -- drinks only: Short / Tall / Grande / Venti
);

CREATE INDEX IF NOT EXISTS idx_menu_category ON menu_items(LOWER(category));
CREATE INDEX IF NOT EXISTS idx_lines_session ON order_line_items(session_id);

WITH seed(name, category, price, stock_qty, allergens) AS (VALUES
 -- Pasta
 ('Spaghetti Bolognese',      'Pasta',      220, 25, 'gluten,egg'),
 ('Carbonara',                'Pasta',      240, 20, 'gluten,egg,dairy'),
 ('Creamy Pesto Penne',       'Pasta',      230, 18, 'gluten,dairy,tree nut'),
 ('Aglio e Olio',             'Pasta',      200, 22, 'gluten'),
 -- Pastries
 ('Butter Croissant',         'Pastries',    85, 30, 'gluten,dairy,egg'),
 ('Ensaymada',                'Pastries',    90, 24, 'gluten,dairy,egg'),
 ('Pain au Chocolat',         'Pastries',   110, 20, 'gluten,dairy,egg'),
 ('Cheese Danish',            'Pastries',   120, 16, 'gluten,dairy,egg'),
 -- Coffee
 ('Cafe Latte',               'Coffee',     130, 40, 'dairy'),
 ('Americano',                'Coffee',     100, 40, ''),
 ('Spanish Latte',            'Coffee',     150, 35, 'dairy'),
 ('Cappuccino',               'Coffee',     130, 35, 'dairy'),
 -- Desserts
 ('Leche Flan',               'Desserts',   120, 20, 'egg,dairy'),
 ('Tiramisu',                 'Desserts',   180, 14, 'gluten,egg,dairy'),
 ('Chocolate Lava Cake',      'Desserts',   190, 12, 'gluten,egg,dairy'),
 ('Halo-Halo',                'Desserts',   140, 25, 'dairy'),
 -- Snacks
 ('Nachos Supreme',           'Snacks',     190, 20, 'dairy'),
 ('Truffle Fries',            'Snacks',     150, 30, 'dairy'),
 ('Onion Rings',              'Snacks',     130, 26, 'gluten'),
 ('Calamari Fritti',          'Snacks',     210, 15, 'gluten,shellfish'),
 -- Dairy
 ('Fresh Milk',               'Dairy',       70, 30, 'dairy'),
 ('Strawberry Milk',          'Dairy',       90, 28, 'dairy'),
 ('Yogurt Parfait',           'Dairy',      120, 18, 'dairy,tree nut'),
 ('Cheese Platter',           'Dairy',      280, 10, 'dairy'),
 -- Refreshers
 ('Iced Tea',                 'Refreshers',  50, 60, ''),
 ('Mango Smoothie',           'Refreshers',  90, 40, ''),
 ('Lemonade',                 'Refreshers',  70, 45, ''),
 ('Calamansi Juice',          'Refreshers',  65, 45, ''),
 -- Burgers
 ('Cheeseburger',             'Burgers',    180, 30, 'gluten,dairy,egg'),
 ('Bacon Burger',             'Burgers',    220, 24, 'gluten,dairy,egg'),
 ('Mushroom Swiss Burger',    'Burgers',    210, 20, 'gluten,dairy,egg'),
 ('Veggie Burger',            'Burgers',    190, 18, 'gluten,soy'),
 -- Chicken
 ('Crispy Fried Chicken',     'Chicken',    210, 25, 'gluten'),
 ('Buffalo Wings',            'Chicken',    230, 22, 'dairy'),
 ('Chicken Inasal',           'Chicken',    190, 24, ''),
 ('Honey Garlic Chicken',     'Chicken',    220, 20, 'soy'),
 -- Filipino
 ('Chicken Adobo',            'Filipino',   190, 28, 'soy'),
 ('Sinigang na Baboy',        'Filipino',   220, 22, ''),
 ('Pork Sisig',               'Filipino',   240, 20, 'egg'),
 ('Kare-Kare',                'Filipino',   280, 14, 'peanut,shellfish'),
 -- Italian
 ('Margherita Pizza',         'Italian',    320, 18, 'gluten,dairy'),
 ('Lasagna',                  'Italian',    290, 16, 'gluten,dairy,egg'),
 ('Risotto ai Funghi',        'Italian',    270, 15, 'dairy'),
 ('Bruschetta',               'Italian',    160, 24, 'gluten'),
 -- Japanese
 ('Salmon Sushi Set',         'Japanese',   340, 14, 'fish,soy,gluten'),
 ('Chicken Katsu Curry',      'Japanese',   250, 20, 'gluten,egg'),
 ('Beef Gyudon',              'Japanese',   230, 20, 'soy'),
 ('Tempura Udon',             'Japanese',   240, 18, 'gluten,shellfish'),
 -- Korean
 ('Bibimbap',                 'Korean',     250, 20, 'egg,soy,sesame'),
 ('Korean Fried Chicken',     'Korean',     260, 20, 'gluten,soy,sesame'),
 ('Beef Bulgogi',             'Korean',     290, 16, 'soy,sesame'),
 ('Kimchi Jjigae',            'Korean',     230, 18, 'soy,fish'),
 -- Spanish
 ('Paella Valenciana',        'Spanish',    320, 12, 'shellfish'),
 ('Gambas al Ajillo',         'Spanish',    280, 14, 'shellfish'),
 ('Chorizo Tapas',            'Spanish',    190, 20, ''),
 ('Patatas Bravas',           'Spanish',    150, 24, 'egg'),
 -- Thai
 ('Authentic Pad Thai',       'Thai',       260, 18, 'peanut,shellfish,egg,soy'),
 ('Green Curry',              'Thai',       240, 18, 'fish'),
 ('Tom Yum Soup',             'Thai',       230, 16, 'shellfish,fish'),
 ('Mango Sticky Rice',        'Thai',       160, 20, ''),
 -- Chinese
 ('Sweet and Sour Pork',      'Chinese',    240, 22, 'gluten,soy'),
 ('Dim Sum Platter',          'Chinese',    280, 15, 'gluten,shellfish,soy'),
 ('Yangchow Fried Rice',      'Chinese',    200, 26, 'egg,soy'),
 ('Beef Broccoli',            'Chinese',    250, 18, 'soy')
)
INSERT INTO menu_items (name, category, price, stock_qty, allergens)
SELECT name, category, price, stock_qty, allergens FROM seed
WHERE NOT EXISTS (SELECT 1 FROM menu_items);

-- ------------------------------------------------------------
-- Menu additions (safe to run on every start: an item is only
-- inserted when no dish with that name exists yet, so existing
-- prices / stock are never overwritten).
-- ------------------------------------------------------------
WITH extra(name, category, price, stock_qty, allergens) AS (VALUES
 -- Pastries
 ('Pretzel',                  'Pastries',    95, 20, 'gluten,dairy'),
 -- Chinese
 ('Xiao Long Bao',            'Chinese',    220, 18, 'gluten,soy'),
 -- Korean
 ('Korean Ramen',             'Korean',     200, 22, 'gluten,egg,soy'),
 ('Kimchi Fried Rice',        'Korean',     220, 20, 'egg,soy,sesame,fish'),
 -- Korean add-ons
 ('Korean BBQ',               'Korean',     280, 14, 'soy,sesame'),
 ('Kimchi',                   'Korean',      60, 40, ''),
 ('Steamed Rice',             'Korean',      40, 60, ''),
 ('Fried Egg',                'Korean',      30, 50, 'egg')
)
INSERT INTO menu_items (name, category, price, stock_qty, allergens)
SELECT name, category, price, stock_qty, allergens FROM extra
WHERE NOT EXISTS (SELECT 1 FROM menu_items m WHERE LOWER(m.name) = LOWER(extra.name));