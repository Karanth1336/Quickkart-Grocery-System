import os, re, sqlite3, traceback, json, random, time
import smtplib, urllib.parse, urllib.request as _ureq
import paypalrestsdk
from werkzeug.utils import secure_filename
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from collections import Counter, defaultdict
from functools import wraps
from flask import (
    Flask, render_template_string, request,
    redirect, url_for, session, flash, g, jsonify
)
from werkzeug.security import generate_password_hash, check_password_hash
OPENROUTER_API_KEY = "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"
# ── Notification config (override via environment variables) ──────────────────
SMTP_HOST  = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT  = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER  = os.environ.get("SMTP_USER", "")
SMTP_PASS  = os.environ.get("SMTP_PASS", "")
SMTP_FROM  = os.environ.get("SMTP_FROM", SMTP_USER)
TWILIO_SID   = os.environ.get("TWILIO_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_TOKEN", "")
TWILIO_FROM  = os.environ.get("TWILIO_FROM", "")
FAST2SMS_KEY = os.environ.get("FAST2SMS_KEY", "")

# ── PayPal Config ─────────────────────────────────────────────────────────────
PAYPAL_CLIENT_ID     = os.environ.get("PAYPAL_CLIENT_ID", "YOUR_PAYPAL_CLIENT_ID_HERE")
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET", "YOUR_PAYPAL_CLIENT_SECRET_HERE")
PAYPAL_MODE          = os.environ.get("PAYPAL_MODE", "sandbox")  # change to "live" for production

paypalrestsdk.configure({
    "mode":          PAYPAL_MODE,
    "client_id":     PAYPAL_CLIENT_ID,
    "client_secret": PAYPAL_CLIENT_SECRET,
})

_OTP_STORE = {}  # {key: {otp, expires, payload}}

# ── Notification helpers ──────────────────────────────────────────────────────
def _send_email(to_addr, subject, html_body):
    """Send an HTML email. Silently fails when SMTP is not configured."""
    if not SMTP_USER or not SMTP_PASS:
        print(f"[EMAIL] (no SMTP configured) To={to_addr} | {subject}")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"QuickKart <{SMTP_FROM}>"
        msg["To"]      = to_addr
        msg.attach(MIMEText(html_body, "html"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as s:
            s.ehlo(); s.starttls(); s.ehlo()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_FROM, [to_addr], msg.as_string())
        return True
    except Exception as e:
        print(f"[EMAIL ERROR] {e}")
        return False


def _send_sms(mobile, text):
    """Send SMS via Twilio or Fast2SMS. Silently fails when not configured."""
    mobile = re.sub(r"\D", "", mobile or "")
    if not mobile:
        return False

    # Try Twilio
    if TWILIO_SID and TWILIO_TOKEN and TWILIO_FROM:
        try:
            import base64
            auth = base64.b64encode(f"{TWILIO_SID}:{TWILIO_TOKEN}".encode()).decode()
            to_num = f"+91{mobile}" if not mobile.startswith("+") else mobile
            data = urllib.parse.urlencode({
                "From": TWILIO_FROM,
                "To":   to_num,
                "Body": text
            }).encode()
            req = _ureq.Request(
                f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
                data=data,
                headers={"Authorization": f"Basic {auth}"}
            )
            _ureq.urlopen(req, timeout=10)
            return True
        except Exception as e:
            print(f"[TWILIO ERROR] {e}")

    # Fast2SMS (Indian fallback)
    if FAST2SMS_KEY:
        try:
            qs = urllib.parse.urlencode({
                "authorization": FAST2SMS_KEY,
                "message":       text,
                "language":      "english",
                "route":         "q",
                "numbers":       mobile
            })
            req = _ureq.Request(
                f"https://www.fast2sms.com/dev/bulkV2?{qs}",
                headers={"cache-control": "no-cache"}
            )
            _ureq.urlopen(req, timeout=10)
            return True
        except Exception as e:
            print(f"[FAST2SMS ERROR] {e}")

    print(f"[SMS] (no provider configured) To={mobile}: {text}")
    return False


def _generate_otp(length=6):
    return "".join(random.choices("0123456789", k=length))


def _order_email_html(order_id, username, items, total, delivery, grand, address, method):
    rows = "".join(
        f"<tr>"
        f"<td style='padding:8px 12px;border-bottom:1px solid #f0f0f0'>{i['name']}</td>"
        f"<td style='padding:8px 12px;border-bottom:1px solid #f0f0f0;text-align:center'>{i['qty']}</td>"
        f"<td style='padding:8px 12px;border-bottom:1px solid #f0f0f0;text-align:right'>&#8377;{i['price']*i['qty']}</td>"
        f"</tr>"
        for i in items
    )
    delivery_str = "FREE" if delivery == 0 else f"&#8377;{delivery}"
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:'Poppins',Arial,sans-serif;background:#f5f5f5;margin:0;padding:20px">
<div style="max-width:580px;margin:0 auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.1)">
  <div style="background:linear-gradient(135deg,#0c831f,#005f28);padding:28px 32px;text-align:center">
    <div style="font-size:2.2rem;font-weight:900;color:#f3a847;letter-spacing:-2px">quick<span style="color:#fff">kart</span></div>
    <div style="color:rgba(255,255,255,.85);font-size:.9rem;margin-top:6px">Order Confirmation</div>
  </div>
  <div style="padding:28px 32px">
    <h2 style="margin:0 0 8px;font-size:1.2rem;color:#1c1c1c">Hey {username}, your order is confirmed! &#127881;</h2>
    <p style="color:#666;font-size:.88rem;margin:0 0 20px">Order <strong>#{order_id}</strong> has been placed successfully. Estimated delivery: 10-20 mins.</p>
    <table style="width:100%;border-collapse:collapse;margin-bottom:16px">
      <thead>
        <tr style="background:#f8f8f8">
          <th style="padding:10px 12px;text-align:left;font-size:.82rem;color:#666">Item</th>
          <th style="padding:10px 12px;text-align:center;font-size:.82rem;color:#666">Qty</th>
          <th style="padding:10px 12px;text-align:right;font-size:.82rem;color:#666">Price</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
    <div style="background:#f8fffe;border-radius:10px;padding:14px 16px;margin-bottom:20px">
      <div style="display:flex;justify-content:space-between;font-size:.86rem;margin-bottom:6px">
        <span style="color:#666">Subtotal</span><span>&#8377;{total}</span>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:.86rem;margin-bottom:8px">
        <span style="color:#666">Delivery</span><span>{delivery_str}</span>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:1rem;font-weight:800;color:#0c831f;border-top:1.5px solid #e0e0e0;padding-top:8px">
        <span>Total</span><span>&#8377;{grand}</span>
      </div>
    </div>
    <div style="font-size:.82rem;color:#555;margin-bottom:8px"><strong>&#128205; Delivery Address:</strong><br>{address}</div>
    <div style="font-size:.82rem;color:#555"><strong>&#128179; Payment:</strong> {method.upper()}</div>
  </div>
  <div style="background:#f8fffe;padding:18px 32px;text-align:center;border-top:1px solid #e8e8e8">
    <p style="font-size:.78rem;color:#999;margin:0">Questions? Email us at
      <a href="mailto:support@quickkart.com" style="color:#0c831f">support@quickkart.com</a>
    </p>
  </div>
</div>
</body>
</html>"""


# ── App setup ─────────────────────────────────────────────
app = Flask(__name__, static_folder="static", static_url_path="/static")
app.secret_key = os.environ.get("SECRET_KEY", "quickkart-dev-secret-key-change-in-production")
DATABASE = "quickkart.db"

# ── Image Upload Config ───────────────────────────────────
UPLOAD_FOLDER    = os.path.join("static", "images", "products")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
app.config["UPLOAD_FOLDER"]    = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB max

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


# ── Error handlers ────────────────────────────────────────
@app.errorhandler(500)
def internal_error(e):
    tb = traceback.format_exc()
    return f"""<!doctype html><html><head><title>500</title>
<style>body{{font-family:monospace;background:#1a1a2e;color:#eee;padding:30px}}
pre{{background:#16213e;padding:20px;border-radius:10px;overflow-x:auto;border-left:4px solid #e94560}}
h1{{color:#e94560}}</style></head><body>
<h1>500 — Server Error</h1><pre>{tb}</pre>
<a href="/" style="color:#0f3460">Go Home</a></body></html>""", 500


@app.errorhandler(404)
def not_found(e):
    return """<!doctype html><html><body style="font-family:sans-serif;text-align:center;padding:80px">
<h1 style="font-size:4rem">404</h1><p>Page not found.</p>
<a href="/">Go Home</a></body></html>""", 404


# ── DB ────────────────────────────────────────────────────
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop("db", None)
    if db:
        db.close()


def _cols(cur, table):
    cur.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in cur.fetchall()]


# ── Products ──────────────────────────────────────────────
PRODUCTS = [
    # Dairy & Eggs
    (1,  "Milk 1L",           "Dairy & Eggs",        32,  "/static/images/products/3075977.png"),
    (2,  "Butter 500g",       "Dairy & Eggs",        55,  "/static/images/products/3080344.png"),
    (3,  "Paneer 200g",       "Dairy & Eggs",        80,  "/static/images/products/2515263.png"),
    (4,  "Curd 400g",         "Dairy & Eggs",        45,  "/static/images/products/2674505.png"),
    (5,  "Eggs (6 pack)",     "Dairy & Eggs",        65,  "/static/images/products/135620.png"),
    (6,  "Cheese Slice 200g", "Dairy & Eggs",        95,  "/static/images/products/3143643.png"),
    (7,  "Ghee 500ml",        "Dairy & Eggs",       280,  "/static/images/products/2938049.png"),
    (8,  "Skimmed Milk 1L",   "Dairy & Eggs",        38,  "/static/images/products/3075977.png"),
    # Fruits & Vegetables
    (9,  "Apple (1 kg)",      "Fruits & Vegetables", 120, "/static/images/products/415682.png"),
    (10, "Banana (6 pcs)",    "Fruits & Vegetables",  45, "/static/images/products/590685.png"),
    (11, "Tomato (500g)",     "Fruits & Vegetables",  30, "/static/images/products/1135525.png"),
    (12, "Onion (1 kg)",      "Fruits & Vegetables",  35, "/static/images/products/2909765.png"),
    (13, "Potato (1 kg)",     "Fruits & Vegetables",  28, "/static/images/products/2286001.png"),
    (14, "Spinach (250g)",    "Fruits & Vegetables",  22, "/static/images/products/2909769.png"),
    (15, "Carrot (500g)",     "Fruits & Vegetables",  35, "/static/images/products/135680.png"),
    (16, "Capsicum (500g)",   "Fruits & Vegetables",  55, "/static/images/products/2909781.png"),
    (17, "Mango (1 kg)",      "Fruits & Vegetables", 130, "/static/images/products/590700.png"),
    (18, "Grapes (500g)",     "Fruits & Vegetables",  85, "/static/images/products/2909761.png"),
    # Staples & Grains
    (19, "Basmati Rice 5kg",  "Staples & Grains",    380, "/static/images/products/1046857.png"),
    (20, "Wheat Flour 5kg",   "Staples & Grains",    220, "/static/images/products/2909771.png"),
    (21, "Toor Dal 1kg",      "Staples & Grains",    145, "/static/images/products/2909755.png"),
    (22, "Moong Dal 1kg",     "Staples & Grains",    130, "/static/images/products/2909755.png"),
    (23, "Chana Dal 1kg",     "Staples & Grains",    120, "/static/images/products/2909755.png"),
    (24, "Poha 500g",         "Staples & Grains",     48, "/static/images/products/1046857.png"),
    (25, "Semolina 1kg",      "Staples & Grains",     55, "/static/images/products/1046857.png"),
    (26, "Oats 500g",         "Staples & Grains",     90, "/static/images/products/2909760.png"),
    # Oils & Condiments
    (27, "Sunflower Oil 1L",  "Oils & Condiments",   145, "/static/images/products/2935394.png"),
    (28, "Mustard Oil 1L",    "Oils & Condiments",   135, "/static/images/products/2935394.png"),
    (29, "Olive Oil 500ml",   "Oils & Condiments",   420, "/static/images/products/2935394.png"),
    (30, "Salt 1kg",          "Oils & Condiments",    22, "/static/images/products/2909760.png"),
    (31, "Sugar 1kg",         "Oils & Condiments",    45, "/static/images/products/2909760.png"),
    (32, "Ketchup 500g",      "Oils & Condiments",    85, "/static/images/products/2515283.png"),
    (33, "Soy Sauce 200ml",   "Oils & Condiments",    65, "/static/images/products/2515283.png"),
    (34, "Vinegar 500ml",     "Oils & Condiments",    55, "/static/images/products/2935394.png"),
    # Snacks & Munchies
    (35, "Lay's Classic 100g","Snacks & Munchies",    20, "/static/images/products/1046876.png"),
    (36, "Kurkure 90g",       "Snacks & Munchies",    20, "/static/images/products/1046876.png"),
    (37, "Biscuits Marie",    "Snacks & Munchies",    30, "/static/images/products/3480823.png"),
    (38, "Parle-G 800g",      "Snacks & Munchies",    55, "/static/images/products/3480823.png"),
    (39, "Maggi 2-min 12pk",  "Snacks & Munchies",   140, "/static/images/products/2515273.png"),
    (40, "Popcorn Salted",    "Snacks & Munchies",    45, "/static/images/products/1046876.png"),
    (41, "Peanuts 200g",      "Snacks & Munchies",    35, "/static/images/products/2909760.png"),
    (42, "Cashews 200g",      "Snacks & Munchies",   180, "/static/images/products/2909760.png"),
    (43, "Almonds 200g",      "Snacks & Munchies",   220, "/static/images/products/2909760.png"),
    # Beverages
    (44, "Coca-Cola 2L",      "Beverages",            90, "/static/images/products/2935433.png"),
    (45, "Pepsi 2L",          "Beverages",            88, "/static/images/products/2935433.png"),
    (46, "Tropicana OJ 1L",   "Beverages",           125, "/static/images/products/2935433.png"),
    (47, "Green Tea 25 bags",  "Beverages",           110, "/static/images/products/2935426.png"),
    (48, "Coffee Nescafe 100g","Beverages",           235, "/static/images/products/924514.png"),
    (49, "Water 5L",           "Beverages",            50, "/static/images/products/2935433.png"),
    (50, "Red Bull 250ml",     "Beverages",           125, "/static/images/products/2935433.png"),
    (51, "Lassi 500ml",        "Beverages",            55, "/static/images/products/2674505.png"),
    # Bakery & Bread
    (52, "White Bread",        "Bakery & Bread",       40, "/static/images/products/1046784.png"),
    (53, "Brown Bread",        "Bakery & Bread",       48, "/static/images/products/1046784.png"),
    (54, "Multigrain Bread",   "Bakery & Bread",       55, "/static/images/products/1046784.png"),
    (55, "Pav (8 pcs)",        "Bakery & Bread",       30, "/static/images/products/1046784.png"),
    (56, "Croissant 2pc",      "Bakery & Bread",       65, "/static/images/products/3480823.png"),
    (57, "Muffin Choco",       "Bakery & Bread",       55, "/static/images/products/3480823.png"),
    # Chocolates & Sweets
    (58, "Dairy Milk 50g",     "Chocolates & Sweets",  60, "/static/images/products/1046786.png"),
    (59, "KitKat 4 finger",    "Chocolates & Sweets",  30, "/static/images/products/1046786.png"),
    (60, "5 Star Bar",         "Chocolates & Sweets",  20, "/static/images/products/1046786.png"),
    (61, "Munch Bar",          "Chocolates & Sweets",  10, "/static/images/products/1046786.png"),
    (62, "Ferrero Rocher 4pc", "Chocolates & Sweets", 200, "/static/images/products/1046786.png"),
    (63, "Oreo Original",      "Chocolates & Sweets",  40, "/static/images/products/3480823.png"),
    (64, "Gulab Jamun 500g",   "Chocolates & Sweets",  95, "/static/images/products/3480823.png"),
    # Spices & Masalas
    (65, "Turmeric 100g",      "Spices & Masalas",     28, "/static/images/products/2909779.png"),
    (66, "Red Chilli 100g",    "Spices & Masalas",     30, "/static/images/products/2909779.png"),
    (67, "Cumin Seeds 100g",   "Spices & Masalas",     32, "/static/images/products/2909779.png"),
    (68, "Coriander Pwd 100g", "Spices & Masalas",     28, "/static/images/products/2909779.png"),
    (69, "Garam Masala 100g",  "Spices & Masalas",     55, "/static/images/products/2909779.png"),
    (70, "Chai Masala 50g",    "Spices & Masalas",     45, "/static/images/products/2909779.png"),
    (71, "Black Pepper 50g",   "Spices & Masalas",     65, "/static/images/products/2909779.png"),
    (72, "Cardamom 20g",       "Spices & Masalas",     80, "/static/images/products/2909779.png"),
    # Frozen Foods
    (73, "Frozen Peas 500g",   "Frozen Foods",         65, "/static/images/products/2909769.png"),
    (74, "Frozen Corn 500g",   "Frozen Foods",         68, "/static/images/products/2909769.png"),
    (75, "Aloo Tikki 8pc",     "Frozen Foods",        120, "/static/images/products/2515273.png"),
    (76, "Chicken Nuggets",    "Frozen Foods",        185, "/static/images/products/2515273.png"),
    (77, "Frozen Paratha 5pc", "Frozen Foods",         95, "/static/images/products/1046784.png"),
    (78, "Ice Cream Vanilla 500ml","Frozen Foods",    130, "/static/images/products/3480823.png"),
    (79, "Ice Cream Choco 500ml", "Frozen Foods",     135, "/static/images/products/3480823.png"),
    # Personal Care
    (80, "Dove Soap 100g",     "Personal Care",        48, "/static/images/products/2942055.png"),
    (81, "Dettol Soap 125g",   "Personal Care",        45, "/static/images/products/2942055.png"),
    (82, "Colgate 200g",       "Personal Care",        65, "/static/images/products/2942055.png"),
    (83, "Shampoo Head S 180ml","Personal Care",       195, "/static/images/products/2942055.png"),
    (84, "Dettol Handwash 200ml","Personal Care",       85, "/static/images/products/2942055.png"),
    (85, "Face Wash 100ml",    "Personal Care",        145, "/static/images/products/2942055.png"),
    # Household
    (86, "Vim Dish Soap 500ml","Household",             85, "/static/images/products/2942076.png"),
    (87, "Harpic 500ml",       "Household",             95, "/static/images/products/2942076.png"),
    (88, "Colin Glass 500ml",  "Household",            110, "/static/images/products/2942076.png"),
    (89, "Surf Excel 1kg",     "Household",            185, "/static/images/products/2942076.png"),
    (90, "Odonil Blocks 2pc",  "Household",             65, "/static/images/products/2942076.png"),
    (91, "Tissue Roll 4pk",    "Household",            120, "/static/images/products/2942076.png"),
    (92, "Garbage Bags 30pc",  "Household",             65, "/static/images/products/2942076.png"),
    # Baby & Kids
    (93, "Pampers S 56pc",     "Baby & Kids",          650, "/static/images/products/3069172.png"),
    (94, "Baby Wipes 80pc",    "Baby & Kids",          180, "/static/images/products/3069172.png"),
    (95, "Cerelac 300g",       "Baby & Kids",          245, "/static/images/products/3069172.png"),
    (96, "Kids Biscuit 200g",  "Baby & Kids",           55, "/static/images/products/3480823.png"),
    (97, "Johnson Baby Oil",   "Baby & Kids",          165, "/static/images/products/3069172.png"),
    # Pet Supplies
    (98,  "Dog Food 1kg",      "Pet Supplies",         320, "/static/images/products/616554.png"),
    (99,  "Cat Food 400g",     "Pet Supplies",         180, "/static/images/products/616554.png"),
    (100, "Pet Shampoo 200ml", "Pet Supplies",         145, "/static/images/products/616554.png"),
]

CATEGORIES = sorted(set(p[2] for p in PRODUCTS))

CATEGORY_ICONS = {
    "Dairy & Eggs":       "🥛",
    "Fruits & Vegetables":"🥦",
    "Staples & Grains":   "🌾",
    "Oils & Condiments":  "🫙",
    "Snacks & Munchies":  "🍟",
    "Beverages":          "🥤",
    "Bakery & Bread":     "🍞",
    "Chocolates & Sweets":"🍫",
    "Spices & Masalas":   "🌶️",
    "Frozen Foods":       "🧊",
    "Personal Care":      "🧴",
    "Household":          "🧹",
    "Baby & Kids":        "👶",
    "Pet Supplies":       "🐾",
}

OFFERS = [
    {"title":"FLAT 20% OFF", "sub":"On your first order",         "code":"FIRST20", "color":"#ff6d00","emoji":"🎉"},
    {"title":"FREE DELIVERY","sub":"On orders above ₹299",        "code":"FREEDEL", "color":"#0db14b","emoji":"🚚"},
    {"title":"BUY 2 GET 1",  "sub":"On all dairy products",       "code":"DAIRY31", "color":"#8b5cf6","emoji":"🥛"},
    {"title":"SAVE ₹50",     "sub":"On grocery orders ₹500+",     "code":"SAVE50",  "color":"#e11d48","emoji":"💰"},
    {"title":"WEEKEND DEAL", "sub":"Extra 10% off on weekends",   "code":"WKND10",  "color":"#0891b2","emoji":"🎊"},
]


# ── DB init ────────────────────────────────────────────────
def init_db():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    cur = db.cursor()

    # users
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'")
    if cur.fetchone():
        cols = _cols(cur, "users")
        if "email" not in cols or "id" not in cols:
            cur.execute("DROP TABLE users")
    cur.execute("""CREATE TABLE IF NOT EXISTS users(
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        email      TEXT UNIQUE NOT NULL,
        username   TEXT NOT NULL,
        password   TEXT NOT NULL,
        role       TEXT NOT NULL DEFAULT 'user',
        mobile     TEXT DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now')))""")
    try:
        cur.execute("ALTER TABLE users ADD COLUMN mobile TEXT DEFAULT ''")
        db.commit()
    except Exception:
        pass

    # products
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='products'")
    if cur.fetchone():
        cols = _cols(cur, "products")
        if "category" not in cols or "stock" not in cols:
            cur.execute("DROP TABLE products")
    cur.execute("""CREATE TABLE IF NOT EXISTS products(
        id       INTEGER PRIMARY KEY,
        name     TEXT NOT NULL,
        category TEXT NOT NULL,
        price    INTEGER NOT NULL,
        image    TEXT NOT NULL,
        stock    INTEGER NOT NULL DEFAULT 100)""")

    # orders
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='orders'")
    if cur.fetchone():
        cols = _cols(cur, "orders")
        if "user_email" not in cols or "delivery_by" not in cols:
            cur.execute("DROP TABLE IF EXISTS order_items")
            cur.execute("DROP TABLE orders")
    cur.execute("""CREATE TABLE IF NOT EXISTS orders(
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_email TEXT NOT NULL,
        status     TEXT NOT NULL DEFAULT 'placed',
        delivery_by TEXT,
        address    TEXT NOT NULL DEFAULT '',
        total      INTEGER NOT NULL DEFAULT 0,
        placed_at  TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS order_items(
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id   INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        qty        INTEGER NOT NULL DEFAULT 1,
        price      INTEGER NOT NULL,
        FOREIGN KEY(order_id)   REFERENCES orders(id),
        FOREIGN KEY(product_id) REFERENCES products(id))""")

    # seed default users
    cur.execute("INSERT OR IGNORE INTO users(email,username,password,role) VALUES(?,?,?,?)",
                ("admin@quickkart.com","Admin",generate_password_hash("admin123"),"admin"))
    cur.execute("INSERT OR IGNORE INTO users(email,username,password,role) VALUES(?,?,?,?)",
                ("delivery@quickkart.com","DeliveryBoy",generate_password_hash("delivery123"),"delivery"))
    cur.executemany("INSERT OR IGNORE INTO products(id,name,category,price,image) VALUES(?,?,?,?,?)", PRODUCTS)
    db.commit()
    db.close()


init_db()


# ── Decorators ────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def d(*a, **kw):
        if "user" not in session:
            if (request.is_json or
                    request.path.startswith("/cart/update") or
                    request.path.startswith("/api/")):
                return jsonify({"ok": False, "error": "login_required"}), 401
            return redirect(url_for("login"))
        return f(*a, **kw)
    return d


def admin_required(f):
    @wraps(f)
    def d(*a, **kw):
        if session.get("role") != "admin":
            flash("Admin access required.", "error")
            return redirect(url_for("home"))
        return f(*a, **kw)
    return d


def delivery_required(f):
    @wraps(f)
    def d(*a, **kw):
        if session.get("role") not in ("admin", "delivery"):
            flash("Access denied.", "error")
            return redirect(url_for("home"))
        return f(*a, **kw)
    return d


# ── Shared assets ─────────────────────────────────────────
FONTS = '<link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,wght@0,300;0,400;0,500;0,600;0,700;0,800;1,400&family=Playfair+Display:wght@700;800;900&display=swap" rel="stylesheet">'

CSS = FONTS + """
<style>
:root{
  --bg:#0e0e0e;--surface:#161616;--surface2:#1e1e1e;
  --card:#1c1c1c;--card2:#242424;
  --border:#2a2a2a;--border2:#333;
  --text:#f0ece4;--text2:#a09890;--text3:#6a6560;
  --accent:#f5c842;--accent2:#e8b820;
  --green:#22c55e;--red:#ef4444;
  --shadow:0 4px 20px rgba(0,0,0,.35);
  --shadow-lg:0 12px 48px rgba(0,0,0,.55);
  --g1:#0c831f;
  --muted:#6a6560;
  --y:#f5c842;
}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{font-family:'DM Sans',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;-webkit-font-smoothing:antialiased}
a{text-decoration:none;color:inherit}
img{max-width:100%}
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:var(--surface)}
::-webkit-scrollbar-thumb{background:#333;border-radius:6px}

/* NAV */
.nav{background:rgba(14,14,14,.94);backdrop-filter:blur(18px);padding:0 28px;height:64px;
  display:flex;justify-content:space-between;align-items:center;
  position:sticky;top:0;z-index:300;gap:16px;border-bottom:1px solid var(--border)}
.brand{font-family:'Playfair Display',serif;font-size:1.55rem;font-weight:900;
  color:var(--accent);letter-spacing:-.3px;flex-shrink:0}
.brand span{color:var(--text)}
.nav-location{display:flex;flex-direction:column;cursor:pointer;
  border-right:1px solid var(--border);padding-right:18px;margin-right:4px;flex-shrink:0}
.nav-location .deliver-label{font-size:.56rem;font-weight:700;color:var(--accent);
  text-transform:uppercase;letter-spacing:1.2px}
.nav-location .address{font-size:.8rem;font-weight:600;color:var(--text);
  display:flex;align-items:center;gap:4px;white-space:nowrap}
.nav-search{flex:1;max-width:520px;position:relative}
.nav-search input{width:100%;padding:10px 16px 10px 40px;background:var(--surface2);
  border:1px solid var(--border2);border-radius:10px;font-family:'DM Sans',sans-serif;
  font-size:.85rem;outline:none;color:var(--text);transition:all .2s}
.nav-search input::placeholder{color:var(--text3)}
.nav-search input:focus{border-color:var(--accent);background:var(--surface);
  box-shadow:0 0 0 3px rgba(245,200,66,.1)}
.nav-search .search-icon{position:absolute;left:12px;top:50%;transform:translateY(-50%);
  color:var(--text3);font-size:.88rem;pointer-events:none}
.nav-right{display:flex;align-items:center;gap:8px;flex-shrink:0}
.nav-link{color:var(--text2);font-weight:600;font-size:.84rem;padding:7px 12px;border-radius:8px;transition:all .14s}
.nav-link:hover{color:var(--text);background:var(--surface2)}
.cart-btn{background:var(--accent);color:#0e0e0e;padding:9px 18px;border-radius:9px;
  font-weight:800;font-size:.83rem;display:flex;align-items:center;gap:7px;
  transition:all .15s;white-space:nowrap}
.cart-btn:hover{background:var(--accent2);transform:translateY(-1px)}
.cart-badge{background:#0e0e0e;color:var(--accent);border-radius:5px;padding:1px 6px;font-size:.68rem;font-weight:900}
.nav-user{background:var(--surface2);color:var(--text);padding:7px 13px;border-radius:9px;
  font-weight:600;font-size:.8rem;cursor:pointer;user-select:none;position:relative;
  display:flex;align-items:center;gap:6px;border:1px solid var(--border2);transition:all .15s}
.nav-user:hover{background:var(--card2)}
.nav-user .chevron{font-size:.56rem;transition:transform .2s;color:var(--text3)}
.user-dropdown{position:absolute;top:calc(100% + 10px);right:0;background:var(--surface);
  border-radius:14px;box-shadow:0 16px 48px rgba(0,0,0,.65);min-width:195px;z-index:500;
  overflow:hidden;border:1px solid var(--border2);
  opacity:0;visibility:hidden;transform:translateY(-8px);transition:all .2s ease}
.user-dropdown.open{opacity:1;visibility:visible;transform:translateY(0)}
.dropdown-header{padding:14px 16px;border-bottom:1px solid var(--border);background:var(--surface2)}
.dropdown-header .dh-name{font-weight:700;font-size:.86rem;color:var(--text)}
.dropdown-header .dh-role{font-size:.68rem;color:var(--text3);margin-top:2px}
.dropdown-item{display:flex;align-items:center;gap:10px;padding:10px 16px;
  font-weight:600;font-size:.82rem;color:var(--text2);transition:all .14s;cursor:pointer}
.dropdown-item:hover{background:var(--card2);color:var(--text)}
.dropdown-item.danger{color:#f87171}
.dropdown-item.danger:hover{background:rgba(239,68,68,.08);color:#ef4444}
.dropdown-item .di-icon{width:17px;text-align:center;font-size:.88rem}
.dropdown-divider{height:1px;background:var(--border);margin:4px 0}

/* FLASHES */
.flash-wrap{max-width:1280px;margin:12px auto 0;padding:0 24px}
.flash{padding:10px 16px;border-radius:10px;font-weight:600;font-size:.83rem;
  margin-bottom:8px;display:flex;align-items:center;gap:8px}
.flash.success{background:rgba(34,197,94,.1);color:#86efac;border:1px solid rgba(34,197,94,.2)}
.flash.error{background:rgba(239,68,68,.1);color:#fca5a5;border:1px solid rgba(239,68,68,.2)}
.flash.info{background:rgba(245,200,66,.1);color:var(--accent);border:1px solid rgba(245,200,66,.2)}

/* HERO */
.bk-hero-wrap{max-width:1280px;margin:0 auto;padding:22px 24px 0}
.bk-hero-main{border-radius:20px;overflow:hidden;background:var(--surface);
  padding:46px 52px;color:var(--text);display:flex;justify-content:space-between;
  align-items:center;min-height:218px;position:relative;margin-bottom:16px;
  border:1px solid var(--border2)}
.bk-hero-main::before{content:'';position:absolute;inset:0;
  background:radial-gradient(ellipse 60% 80% at 80% 50%,rgba(245,200,66,.06) 0%,transparent 70%);
  pointer-events:none}
.bk-hero-text{position:relative;z-index:1;max-width:520px}
.bk-hero-text .hero-eyebrow{display:inline-flex;align-items:center;gap:6px;
  background:rgba(245,200,66,.1);color:var(--accent);border:1px solid rgba(245,200,66,.22);
  border-radius:20px;padding:4px 12px;font-size:.7rem;font-weight:700;
  letter-spacing:.5px;text-transform:uppercase;margin-bottom:12px}
.bk-hero-text h1{font-family:'Playfair Display',serif;font-size:2.3rem;font-weight:900;
  line-height:1.16;margin-bottom:10px;color:var(--text)}
.bk-hero-text h1 em{font-style:normal;color:var(--accent)}
.bk-hero-text p{font-size:.9rem;color:var(--text2);margin-bottom:22px;line-height:1.65}
.bk-hero-btn{display:inline-flex;align-items:center;gap:8px;background:var(--accent);color:#0e0e0e;
  padding:12px 24px;border-radius:10px;font-weight:800;font-size:.88rem;transition:all .2s}
.bk-hero-btn:hover{background:var(--accent2);transform:translateY(-2px);box-shadow:0 8px 24px rgba(245,200,66,.25)}
.bk-hero-img{position:relative;z-index:1;flex-shrink:0}
.bk-hero-img img{width:250px;height:170px;object-fit:contain;filter:drop-shadow(0 8px 28px rgba(0,0,0,.5))}
.hero-stats{display:flex;gap:28px;margin-top:18px}
.hero-stat .hs-val{font-size:1.25rem;font-weight:800;color:var(--text);line-height:1}
.hero-stat .hs-lbl{font-size:.64rem;color:var(--text3);font-weight:500;margin-top:2px;
  text-transform:uppercase;letter-spacing:.6px}

/* PROMO BANNERS */
.bk-promo-row{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;
  max-width:1280px;margin:0 auto;padding:0 24px 18px}
.bk-promo{border-radius:16px;padding:20px 18px;display:flex;justify-content:space-between;
  align-items:center;overflow:hidden;position:relative;min-height:132px;cursor:pointer;
  transition:transform .2s,box-shadow .2s;border:1px solid rgba(255,255,255,.05)}
.bk-promo:hover{transform:translateY(-3px);box-shadow:var(--shadow-lg)}
.bk-promo-text h3{font-family:'Playfair Display',serif;font-size:1.08rem;font-weight:800;
  line-height:1.25;margin-bottom:5px}
.bk-promo-text p{font-size:.72rem;opacity:.72;font-weight:400;margin-bottom:11px;line-height:1.5}
.bk-promo-btn{display:inline-flex;align-items:center;gap:4px;
  background:rgba(255,255,255,.14);backdrop-filter:blur(8px);
  padding:6px 13px;border-radius:7px;font-weight:700;font-size:.74rem;
  color:#fff;border:1px solid rgba(255,255,255,.18);transition:all .15s}
.bk-promo-btn:hover{background:rgba(255,255,255,.24)}
.bk-promo-img{width:90px;height:84px;object-fit:contain;flex-shrink:0;
  filter:drop-shadow(0 4px 10px rgba(0,0,0,.3))}

/* CATEGORY CHIPS */
.bk-cats-wrap{max-width:1280px;margin:0 auto;padding:4px 24px 10px}
.bk-cats-title{font-size:.66rem;font-weight:700;color:var(--text3);
  text-transform:uppercase;letter-spacing:1.5px;margin-bottom:10px}
.bk-cats-scroll{display:flex;gap:7px;overflow-x:auto;scrollbar-width:none;padding-bottom:4px}
.bk-cats-scroll::-webkit-scrollbar{display:none}
.bk-cat-chip{flex-shrink:0;display:flex;align-items:center;gap:6px;
  padding:7px 13px;background:var(--surface2);border-radius:22px;cursor:pointer;
  transition:all .15s;text-decoration:none;border:1px solid var(--border2)}
.bk-cat-chip:hover{background:var(--card2);transform:translateY(-1px)}
.bk-cat-chip.active{background:var(--accent);border-color:var(--accent)}
.bk-cat-chip .cat-emoji{font-size:.95rem;line-height:1}
.bk-cat-chip .cat-label{font-size:.76rem;font-weight:600;color:var(--text2);white-space:nowrap}
.bk-cat-chip.active .cat-label{color:#0e0e0e;font-weight:800}

/* PRODUCT SECTIONS */
.bk-section{max-width:1280px;margin:0 auto;padding:14px 24px}
.bk-section-hd{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.bk-section-title{font-family:'Playfair Display',serif;font-size:1.1rem;font-weight:800;
  display:flex;align-items:center;gap:8px;color:var(--text)}
.bk-see-all{font-size:.74rem;font-weight:600;color:var(--text3);
  background:var(--surface2);padding:5px 11px;border-radius:20px;
  border:1px solid var(--border2);transition:all .14s}
.bk-see-all:hover{color:var(--text);background:var(--card2)}
.section{max-width:1280px;margin:0 auto;padding:0 24px 20px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(168px,1fr));gap:10px}

/* PRODUCT CARD */
.card{background:var(--card);border-radius:16px;padding:13px;
  transition:transform .2s,box-shadow .2s,border-color .2s;
  position:relative;border:1px solid var(--border)}
.card:hover{transform:translateY(-3px);box-shadow:0 14px 40px rgba(0,0,0,.55);border-color:var(--border2)}
.card-img-wrap{background:var(--surface2);border-radius:12px;
  display:flex;align-items:center;justify-content:center;height:118px;
  margin-bottom:10px;overflow:hidden;transition:background .2s}
.card:hover .card-img-wrap{background:var(--card2)}
.card img{width:76px;height:76px;object-fit:contain;
  transition:transform .25s;filter:drop-shadow(0 3px 7px rgba(0,0,0,.35))}
.card:hover img{transform:scale(1.09)}
.card-name{font-weight:600;font-size:.79rem;margin-bottom:3px;line-height:1.35;color:var(--text)}
.card-price{color:var(--text);font-weight:800;font-size:.9rem;margin-bottom:11px}
.card-badge{position:absolute;top:7px;left:7px;background:var(--accent);color:#0e0e0e;
  font-size:.54rem;font-weight:900;padding:2px 6px;border-radius:5px;letter-spacing:.5px}
.qty-ctrl{display:flex;align-items:center;justify-content:space-between;
  background:rgba(245,200,66,.08);border-radius:9px;overflow:hidden;border:1px solid var(--accent)}
.qty-btn{background:none;border:none;width:32px;height:32px;font-size:.95rem;
  font-weight:800;cursor:pointer;color:var(--accent);
  display:flex;align-items:center;justify-content:center;transition:background .12s}
.qty-btn:hover{background:rgba(245,200,66,.15)}
.qty-num{font-weight:800;font-size:.86rem;color:var(--accent)}
.add-btn{background:var(--accent);color:#0e0e0e;border:none;
  border-radius:9px;padding:9px 0;width:100%;font-family:'DM Sans',sans-serif;
  font-weight:800;font-size:.79rem;cursor:pointer;transition:all .15s}
.add-btn:hover{background:var(--accent2);transform:translateY(-1px)}
.add-btn:active{transform:scale(.97)}

/* CART PAGE */
.cart-wrap{max-width:980px;margin:0 auto;padding:24px;
  display:grid;grid-template-columns:1fr 340px;gap:20px;align-items:start}
@media(max-width:740px){.cart-wrap{grid-template-columns:1fr}}
.cart-item{background:var(--card);border-radius:14px;padding:13px 17px;
  display:flex;align-items:center;gap:13px;margin-bottom:9px;
  border:1px solid var(--border);transition:border-color .14s}
.cart-item:hover{border-color:var(--border2)}
.cart-item-img{background:var(--surface2);border-radius:10px;width:50px;height:50px;
  display:flex;align-items:center;justify-content:center;flex-shrink:0}
.cart-item-img img{width:36px;height:36px;object-fit:contain}
.ci-name{font-weight:700;font-size:.86rem;color:var(--text)}
.ci-unit{font-size:.72rem;color:var(--text3);margin-top:2px}
.ci-sub{font-weight:900;color:var(--text);font-size:.94rem;white-space:nowrap}
.order-summary{background:var(--card);border-radius:14px;padding:20px;
  border:1px solid var(--border);position:sticky;top:76px}
.os-title{font-family:'Playfair Display',serif;font-weight:800;font-size:.98rem;
  margin-bottom:14px;padding-bottom:12px;border-bottom:1px solid var(--border);color:var(--text)}
.os-row{display:flex;justify-content:space-between;align-items:center;
  padding:6px 0;font-size:.83rem;color:var(--text2)}
.os-row.total{border-top:1px solid var(--border);margin-top:8px;padding-top:12px;
  font-weight:900;font-size:.98rem;color:var(--text)}
.os-row .green{color:var(--green);font-weight:700}
.coupon-box{display:flex;gap:7px;margin:12px 0}
.coupon-input{flex:1;padding:9px 12px;background:var(--surface2);
  border:1px solid var(--border2);border-radius:9px;
  font-family:inherit;font-size:.82rem;outline:none;color:var(--text);transition:border .14s}
.coupon-input::placeholder{color:var(--text3)}
.coupon-input:focus{border-color:var(--accent);box-shadow:0 0 0 2px rgba(245,200,66,.1)}
.coupon-btn{padding:9px 13px;background:var(--surface2);color:var(--text2);
  border:1px solid var(--border2);border-radius:9px;font-family:inherit;
  font-weight:700;cursor:pointer;font-size:.8rem;transition:all .14s}
.coupon-btn:hover{background:var(--card2);color:var(--text)}

/* AUTH */
.auth-wrap{display:flex;min-height:100vh}
.auth-left{flex:1;background:var(--surface);display:flex;flex-direction:column;
  justify-content:center;align-items:center;padding:48px;color:var(--text);
  text-align:center;position:relative;overflow:hidden;border-right:1px solid var(--border)}
.auth-left::before{content:'';position:absolute;top:-80px;left:-80px;
  width:360px;height:360px;
  background:radial-gradient(circle,rgba(245,200,66,.06) 0%,transparent 70%);pointer-events:none}
.auth-left h2{font-family:'Playfair Display',serif;font-size:2.1rem;font-weight:900;
  margin-bottom:12px;color:var(--text)}
.auth-left h2 span{color:var(--accent)}
.auth-left p{opacity:.5;font-size:.88rem;max-width:300px;line-height:1.65}
.auth-features{margin-top:32px;display:flex;flex-direction:column;gap:9px;
  text-align:left;width:100%;max-width:275px;position:relative}
.auth-feature{display:flex;align-items:center;gap:10px;background:var(--surface2);
  padding:10px 14px;border-radius:10px;font-size:.82rem;font-weight:500;
  border:1px solid var(--border2);color:var(--text2)}
.auth-right{width:480px;display:flex;align-items:center;justify-content:center;
  padding:48px 40px;background:var(--bg)}
.auth-box{width:100%}
.auth-box h1{font-family:'Playfair Display',serif;font-size:1.75rem;font-weight:900;
  margin-bottom:5px;color:var(--text)}
.auth-box .sub{color:var(--text3);font-size:.84rem;margin-bottom:26px}
.form-group{margin-bottom:15px}
.form-group label{display:block;font-weight:600;font-size:.76rem;margin-bottom:5px;
  color:var(--text2);letter-spacing:.3px;text-transform:uppercase}
.form-group input{width:100%;padding:11px 14px;background:var(--surface2);
  border:1px solid var(--border2);border-radius:10px;font-family:inherit;
  font-size:.9rem;outline:none;color:var(--text);transition:all .2s}
.form-group input::placeholder{color:var(--text3)}
.form-group input:focus{border-color:var(--accent);background:var(--surface);
  box-shadow:0 0 0 3px rgba(245,200,66,.1)}
.gmail-hint{font-size:.7rem;color:var(--text3);margin-top:4px;display:flex;align-items:center;gap:4px}
@media(max-width:700px){.auth-left{display:none}.auth-right{width:100%;padding:28px}}

/* BUTTONS */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;
  padding:11px 22px;border:none;border-radius:10px;font-family:inherit;
  font-size:.86rem;font-weight:700;cursor:pointer;transition:all .15s}
.btn-green{background:var(--accent);color:#0e0e0e}
.btn-green:hover{background:var(--accent2);transform:translateY(-1px)}
.btn-yellow{background:var(--accent);color:#0e0e0e}
.btn-yellow:hover{background:var(--accent2);transform:translateY(-1px)}
.btn-orange{background:#f59e0b;color:#0e0e0e}
.btn-orange:hover{background:#e08c00}
.btn-red{background:rgba(239,68,68,.12);color:#f87171;border:1px solid rgba(239,68,68,.2)}
.btn-red:hover{background:rgba(239,68,68,.2)}
.btn-gray{background:var(--surface2);color:var(--text2);border:1px solid var(--border2)}
.btn-gray:hover{background:var(--card2);color:var(--text)}
.btn-full{width:100%}
.btn-sm{padding:7px 13px;font-size:.76rem;border-radius:8px}
.btn:active{transform:scale(.97)}

/* SHARED UTILS */
.panel-wrap{max-width:1280px;margin:0 auto;padding:24px}
.panel-hd{font-family:'Playfair Display',serif;font-size:1.35rem;font-weight:900;
  margin-bottom:22px;display:flex;align-items:center;gap:10px;color:var(--text)}
.stats-row{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:13px;margin-bottom:22px}
.stat{background:var(--card);border-radius:13px;padding:20px;border:1px solid var(--border)}
.stat.green .num{color:var(--green)}
.stat.orange .num{color:#f59e0b}
.stat.blue .num{color:#60a5fa}
.stat.purple .num{color:#a78bfa}
.stat .num{font-size:1.9rem;font-weight:900;line-height:1;color:var(--text)}
.stat .lbl{color:var(--text3);font-size:.76rem;margin-top:5px;font-weight:500}
table{width:100%;border-collapse:collapse;background:var(--card);border-radius:12px;overflow:hidden;font-size:.82rem}
thead tr{background:var(--surface2)}
th{padding:10px 16px;text-align:left;font-weight:700;color:var(--text3);
  font-size:.64rem;text-transform:uppercase;letter-spacing:1px}
td{padding:10px 16px;border-top:1px solid var(--border);vertical-align:middle;color:var(--text2)}
tr:hover td{background:var(--surface2)}
.badge{display:inline-block;padding:3px 9px;border-radius:20px;font-size:.66rem;font-weight:700}
.badge.placed{background:rgba(96,165,250,.13);color:#93c5fd}
.badge.confirmed{background:rgba(251,191,36,.1);color:#fcd34d}
.badge.out{background:rgba(167,139,250,.1);color:#c4b5fd}
.badge.delivered{background:rgba(34,197,94,.1);color:#86efac}
.badge.cancelled{background:rgba(239,68,68,.1);color:#fca5a5}
.tab-nav{display:flex;gap:3px;background:var(--surface2);border-radius:10px;padding:4px;
  margin-bottom:20px;width:fit-content;flex-wrap:wrap;border:1px solid var(--border)}
.tab-btn{padding:7px 15px;border-radius:7px;font-weight:600;font-size:.79rem;color:var(--text3);
  cursor:pointer;border:none;background:none;font-family:inherit;transition:all .14s}
.tab-btn.active{background:var(--card2);color:var(--text);font-weight:700}
.tab-content{display:none}.tab-content.active{display:block}
.toolbar{display:flex;gap:10px;align-items:center;margin-bottom:14px;flex-wrap:wrap}
.search-bar{padding:8px 13px;background:var(--surface2);border:1px solid var(--border2);
  border-radius:9px;font-family:inherit;font-size:.82rem;outline:none;
  width:235px;color:var(--text);transition:border .14s}
.search-bar::placeholder{color:var(--text3)}
.search-bar:focus{border-color:var(--accent)}
.status-select{padding:5px 10px;background:var(--surface2);border:1px solid var(--border2);
  border-radius:7px;font-family:inherit;font-size:.77rem;font-weight:600;
  cursor:pointer;outline:none;color:var(--text)}
.chip{background:var(--surface2);border:1px solid var(--border);border-radius:6px;
  padding:2px 8px;font-size:.68rem;font-weight:600;color:var(--text3)}
.empty{text-align:center;padding:60px 20px;color:var(--text3)}
.empty .icon{font-size:2.8rem;margin-bottom:12px;opacity:.4}

/* NAV PILLS */
.nav-pill-admin{background:rgba(167,139,250,.13);color:#c4b5fd!important;
  padding:7px 13px;border-radius:8px;font-weight:700;font-size:.77rem;
  display:flex;align-items:center;gap:5px;border:1px solid rgba(167,139,250,.2);transition:all .14s}
.nav-pill-admin:hover{background:rgba(167,139,250,.2)}
.nav-pill-delivery{background:rgba(245,200,66,.1);color:var(--accent)!important;
  padding:7px 13px;border-radius:8px;font-weight:700;font-size:.77rem;
  display:flex;align-items:center;gap:5px;border:1px solid rgba(245,200,66,.2);transition:all .14s}
.nav-pill-delivery:hover{background:rgba(245,200,66,.18)}
.role-badge{display:inline-block;padding:2px 7px;border-radius:5px;font-size:.58rem;
  font-weight:800;letter-spacing:.5px;vertical-align:middle;margin-left:4px}
.admin-badge{background:rgba(167,139,250,.13);color:#c4b5fd;border:1px solid rgba(167,139,250,.18)}
.delivery-badge{background:rgba(245,200,66,.1);color:var(--accent);border:1px solid rgba(245,200,66,.18)}

/* CHAT FAB */
#chat-fab{position:fixed;bottom:24px;right:24px;z-index:1000;
  width:52px;height:52px;background:var(--accent);border-radius:50%;
  display:flex;align-items:center;justify-content:center;
  cursor:pointer;box-shadow:0 4px 18px rgba(245,200,66,.3);border:none;transition:all .2s}
#chat-fab:hover{transform:scale(1.1);box-shadow:0 6px 26px rgba(245,200,66,.45)}
#chat-fab svg{width:23px;height:23px;fill:#0e0e0e}
#chat-fab .fab-badge{position:absolute;top:-2px;right:-2px;background:#ef4444;
  color:#fff;border-radius:50%;width:17px;height:17px;font-size:.58rem;
  font-weight:900;display:flex;align-items:center;justify-content:center;border:2px solid var(--bg)}
#chat-window{position:fixed;bottom:88px;right:24px;z-index:999;width:342px;
  background:var(--surface);border-radius:20px;
  box-shadow:0 20px 60px rgba(0,0,0,.7);display:none;flex-direction:column;
  overflow:hidden;max-height:490px;border:1px solid var(--border2)}
@keyframes slideUp{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:translateY(0)}}
#chat-window.open{display:flex;animation:slideUp .2s ease}
.chat-hd{background:var(--surface2);border-bottom:1px solid var(--border);
  padding:13px 16px;display:flex;align-items:center;gap:10px;color:var(--text)}
.chat-avatar{width:35px;height:35px;background:var(--accent);border-radius:50%;
  display:flex;align-items:center;justify-content:center;font-size:1rem;flex-shrink:0}
.chat-hd-info .name{font-weight:700;font-size:.88rem;color:var(--text)}
.chat-hd-info .status{font-size:.68rem;color:var(--text3);display:flex;align-items:center;gap:4px;margin-top:1px}
.online-dot{width:5px;height:5px;background:var(--green);border-radius:50%;animation:blink 1.5s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
.chat-close{margin-left:auto;background:none;border:none;color:var(--text3);
  font-size:1.05rem;cursor:pointer;transition:color .14s;line-height:1}
.chat-close:hover{color:var(--text)}
.chat-msgs{flex:1;overflow-y:auto;padding:13px;display:flex;flex-direction:column;gap:8px;
  background:var(--bg);min-height:250px;max-height:310px}
.msg{display:flex;gap:6px;align-items:flex-end;max-width:88%}
.msg.bot{align-self:flex-start}
.msg.user{align-self:flex-end;flex-direction:row-reverse}
.msg-bubble{padding:8px 12px;border-radius:13px;font-size:.81rem;line-height:1.5;font-weight:400}
.msg.bot .msg-bubble{background:var(--surface);color:var(--text);
  border:1px solid var(--border2);border-bottom-left-radius:3px}
.msg.user .msg-bubble{background:var(--accent);color:#0e0e0e;font-weight:600;border-bottom-right-radius:3px}
.msg-time{font-size:.6rem;color:var(--text3);margin:0 3px 2px;white-space:nowrap}
.bot-icon{width:25px;height:25px;background:var(--surface2);border-radius:50%;
  display:flex;align-items:center;justify-content:center;font-size:.78rem;
  flex-shrink:0;border:1px solid var(--border)}
.chat-suggestions{display:flex;gap:5px;flex-wrap:wrap;padding:8px 12px;
  background:var(--surface2);border-top:1px solid var(--border)}
.suggestion{padding:4px 9px;background:var(--card2);color:var(--text2);
  border:1px solid var(--border2);border-radius:20px;font-size:.7rem;
  font-weight:600;cursor:pointer;transition:all .14s}
.suggestion:hover{background:var(--accent);color:#0e0e0e;border-color:var(--accent)}
.chat-input-row{display:flex;gap:6px;padding:9px 11px;
  background:var(--surface2);border-top:1px solid var(--border)}
.chat-input{flex:1;padding:8px 12px;background:var(--card2);
  border:1px solid var(--border2);border-radius:20px;font-family:inherit;
  font-size:.81rem;outline:none;color:var(--text);transition:border .14s}
.chat-input::placeholder{color:var(--text3)}
.chat-input:focus{border-color:var(--accent)}
.chat-send{width:33px;height:33px;background:var(--accent);border:none;border-radius:50%;
  cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:all .14s}
.chat-send:hover{background:var(--accent2)}
.chat-send svg{width:15px;height:15px;fill:#0e0e0e}
.typing{display:flex;gap:3px;align-items:center;padding:4px 0}
.typing span{width:5px;height:5px;background:var(--border2);border-radius:50%;animation:bounce .8s infinite}
.typing span:nth-child(2){animation-delay:.15s}
.typing span:nth-child(3){animation-delay:.3s}
@keyframes bounce{0%,100%{transform:translateY(0)}50%{transform:translateY(-4px)}}

/* ORDERS PAGE */
.orders-wrap{max-width:820px;margin:0 auto;padding:24px}
.order-card{background:var(--card);border-radius:14px;padding:17px 20px;
  margin-bottom:10px;border:1px solid var(--border);transition:border-color .15s}
.order-card:hover{border-color:var(--border2)}
.order-hd{display:flex;justify-content:space-between;align-items:flex-start;
  margin-bottom:10px;flex-wrap:wrap;gap:8px}
.order-num{font-weight:800;font-size:.92rem;color:var(--text)}
.order-item-row{display:flex;align-items:center;gap:10px;padding:8px 0;border-top:1px solid var(--border)}
.oitem-img{width:33px;height:33px;object-fit:contain}
.divider{height:1px;background:var(--border);margin:13px 0}

/* ANIMATIONS */
@keyframes fadeIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.bk-hero-main{animation:fadeIn .45s ease both}
.bk-promo-row{animation:fadeIn .45s ease .07s both}
.bk-cats-wrap{animation:fadeIn .45s ease .13s both}
.bk-section{animation:fadeIn .4s ease both}

@media(max-width:600px){
  .grid{grid-template-columns:repeat(auto-fill,minmax(145px,1fr))}
  .nav{padding:0 14px;gap:8px;height:56px}
  .nav-location{display:none}
  #chat-window{width:calc(100vw - 18px);right:9px}
  .bk-promo-row{grid-template-columns:1fr}
  .bk-hero-main{padding:24px 20px}
  .bk-hero-img{display:none}
  .bk-hero-text h1{font-size:1.65rem}
  .bk-section,.bk-hero-wrap,.bk-promo-row,.bk-cats-wrap,.orders-wrap,.panel-wrap,.cart-wrap{padding-left:14px;padding-right:14px}
}
</style>"""


def get_flashes():
    msgs = session.pop("_flashes", [])
    if not msgs:
        return ""
    html = '<div class="flash-wrap">'
    for cat, msg in msgs:
        html += f'<div class="flash {cat}">{msg}</div>'
    return html + "</div>"


def nav_html():
    user  = session.get("user", "")
    role  = session.get("role", "")
    count = sum(session.get("cart", {}).values())
    links = ""
    if role == "admin":
        links += '<a href="/admin" class="nav-pill-admin">&#9881; Admin Panel</a>'
    if role in ("admin", "delivery"):
        links += '<a href="/delivery" class="nav-pill-delivery">&#128693; Delivery</a>'
    role_badge = ""
    if role == "admin":
        role_badge = '<span class="role-badge admin-badge">ADMIN</span>'
    elif role == "delivery":
        role_badge = '<span class="role-badge delivery-badge">DELIVERY</span>'
    cb = (f'<span style="background:var(--g1);color:#fff;border-radius:20px;'
          f'padding:1px 7px;font-size:.72rem;margin-left:auto">{count}</span>') if count else ""
    return f"""<nav class="nav">
  <a href="/" class="brand">&#9889; Quick<span>Kart</span></a>
  <div class="nav-right">
    <div class="nav-user" onclick="toggleUserDropdown(event)" id="user-menu-btn">
      &#128100; {user} {role_badge}
      <span class="chevron" id="user-chevron">&#9660;</span>
      <div class="user-dropdown" id="user-dropdown">
        <div class="dropdown-header">
          <div class="dh-name">&#128100; {user}</div>
          <div class="dh-role">{role.capitalize() if role else 'Customer'}</div>
        </div>
        <a href="/orders" class="dropdown-item"><span class="di-icon">&#128230;</span> My Orders</a>
        <a href="/cart" class="dropdown-item"><span class="di-icon">&#128722;</span> My Cart {cb}</a>
        <div class="dropdown-divider"></div>
        <a href="/logout" class="dropdown-item danger"><span class="di-icon">&#128682;</span> Logout</a>
      </div>
    </div>
    {links}
    <a href="/cart" class="cart-btn">&#128722; Cart <span class="cart-badge">{count}</span></a>
  </div>
</nav>
<script>
function toggleUserDropdown(e){{
  e.stopPropagation();
  var dd=document.getElementById('user-dropdown');
  var ch=document.getElementById('user-chevron');
  dd.classList.toggle('open');
  ch.style.transform=dd.classList.contains('open')?'rotate(180deg)':'';
}}
document.addEventListener('click',function(){{
  var dd=document.getElementById('user-dropdown');
  var ch=document.getElementById('user-chevron');
  if(dd){{dd.classList.remove('open');if(ch)ch.style.transform='';}}
}});
</script>"""


CHAT_WIDGET = """
<button id="chat-fab" onclick="toggleChat()" aria-label="Open AI Support Chat">
  <svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12c0 1.85.5 3.58 1.37 5.07L2 22l5.18-1.35A9.94 9.94 0 0012 22c5.52 0 10-4.48 10-10S17.52 2 12 2zm-1 15H9v-2h2v2zm0-4H9V7h2v6zm4 4h-2v-2h2v2zm0-4h-2V7h2v6z"/></svg>
  <span class="fab-badge">AI</span>
</button>
<div id="chat-window">
  <div class="chat-hd">
    <div class="chat-avatar">&#129302;</div>
    <div class="chat-hd-info">
      <div class="name">QuickKart AI</div>
      <div class="status"><span class="online-dot"></span> Online &mdash; here to help</div>
    </div>
    <button class="chat-close" onclick="toggleChat()">&#10005;</button>
  </div>
  <div class="chat-msgs" id="chat-msgs"></div>
  <div class="chat-suggestions" id="chat-suggestions">
    <span class="suggestion" onclick="sendSuggestion(this)">Track my order</span>
    <span class="suggestion" onclick="sendSuggestion(this)">Offers &amp; coupons</span>
    <span class="suggestion" onclick="sendSuggestion(this)">Delivery time</span>
    <span class="suggestion" onclick="sendSuggestion(this)">Return policy</span>
    <span class="suggestion" onclick="sendSuggestion(this)">Payment methods</span>
  </div>
  <div class="chat-input-row">
    <input class="chat-input" id="chat-input" placeholder="Ask me anything..."
      onkeydown="if(event.key==='Enter')sendMsg()">
    <button class="chat-send" onclick="sendMsg()">
      <svg viewBox="0 0 24 24"><path d="M2 21l21-9L2 3v7l15 2-15 2z"/></svg>
    </button>
  </div>
</div>
<script>
const chatMsgs = document.getElementById('chat-msgs');
let chatOpen = false;
let chatHistory = [];
function toggleChat(){
  chatOpen = !chatOpen;
  document.getElementById('chat-window').classList.toggle('open', chatOpen);
  if(chatOpen && chatMsgs.children.length === 0){
    addMsg('bot', "Hi! I'm your QuickKart AI assistant. How can I help you today? 🛒");
  }
}
function addMsg(role, text){
  const now = new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
  const wrap = document.createElement('div');
  wrap.className = `msg ${role}`;
  if(role === 'bot'){
    wrap.innerHTML = `<div class="bot-icon">🤖</div>
      <div><div class="msg-bubble">${text}</div><div class="msg-time">${now}</div></div>`;
  } else {
    wrap.innerHTML = `<div><div class="msg-bubble">${text}</div>
      <div class="msg-time" style="text-align:right">${now}</div></div>`;
  }
  chatMsgs.appendChild(wrap);
  chatMsgs.scrollTop = chatMsgs.scrollHeight;
}
function addTyping(){
  const t = document.createElement('div');
  t.className = 'msg bot'; t.id = 'typing-indicator';
  t.innerHTML = `<div class="bot-icon">🤖</div>
    <div class="msg-bubble" style="background:#fff;border:1px solid #e5f7ea">
      <div class="typing"><span></span><span></span><span></span></div>
    </div>`;
  chatMsgs.appendChild(t);
  chatMsgs.scrollTop = chatMsgs.scrollHeight;
}
function removeTyping(){
  const t = document.getElementById('typing-indicator');
  if(t) t.remove();
}
function sendSuggestion(el){
  const text = el.innerText;
  document.getElementById('chat-suggestions').style.display = 'none';
  sendMsgText(text);
}
function sendMsg(){
  const inp = document.getElementById('chat-input');
  const text = inp.value.trim();
  if(!text) return;
  inp.value = '';
  sendMsgText(text);
}
async function sendMsgText(text){
  addMsg('user', text);
  chatHistory.push({role:'user', content: text});
  addTyping();
  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({messages: chatHistory})
    });
    const data = await resp.json();
    removeTyping();
    const reply = data.reply || "Sorry, I couldn't understand that. Please try again.";
    addMsg('bot', reply);
    chatHistory.push({role:'assistant', content: reply});
    if(chatHistory.length > 20) chatHistory = chatHistory.slice(-20);
  } catch(e){
    removeTyping();
    addMsg('bot', "Sorry, I'm having trouble connecting. Please try again.");
  }
}
</script>
"""


# ── AI Chat API ───────────────────────────────────────────
@app.route("/api/chat", methods=["POST"])
@login_required
def api_chat():
    data     = request.get_json(silent=True) or {}
    messages = data.get("messages", [])
    if not messages:
        return jsonify({"reply": "Hi! How can I help you?"})
    messages = messages[-10:]
    system_prompt = """You are a friendly and helpful AI customer support assistant for QuickKart,
India's fastest grocery delivery app. You help customers with:
- Order tracking and status (tell them to check the 'Orders' page)
- Delivery time: typically 10-20 minutes
- Return/refund policy: returns accepted within 24 hours for damaged items, contact support
- Offers & coupons: FIRST20 (20% off first order), FREEDEL (free delivery on orders above Rs.299),
  DAIRY31 (buy 2 get 1 on dairy), SAVE50 (save Rs.50 on orders Rs.500+), WKND10 (10% off weekends)
- Payment methods: Cash on Delivery, PayPal
- Products: 100+ products across Dairy, Fruits, Vegetables, Grains, Snacks, Beverages, Bakery,
  Chocolates, Spices, Frozen Foods, Personal Care, Household, Baby & Kids, Pet Supplies
- Free delivery on orders above Rs.299, otherwise Rs.30 delivery fee
- Store timings: 24/7 delivery available
Always be warm, helpful, concise (2-3 sentences max), and use occasional relevant emojis.
If you don't know something, suggest contacting support at support@quickkart.com."""

    # Build messages with system prompt included for OpenRouter
    full_messages = [{"role": "system", "content": system_prompt}] + messages

    payload = json.dumps({
        "model":      "openai/gpt-4o-mini",
        "max_tokens": 300,
        "messages":   full_messages
    }).encode("utf-8")

    req = _ureq.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=payload,
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "HTTP-Referer":  "https://quickkart.app",
            "X-Title":       "QuickKart",
        },
        method="POST"
    )
    try:
        with _ureq.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
        reply = result["choices"][0]["message"]["content"]
    except Exception as ex:
        print(f"[CHAT ERROR] {ex}")
        reply = "I'm having a little trouble right now. Please try again or email support@quickkart.com 😊"
    return jsonify({"reply": reply})


# ── PANEL ROUTER ──────────────────────────────────────────
@app.route("/panel")
@login_required
def panel():
    role = session.get("role", "user")
    if role == "admin":    return redirect(url_for("admin"))
    if role == "delivery": return redirect(url_for("delivery"))
    return redirect(url_for("orders_page"))


# ── LOGIN ─────────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    if "user" in session:
        return redirect(url_for("home"))
    error = ""
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not email or not password:
            error = "All fields required."
        else:
            db  = get_db()
            row = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if row and check_password_hash(row["password"], password):
                session.clear()
                session["user"]  = row["username"]
                session["email"] = row["email"]
                session["role"]  = row["role"]
                session["cart"]  = {}
                return redirect(url_for("home"))
            error = "Invalid email or password."
    return render_template_string("""<!doctype html><html><head><meta charset="utf-8">
<title>Login - QuickKart</title>""" + CSS + """</head><body>
<div class="auth-wrap">
  <div class="auth-left">
    <div style="font-size:3rem;margin-bottom:12px">&#9889;</div>
    <h2>QuickKart</h2>
    <p>India's fastest grocery delivery. Fresh essentials at your doorstep in minutes.</p>
    <div class="auth-features">
      <div class="auth-feature">&#128640; Delivery in 10-20 minutes</div>
      <div class="auth-feature">&#127881; Exclusive member offers</div>
      <div class="auth-feature">&#128100; 100+ product categories</div>
      <div class="auth-feature">&#128274; Secure Gmail login</div>
    </div>
  </div>
  <div class="auth-right">
    <div class="auth-box">
      <h1>Welcome back!</h1>
      <p class="sub">Sign in to your QuickKart account</p>
      {% if error %}<div class="flash error">{{ error }}</div>{% endif %}
      <form method="POST">
        <div class="form-group">
          <label>Gmail Address</label>
          <input type="email" name="email" placeholder="you@gmail.com" required autocomplete="email">
        </div>
        <div class="form-group">
          <label>Password</label>
          <input type="password" name="password" placeholder="Your password" required>
        </div>
        <button class="btn btn-green btn-full" style="margin-top:4px">Sign In &#8594;</button>
      </form>
      <p style="text-align:center;margin-top:20px;font-size:.85rem;color:var(--muted)">
        New to QuickKart? <a href="/register" style="color:var(--g1);font-weight:800">Create account</a>
      </p>
    </div>
  </div>
</div>
</body></html>""", error=error)


# ── REGISTER ──────────────────────────────────────────────
@app.route("/register", methods=["GET", "POST"])
def register():
    if "user" in session:
        return redirect(url_for("home"))
    error = ""
    if request.method == "POST":
        name    = request.form.get("name", "").strip()
        email   = request.form.get("email", "").strip().lower()
        pw      = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if not all([name, email, pw, confirm]):
            error = "All fields are required."
        elif not re.fullmatch(r"[a-zA-Z0-9._%+\-]+@gmail\.com", email):
            error = "Only @gmail.com addresses are accepted."
        elif len(pw) < 6:
            error = "Password must be at least 6 characters."
        elif pw != confirm:
            error = "Passwords do not match."
        else:
            db = get_db()
            if db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
                error = "This Gmail is already registered."
            else:
                mobile_num = re.sub(r"\D", "", request.form.get("mobile", "").strip())[:10]
                db.execute(
                    "INSERT INTO users(email,username,password,role,mobile) VALUES(?,?,?,?,?)",
                    (email, name, generate_password_hash(pw), "user", mobile_num)
                )
                db.commit()
                flash("Account created! Please log in.", "success")
                return redirect(url_for("login"))
    return render_template_string("""<!doctype html><html><head><meta charset="utf-8">
<title>Register - QuickKart</title>""" + CSS + """</head><body>
<div class="auth-wrap">
  <div class="auth-left">
    <div style="font-size:3rem;margin-bottom:12px">&#9889;</div>
    <h2>Join QuickKart!</h2>
    <p>Create your account and enjoy groceries delivered in 10-20 minutes with exclusive member deals.</p>
    <div class="auth-features">
      <div class="auth-feature">&#127881; 20% OFF your first order</div>
      <div class="auth-feature">&#128020; Free delivery on orders above &#8377;299</div>
      <div class="auth-feature">&#129302; 24/7 AI customer support</div>
    </div>
  </div>
  <div class="auth-right">
    <div class="auth-box">
      <h1>Create Account</h1>
      <p class="sub">Sign up with your Gmail to get started</p>
      {% if error %}<div class="flash error">{{ error }}</div>{% endif %}
      <form method="POST">
        <div class="form-group">
          <label>Full Name</label>
          <input name="name" placeholder="Your full name" required>
        </div>
        <div class="form-group">
          <label>Gmail Address</label>
          <input type="email" name="email" placeholder="yourname@gmail.com" required>
          <div class="gmail-hint">&#9888;&#65039; Only @gmail.com is accepted</div>
        </div>
        <div class="form-group">
          <label>Mobile Number</label>
          <input type="tel" name="mobile" placeholder="10-digit mobile number" maxlength="10" pattern="[0-9]{10}">
          <div class="gmail-hint">&#128242; Used for order OTP confirmation</div>
        </div>
        <div class="form-group">
          <label>Password</label>
          <input type="password" name="password" placeholder="Min 6 characters" required>
        </div>
        <div class="form-group">
          <label>Confirm Password</label>
          <input type="password" name="confirm" placeholder="Repeat password" required>
        </div>
        <button class="btn btn-green btn-full" style="margin-top:4px">Create Account &#8594;</button>
      </form>
      <p style="text-align:center;margin-top:20px;font-size:.85rem;color:var(--muted)">
        Already have an account? <a href="/login" style="color:var(--g1);font-weight:800">Login</a>
      </p>
    </div>
  </div>
</div>
</body></html>""", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── HOME ──────────────────────────────────────────────────
@app.route("/")
@login_required
def home():
    db   = get_db()
    cat  = request.args.get("cat", "All")
    q    = request.args.get("q", "").strip()
    query  = "SELECT * FROM products WHERE 1=1"
    params = []
    if cat != "All":
        query += " AND category=?"; params.append(cat)
    if q:
        query += " AND name LIKE ?"; params.append(f"%{q}%")
    query += " ORDER BY category, name"
    products = db.execute(query, params).fetchall()
    cart     = session.get("cart", {})
    grouped  = {}
    for p in products:
        c = p["category"]
        if c not in grouped:
            grouped[c] = []
        grouped[c].append({
            "id": p["id"], "name": p["name"], "price": p["price"],
            "image": p["image"], "qty": cart.get(str(p["id"]), 0),
        })
    cat_tabs  = [{"name": c, "icon": CATEGORY_ICONS.get(c, "")} for c in CATEGORIES]
    role      = session.get("role", "")
    user      = session.get("user", "")
    count     = sum(cart.values())
    role_badge = ""
    if role == "admin":
        role_badge = '<span class="role-badge admin-badge">ADMIN</span>'
    elif role == "delivery":
        role_badge = '<span class="role-badge delivery-badge">DELIVERY</span>'
    cb = (f'<span style="background:var(--g1);color:#fff;border-radius:4px;padding:1px 7px;font-size:.72rem;margin-left:auto">{count}</span>') if count else ""
    nav_user_html = f"""<div class="nav-user" onclick="toggleUserDropdown(event)" id="user-menu-btn">
  &#128100; {user} {role_badge}
  <span class="chevron" id="user-chevron">&#9660;</span>
  <div class="user-dropdown" id="user-dropdown">
    <div class="dropdown-header">
      <div class="dh-name">&#128100; {user}</div>
      <div class="dh-role">{role.capitalize() if role else 'Customer'}</div>
    </div>
    <a href="/orders" class="dropdown-item"><span class="di-icon">&#128230;</span> My Orders</a>
    <a href="/cart" class="dropdown-item"><span class="di-icon">&#128722;</span> My Cart {cb}</a>
    <div class="dropdown-divider"></div>
    <a href="/logout" class="dropdown-item danger"><span class="di-icon">&#128682;</span> Logout</a>
  </div>
</div>
<script>
function toggleUserDropdown(e){{
  e.stopPropagation();
  var d=document.getElementById('user-dropdown'),c=document.getElementById('user-chevron');
  d.classList.toggle('open');
  c.style.transform=d.classList.contains('open')?'rotate(180deg)':'';
}}
document.addEventListener('click',function(){{
  var d=document.getElementById('user-dropdown'),c=document.getElementById('user-chevron');
  if(d){{d.classList.remove('open');if(c)c.style.transform='';}}
}});
</script>"""
    nav_links_html = ""
    if role == "admin":
        nav_links_html += '<a href="/admin" class="nav-pill-admin">&#9881; Admin</a>'
    if role in ("admin", "delivery"):
        nav_links_html += '<a href="/delivery" class="nav-pill-delivery">&#128693; Delivery</a>'

    tmpl = """<!doctype html><html><head>
<meta charset="utf-8"><title>QuickKart - Shop</title>""" + CSS + """
</head><body>
<nav class="nav">
  <a href="/" class="brand">quick<span>kart</span></a>
  <div class="nav-location">
    <span class="deliver-label">Delivery in 10-20 mins</span>
    <span class="address">&#128205; Your Location &#9660;</span>
  </div>
  <div class="nav-search">
    <span class="search-icon">&#128269;</span>
    <form method="GET" style="width:100%">
      {% if cat != 'All' %}<input type="hidden" name="cat" value="{{ cat }}">{% endif %}
      <input name="q" value="{{ q }}" placeholder='Search "egg"' autocomplete="off">
    </form>
  </div>
  <div class="nav-right">
    {{ nav_user|safe }}
    {{ nav_links|safe }}
    <a href="/cart" class="cart-btn">
      &#128722; My Cart
      {% if cart_count %}<span class="cart-badge">{{ cart_count }}</span>{% endif %}
    </a>
  </div>
</nav>
{{ flashes|safe }}
<div class="bk-hero-wrap">
  <div class="bk-hero-main">
    <div class="bk-hero-text">
      <div class="hero-eyebrow">&#9889; 10-minute delivery</div>
      <h1>Your groceries,<br><em>instantly delivered</em></h1>
      <p>Fresh produce, dairy, snacks &amp; 500+ essentials &mdash;<br>at your door in minutes.</p>
      <a href="/?cat=Fruits+%26+Vegetables" class="bk-hero-btn">Shop Now &#8594;</a>
      <div class="hero-stats">
        <div class="hero-stat"><span class="hs-val">500+</span><span class="hs-lbl">Products</span></div>
        <div class="hero-stat"><span class="hs-val">10 min</span><span class="hs-lbl">Delivery</span></div>
        <div class="hero-stat"><span class="hs-val">Free</span><span class="hs-lbl">Above &#8377;299</span></div>
      </div>
    </div>
    <div class="bk-hero-img">
      <img src="/static/images/products/2553691.png" alt="Fresh groceries">
    </div>
  </div>
</div>
<div class="bk-promo-row">
  <div class="bk-promo" style="background:linear-gradient(135deg,#1ab69d,#0d8c7a);color:#fff">
    <div class="bk-promo-text">
      <h3>Pharmacy at<br>your doorstep!</h3>
      <p>Cough syrups, pain<br>relief sprays &amp; more</p>
      <a href="/?cat=Personal+Care" class="bk-promo-btn">Order Now &#8594;</a>
    </div>
    <img src="/static/images/products/2942055.png" class="bk-promo-img" alt="Pharmacy">
  </div>
  <div class="bk-promo" style="background:linear-gradient(135deg,#f5a623,#e8902a);color:#fff">
    <div class="bk-promo-text">
      <h3>Pet care supplies<br>at your door</h3>
      <p>Food, treats,<br>toys &amp; more</p>
      <a href="/?cat=Pet+Supplies" class="bk-promo-btn" style="color:#e8902a">Order Now &#8594;</a>
    </div>
    <img src="/static/images/products/616554.png" class="bk-promo-img" alt="Pet">
  </div>
  <div class="bk-promo" style="background:linear-gradient(135deg,#e8ecf0,#d8dde3);color:#222">
    <div class="bk-promo-text">
      <h3>No time for<br>a diaper run?</h3>
      <p>Get baby care<br>essentials</p>
      <a href="/?cat=Baby+%26+Kids" class="bk-promo-btn">Order Now &#8594;</a>
    </div>
    <img src="/static/images/products/3069172.png" class="bk-promo-img" alt="Baby">
  </div>
</div>
<div class="bk-cats-wrap">
  <h2 class="bk-cats-title">&#128717; Shop by Category</h2>
  <div class="bk-cats-scroll">
    <a href="/" class="bk-cat-chip {% if cat == 'All' %}active{% endif %}">
      <span class="cat-emoji">&#128722;</span><span class="cat-label">All</span>
    </a>
    {% for tab in cat_tabs %}
    <a href="/?cat={{ tab.name|urlencode }}" class="bk-cat-chip {% if cat == tab.name %}active{% endif %}">
      <span class="cat-emoji">{{ tab.icon }}</span>
      <span class="cat-label">{{ tab.name.split(' &')[0][:12] }}</span>
    </a>
    {% endfor %}
  </div>
</div>
{% if not grouped %}
<div style="max-width:1280px;margin:40px auto;padding:0 20px;text-align:center">
  <div style="font-size:3rem;margin-bottom:12px">&#128717;</div>
  <p style="color:var(--muted);font-weight:600">No products found for "{{ q }}"</p>
  <a href="/" class="btn btn-yellow" style="margin-top:16px;display:inline-block">Clear search</a>
</div>
{% endif %}
{% for category, items in grouped.items() %}
<div class="bk-section">
  <div class="bk-section-hd">
    <div class="bk-section-title">{{ icons.get(category, "&#128230;") }} {{ category }}</div>
    <a href="/?cat={{ category|urlencode }}" class="bk-see-all">See all &#8250;</a>
  </div>
  <div class="grid">
    {% for p in items %}
    <div class="card">
      {% if loop.index <= 2 %}<div class="card-badge">BEST</div>{% endif %}
      <div class="card-img-wrap">
        <img src="{{ p.image }}" alt="{{ p.name }}">
      </div>
      <div class="card-name">{{ p.name }}</div>
      <div class="card-price">&#8377;{{ p.price }}</div>
      {% if p.qty > 0 %}
      <div class="qty-ctrl" id="qc-{{ p.id }}">
        <button class="qty-btn" data-pid="{{ p.id }}" data-action="remove">&#8722;</button>
        <span class="qty-num">{{ p.qty }}</span>
        <button class="qty-btn" data-pid="{{ p.id }}" data-action="add">+</button>
      </div>
      {% else %}
      <div id="qc-{{ p.id }}">
        <button class="add-btn" data-pid="{{ p.id }}" data-action="add">+ Add</button>
      </div>
      {% endif %}
    </div>
    {% endfor %}
  </div>
</div>
{% endfor %}
{{ chat|safe }}
<script>
function cartUpdate(pid, action, btn) {
  if (btn) { btn.disabled = true; btn.style.opacity = '0.6'; }
  fetch('/cart/update', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({pid: pid, action: action})
  })
  .then(function(r) {
    if (r.status === 401) { window.location = '/login'; return null; }
    if (!r.ok) { throw new Error('Server error ' + r.status); }
    return r.json();
  })
  .then(function(data) {
    if (!data) return;
    if (!data.ok) { window.location = '/login'; return; }
    var qty  = data.qty;
    var wrap = document.getElementById('qc-' + pid);
    if (!wrap) return;
    if (qty > 0) {
      wrap.className = 'qty-ctrl';
      wrap.innerHTML =
        '<button class="qty-btn" data-pid="' + pid + '" data-action="remove">&#8722;</button>' +
        '<span class="qty-num">' + qty + '</span>' +
        '<button class="qty-btn" data-pid="' + pid + '" data-action="add">+</button>';
    } else {
      wrap.className = '';
      wrap.innerHTML = '<button class="add-btn" data-pid="' + pid + '" data-action="add">+ Add</button>';
    }
    document.querySelectorAll('.cart-badge').forEach(function(b) {
      b.textContent = data.cart_total || 0;
    });
  })
  .catch(function(err) {
    console.error('Cart error:', err);
    if (btn) { btn.disabled = false; btn.style.opacity = ''; }
  });
}

// Single event delegation listener — works for initial AND dynamically-inserted buttons
document.addEventListener('click', function(e) {
  var btn = e.target.closest('[data-pid][data-action]');
  if (!btn) return;
  e.preventDefault();
  e.stopPropagation();
  var pid    = parseInt(btn.getAttribute('data-pid'), 10);
  var action = btn.getAttribute('data-action');
  cartUpdate(pid, action, btn);
});
</script>
</body></html>"""
    return render_template_string(tmpl,
        nav_user=nav_user_html, nav_links=nav_links_html,
        flashes=get_flashes(), chat=CHAT_WIDGET,
        grouped=grouped, cat_tabs=cat_tabs, icons=CATEGORY_ICONS,
        cat=cat, q=q, cart_count=count)


# ── CART add/remove ───────────────────────────────────────
@app.route("/cart/add/<int:pid>")
@login_required
def cart_add(pid):
    db = get_db()
    if not db.execute("SELECT id FROM products WHERE id=?", (pid,)).fetchone():
        flash("Product not found.", "error")
        return redirect(url_for("home"))
    cart = dict(session.get("cart", {}))
    cart[str(pid)] = cart.get(str(pid), 0) + 1
    session["cart"] = cart
    session.modified = True
    return redirect(request.referrer or url_for("home"))


@app.route("/cart/remove/<int:pid>")
@login_required
def cart_remove(pid):
    cart = dict(session.get("cart", {}))
    key  = str(pid)
    if key in cart:
        cart[key] -= 1
        if cart[key] <= 0:
            del cart[key]
    session["cart"] = cart
    session.modified = True
    return redirect(request.referrer or url_for("cart_page"))


# ── CART JSON API (no page reload) ───────────────────────
@app.route("/cart/update", methods=["POST"])
@login_required
def cart_update():
    data   = request.get_json(force=True) or {}
    pid    = str(data.get("pid", ""))
    action = data.get("action", "add")
    db     = get_db()
    if not db.execute("SELECT id FROM products WHERE id=?", (pid,)).fetchone():
        return jsonify({"ok": False, "error": "not found"}), 404
    cart = dict(session.get("cart", {}))
    if action == "add":
        cart[pid] = cart.get(pid, 0) + 1
    elif action == "remove":
        if pid in cart:
            cart[pid] -= 1
            if cart[pid] <= 0:
                del cart[pid]
    session["cart"] = cart
    session.modified = True
    return jsonify({"ok": True, "qty": cart.get(pid, 0),
                    "cart_total": sum(cart.values())})


# ── CART PAGE ─────────────────────────────────────────────
@app.route("/cart")
@login_required
def cart_page():
    db    = get_db()
    cart  = session.get("cart", {})
    items, total = [], 0
    for pid_str, qty in cart.items():
        p = db.execute("SELECT * FROM products WHERE id=?", (pid_str,)).fetchone()
        if p:
            sub = p["price"] * qty
            items.append({"id": p["id"], "name": p["name"], "price": p["price"],
                          "qty": qty, "sub": sub, "image": p["image"]})
            total += sub
    delivery = 0 if total >= 299 else 30
    return render_template_string("""<!doctype html><html><head>
<meta charset="utf-8"><title>Cart - QuickKart</title>""" + CSS + """
</head><body>
{{ nav|safe }}{{ flashes|safe }}
<div style="max-width:1200px;margin:0 auto;padding:20px 20px">
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:20px">
    <h2 style="font-size:1.3rem;font-weight:900;letter-spacing:-.3px">My Cart</h2>
    {% if items %}<span style="background:var(--surface2);color:var(--text3);border-radius:6px;padding:2px 9px;font-size:.76rem;font-weight:700;border:1px solid var(--border)">{{ items|length }} item{{ 's' if items|length != 1 }}</span>{% endif %}
  </div>
  {% if not items %}
  <div style="text-align:center;padding:72px 20px;background:var(--card);border-radius:16px;border:1px solid var(--border)">
    <div style="font-size:3rem;margin-bottom:14px;opacity:.5">&#128722;</div>
    <div style="font-size:1.05rem;font-weight:800;margin-bottom:6px;color:var(--text)">Your cart is empty</div>
    <div style="font-size:.84rem;color:var(--text3);margin-bottom:24px">Looks like you haven't added anything yet.</div>
    <a href="/" class="btn btn-yellow" style="display:inline-flex">&#9889; Start Shopping</a>
  </div>
  {% else %}
  <div class="cart-wrap" style="max-width:100%">
    <div class="cart-items">
      <!-- Free delivery banner -->
      {% if delivery > 0 %}
      <div style="background:rgba(245,200,66,.08);border:1.5px dashed rgba(245,200,66,.4);border-radius:12px;
                  padding:12px 16px;margin-bottom:14px;display:flex;align-items:center;gap:10px;font-size:.83rem;font-weight:600;color:var(--text2)">
        &#128666; Add <strong style="color:var(--accent)">&nbsp;&#8377;{{ 299 - total }}&nbsp;</strong> more for <strong style="color:var(--green)">&nbsp;FREE delivery</strong>
      </div>
      {% else %}
      <div style="background:rgba(34,197,94,.08);border:1px solid rgba(34,197,94,.22);border-radius:12px;
                  padding:12px 16px;margin-bottom:14px;display:flex;align-items:center;gap:8px;font-size:.83rem;font-weight:600;color:var(--green)">
        &#9989; You've unlocked <strong>FREE delivery!</strong>
      </div>
      {% endif %}

      {% for i in items %}
      <div class="cart-item">
        <div class="cart-item-img"><img src="{{ i.image }}" alt="{{ i.name }}"></div>
        <div style="flex:1">
          <div class="ci-name">{{ i.name }}</div>
          <div class="ci-unit">&#8377;{{ i.price }} per unit</div>
        </div>
        <div style="display:flex;align-items:center;gap:14px">
          <div class="qty-ctrl" style="width:96px">
            <button class="qty-btn" onclick="location.href='/cart/remove/{{ i.id }}'">&#8722;</button>
            <span class="qty-num">{{ i.qty }}</span>
            <button class="qty-btn" onclick="location.href='/cart/add/{{ i.id }}'">+</button>
          </div>
          <div class="ci-sub">&#8377;{{ i.sub }}</div>
        </div>
      </div>
      {% endfor %}
    </div>
    <div class="order-summary">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:16px;padding-bottom:12px;border-bottom:1px solid var(--border)">
        <div style="width:32px;height:32px;background:var(--y);border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:1rem">&#128230;</div>
        <div class="os-title" style="margin:0;padding:0;border:none;flex:1">Order Summary</div>
      </div>
      {% for i in items %}
      <div class="os-row">
        <span style="color:var(--muted);font-size:.82rem">{{ i.name }} x{{ i.qty }}</span>
        <span style="font-weight:700">&#8377;{{ i.sub }}</span>
      </div>
      {% endfor %}
      <div style="height:1px;background:var(--border);margin:10px 0"></div>
      <div class="coupon-box">
        <input class="coupon-input" id="coupon-input" placeholder="&#127987; Coupon code" oninput="this.value=this.value.toUpperCase()">
        <button class="coupon-btn" onclick="applyCoupon()">Apply</button>
      </div>
      <div id="coupon-msg" style="font-size:.78rem;font-weight:600;margin:-6px 0 8px;display:none"></div>
      <script>
      var COUPONS = {
        'FIRST20': {label:'20% OFF on first order', pct:20},
        'FREEDEL': {label:'Free delivery on orders ₹299+', pct:0, freedel:true},
        'DAIRY31': {label:'Buy 2 Get 1 on dairy', pct:0},
        'SAVE50':  {label:'Save ₹50 on orders ₹500+', flat:50},
        'WKND10':  {label:'10% off this weekend', pct:10}
      };
      var appliedCoupon = null;
      function applyCoupon(){
        var code = document.getElementById('coupon-input').value.trim().toUpperCase();
        var msg = document.getElementById('coupon-msg');
        msg.style.display = 'block';
        if(!code){ msg.style.color='#f87171'; msg.textContent='Please enter a coupon code.'; return; }
        if(!COUPONS[code]){ msg.style.color='#f87171'; msg.textContent='Invalid coupon code. Try: FIRST20, FREEDEL, SAVE50, WKND10'; return; }
        appliedCoupon = code;
        msg.style.color='#22c55e';
        msg.textContent='✓ Coupon "'+code+'" applied — '+COUPONS[code].label+'!';
      }
      </script>
      <div class="os-row"><span style="color:var(--muted)">Subtotal</span><span style="font-weight:700">&#8377;{{ total }}</span></div>
      <div class="os-row">
        <span style="color:var(--muted)">Delivery</span>
        <span>{% if delivery == 0 %}<span class="green" style="color:var(--green);font-weight:700">FREE</span>{% else %}<span style="font-weight:700;color:var(--text)">&#8377;{{ delivery }}</span>{% endif %}</span>
      </div>
      <div class="os-row total">
        <span style="font-weight:900">Total</span>
        <span style="font-size:1.1rem;font-weight:900">&#8377;{{ total + delivery }}</span>
      </div>
      <button class="btn btn-yellow btn-full" style="margin-top:16px;font-size:.9rem;padding:14px" onclick="location.href='/checkout'">
        Proceed to Checkout &#8594;
      </button>
      <button class="btn btn-gray btn-full" style="margin-top:8px;font-size:.85rem" onclick="location.href='/'">
        &#8592; Continue Shopping
      </button>
      <div style="margin-top:14px;padding:10px 14px;background:var(--surface2);border-radius:9px;
                  font-size:.72rem;color:var(--text3);display:flex;align-items:center;gap:6px;font-weight:600;border:1px solid var(--border)">
        &#128274; Safe & secure checkout
      </div>
    </div>
  </div>
  {% endif %}
</div>
{{ chat|safe }}
</body></html>""", nav=nav_html(), flashes=get_flashes(), chat=CHAT_WIDGET,
        items=items, total=total, delivery=delivery)


# ── CHECKOUT ──────────────────────────────────────────────
@app.route("/checkout", methods=["GET", "POST"])
@login_required
def checkout():
    cart = session.get("cart", {})
    if not cart:
        flash("Your cart is empty.", "error")
        return redirect(url_for("cart_page"))
    db = get_db()
    items, total = [], 0
    for pid_str, qty in cart.items():
        p = db.execute("SELECT * FROM products WHERE id=?", (pid_str,)).fetchone()
        if p:
            sub = p["price"] * qty
            items.append({"id": p["id"], "name": p["name"], "price": p["price"],
                          "qty": qty, "sub": sub})
            total += sub
    delivery = 0 if total >= 299 else 30
    grand    = total + delivery
    row      = db.execute("SELECT mobile FROM users WHERE email=?", (session["email"],)).fetchone()
    user_mobile = (row["mobile"] or "").strip() if row else ""

    if request.method == "POST":
        step    = request.form.get("step", "address")
        address = request.form.get("address", "").strip()
        method  = request.form.get("method", "cod")
        mobile  = re.sub(r"\D", "", request.form.get("mobile", user_mobile).strip())[:10]

        if step == "verify":
            entered = request.form.get("otp", "").strip()
            key     = session.get("otp_key", "")
            store   = _OTP_STORE.get(key, {})
            if not store or time.time() > store.get("expires", 0):
                flash("OTP expired. Please try again.", "error")
                _OTP_STORE.pop(key, None)
                session.pop("otp_key", None)
                return redirect(url_for("checkout"))
            if entered != store["otp"]:
                flash("Invalid OTP. Please try again.", "error")
                return redirect(url_for("checkout_verify"))
            payload = store["payload"]
            _OTP_STORE.pop(key, None)
            session.pop("otp_key", None)
            oid = db.execute(
                "INSERT INTO orders(user_email,address,total,status) VALUES(?,?,?,?)",
                (session["email"], payload["address"], payload["grand"], "placed")
            ).lastrowid
            for i in payload["items"]:
                db.execute(
                    "INSERT INTO order_items(order_id,product_id,qty,price) VALUES(?,?,?,?)",
                    (oid, i["id"], i["qty"], i["price"])
                )
            db.commit()
            session["cart"] = {}
            _send_email(
                session["email"],
                f"QuickKart – Order #{oid} Confirmed! 🎉",
                _order_email_html(
                    oid, session["user"], payload["items"],
                    payload["total"], payload["delivery"], payload["grand"],
                    payload["address"], payload["method"]
                )
            )
            flash(f"Order #{oid} placed! Estimated delivery: 10-20 mins. 🚀", "success")
            return redirect(url_for("orders_page"))

        if not address:
            flash("Please enter delivery address.", "error")
            return redirect(url_for("checkout"))

        # For PayPal: store payload in OTP store then redirect to PayPal directly
        if method == "paypal":
            key = f"{session['email']}_{int(time.time())}"
            _OTP_STORE[key] = {
                "otp":     None,
                "expires": time.time() + 600,
                "payload": {
                    "address": address, "method": method,
                    "items":   items,   "total":  total,
                    "delivery":delivery,"grand":  grand,
                    "mobile":  mobile,
                }
            }
            session["otp_key"] = key
            return redirect(url_for("paypal_create"), code=307)

        otp = _generate_otp()
        key = f"{session['email']}_{int(time.time())}"
        _OTP_STORE[key] = {
            "otp":     otp,
            "expires": time.time() + 300,
            "payload": {
                "address": address, "method": method,
                "items":   items,   "total":  total,
                "delivery":delivery,"grand":  grand,
                "mobile":  mobile,
            }
        }
        session["otp_key"] = key
        sent_sms   = _send_sms(mobile, f"QuickKart OTP: {otp}. Valid 5 mins. Do not share.")
        sent_email = _send_email(
            session["email"],
            "QuickKart – Your Order OTP",
            f"<h2>Your QuickKart OTP is: <strong>{otp}</strong></h2>"
            f"<p>Valid for 5 minutes. Do not share this code.</p>"
        )
        delivery_info = []
        if sent_sms:   delivery_info.append(f"SMS to *****{mobile[-4:]}")
        if sent_email: delivery_info.append("email")
        via = " & ".join(delivery_info) if delivery_info else "console (configure SMTP/SMS)"
        flash(f"OTP sent via {via}. Enter it below to confirm your order.", "info")
        return redirect(url_for("checkout_verify"))

    return render_template_string("<!doctype html><html><head>"
        r'<meta charset="utf-8">' + "<title>Checkout - QuickKart</title>" + CSS + """
</head><body>
{{ nav|safe }}{{ flashes|safe }}
<div style="max-width:860px;margin:24px auto;padding:0 16px">
  <h2 style="font-size:1.4rem;font-weight:900;margin-bottom:20px">&#128179; Checkout</h2>
  <div style="display:grid;grid-template-columns:1fr 320px;gap:20px">
    <div>
      <div style="background:var(--card);border-radius:16px;padding:22px;box-shadow:var(--shadow);margin-bottom:16px">
        <h3 style="font-weight:900;margin-bottom:16px;color:var(--text)">&#128205; Delivery Address</h3>
        <form method="POST" id="checkout-form">
          <input type="hidden" name="step" value="address">
          <div class="form-group" style="margin-bottom:12px">
            <label style="font-size:.82rem;font-weight:700;color:var(--text2);display:block;margin-bottom:5px">
              Mobile Number (for OTP)
            </label>
            <input type="tel" name="mobile" value="{{ user_mobile }}" placeholder="10-digit mobile number"
              maxlength="10" pattern="[0-9]{10}"
              style="width:100%;padding:11px 14px;border:1.5px solid var(--border);border-radius:10px;
                     font-family:inherit;font-size:.9rem;outline:none">
            <div style="font-size:.74rem;color:var(--muted);margin-top:4px">&#128242; An OTP will be sent here to confirm your order</div>
          </div>
          <div class="form-group" style="margin:0">
            <textarea name="address" rows="3" required
              placeholder="House no., Street, Area, City, PIN code"
              style="width:100%;padding:12px;border:1.5px solid var(--border);border-radius:12px;
                     font-family:inherit;font-size:.9rem;resize:vertical;outline:none"></textarea>
          </div>
        </form>
      </div>
      <div style="background:var(--card);border-radius:16px;padding:22px;box-shadow:var(--shadow)">
        <h3 style="font-weight:900;margin-bottom:16px;color:var(--text)">&#128179; Payment Method</h3>
        <!-- COD -->
        <label id="lbl-cod" style="display:flex;align-items:center;gap:10px;padding:13px 16px;
          border:2px solid var(--g1);border-radius:12px;cursor:pointer;margin-bottom:10px;background:rgba(12,131,31,.12)">
          <input type="radio" name="method" value="cod" form="checkout-form" checked id="radio-cod">
          <span style="font-size:1.2rem">&#128181;</span>
          <div>
            <div style="font-weight:800;font-size:.9rem;color:var(--text)">Cash on Delivery</div>
            <div style="font-size:.74rem;color:var(--muted)">Pay when your order arrives</div>
          </div>
        </label>
        <!-- PayPal -->
        <label id="lbl-paypal" style="display:flex;align-items:center;gap:10px;padding:13px 16px;
          border:1.5px solid var(--border);border-radius:12px;cursor:pointer;margin-bottom:10px">
          <input type="radio" name="method" value="paypal" form="checkout-form" id="radio-paypal">
          <img src="/static/images/paypal/paypal-logo.png" alt="PayPal" style="width:28px;height:28px;object-fit:contain">
          <div style="flex:1">
            <div style="font-weight:800;font-size:.9rem;color:var(--text)">PayPal</div>
            <div style="font-size:.74rem;color:var(--muted)">Pay securely via PayPal</div>
          </div>
        </label>
        <div id="paypal-panel" style="display:none;margin:-4px 0 10px;padding:16px;
          background:rgba(0,48,135,.15);border-radius:14px;border:1.5px solid rgba(0,48,135,.3)">
          <div style="font-size:.84rem;color:var(--text2);font-weight:600">&#128178; You will be redirected to PayPal to complete your payment securely.</div>
        </div>
      </div>
      <script>
      const payMethods = ['cod','paypal'];
      const borderColors = {cod:'var(--g1)',paypal:'#003087'};
      const bgColors = {cod:'rgba(12,131,31,.12)',paypal:'rgba(0,48,135,.15)'};
      function selectPayment(m){
        payMethods.forEach(function(p){
          var lbl=document.getElementById('lbl-'+p);
          var panel=document.getElementById(p+'-panel');
          if(lbl){lbl.style.border=(p===m?'2px solid '+borderColors[m]:'1.5px solid var(--border)');
            lbl.style.background=(p===m?bgColors[m]:'');}
          if(panel){panel.style.display=(p===m?'block':'none');}
        });
        var r=document.getElementById('radio-'+m);if(r)r.checked=true;
      }
      payMethods.forEach(function(m){
        var r=document.getElementById('radio-'+m);
        if(r)r.addEventListener('change',function(){selectPayment(m);});
      });
      </script>
    </div>
    <div class="order-summary">
      <div class="os-title">Order Summary</div>
      {% for i in items %}
      <div class="os-row">
        <span style="color:var(--muted);font-size:.82rem">{{ i.name }} x{{ i.qty }}</span>
        <span>&#8377;{{ i.sub }}</span>
      </div>
      {% endfor %}
      <div class="os-row"><span>Subtotal</span><span>&#8377;{{ total }}</span></div>
      <div class="os-row"><span>Delivery</span>
        <span>{% if delivery == 0 %}<span class="green">FREE</span>{% else %}&#8377;{{ delivery }}{% endif %}</span>
      </div>
      <div class="os-row total"><span>Total</span><span class="green">&#8377;{{ grand }}</span></div>
      <button type="submit" form="checkout-form" class="btn btn-green btn-full" style="margin-top:16px">
        Place Order &#8594;
      </button>
    </div>
  </div>
</div>
{{ chat|safe }}
</body></html>""", nav=nav_html(), flashes=get_flashes(), chat=CHAT_WIDGET,
        items=items, total=total, delivery=delivery, grand=grand,
        user_mobile=user_mobile)


# ── OTP VERIFY PAGE ───────────────────────────────────────
@app.route("/checkout/verify", methods=["GET", "POST"])
@login_required
def checkout_verify():
    key   = session.get("otp_key", "")
    store = _OTP_STORE.get(key, {})
    if not store:
        flash("No pending order. Please start checkout again.", "error")
        return redirect(url_for("checkout"))
    if time.time() > store.get("expires", 0):
        _OTP_STORE.pop(key, None)
        session.pop("otp_key", None)
        flash("OTP expired. Please try again.", "error")
        return redirect(url_for("checkout"))
    payload = store["payload"]
    mobile  = payload.get("mobile", "")
    masked  = ("*" * max(0, len(mobile) - 4) + mobile[-4:]) if len(mobile) >= 4 else mobile
    if request.method == "POST" and request.form.get("action") == "resend":
        new_otp = _generate_otp()
        store["otp"]     = new_otp
        store["expires"] = time.time() + 300
        _send_sms(mobile, f"QuickKart OTP: {new_otp}. Valid 5 mins. Do not share.")
        _send_email(session["email"], "QuickKart – New Order OTP",
                    f"<h2>New OTP: <strong>{new_otp}</strong></h2><p>Valid 5 minutes.</p>")
        flash("New OTP sent!", "success")
    grand = payload.get("grand", 0)
    return render_template_string("""<!doctype html><html><head>
<meta charset="utf-8"><title>Verify OTP - QuickKart</title>""" + CSS + """
</head><body>
{{ nav|safe }}{{ flashes|safe }}
<div style="max-width:460px;margin:48px auto;padding:0 16px">
  <div style="background:var(--card);border-radius:20px;padding:36px 32px;box-shadow:var(--shadow-lg);text-align:center;border:1px solid var(--border)">
    <div style="font-size:3rem;margin-bottom:12px">&#128242;</div>
    <h2 style="font-size:1.3rem;font-weight:900;margin-bottom:8px;color:var(--text)">Verify Your Order</h2>
    <p style="color:var(--muted);font-size:.88rem;margin-bottom:6px">
      An OTP has been sent to your mobile
      {% if masked %}<strong>{{ masked }}</strong>{% endif %} and email.
    </p>
    <p style="font-size:.8rem;color:var(--muted);margin-bottom:24px">
      Order total: <strong style="color:var(--g1)">&#8377;{{ grand }}</strong>
    </p>
    <form method="POST" action="/checkout">
      <input type="hidden" name="step" value="verify">
      <input type="hidden" name="address" value="{{ address }}">
      <input type="hidden" name="method"  value="{{ method }}">
      <div style="margin-bottom:20px">
        <input name="otp" type="text" inputmode="numeric" autocomplete="one-time-code"
          maxlength="6" placeholder="— — — — — —"
          style="width:100%;padding:16px;text-align:center;font-size:1.6rem;letter-spacing:12px;
                 font-weight:900;border:2px solid var(--border);border-radius:14px;
                 font-family:monospace;outline:none;transition:border .2s;
                 background:var(--surface2);color:var(--text)"
          onfocus="this.style.borderColor='var(--g1)'"
          onblur="this.style.borderColor='var(--border)'" required autofocus>
      </div>
      <button type="submit" class="btn btn-green btn-full" style="font-size:1rem;padding:14px">
        &#9989; Confirm Order
      </button>
    </form>
    <form method="POST" action="/checkout/verify" style="margin-top:14px">
      <input type="hidden" name="action" value="resend">
      <button type="submit" class="btn btn-gray btn-full" style="font-size:.85rem">
        &#128260; Resend OTP
      </button>
    </form>
    <p style="margin-top:16px;font-size:.75rem;color:var(--muted)">
      &#128274; OTP expires in 5 minutes. Do not share it.
    </p>
    <a href="/checkout" style="display:block;margin-top:10px;font-size:.8rem;color:var(--muted)">
      &#8592; Back to Checkout
    </a>
  </div>
</div>
{{ chat|safe }}
</body></html>""", nav=nav_html(), flashes=get_flashes(), chat=CHAT_WIDGET,
        masked=masked, grand=grand,
        address=payload.get("address", ""), method=payload.get("method", "cod"))


# ── PAYPAL: Create Payment ─────────────────────────────────
@app.route("/paypal/create", methods=["POST"])
@login_required
def paypal_create():
    key   = session.get("otp_key", "")
    store = _OTP_STORE.get(key, {})
    if not store:
        flash("Session expired. Please start checkout again.", "error")
        return redirect(url_for("checkout"))
    payload  = store["payload"]
    grand    = payload["grand"]
    payment = paypalrestsdk.Payment({
        "intent": "sale",
        "payer":  {"payment_method": "paypal"},
        "redirect_urls": {
            "return_url": url_for("paypal_success", _external=True),
            "cancel_url": url_for("paypal_cancel",  _external=True),
        },
        "transactions": [{
            "amount": {
                "total":    str(grand),
                "currency": "USD",
            },
            "description": f"QuickKart Order — {session['user']}",
        }]
    })
    if payment.create():
        session["paypal_payment_id"] = payment.id
        for link in payment.links:
            if link.rel == "approval_url":
                return redirect(link.href)
    flash("PayPal payment could not be initiated. Please try again.", "error")
    return redirect(url_for("checkout"))


# ── PAYPAL: Success Callback ───────────────────────────────
@app.route("/paypal/success")
@login_required
def paypal_success():
    payment_id = request.args.get("paymentId")
    payer_id   = request.args.get("PayerID")
    if not payment_id or not payer_id:
        flash("PayPal payment was not completed.", "error")
        return redirect(url_for("checkout"))

    payment = paypalrestsdk.Payment.find(payment_id)
    if payment.execute({"payer_id": payer_id}):
        key   = session.get("otp_key", "")
        store = _OTP_STORE.get(key, {})
        if not store:
            flash("Session expired after PayPal payment. Contact support.", "error")
            return redirect(url_for("home"))
        payload = store["payload"]
        db  = get_db()
        oid = db.execute(
            "INSERT INTO orders(user_email,address,total,status) VALUES(?,?,?,?)",
            (session["email"], payload["address"], payload["grand"], "placed")
        ).lastrowid
        for i in payload["items"]:
            db.execute(
                "INSERT INTO order_items(order_id,product_id,qty,price) VALUES(?,?,?,?)",
                (oid, i["id"], i["qty"], i["price"])
            )
        db.commit()
        _OTP_STORE.pop(key, None)
        session.pop("otp_key", None)
        session.pop("paypal_payment_id", None)
        session["cart"] = {}
        _send_email(
            session["email"],
            f"QuickKart – Order #{oid} Confirmed! 🎉",
            _order_email_html(
                oid, session["user"], payload["items"],
                payload["total"], payload["delivery"], payload["grand"],
                payload["address"], "paypal"
            )
        )
        flash(f"Payment successful! Order #{oid} placed. Estimated delivery: 10-20 mins. 🚀", "success")
        return redirect(url_for("orders_page"))
    flash("PayPal payment execution failed. Please try again.", "error")
    return redirect(url_for("checkout"))


# ── PAYPAL: Cancel Callback ────────────────────────────────
@app.route("/paypal/cancel")
@login_required
def paypal_cancel():
    flash("PayPal payment was cancelled. You can try again.", "info")
    return redirect(url_for("checkout"))


# ── ORDERS ────────────────────────────────────────────────
@app.route("/orders")
@login_required
def orders_page():
    db     = get_db()
    orders = db.execute(
        "SELECT * FROM orders WHERE user_email=? ORDER BY placed_at DESC",
        (session["email"],)
    ).fetchall()
    oi = {}
    for o in orders:
        rows = db.execute(
            """SELECT oi.*, p.name, p.image FROM order_items oi
               JOIN products p ON oi.product_id=p.id WHERE oi.order_id=?""",
            (o["id"],)
        ).fetchall()
        oi[o["id"]] = [{"name": r["name"], "image": r["image"],
                         "qty": r["qty"], "price": r["price"]} for r in rows]
    SC = {"placed": "placed", "confirmed": "confirmed", "out_for_delivery": "out",
          "delivered": "delivered", "cancelled": "cancelled"}
    return render_template_string("""<!doctype html><html><head>
<meta charset="utf-8"><title>My Orders - QuickKart</title>""" + CSS + """
</head><body>
{{ nav|safe }}{{ flashes|safe }}
<div class="orders-wrap">
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:20px">
    <div style="width:36px;height:36px;background:#1c1c1c;border-radius:9px;display:flex;align-items:center;justify-content:center;font-size:1.1rem">&#128230;</div>
    <h2 style="font-size:1.25rem;font-weight:900;letter-spacing:-.3px">My Orders</h2>
    {% if orders %}<span style="background:var(--surface2);color:var(--text3);border-radius:6px;padding:2px 9px;font-size:.74rem;font-weight:700;border:1px solid var(--border)">{{ orders|length }}</span>{% endif %}
  </div>

  {% if not orders %}
  <div style="text-align:center;padding:72px 20px;background:var(--card);border-radius:16px;border:1px solid var(--border)">
    <div style="font-size:3rem;margin-bottom:14px;opacity:.4">&#128230;</div>
    <div style="font-size:1.05rem;font-weight:800;margin-bottom:6px;color:var(--text)">No orders yet</div>
    <div style="font-size:.82rem;color:var(--text3);margin-bottom:22px">Your order history will appear here.</div>
    <a href="/" class="btn btn-yellow" style="display:inline-flex">&#9889; Start Shopping</a>
  </div>
  {% endif %}

  {% for o in orders %}
  <div class="order-card">
    <div class="order-hd">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <span class="order-num">Order #{{ o['id'] }}</span>
        <span class="badge {{ sc.get(o['status'],'placed') }}">
          {{ o['status'].replace('_',' ').title() }}
        </span>
      </div>
      <div style="font-size:.76rem;color:var(--text3);font-weight:600">{{ o['placed_at'][:16] }}</div>
    </div>

    {% for item in oi.get(o['id'],[]) %}
    <div class="order-item-row">
      <div style="width:38px;height:38px;background:var(--surface2);border-radius:8px;display:flex;align-items:center;justify-content:center;flex-shrink:0">
        <img class="oitem-img" src="{{ item.image }}" alt="{{ item.name }}">
      </div>
      <span style="flex:1;font-weight:700;font-size:.86rem">{{ item.name }}</span>
      <span style="color:var(--text3);font-size:.8rem;font-weight:600">x{{ item.qty }}</span>
      <span style="font-weight:900;color:var(--text);margin-left:12px;font-size:.9rem">&#8377;{{ item.price * item.qty }}</span>
    </div>
    {% endfor %}

    <div style="display:flex;justify-content:space-between;align-items:center;
                margin-top:12px;padding-top:12px;border-top:1px solid var(--border)">
      <span style="font-size:.78rem;color:var(--text3);font-weight:600;max-width:60%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
        &#128205; {{ o['address'][:55] }}{% if o['address']|length > 55 %}…{% endif %}
      </span>
      <span style="font-weight:900;font-size:1rem;color:var(--text)">&#8377;{{ o['total'] }}</span>
    </div>

    {% if o['status'] == 'out_for_delivery' %}
    <div style="margin-top:10px;background:rgba(245,200,66,.08);border:1px solid rgba(245,200,66,.22);border-radius:9px;
                padding:10px 14px;font-size:.79rem;font-weight:600;color:var(--accent);display:flex;align-items:center;gap:7px">
      &#128666; Your order is on its way! Arriving soon.
    </div>
    {% elif o['status'] == 'delivered' %}
    <div style="margin-top:10px;background:rgba(34,197,94,.08);border:1px solid rgba(34,197,94,.2);border-radius:9px;
                padding:10px 14px;font-size:.79rem;font-weight:600;color:var(--green);display:flex;align-items:center;gap:7px">
      &#9989; Delivered successfully!
    </div>
    {% endif %}
  </div>
  {% endfor %}
</div>
{{ chat|safe }}
</body></html>""", nav=nav_html(), flashes=get_flashes(), chat=CHAT_WIDGET,
        orders=orders, oi=oi, sc=SC)


# ── ADMIN PANEL ───────────────────────────────────────────
@app.route("/admin")
@login_required
@admin_required
def admin():
    db = get_db()
    total_orders   = db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    total_users    = db.execute("SELECT COUNT(*) FROM users WHERE role='user'").fetchone()[0]
    total_revenue  = db.execute("SELECT COALESCE(SUM(total),0) FROM orders WHERE status!='cancelled'").fetchone()[0]
    pending        = db.execute("SELECT COUNT(*) FROM orders WHERE status='placed'").fetchone()[0]
    delivered_today= db.execute(
        "SELECT COUNT(*) FROM orders WHERE status='delivered' AND date(updated_at)=date('now')"
    ).fetchone()[0]
    orders   = db.execute(
        "SELECT o.*,u.username FROM orders o LEFT JOIN users u ON o.user_email=u.email ORDER BY o.placed_at DESC LIMIT 60"
    ).fetchall()
    users    = db.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    products = db.execute("SELECT * FROM products ORDER BY category,name").fetchall()
    agents   = db.execute("SELECT * FROM users WHERE role='delivery'").fetchall()
    SC = {"placed":"placed","confirmed":"confirmed","out_for_delivery":"out",
          "delivered":"delivered","cancelled":"cancelled"}
    agent_counts = {
        ag["username"]: db.execute(
            "SELECT COUNT(*) FROM orders WHERE delivery_by=?", (ag["username"],)
        ).fetchone()[0]
        for ag in agents
    }
    return render_template_string("""<!doctype html><html><head>
<meta charset="utf-8"><title>Admin Panel - QuickKart</title>
<link href="https://fonts.googleapis.com/css2?family=Figtree:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
""" + CSS + """
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Figtree',sans-serif!important;background:#f5f5f0}

/* ── LAYOUT ── */
.bl-shell{display:flex;min-height:100vh;background:#f5f5f0}

/* ── SIDEBAR ── */
.bl-sidebar{
  width:232px;background:#1c1c1c;display:flex;flex-direction:column;
  position:fixed;top:0;left:0;height:100vh;z-index:200;overflow-y:auto;
  transition:transform .2s
}
.bl-sb-logo{
  display:flex;align-items:center;gap:10px;padding:22px 20px 18px;
  border-bottom:1px solid rgba(255,255,255,.08)
}
.bl-sb-logo .logo-icon{
  width:36px;height:36px;background:#f7c71f;border-radius:8px;
  display:flex;align-items:center;justify-content:center;font-size:1.2rem;flex-shrink:0
}
.bl-sb-logo .logo-text{font-weight:900;font-size:1rem;color:#fff;letter-spacing:-.3px}
.bl-sb-logo .logo-text span{color:#f7c71f}
.bl-sb-logo .logo-sub{font-size:.62rem;color:rgba(255,255,255,.35);font-weight:500;letter-spacing:.5px;text-transform:uppercase;margin-top:1px}

.bl-sb-section{padding:20px 12px 8px}
.bl-sb-sec-label{
  font-size:.6rem;font-weight:700;letter-spacing:1.8px;text-transform:uppercase;
  color:rgba(255,255,255,.25);padding:0 8px;margin-bottom:8px
}
.bl-nav{
  display:flex;align-items:center;gap:10px;padding:10px 10px;border-radius:10px;
  color:rgba(255,255,255,.5);font-weight:600;font-size:.84rem;cursor:pointer;
  transition:all .14s;margin-bottom:2px;border:none;background:none;width:100%;
  text-align:left;font-family:inherit;text-decoration:none;line-height:1
}
.bl-nav:hover{background:rgba(255,255,255,.06);color:rgba(255,255,255,.85)}
.bl-nav.active{background:#f7c71f;color:#1c1c1c;font-weight:800}
.bl-nav.active .ni-badge{background:#1c1c1c;color:#f7c71f}
.bl-nav .ni-icon{font-size:.95rem;width:20px;text-align:center;flex-shrink:0}
.bl-nav .ni-count{margin-left:auto;font-size:.65rem;color:rgba(255,255,255,.3);font-weight:700}
.bl-nav .ni-badge{
  margin-left:auto;background:#ef4444;color:#fff;border-radius:6px;
  padding:1px 7px;font-size:.62rem;font-weight:800;min-width:20px;text-align:center
}
.bl-sb-divider{height:1px;background:rgba(255,255,255,.07);margin:12px 12px}
.bl-sb-footer{margin-top:auto;padding:12px;border-top:1px solid rgba(255,255,255,.07)}

/* ── MAIN ── */
.bl-main{margin-left:232px;flex:1;min-height:100vh;display:flex;flex-direction:column}
.bl-topbar{
  background:#fff;border-bottom:1px solid #e8e8e2;padding:0 28px;
  height:58px;display:flex;align-items:center;justify-content:space-between;
  position:sticky;top:0;z-index:100
}
.bl-topbar .tb-left{display:flex;flex-direction:column;gap:1px}
.bl-topbar .tb-title{font-size:1rem;font-weight:800;color:#1c1c1c;letter-spacing:-.3px}
.bl-topbar .tb-sub{font-size:.72rem;color:#9e9e8e;font-weight:500}
.bl-topbar .tb-right{display:flex;align-items:center;gap:10px}
.bl-pill{
  display:inline-flex;align-items:center;gap:5px;padding:7px 14px;border-radius:8px;
  font-size:.78rem;font-weight:700;cursor:pointer;border:none;font-family:inherit;
  transition:all .14s;text-decoration:none
}
.bl-pill-yellow{background:#f7c71f;color:#1c1c1c}.bl-pill-yellow:hover{background:#e6b800}
.bl-pill-ghost{background:#f0f0ea;color:#555;border:1px solid #e0e0d8}.bl-pill-ghost:hover{background:#e5e5de}
.bl-pill-red{background:#fff0f0;color:#c0392b;border:1px solid #ffd5d0}.bl-pill-red:hover{background:#ffe0dd}
.bl-pill-sm{padding:5px 10px;font-size:.73rem;border-radius:7px}

.bl-body{padding:24px 28px;flex:1}

/* ── KPI CARDS ── */
.kpi-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:14px;margin-bottom:24px}
.kpi{
  background:#fff;border-radius:14px;padding:20px 18px;
  border:1px solid #e8e8e2;position:relative;overflow:hidden;
  transition:transform .14s,box-shadow .14s
}
.kpi:hover{transform:translateY(-2px);box-shadow:0 8px 24px rgba(0,0,0,.08)}
.kpi .kpi-ico{
  width:40px;height:40px;border-radius:10px;display:flex;align-items:center;
  justify-content:center;font-size:1.1rem;margin-bottom:14px
}
.kpi .kpi-val{font-size:1.7rem;font-weight:900;letter-spacing:-.5px;line-height:1;color:#1c1c1c}
.kpi .kpi-lbl{font-size:.72rem;font-weight:600;color:#9e9e8e;margin-top:5px;text-transform:uppercase;letter-spacing:.5px}
.kpi .kpi-tag{
  position:absolute;top:14px;right:14px;font-size:.62rem;font-weight:800;
  padding:2px 8px;border-radius:20px;background:#f0fdf4;color:#15803d
}
.kpi-rev .kpi-ico{background:#fffbeb;color:#b45309}
.kpi-ord .kpi-ico{background:#f0fdf4;color:#15803d}
.kpi-usr .kpi-ico{background:#f0f9ff;color:#0369a1}
.kpi-pnd .kpi-ico{background:#fff7ed;color:#c2410c}
.kpi-del .kpi-ico{background:#f5f3ff;color:#6d28d9}
.kpi-rev .kpi-val{color:#b45309}
.kpi-ord .kpi-val{color:#15803d}
.kpi-pnd .kpi-val{color:#c2410c}

/* ── DATA CARD ── */
.dc{
  background:#fff;border-radius:14px;border:1px solid #e8e8e2;
  overflow:hidden;margin-bottom:20px
}
.dc-hd{
  padding:16px 20px;border-bottom:1px solid #f0f0ea;
  display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap
}
.dc-hd h3{font-size:.9rem;font-weight:800;color:#1c1c1c;letter-spacing:-.2px}
.dc-actions{display:flex;gap:8px;align-items:center}
.bl-search{
  padding:8px 13px;border:1.5px solid #e0e0d8;border-radius:9px;
  font-size:.82rem;font-family:inherit;outline:none;width:210px;background:#fafaf5;
  transition:border .14s
}
.bl-search:focus{border-color:#f7c71f;background:#fff}

/* ── TABLE ── */
.dc table{width:100%;border-collapse:collapse;font-size:.83rem}
.dc table thead tr{background:#fafaf5}
.dc table th{
  padding:10px 18px;font-size:.65rem;color:#9e9e8e;font-weight:700;
  text-transform:uppercase;letter-spacing:.8px;text-align:left;white-space:nowrap
}
.dc table td{padding:12px 18px;border-top:1px solid #f0f0ea;vertical-align:middle}
.dc table tbody tr:hover{background:#fafaf5}
.dc .tbl-scroll{overflow-x:auto}

/* ── BADGES ── */
.bs{display:inline-flex;align-items:center;padding:3px 9px;border-radius:20px;font-size:.68rem;font-weight:800;letter-spacing:.2px}
.bs-placed{background:#dbeafe;color:#1e40af}
.bs-confirmed{background:#fef3c7;color:#92400e}
.bs-out{background:#f3e8ff;color:#6b21a8}
.bs-delivered{background:#dcfce7;color:#166534}
.bs-cancelled{background:#fee2e2;color:#9b1c1c}

/* ── ROLE PILLS ── */
.rp{display:inline-block;padding:3px 9px;border-radius:20px;font-size:.66rem;font-weight:800;letter-spacing:.3px}
.rp-admin{background:#1c1c1c;color:#f7c71f}
.rp-delivery{background:#f7c71f;color:#1c1c1c}
.rp-user{background:#f0f0ea;color:#555}

/* ── AGENT CARDS ── */
.ag-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:14px}
.ag-card{
  background:#fff;border-radius:14px;padding:18px;border:1px solid #e8e8e2;
  transition:transform .14s,box-shadow .14s
}
.ag-card:hover{transform:translateY(-2px);box-shadow:0 6px 20px rgba(0,0,0,.08)}
.ag-avatar{
  width:44px;height:44px;border-radius:11px;background:#f7c71f;
  display:flex;align-items:center;justify-content:center;
  color:#1c1c1c;font-weight:900;font-size:1.1rem;flex-shrink:0
}
.ag-bar{height:5px;background:#f0f0ea;border-radius:5px;overflow:hidden;margin-top:12px}
.ag-bar-fill{height:100%;background:#f7c71f;border-radius:5px;transition:width .4s}

/* ── STATUS SELECT ── */
.bl-sel{
  padding:6px 10px;border:1.5px solid #e0e0d8;border-radius:8px;
  font-family:inherit;font-size:.78rem;font-weight:700;cursor:pointer;
  outline:none;background:#fff;transition:border .14s
}
.bl-sel:focus{border-color:#f7c71f}

/* ── TABS ── */
.bl-tab{display:none}.bl-tab.active{display:block}

/* ── EMPTY ── */
.bl-empty{text-align:center;padding:56px 24px;color:#9e9e8e}
.bl-empty .em-icon{font-size:2.8rem;margin-bottom:12px}
.bl-empty p{font-weight:600;font-size:.88rem}

/* ── CHIP ── */
.bl-chip{
  background:#f0f0ea;color:#555;border-radius:6px;
  padding:2px 8px;font-size:.72rem;font-weight:700
}

@media(max-width:900px){
  .bl-sidebar{transform:translateX(-100%)}
  .bl-main{margin-left:0}
  .kpi-grid{grid-template-columns:repeat(2,1fr)}
}
@media(max-width:560px){.kpi-grid{grid-template-columns:1fr}}
</style>
</head><body>
{{ nav|safe }}{{ flashes|safe }}
<div class="bl-shell">

  <!-- SIDEBAR -->
  <aside class="bl-sidebar">
    <div class="bl-sb-logo">
      <div class="logo-icon">&#9889;</div>
      <div>
        <div class="logo-text">Quick<span>Kart</span></div>
        <div class="logo-sub">Admin Panel</div>
      </div>
    </div>

    <div class="bl-sb-section">
      <div class="bl-sb-sec-label">Dashboard</div>
      <button class="bl-nav active" onclick="showAdminTab('overview',this)">
        <span class="ni-icon">&#9632;</span> Overview
      </button>
    </div>

    <div class="bl-sb-section">
      <div class="bl-sb-sec-label">Operations</div>
      <button class="bl-nav" onclick="showAdminTab('orders',this)">
        <span class="ni-icon">&#128230;</span> Orders
        {% if pending > 0 %}<span class="ni-badge">{{ pending }}</span>
        {% else %}<span class="ni-count">{{ total_orders }}</span>{% endif %}
      </button>
      <button class="bl-nav" onclick="showAdminTab('products',this)">
        <span class="ni-icon">&#128717;</span> Products
        <span class="ni-count">{{ products|length }}</span>
      </button>
      <button class="bl-nav" onclick="showAdminTab('users',this)">
        <span class="ni-icon">&#128101;</span> Users
        <span class="ni-count">{{ total_users }}</span>
      </button>
      <button class="bl-nav" onclick="showAdminTab('agents',this)">
        <span class="ni-icon">&#128693;</span> Delivery Agents
        <span class="ni-count">{{ agents|length }}</span>
      </button>
    </div>

    <div class="bl-sb-divider"></div>

    <div style="padding:0 12px 12px">
      <a href="/" class="bl-nav"><span class="ni-icon">&#127968;</span> View Store</a>
      <a href="/delivery" class="bl-nav"><span class="ni-icon">&#128640;</span> Delivery Panel</a>
      <a href="/logout" class="bl-nav" style="color:#f87171"><span class="ni-icon">&#128274;</span> Logout</a>
    </div>
  </aside>

  <!-- MAIN -->
  <main class="bl-main">

    <!-- ── OVERVIEW ── -->
    <div class="bl-tab active" id="tab-overview">
      <div class="bl-topbar">
        <div class="tb-left">
          <div class="tb-title">Dashboard Overview</div>
          <div class="tb-sub">Welcome back, {{ session_user }} &mdash; here's your store at a glance</div>
        </div>
        <div class="tb-right">
          <span style="font-size:.75rem;color:#9e9e8e;font-weight:600">&#128197; Live data</span>
        </div>
      </div>
      <div class="bl-body">
        <div class="kpi-grid">
          <div class="kpi kpi-rev">
            <div class="kpi-ico">&#8377;</div>
            <div class="kpi-val">&#8377;{{ revenue }}</div>
            <div class="kpi-lbl">Total Revenue</div>
            <div class="kpi-tag">&#9650; All time</div>
          </div>
          <div class="kpi kpi-ord">
            <div class="kpi-ico">&#128230;</div>
            <div class="kpi-val">{{ total_orders }}</div>
            <div class="kpi-lbl">Total Orders</div>
          </div>
          <div class="kpi kpi-usr">
            <div class="kpi-ico">&#128101;</div>
            <div class="kpi-val">{{ total_users }}</div>
            <div class="kpi-lbl">Customers</div>
          </div>
          <div class="kpi kpi-pnd">
            <div class="kpi-ico">&#9201;</div>
            <div class="kpi-val">{{ pending }}</div>
            <div class="kpi-lbl">Pending Orders</div>
          </div>
          <div class="kpi kpi-del">
            <div class="kpi-ico">&#9989;</div>
            <div class="kpi-val">{{ del_today }}</div>
            <div class="kpi-lbl">Delivered Today</div>
          </div>
        </div>

        <div class="dc">
          <div class="dc-hd">
            <h3>&#128293; Recent Orders</h3>
            <span style="font-size:.75rem;color:#9e9e8e;font-weight:600">Last 10 orders</span>
          </div>
          <div class="tbl-scroll">
          <table><thead><tr><th>#</th><th>Customer</th><th>Total</th><th>Status</th><th>Date</th></tr></thead>
          <tbody>{% for o in orders[:10] %}
          <tr>
            <td><span class="bl-chip">#{{ o['id'] }}</span></td>
            <td style="font-weight:700;color:#1c1c1c">{{ o['username'] or o['user_email'][:20] }}</td>
            <td style="font-weight:900;color:#15803d">&#8377;{{ o['total'] }}</td>
            <td><span class="bs bs-{{ sc.get(o['status'],'placed') }}">{{ o['status'].replace('_',' ').title() }}</span></td>
            <td style="font-size:.74rem;color:#9e9e8e">{{ o['placed_at'][:16] }}</td>
          </tr>{% endfor %}
          </tbody></table>
          </div>
        </div>
      </div>
    </div>

    <!-- ── ORDERS ── -->
    <div class="bl-tab" id="tab-orders">
      <div class="bl-topbar">
        <div class="tb-left">
          <div class="tb-title">Order Management</div>
          <div class="tb-sub">{{ total_orders }} total &bull; {{ pending }} pending action</div>
        </div>
      </div>
      <div class="bl-body">
        <div class="dc">
          <div class="dc-hd">
            <h3>All Orders</h3>
            <div class="dc-actions">
              <input class="bl-search" placeholder="&#128269; Search orders..." oninput="filterTable(this,'tbl-orders')">
            </div>
          </div>
          <div class="tbl-scroll">
          <table id="tbl-orders">
            <thead><tr><th>#</th><th>Customer</th><th>Total</th><th>Status</th><th>Agent</th><th>Date</th><th>Update</th></tr></thead>
            <tbody>{% for o in orders %}
            <tr>
              <td><span class="bl-chip">#{{ o['id'] }}</span></td>
              <td>
                <div style="font-weight:700;color:#1c1c1c">{{ o['username'] or 'N/A' }}</div>
                <div style="font-size:.7rem;color:#9e9e8e">{{ o['user_email'] }}</div>
              </td>
              <td style="font-weight:900;color:#15803d">&#8377;{{ o['total'] }}</td>
              <td><span class="bs bs-{{ sc.get(o['status'],'placed') }}">{{ o['status'].replace('_',' ').title() }}</span></td>
              <td style="font-size:.8rem;color:#555;font-weight:600">{{ o['delivery_by'] or '&mdash;' }}</td>
              <td style="font-size:.73rem;color:#9e9e8e">{{ o['placed_at'][:16] }}</td>
              <td>
                <form method="POST" action="/admin/order/{{ o['id'] }}/update" style="display:flex;gap:5px;flex-wrap:wrap">
                  <select name="status" class="bl-sel">
                    {% for s in ['placed','confirmed','out_for_delivery','delivered','cancelled'] %}
                    <option value="{{ s }}" {% if o['status']==s %}selected{% endif %}>{{ s.replace('_',' ').title() }}</option>
                    {% endfor %}
                  </select>
                  <select name="agent" class="bl-sel">
                    <option value="">No agent</option>
                    {% for ag in agents %}
                    <option value="{{ ag['username'] }}" {% if o['delivery_by']==ag['username'] %}selected{% endif %}>{{ ag['username'] }}</option>
                    {% endfor %}
                  </select>
                  <button class="bl-pill bl-pill-yellow bl-pill-sm">&#10003;</button>
                </form>
              </td>
            </tr>{% endfor %}
            </tbody>
          </table>
          </div>
        </div>
      </div>
    </div>

    <!-- ── PRODUCTS ── -->
    <div class="bl-tab" id="tab-products">
      <div class="bl-topbar">
        <div class="tb-left">
          <div class="tb-title">Product Management</div>
          <div class="tb-sub">{{ products|length }} products across all categories</div>
        </div>
      </div>
      <div class="bl-body">
        <div class="dc">
          <div class="dc-hd">
            <h3>All Products</h3>
            <div class="dc-actions">
              <input class="bl-search" placeholder="&#128269; Search products..." oninput="filterTable(this,'tbl-products')">
              <a href="/admin/product/add" class="bl-pill bl-pill-yellow bl-pill-sm">+ Add Product</a>
              <a href="/admin/images" class="bl-pill bl-pill-sm" style="background:rgba(96,165,250,.13);color:#93c5fd;border:1px solid rgba(96,165,250,.2)">&#128247; Image Gallery</a>
            </div>
          </div>
          <div class="tbl-scroll">
          <table id="tbl-products">
            <thead><tr><th>ID</th><th>Product</th><th>Category</th><th>Price</th><th>Stock</th><th>Actions</th></tr></thead>
            <tbody>{% for p in products %}
            <tr>
              <td style="color:#9e9e8e;font-size:.78rem;font-weight:600">{{ p['id'] }}</td>
              <td style="font-weight:700;color:#1c1c1c;font-size:.85rem">{{ p['name'] }}</td>
              <td><span class="bl-chip">{{ p['category'] }}</span></td>
              <td style="font-weight:900;color:#15803d">&#8377;{{ p['price'] }}</td>
              <td>
                <span style="font-weight:800;font-size:.85rem;{% if p['stock'] < 10 %}color:#c2410c{% else %}color:#1c1c1c{% endif %}">
                  {{ p['stock'] }}{% if p['stock'] < 10 %} <span style="font-size:.65rem;background:#fff7ed;color:#c2410c;padding:1px 5px;border-radius:4px;font-weight:700">LOW</span>{% endif %}
                </span>
              </td>
              <td>
                <div style="display:flex;gap:6px">
                  <a href="/admin/product/{{ p['id'] }}/edit" class="bl-pill bl-pill-ghost bl-pill-sm">&#9998; Edit</a>
                  <a href="/admin/product/{{ p['id'] }}/delete" class="bl-pill bl-pill-red bl-pill-sm"
                     onclick="return confirm('Delete this product?')">&#128465;</a>
                </div>
              </td>
            </tr>{% endfor %}
            </tbody>
          </table>
          </div>
        </div>
      </div>
    </div>

    <!-- ── USERS ── -->
    <div class="bl-tab" id="tab-users">
      <div class="bl-topbar">
        <div class="tb-left">
          <div class="tb-title">User Management</div>
          <div class="tb-sub">{{ users|length }} registered accounts</div>
        </div>
      </div>
      <div class="bl-body">
        <div class="dc">
          <div class="dc-hd">
            <h3>All Users</h3>
            <input class="bl-search" placeholder="&#128269; Search users..." oninput="filterTable(this,'tbl-users')">
          </div>
          <div class="tbl-scroll">
          <table id="tbl-users">
            <thead><tr><th>ID</th><th>Username</th><th>Email</th><th>Role</th><th>Joined</th><th>Change Role</th></tr></thead>
            <tbody>{% for u in users %}
            <tr>
              <td style="color:#9e9e8e;font-size:.78rem;font-weight:600">{{ u['id'] }}</td>
              <td style="font-weight:700;color:#1c1c1c">{{ u['username'] }}</td>
              <td style="font-size:.8rem;color:#555">{{ u['email'] }}</td>
              <td><span class="rp rp-{{ u['role'] }}">{{ u['role'].upper() }}</span></td>
              <td style="font-size:.73rem;color:#9e9e8e">{{ u['created_at'][:10] }}</td>
              <td>
                <form method="POST" action="/admin/user/{{ u['id'] }}/role" style="display:flex;gap:6px">
                  <select name="role" class="bl-sel">
                    {% for r in ['user','admin','delivery'] %}
                    <option {% if u['role']==r %}selected{% endif %} value="{{ r }}">{{ r.title() }}</option>
                    {% endfor %}
                  </select>
                  <button class="bl-pill bl-pill-yellow bl-pill-sm">Save</button>
                </form>
              </td>
            </tr>{% endfor %}
            </tbody>
          </table>
          </div>
        </div>
      </div>
    </div>

    <!-- ── AGENTS ── -->
    <div class="bl-tab" id="tab-agents">
      <div class="bl-topbar">
        <div class="tb-left">
          <div class="tb-title">Delivery Agents</div>
          <div class="tb-sub">{{ agents|length }} active delivery partners</div>
        </div>
      </div>
      <div class="bl-body">
        {% if agents %}
        <div class="ag-grid">
          {% for ag in agents %}{% set cnt = agent_counts.get(ag['username'],0) %}
          <div class="ag-card">
            <div style="display:flex;align-items:center;gap:12px;margin-bottom:14px">
              <div class="ag-avatar">{{ ag['username'][0].upper() }}</div>
              <div style="flex:1;min-width:0">
                <div style="font-weight:800;font-size:.9rem;color:#1c1c1c;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{{ ag['username'] }}</div>
                <div style="font-size:.7rem;color:#9e9e8e;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{{ ag['email'] }}</div>
              </div>
              <span class="rp rp-delivery">DELIVERY</span>
            </div>
            <div style="display:flex;justify-content:space-between;align-items:flex-end">
              <div>
                <div style="font-size:1.6rem;font-weight:900;color:#1c1c1c;letter-spacing:-.5px">{{ cnt }}</div>
                <div style="font-size:.7rem;color:#9e9e8e;font-weight:600;text-transform:uppercase;letter-spacing:.5px">Deliveries</div>
              </div>
              <div style="text-align:right">
                <div style="font-size:.92rem;color:#15803d;font-weight:900">&#8377;{{ cnt * 30 }}</div>
                <div style="font-size:.68rem;color:#9e9e8e">Est. earned</div>
              </div>
            </div>
            <div class="ag-bar">
              <div class="ag-bar-fill" style="width:{% if cnt > 0 %}{{ [cnt*5,100]|min }}%{% else %}0%{% endif %}"></div>
            </div>
          </div>
          {% endfor %}
        </div>
        {% else %}
        <div class="bl-empty">
          <div class="em-icon">&#128693;</div>
          <p>No delivery agents yet. Promote a user in the Users tab.</p>
        </div>
        {% endif %}
      </div>
    </div>

  </main>
</div>

<script>
function showAdminTab(name, btn) {
  document.querySelectorAll('.bl-tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.bl-nav').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  if (btn) btn.classList.add('active');
}
function filterTable(inp, tid) {
  const v = inp.value.toLowerCase();
  document.querySelectorAll('#' + tid + ' tbody tr').forEach(r => {
    r.style.display = r.textContent.toLowerCase().includes(v) ? '' : 'none';
  });
}
</script>
</body></html>""", nav=nav_html(), flashes=get_flashes(),
        revenue=total_revenue, total_orders=total_orders, total_users=total_users,
        pending=pending, del_today=delivered_today,
        orders=orders, users=users, products=products,
        agents=agents, sc=SC, agent_counts=agent_counts,
        session_user=session.get("user", "Admin"))


# ── ADMIN: Update order ───────────────────────────────────
@app.route("/admin/order/<int:oid>/update", methods=["POST"])
@login_required
@admin_required
def admin_order_update(oid):
    status = request.form.get("status", "placed")
    agent  = request.form.get("agent", "").strip() or None
    db = get_db()
    if not db.execute("SELECT id FROM orders WHERE id=?", (oid,)).fetchone():
        flash("Order not found.", "error")
        return redirect(url_for("admin"))
    db.execute(
        "UPDATE orders SET status=?,delivery_by=?,updated_at=datetime('now') WHERE id=?",
        (status, agent, oid)
    )
    db.commit()
    flash(f"Order #{oid} updated to '{status.replace('_',' ')}'.", "success")
    return redirect(url_for("admin"))


# ── ADMIN: Change user role ───────────────────────────────
@app.route("/admin/user/<int:uid>/role", methods=["POST"])
@login_required
@admin_required
def admin_user_role(uid):
    role = request.form.get("role", "user")
    if role not in ("user", "admin", "delivery"):
        flash("Invalid role.", "error")
        return redirect(url_for("admin"))
    db = get_db()
    db.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
    db.commit()
    flash("User role updated.", "success")
    return redirect(url_for("admin"))


# ── ADMIN: Add product ────────────────────────────────────
@app.route("/admin/product/add", methods=["GET", "POST"])
@login_required
@admin_required
def admin_product_add():
    error = ""
    if request.method == "POST":
        name     = request.form.get("name", "").strip()
        category = request.form.get("category", "").strip()
        price    = request.form.get("price", "0").strip()
        image    = request.form.get("image", "").strip()
        stock    = request.form.get("stock", "100").strip()
        # Handle image file upload
        file = request.files.get("image_file")
        if file and file.filename and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            save_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
            file.save(save_path)
            image = f"/static/images/products/{filename}"
        if not all([name, category, price, image]):
            error = "All fields are required (provide image URL or upload a file)."
        else:
            try:
                price_int = int(price)
                stock_int = int(stock)
            except ValueError:
                error = "Price and stock must be numbers."
            else:
                db = get_db()
                db.execute(
                    "INSERT INTO products(name,category,price,image,stock) VALUES(?,?,?,?,?)",
                    (name, category, price_int, image, stock_int)
                )
                db.commit()
                flash(f"Product '{name}' added.", "success")
                return redirect(url_for("admin"))
    return render_template_string("""<!doctype html><html><head>
<meta charset="utf-8"><title>Add Product - QuickKart</title>""" + CSS + """
</head><body>{{ nav|safe }}{{ flashes|safe }}
<div style="max-width:560px;margin:32px auto;padding:0 16px">
  <div style="background:var(--card);border-radius:16px;padding:28px;box-shadow:var(--shadow);border:1px solid var(--border)">
    <h2 style="font-size:1.3rem;font-weight:900;margin-bottom:20px;color:var(--text)">&#128722; Add New Product</h2>
    {% if error %}<div class="flash error">{{ error }}</div>{% endif %}
    <form method="POST" enctype="multipart/form-data">
      <div class="form-group"><label>Product Name</label>
        <input name="name" required placeholder="e.g. Milk 1L"></div>
      <div class="form-group"><label>Category</label>
        <select name="category" style="width:100%;padding:12px 14px;border:1.5px solid var(--border);border-radius:12px;font-family:inherit;font-size:.9rem;outline:none">
          {% for cat in categories %}
          <option value="{{ cat }}">{{ cat }}</option>
          {% endfor %}
        </select></div>
      <div class="form-group"><label>Price (₹)</label>
        <input name="price" type="number" min="1" required placeholder="e.g. 50"></div>
      <div class="form-group">
        <label>Upload Product Image</label>
        <input name="image_file" type="file" accept="image/*"
          style="width:100%;padding:10px 14px;border:1.5px solid var(--border);border-radius:12px;
                 font-family:inherit;font-size:.85rem;background:var(--surface2);color:var(--text)">
        <div style="font-size:.72rem;color:var(--text3);margin-top:4px">PNG, JPG, WEBP — max 5MB. Or paste URL below.</div>
      </div>
      <div class="form-group"><label>Image URL (if not uploading)</label>
        <input name="image" placeholder="https://... or leave blank if uploading file above"></div>
      <div class="form-group"><label>Stock</label>
        <input name="stock" type="number" min="0" value="100" required></div>
      <div style="display:flex;gap:10px;margin-top:8px">
        <button class="btn btn-green" style="flex:1">Add Product</button>
        <a href="/admin" class="btn btn-gray" style="flex:1;justify-content:center">Cancel</a>
      </div>
    </form>
  </div>
</div>
</body></html>""", nav=nav_html(), flashes=get_flashes(), error=error, categories=CATEGORIES)


# ── ADMIN: Edit product ───────────────────────────────────
@app.route("/admin/product/<int:pid>/edit", methods=["GET", "POST"])
@login_required
@admin_required
def admin_product_edit(pid):
    db = get_db()
    p  = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not p:
        flash("Product not found.", "error")
        return redirect(url_for("admin"))
    error = ""
    if request.method == "POST":
        name     = request.form.get("name", "").strip()
        category = request.form.get("category", "").strip()
        price    = request.form.get("price", "0").strip()
        image    = request.form.get("image", "").strip() or p["image"]
        stock    = request.form.get("stock", "100").strip()
        # Handle image file upload
        file = request.files.get("image_file")
        if file and file.filename and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            save_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
            file.save(save_path)
            image = f"/static/images/products/{filename}"
        if not all([name, category, price, image]):
            error = "All fields are required."
        else:
            try:
                price_int = int(price)
                stock_int = int(stock)
            except ValueError:
                error = "Price and stock must be numbers."
            else:
                db.execute(
                    "UPDATE products SET name=?,category=?,price=?,image=?,stock=? WHERE id=?",
                    (name, category, price_int, image, stock_int, pid)
                )
                db.commit()
                flash(f"Product '{name}' updated.", "success")
                return redirect(url_for("admin"))
    return render_template_string("""<!doctype html><html><head>
<meta charset="utf-8"><title>Edit Product - QuickKart</title>""" + CSS + """
</head><body>{{ nav|safe }}{{ flashes|safe }}
<div style="max-width:560px;margin:32px auto;padding:0 16px">
  <div style="background:var(--card);border-radius:16px;padding:28px;box-shadow:var(--shadow);border:1px solid var(--border)">
    <h2 style="font-size:1.3rem;font-weight:900;margin-bottom:20px;color:var(--text)">&#9998; Edit Product #{{ p['id'] }}</h2>
    {% if error %}<div class="flash error">{{ error }}</div>{% endif %}
    <div style="text-align:center;margin-bottom:18px">
      <img src="{{ p['image'] }}" alt="Current image"
        style="width:90px;height:90px;object-fit:contain;background:var(--surface2);
               border-radius:12px;padding:8px;border:1px solid var(--border)">
      <div style="font-size:.72rem;color:var(--text3);margin-top:6px">Current Image</div>
    </div>
    <form method="POST" enctype="multipart/form-data">
      <div class="form-group"><label>Product Name</label>
        <input name="name" required value="{{ p['name'] }}"></div>
      <div class="form-group"><label>Category</label>
        <select name="category" style="width:100%;padding:12px 14px;border:1.5px solid var(--border);border-radius:12px;font-family:inherit;font-size:.9rem;outline:none">
          {% for cat in categories %}
          <option value="{{ cat }}" {% if cat==p['category'] %}selected{% endif %}>{{ cat }}</option>
          {% endfor %}
        </select></div>
      <div class="form-group"><label>Price (₹)</label>
        <input name="price" type="number" min="1" required value="{{ p['price'] }}"></div>
      <div class="form-group">
        <label>Upload New Image (replaces current)</label>
        <input name="image_file" type="file" accept="image/*"
          style="width:100%;padding:10px 14px;border:1.5px solid var(--border);border-radius:12px;
                 font-family:inherit;font-size:.85rem;background:var(--surface2);color:var(--text)">
        <div style="font-size:.72rem;color:var(--text3);margin-top:4px">PNG, JPG, WEBP — max 5MB</div>
      </div>
      <div class="form-group"><label>Image URL (leave blank to keep current)</label>
        <input name="image" placeholder="https://... or blank to keep existing" value="{{ p['image'] }}"></div>
      <div class="form-group"><label>Stock</label>
        <input name="stock" type="number" min="0" required value="{{ p['stock'] }}"></div>
      <div style="display:flex;gap:10px;margin-top:8px">
        <button class="btn btn-green" style="flex:1">Save Changes</button>
        <a href="/admin" class="btn btn-gray" style="flex:1;justify-content:center">Cancel</a>
      </div>
    </form>
  </div>
</div>
</body></html>""", nav=nav_html(), flashes=get_flashes(), error=error, p=p, categories=CATEGORIES)


# ── ADMIN: Image Upload API ───────────────────────────────
@app.route("/admin/image/upload", methods=["POST"])
@login_required
@admin_required
def admin_image_upload():
    """AJAX endpoint: upload an image, return its local URL."""
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"ok": False, "error": "No file provided"}), 400
    if not allowed_file(file.filename):
        return jsonify({"ok": False, "error": "File type not allowed. Use PNG, JPG, JPEG, GIF or WEBP"}), 400
    filename  = secure_filename(file.filename)
    save_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    file.save(save_path)
    return jsonify({"ok": True, "url": f"/static/images/products/{filename}"})


# ── ADMIN: Image Gallery ──────────────────────────────────
@app.route("/admin/images")
@login_required
@admin_required
def admin_images():
    """Browse & manage all uploaded product images."""
    folder = app.config["UPLOAD_FOLDER"]
    files  = sorted(f for f in os.listdir(folder)
                    if f.lower().rsplit(".", 1)[-1] in ALLOWED_EXTENSIONS)
    return render_template_string("""<!doctype html><html><head>
<meta charset="utf-8"><title>Image Gallery - QuickKart</title>""" + CSS + """
<style>
.img-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:14px;margin-top:20px}
.img-card{background:var(--card);border-radius:12px;overflow:hidden;border:1px solid var(--border);
  display:flex;flex-direction:column;align-items:center}
.img-card img{width:100%;height:120px;object-fit:contain;background:var(--surface2);padding:10px}
.img-card .img-name{font-size:.65rem;color:var(--text3);padding:6px 8px;word-break:break-all;text-align:center}
.img-card .img-url{font-size:.62rem;color:var(--accent);padding:0 8px 6px;cursor:pointer;
  word-break:break-all;text-align:center}
.upload-zone{border:2px dashed var(--border2);border-radius:14px;padding:32px;text-align:center;
  background:var(--surface2);cursor:pointer;transition:all .2s}
.upload-zone:hover{border-color:var(--accent);background:rgba(245,200,66,.05)}
</style>
</head><body>{{ nav|safe }}{{ flashes|safe }}
<div style="max-width:1100px;margin:28px auto;padding:0 20px">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:20px">
    <h2 style="font-size:1.3rem;font-weight:900;color:var(--text)">&#128247; Image Gallery</h2>
    <a href="/admin" class="btn btn-gray btn-sm">&#8592; Back to Admin</a>
  </div>
  <!-- Upload Box -->
  <div style="background:var(--card);border-radius:16px;padding:24px;border:1px solid var(--border);margin-bottom:24px">
    <h3 style="font-weight:800;font-size:.95rem;margin-bottom:14px;color:var(--text)">&#128640; Upload New Image</h3>
    <form id="upload-form" enctype="multipart/form-data">
      <div class="upload-zone" onclick="document.getElementById('file-input').click()">
        <div style="font-size:2.5rem;margin-bottom:8px">&#128247;</div>
        <div style="font-weight:700;font-size:.9rem;color:var(--text)">Click to choose image</div>
        <div style="font-size:.76rem;color:var(--text3);margin-top:4px">PNG, JPG, WEBP, GIF — max 5MB</div>
        <input type="file" id="file-input" name="file" accept="image/*" style="display:none"
          onchange="uploadFile(this)">
      </div>
      <div id="upload-status" style="margin-top:12px;font-size:.84rem;font-weight:600"></div>
    </form>
  </div>
  <!-- Gallery -->
  <h3 style="font-weight:800;font-size:.95rem;margin-bottom:4px;color:var(--text)">
    &#128444; Uploaded Images <span style="color:var(--text3);font-weight:400;font-size:.8rem">({{ files|length }} files)</span>
  </h3>
  <div style="font-size:.74rem;color:var(--text3);margin-bottom:12px">Click the URL below any image to copy it.</div>
  <div class="img-grid">
    {% for f in files %}
    <div class="img-card">
      <img src="/static/images/products/{{ f }}" alt="{{ f }}" loading="lazy">
      <div class="img-name">{{ f }}</div>
      <div class="img-url" onclick="copyUrl('/static/images/products/{{ f }}', this)"
        title="Click to copy">/static/images/products/{{ f }}</div>
    </div>
    {% endfor %}
  </div>
  {% if not files %}
  <div class="empty"><div class="icon">&#128247;</div>No images uploaded yet.</div>
  {% endif %}
</div>
<script>
async function uploadFile(input) {
  const status = document.getElementById('upload-status');
  if (!input.files[0]) return;
  status.style.color = 'var(--accent)';
  status.textContent = '⏳ Uploading...';
  const fd = new FormData();
  fd.append('file', input.files[0]);
  try {
    const res = await fetch('/admin/image/upload', {method:'POST', body:fd});
    const data = await res.json();
    if (data.ok) {
      status.style.color = 'var(--green)';
      status.textContent = '✓ Uploaded! URL: ' + data.url;
      setTimeout(() => location.reload(), 1200);
    } else {
      status.style.color = '#f87171';
      status.textContent = '✗ ' + data.error;
    }
  } catch(e) {
    status.style.color = '#f87171';
    status.textContent = '✗ Upload failed.';
  }
}
function copyUrl(url, el) {
  navigator.clipboard.writeText(url).then(() => {
    const orig = el.textContent;
    el.textContent = '✓ Copied!';
    el.style.color = 'var(--green)';
    setTimeout(() => { el.textContent = orig; el.style.color = 'var(--accent)'; }, 1500);
  });
}
</script>
</body></html>""", nav=nav_html(), flashes=get_flashes(), files=files)


# ── ADMIN: Delete product ─────────────────────────────────
@app.route("/admin/product/<int:pid>/delete")
@login_required
@admin_required
def admin_product_delete(pid):
    db = get_db()
    p  = db.execute("SELECT name FROM products WHERE id=?", (pid,)).fetchone()
    if not p:
        flash("Product not found.", "error")
        return redirect(url_for("admin"))
    db.execute("DELETE FROM order_items WHERE product_id=?", (pid,))
    db.execute("DELETE FROM products WHERE id=?", (pid,))
    db.commit()
    flash(f"Product '{p['name']}' deleted.", "success")
    return redirect(url_for("admin"))


# ── DELIVERY PANEL ────────────────────────────────────────
@app.route("/delivery")
@login_required
@delivery_required
def delivery():
    db    = get_db()
    agent = session["user"]
    role  = session["role"]
    if role == "admin":
        orders = db.execute(
            "SELECT o.*,u.username FROM orders o LEFT JOIN users u ON o.user_email=u.email ORDER BY o.placed_at DESC LIMIT 100"
        ).fetchall()
    else:
        orders = db.execute(
            """SELECT o.*,u.username FROM orders o LEFT JOIN users u ON o.user_email=u.email
               WHERE o.delivery_by=? OR (o.status='placed' AND o.delivery_by IS NULL)
               ORDER BY o.placed_at DESC""",
            (agent,)
        ).fetchall()
    oi = {}
    for o in orders:
        rows = db.execute(
            "SELECT oi.*,p.name,p.image FROM order_items oi JOIN products p ON oi.product_id=p.id WHERE oi.order_id=?",
            (o["id"],)
        ).fetchall()
        oi[o["id"]] = [{"name": r["name"], "image": r["image"],
                         "qty": r["qty"], "price": r["price"]} for r in rows]
    today_del   = db.execute(
        "SELECT COUNT(*) FROM orders WHERE delivery_by=? AND status='delivered' AND date(updated_at)=date('now')",
        (agent,)
    ).fetchone()[0]
    total_del   = db.execute(
        "SELECT COUNT(*) FROM orders WHERE delivery_by=? AND status='delivered'",
        (agent,)
    ).fetchone()[0]
    active_count= db.execute(
        "SELECT COUNT(*) FROM orders WHERE delivery_by=? AND status IN ('confirmed','out_for_delivery')",
        (agent,)
    ).fetchone()[0]
    today_earn  = today_del * 30
    SC = {"placed":"placed","confirmed":"confirmed","out_for_delivery":"out",
          "delivered":"delivered","cancelled":"cancelled"}
    STATUS_NEXT = {
        "placed":           ("confirmed",        "Confirm Order",     "btn-green"),
        "confirmed":        ("out_for_delivery",  "Mark Out for Del",  "btn-orange"),
        "out_for_delivery": ("delivered",         "Mark Delivered",    "btn-green"),
    }
    return render_template_string("""<!doctype html><html><head>
<meta charset="utf-8"><title>Delivery Panel - QuickKart</title>""" + CSS + """
<style>
.dlv-root{display:flex;min-height:calc(100vh - 64px);background:#0c0e14}
.dlv-sidebar{width:240px;background:#0c0e14;border-right:1px solid rgba(255,255,255,.07);
  display:flex;flex-direction:column;position:sticky;top:64px;height:calc(100vh - 64px);overflow-y:auto;flex-shrink:0}
.dlv-sb-brand{padding:22px 18px 16px;border-bottom:1px solid rgba(255,255,255,.07)}
.dlv-sb-brand .logo{font-size:1rem;font-weight:900;color:#fff;display:flex;align-items:center;gap:8px}
.dlv-sb-brand .logo em{font-style:normal;color:#fbbf24}
.dlv-sb-brand .sub{font-size:.7rem;color:rgba(255,255,255,.4);margin-top:3px}
.dlv-agent-card{margin:14px;background:rgba(255,255,255,.06);border-radius:12px;padding:14px;border:1px solid rgba(255,255,255,.1)}
.dlv-agent-card .name{font-weight:800;color:#fff;font-size:.88rem}
.dlv-agent-card .role{font-size:.7rem;color:#fbbf24;font-weight:700;margin-top:2px}
.dlv-sb-section{padding:14px 12px 6px}
.dlv-sb-title{font-size:.6rem;font-weight:700;color:rgba(255,255,255,.3);letter-spacing:1.5px;
  text-transform:uppercase;padding:0 8px;margin-bottom:6px}
.dlv-nav-item{display:flex;align-items:center;gap:10px;padding:10px 10px;border-radius:10px;
  color:rgba(255,255,255,.55);font-weight:600;font-size:.84rem;cursor:pointer;
  transition:all .15s;margin-bottom:2px;border:none;background:none;width:100%;
  text-align:left;font-family:inherit;text-decoration:none}
.dlv-nav-item:hover{background:rgba(255,255,255,.07);color:rgba(255,255,255,.85)}
.dlv-nav-item.active{background:linear-gradient(135deg,rgba(251,191,36,.15),rgba(245,158,11,.08));
  color:#fbbf24;border:1px solid rgba(251,191,36,.2)}
.dlv-nav-item .di{font-size:.95rem;width:20px;text-align:center;flex-shrink:0}
.dlv-sb-footer{margin-top:auto;padding:14px 12px;border-top:1px solid rgba(255,255,255,.07)}
.dlv-main{flex:1;background:#f1f5f9;overflow-y:auto}
.dlv-topbar{background:#fff;border-bottom:1px solid #e2e8f0;padding:14px 26px;
  display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:10}
.dlv-topbar .pt{font-size:1.1rem;font-weight:900;color:#0f172a}
.dlv-topbar .ps{font-size:.76rem;color:#94a3b8;margin-top:2px}
.dlv-content{padding:24px}
.dlv-stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:14px;margin-bottom:24px}
.dlv-stat{background:#fff;border-radius:14px;padding:18px;box-shadow:0 1px 6px rgba(0,0,0,.06);
  border:1px solid #e2e8f0;text-align:center;transition:transform .15s}
.dlv-stat:hover{transform:translateY(-2px)}
.dlv-stat .sv{font-size:1.9rem;font-weight:900;line-height:1}
.dlv-stat .sl{font-size:.72rem;font-weight:600;color:#94a3b8;margin-top:5px}
.dlv-stat.yellow .sv{color:#f59e0b}.dlv-stat.green .sv{color:#0db14b}
.dlv-stat.blue .sv{color:#3b82f6}.dlv-stat.purple .sv{color:#8b5cf6}
.dlv-active-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px;margin-bottom:24px}
.dlv-order-card{background:#fff;border-radius:14px;padding:16px;box-shadow:0 1px 8px rgba(0,0,0,.07);
  border:1px solid #e2e8f0;transition:transform .15s,box-shadow .15s}
.dlv-order-card:hover{transform:translateY(-2px);box-shadow:0 6px 20px rgba(0,0,0,.1)}
.dlv-order-card.status-placed{border-top:3px solid #3b82f6}
.dlv-order-card.status-confirmed{border-top:3px solid #f59e0b}
.dlv-order-card.status-out_for_delivery{border-top:3px solid #8b5cf6}
.dlv-order-card .oc-num{font-weight:900;font-size:.9rem;color:#0f172a}
.dlv-order-card .oc-customer{font-weight:700;font-size:.82rem;color:#0f172a;margin-top:6px}
.dlv-order-card .oc-address{font-size:.75rem;color:#94a3b8;margin-top:3px}
.dlv-order-card .oc-items{margin:10px 0;display:flex;flex-wrap:wrap;gap:5px}
.dlv-order-card .oc-item-tag{background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;
  padding:3px 8px;font-size:.7rem;font-weight:600;color:#475569}
.dlv-order-card .oc-footer{display:flex;justify-content:space-between;align-items:center;
  padding-top:10px;border-top:1px solid #f1f5f9;margin-top:10px}
.dlv-order-card .oc-total{font-weight:900;color:#0db14b;font-size:.95rem}
.dlv-action-btn{display:inline-flex;align-items:center;gap:5px;padding:8px 14px;border-radius:9px;
  font-size:.78rem;font-weight:800;cursor:pointer;border:none;font-family:inherit;transition:all .15s}
.dlv-btn-pickup{background:linear-gradient(135deg,#f59e0b,#d97706);color:#fff}
.dlv-btn-deliver{background:linear-gradient(135deg,#0db14b,#059669);color:#fff}
.sb2{display:inline-flex;align-items:center;padding:3px 9px;border-radius:20px;font-size:.7rem;font-weight:800}
.sb2-placed{background:#dbeafe;color:#1d4ed8}.sb2-confirmed{background:#fef3c7;color:#92400e}
.sb2-out{background:#ede9fe;color:#5b21b6}.sb2-delivered{background:#dcfce7;color:#166534}
.sb2-cancelled{background:#fee2e2;color:#991b1b}
.dlv-sc{background:#fff;border-radius:14px;box-shadow:0 1px 6px rgba(0,0,0,.06);
  border:1px solid #e2e8f0;overflow:hidden;margin-bottom:20px}
.dlv-sc-hd{padding:16px 20px;border-bottom:1px solid #f1f5f9;
  display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
.dlv-sc-hd h3{font-size:.92rem;font-weight:900;color:#0f172a}
.dlv-search{padding:8px 13px;border:1.5px solid #e2e8f0;border-radius:9px;
  font-size:.82rem;font-family:inherit;outline:none;width:200px}
.dlv-tab{display:none}.dlv-tab.active{display:block}
@media(max-width:768px){.dlv-sidebar{display:none}.dlv-root{display:block}}
</style>
</head><body>
{{ nav|safe }}{{ flashes|safe }}
<div class="dlv-root">
  <div class="dlv-sidebar">
    <div class="dlv-sb-brand">
      <div class="logo">&#128693; <em>Delivery</em> Panel</div>
      <div class="sub">QuickKart Field Agent</div>
    </div>
    <div class="dlv-agent-card">
      <div style="display:flex;align-items:center;gap:10px">
        <div style="width:38px;height:38px;border-radius:10px;background:linear-gradient(135deg,#f59e0b,#d97706);
          display:flex;align-items:center;justify-content:center;color:#fff;font-weight:900;font-size:1rem;flex-shrink:0">
          {{ agent[0].upper() }}</div>
        <div><div class="name">{{ agent }}</div>
          <div class="role">&#9889; Delivery Partner</div></div>
      </div>
      <div style="margin-top:10px;font-size:.72rem;color:rgba(255,255,255,.5)">
        &#128230; {{ total_del }} total &nbsp;&#183;&nbsp; &#9989; {{ today_del }} today
      </div>
    </div>
    <div class="dlv-sb-section">
      <div class="dlv-sb-title">Navigation</div>
      <button class="dlv-nav-item active" onclick="showDlvTab('active',this)">
        <span class="di">&#128293;</span> Active Orders
        {% if active_count > 0 %}
        <span style="margin-left:auto;background:#ef4444;color:#fff;border-radius:8px;padding:1px 7px;font-size:.65rem;font-weight:800">{{ active_count }}</span>
        {% endif %}
      </button>
      <button class="dlv-nav-item" onclick="showDlvTab('all',this)">
        <span class="di">&#128230;</span> All Orders
      </button>
      <button class="dlv-nav-item" onclick="showDlvTab('earnings',this)">
        <span class="di">&#128181;</span> Earnings
      </button>
    </div>
    <div class="dlv-sb-footer">
      <a href="/" class="dlv-nav-item"><span class="di">&#127968;</span> View Store</a>
      {% if role == 'admin' %}<a href="/admin" class="dlv-nav-item"><span class="di">&#9881;</span> Admin Panel</a>{% endif %}
      <a href="/logout" class="dlv-nav-item" style="color:#fca5a5"><span class="di">&#128274;</span> Logout</a>
    </div>
  </div>
  <div class="dlv-main">
    <!-- ACTIVE ORDERS -->
    <div class="dlv-tab active" id="dtab-active">
      <div class="dlv-topbar">
        <div><div class="pt">&#128293; Active Orders</div>
        <div class="ps">{{ active_count }} orders need attention</div></div>
      </div>
      <div class="dlv-content">
        <div class="dlv-stats">
          <div class="dlv-stat yellow"><div class="sv">{{ today_del }}</div><div class="sl">Delivered Today</div></div>
          <div class="dlv-stat green"><div class="sv">&#8377;{{ today_earn }}</div><div class="sl">Earned Today</div></div>
          <div class="dlv-stat blue"><div class="sv">{{ total_del }}</div><div class="sl">Total Deliveries</div></div>
          <div class="dlv-stat purple"><div class="sv">{{ active_count }}</div><div class="sl">Active Now</div></div>
        </div>
        {% set active_orders = orders | selectattr('status','in',['placed','confirmed','out_for_delivery']) | list %}
        {% if not active_orders %}
        <div style="text-align:center;background:#fff;border-radius:14px;border:1px solid #e2e8f0;padding:60px 20px">
          <div style="font-size:3.5rem;margin-bottom:12px">&#127881;</div>
          <p style="font-weight:800;font-size:1rem;color:#0f172a">No active orders right now!</p>
        </div>
        {% else %}
        <div class="dlv-active-grid">
          {% for o in active_orders %}
          {% set css_status = 'status-' + o['status'] %}
          <div class="dlv-order-card {{ css_status }}">
            <div style="display:flex;justify-content:space-between;align-items:flex-start">
              <span class="oc-num">Order #{{ o['id'] }}</span>
              {% set bs = {'placed':'placed','confirmed':'confirmed','out_for_delivery':'out'} %}
              <span class="sb2 sb2-{{ bs.get(o['status'],'placed') }}">{{ o['status'].replace('_',' ').title() }}</span>
            </div>
            <div class="oc-customer">&#128100; {{ o['username'] or o['user_email'] }}</div>
            <div class="oc-address">&#128205; {{ o['address'] }}</div>
            <div class="oc-items">
              {% for item in oi.get(o['id'],[]) %}
              <span class="oc-item-tag">{{ item.name }} x{{ item.qty }}</span>
              {% endfor %}
            </div>
            <div class="oc-footer">
              <div class="oc-total">&#8377;{{ o['total'] }}</div>
              {% if o['status'] in status_next %}{% set nxt = status_next[o['status']] %}
              <form method="POST" action="/delivery/order/{{ o['id'] }}/update">
                <input type="hidden" name="status" value="{{ nxt[0] }}">
                <button class="dlv-action-btn {% if nxt[0] == 'out_for_delivery' %}dlv-btn-pickup{% else %}dlv-btn-deliver{% endif %}">
                  {{ nxt[1] }} &#8594;
                </button>
              </form>{% endif %}
            </div>
          </div>
          {% endfor %}
        </div>
        {% endif %}
      </div>
    </div>
    <!-- ALL ORDERS -->
    <div class="dlv-tab" id="dtab-all">
      <div class="dlv-topbar">
        <div><div class="pt">&#128230; All Orders</div>
        <div class="ps">Complete order history</div></div>
      </div>
      <div class="dlv-content">
        <div class="dlv-sc">
          <div class="dlv-sc-hd">
            <h3>Order History <span class="chip" style="margin-left:6px">{{ orders|length }}</span></h3>
            <input class="dlv-search" placeholder="&#128269; Search..." oninput="filterDlvTable(this)">
          </div>
          <table id="dtbl-all">
            <thead><tr><th>#</th><th>Customer</th><th>Items</th><th>Total</th><th>Status</th><th>Update</th></tr></thead>
            <tbody>
            {% for o in orders %}
            <tr>
              <td><span class="chip">#{{ o['id'] }}</span></td>
              <td><div style="font-weight:700;font-size:.84rem">{{ o['username'] or 'N/A' }}</div>
                <div style="font-size:.7rem;color:#94a3b8">{{ o['placed_at'][:10] }}</div></td>
              <td>{% for item in oi.get(o['id'],[]) %}
                <div style="font-size:.72rem;color:#475569">{{ item.name }} x{{ item.qty }}</div>
                {% endfor %}</td>
              <td style="font-weight:900;color:#0db14b">&#8377;{{ o['total'] }}</td>
              <td>{% set sc2={'placed':'placed','confirmed':'confirmed','out_for_delivery':'out','delivered':'delivered','cancelled':'cancelled'} %}
                <span class="sb2 sb2-{{ sc2.get(o['status'],'placed') }}">{{ o['status'].replace('_',' ').title() }}</span></td>
              <td>
                <form method="POST" action="/delivery/order/{{ o['id'] }}/update" style="display:flex;gap:5px">
                  <select name="status" class="status-select">
                    {% for s in ['placed','confirmed','out_for_delivery','delivered','cancelled'] %}
                    <option value="{{ s }}" {% if o['status']==s %}selected{% endif %}>{{ s.replace('_',' ').title() }}</option>
                    {% endfor %}
                  </select>
                  <button class="dlv-action-btn dlv-btn-deliver" style="padding:6px 10px">&#10003;</button>
                </form>
              </td>
            </tr>{% endfor %}
            </tbody>
          </table>
        </div>
      </div>
    </div>
    <!-- EARNINGS -->
    <div class="dlv-tab" id="dtab-earnings">
      <div class="dlv-topbar">
        <div><div class="pt">&#128181; My Earnings</div>
        <div class="ps">Track your performance and income</div></div>
      </div>
      <div class="dlv-content">
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:16px;margin-bottom:20px">
          <div style="background:#fff;border-radius:14px;padding:20px;border:1px solid #e2e8f0;box-shadow:0 1px 6px rgba(0,0,0,.06)">
            <div style="font-size:1.7rem;font-weight:900">{{ today_del }}</div>
            <div style="font-size:.72rem;color:#94a3b8;margin-top:5px">Deliveries Today</div>
          </div>
          <div style="background:#fff;border-radius:14px;padding:20px;border:1px solid #e2e8f0;box-shadow:0 1px 6px rgba(0,0,0,.06)">
            <div style="font-size:1.7rem;font-weight:900;color:#0db14b">&#8377;{{ today_earn }}</div>
            <div style="font-size:.72rem;color:#94a3b8;margin-top:5px">Earned Today</div>
          </div>
          <div style="background:#fff;border-radius:14px;padding:20px;border:1px solid #e2e8f0;box-shadow:0 1px 6px rgba(0,0,0,.06)">
            <div style="font-size:1.7rem;font-weight:900;color:#3b82f6">{{ total_del }}</div>
            <div style="font-size:.72rem;color:#94a3b8;margin-top:5px">Total Deliveries</div>
          </div>
          <div style="background:#fff;border-radius:14px;padding:20px;border:1px solid #e2e8f0;box-shadow:0 1px 6px rgba(0,0,0,.06)">
            <div style="font-size:1.7rem;font-weight:900;color:#8b5cf6">&#8377;{{ total_del * 30 }}</div>
            <div style="font-size:.72rem;color:#94a3b8;margin-top:5px">Total Earned</div>
          </div>
        </div>
        <div style="background:#fffbeb;border:1px solid #fde68a;border-radius:12px;padding:16px 20px;
          font-size:.84rem;font-weight:600;color:#92400e;display:flex;align-items:center;gap:10px">
          &#128181; Earnings calculated at <strong>&#8377;30 per delivery</strong>.
        </div>
      </div>
    </div>
  </div>
</div>
<script>
function showDlvTab(name,btn){
  document.querySelectorAll('.dlv-tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.dlv-nav-item').forEach(b=>b.classList.remove('active'));
  document.getElementById('dtab-'+name).classList.add('active');
  if(btn)btn.classList.add('active');
}
function filterDlvTable(inp){
  const v=inp.value.toLowerCase();
  document.querySelectorAll('#dtbl-all tbody tr').forEach(r=>{
    r.style.display=r.textContent.toLowerCase().includes(v)?'':'none';
  });
}
</script>
</body></html>""", nav=nav_html(), flashes=get_flashes(),
        orders=orders, oi=oi, agent=agent,
        today_del=today_del, today_earn=today_earn,
        total_del=total_del, active_count=active_count,
        status_next=STATUS_NEXT, role=role)


# ── DELIVERY: Update order status ─────────────────────────
@app.route("/delivery/order/<int:oid>/update", methods=["POST"])
@login_required
@delivery_required
def delivery_order_update(oid):
    status = request.form.get("status", "placed")
    db = get_db()
    o  = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        flash("Order not found.", "error")
        return redirect(url_for("delivery"))
    if session["role"] == "delivery":
        if o["delivery_by"] and o["delivery_by"] != session["user"]:
            flash("Not your order.", "error")
            return redirect(url_for("delivery"))
        if not o["delivery_by"]:
            db.execute(
                "UPDATE orders SET status=?,delivery_by=?,updated_at=datetime('now') WHERE id=?",
                (status, session["user"], oid)
            )
        else:
            db.execute(
                "UPDATE orders SET status=?,updated_at=datetime('now') WHERE id=?",
                (status, oid)
            )
    else:
        db.execute(
            "UPDATE orders SET status=?,updated_at=datetime('now') WHERE id=?",
            (status, oid)
        )
    db.commit()
    flash(f"Order #{oid} marked '{status.replace('_',' ')}'.", "success")
    return redirect(url_for("delivery"))


# ── Run ───────────────────────────────────────────────────
if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "true").lower() == "true"
    app.run(debug=debug, use_reloader=False)