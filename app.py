"""
PricePulse — Multi-Platform E-Commerce Price Tracker
Flask backend with scrapers for Amazon, Flipkart, and Snapdeal.
WhatsApp notifications via Twilio.
"""

from flask import Flask, render_template, request, jsonify
import requests
from bs4 import BeautifulSoup
import sqlite3
import threading
import time
from datetime import datetime
from collections import deque
import re
import json
import os

# ═══════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════
app = Flask(__name__)
CHECK_INTERVAL = 60  # seconds between scraping cycles

# Rolling activity log (last 200 entries, thread-safe)
log_lock = threading.Lock()
activity_logs = deque(maxlen=200)

# Monitor thread control
monitor_running = False
monitor_thread = None

# ═══════════════════════════════════════════════════════════════
# WhatsApp Notification Config (Twilio)
# ═══════════════════════════════════════════════════════════════
WHATSAPP_CONFIG_FILE = "whatsapp_config.json"

DEFAULT_WHATSAPP_CONFIG = {
    "enabled": False,
    "account_sid": "",
    "auth_token": "",
    "from_number": "whatsapp:+14155238886",  # Twilio sandbox default
    "to_number": "",                          # User's WhatsApp number with country code
}


def load_whatsapp_config():
    """Load WhatsApp config from JSON file."""
    if os.path.exists(WHATSAPP_CONFIG_FILE):
        try:
            with open(WHATSAPP_CONFIG_FILE, "r") as f:
                config = json.load(f)
            # Merge with defaults for any missing keys
            merged = {**DEFAULT_WHATSAPP_CONFIG, **config}
            return merged
        except (json.JSONDecodeError, IOError):
            pass
    return DEFAULT_WHATSAPP_CONFIG.copy()


def save_whatsapp_config(config):
    """Save WhatsApp config to JSON file."""
    with open(WHATSAPP_CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def send_whatsapp(message):
    """Send a WhatsApp message via Twilio API."""
    config = load_whatsapp_config()

    if not config.get("enabled"):
        return False, "WhatsApp notifications are disabled."

    sid = config.get("account_sid", "").strip()
    token = config.get("auth_token", "").strip()
    from_num = config.get("from_number", "").strip()
    to_num = config.get("to_number", "").strip()

    if not all([sid, token, from_num, to_num]):
        return False, "WhatsApp config is incomplete."

    # Ensure whatsapp: prefix
    if not to_num.startswith("whatsapp:"):
        to_num = f"whatsapp:{to_num}"
    if not from_num.startswith("whatsapp:"):
        from_num = f"whatsapp:{from_num}"

    try:
        # Use Twilio REST API directly (no twilio SDK needed)
        url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
        data = {
            "From": from_num,
            "To": to_num,
            "Body": message,
        }
        response = requests.post(url, data=data, auth=(sid, token), timeout=15)

        if response.status_code in (200, 201):
            log_activity(f"WhatsApp sent: {message[:60]}...", "ALERT")
            return True, "Message sent successfully."
        else:
            error_msg = response.json().get("message", response.text[:200])
            log_activity(f"WhatsApp send failed: {error_msg}", "ERROR")
            return False, error_msg

    except Exception as e:
        log_activity(f"WhatsApp error: {str(e)}", "ERROR")
        return False, str(e)

# Realistic browser headers for scraping
HEADERS_POOL = [
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Connection": "keep-alive",
        "DNT": "1",
    },
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Connection": "keep-alive",
    },
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Connection": "keep-alive",
    },
]

_header_index = 0


def get_headers():
    """Rotate through the headers pool for each request."""
    global _header_index
    headers = HEADERS_POOL[_header_index % len(HEADERS_POOL)]
    _header_index += 1
    return headers


# ═══════════════════════════════════════════════════════════════
# Database Setup
# ═══════════════════════════════════════════════════════════════
def get_db():
    """Get a thread-local database connection."""
    conn = sqlite3.connect("products.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialize the database schema and run migrations."""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            target_price REAL NOT NULL,
            platform TEXT DEFAULT 'auto'
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            price REAL,
            stock INTEGER DEFAULT 1,
            timestamp TEXT NOT NULL,
            FOREIGN KEY (product_id) REFERENCES products(id)
        )
    """)

    # Migration: add platform column if it doesn't exist
    try:
        cursor.execute("SELECT platform FROM products LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE products ADD COLUMN platform TEXT DEFAULT 'auto'")
        log_activity("DB: Migrated — added 'platform' column to products.")

    # Migration: add stock column to history if missing
    try:
        cursor.execute("SELECT stock FROM history LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE history ADD COLUMN stock INTEGER DEFAULT 1")
        log_activity("DB: Migrated — added 'stock' column to history.")

    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════════
# Activity Logging
# ═══════════════════════════════════════════════════════════════
def log_activity(message, level="INFO"):
    """Thread-safe activity logging."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    prefix = ""
    if level == "ALERT":
        prefix = "ALERT: "
    elif level == "ERROR":
        prefix = "Error: "
    elif level == "WARN":
        prefix = "Warning: "

    formatted = f"[{timestamp}] {prefix}{message}"
    with log_lock:
        activity_logs.append(formatted)


# ═══════════════════════════════════════════════════════════════
# Platform Detection
# ═══════════════════════════════════════════════════════════════
def detect_platform(url):
    """Auto-detect the e-commerce platform from URL."""
    url_lower = url.lower()
    if "amazon.in" in url_lower or "amazon.com" in url_lower:
        return "amazon"
    elif "flipkart.com" in url_lower:
        return "flipkart"
    elif "snapdeal.com" in url_lower:
        return "snapdeal"
    else:
        return "other"


# ═══════════════════════════════════════════════════════════════
# Price Extraction Helpers
# ═══════════════════════════════════════════════════════════════
def clean_price(text):
    """Extract numeric price from a string like '₹1,299.00' or 'Rs. 1299'."""
    if not text:
        return None
    # Remove currency symbols, commas, spaces, and common prefixes
    cleaned = re.sub(r'[₹,\s]', '', text)
    cleaned = cleaned.replace('Rs.', '').replace('Rs', '').replace('INR', '').strip()
    # Extract numeric portion (handle decimals)
    match = re.search(r'[\d]+(?:\.[\d]+)?', cleaned)
    if match:
        try:
            return float(match.group())
        except ValueError:
            return None
    return None


# ═══════════════════════════════════════════════════════════════
# Platform-Specific Scrapers
# ═══════════════════════════════════════════════════════════════
def scrape_amazon(url):
    """Scrape product price and stock status from Amazon India / Amazon.com."""
    try:
        res = requests.get(url, headers=get_headers(), timeout=15)
        soup = BeautifulSoup(res.text, "html.parser")

        price = None

        # Strategy 1: Deal price
        tag = soup.find("span", {"id": "priceblock_dealprice"})
        if tag:
            price = clean_price(tag.get_text())

        # Strategy 2: Our price
        if price is None:
            tag = soup.find("span", {"id": "priceblock_ourprice"})
            if tag:
                price = clean_price(tag.get_text())

        # Strategy 3: .a-price-whole (newer Amazon layout)
        if price is None:
            tag = soup.find("span", {"class": "a-price-whole"})
            if tag:
                price = clean_price(tag.get_text())

        # Strategy 4: span.a-offscreen inside .a-price (most reliable fallback)
        if price is None:
            price_div = soup.find("div", {"id": "corePrice_feature_div"})
            if price_div:
                offscreen = price_div.find("span", {"class": "a-offscreen"})
                if offscreen:
                    price = clean_price(offscreen.get_text())

        # Strategy 5: apex_offerDisplay_desktop price
        if price is None:
            tag = soup.select_one("#apex_offerDisplay_desktop .a-offscreen")
            if tag:
                price = clean_price(tag.get_text())

        # Stock detection
        availability = soup.find("div", {"id": "availability"})
        in_stock = True
        if availability:
            avail_text = availability.get_text().lower()
            if "unavailable" in avail_text or "out of stock" in avail_text:
                in_stock = False

        return price, in_stock

    except Exception as e:
        log_activity(f"Amazon scrape error: {str(e)}", "ERROR")
        return None, False


def scrape_flipkart(url):
    """Scrape product price and stock status from Flipkart."""
    try:
        res = requests.get(url, headers=get_headers(), timeout=15)
        soup = BeautifulSoup(res.text, "html.parser")

        price = None

        # Strategy 1: div._30jeq3 (primary price class)
        tag = soup.find("div", {"class": "_30jeq3"})
        if tag:
            price = clean_price(tag.get_text())

        # Strategy 2: div._16Jk6d (discounted price)
        if price is None:
            tag = soup.find("div", {"class": "_16Jk6d"})
            if tag:
                price = clean_price(tag.get_text())

        # Strategy 3: CSS selector for newer Flipkart layout
        if price is None:
            tag = soup.select_one("div.Nx9bqj.CxhGGd")
            if tag:
                price = clean_price(tag.get_text())

        # Strategy 4: Generic price pattern search
        if price is None:
            tag = soup.select_one("div.Nx9bqj")
            if tag:
                price = clean_price(tag.get_text())

        # Stock detection
        in_stock = True
        page_text = soup.get_text().lower()
        if "sold out" in page_text or "currently unavailable" in page_text:
            in_stock = False

        return price, in_stock

    except Exception as e:
        log_activity(f"Flipkart scrape error: {str(e)}", "ERROR")
        return None, False


def scrape_snapdeal(url):
    """Scrape product price and stock status from Snapdeal."""
    try:
        res = requests.get(url, headers=get_headers(), timeout=15)
        soup = BeautifulSoup(res.text, "html.parser")

        price = None

        # Strategy 1: span.payBlkBig (original selector)
        tag = soup.find("span", {"class": "payBlkBig"})
        if tag:
            price = clean_price(tag.get_text())

        # Strategy 2: span.pdp-final-price
        if price is None:
            tag = soup.find("span", {"class": "pdp-final-price"})
            if tag:
                price = clean_price(tag.get_text())

        # Stock detection
        in_stock = "sold out" not in soup.get_text().lower()

        return price, in_stock

    except Exception as e:
        log_activity(f"Snapdeal scrape error: {str(e)}", "ERROR")
        return None, False


def scrape_generic(url):
    """Generic fallback scraper — best-effort price extraction."""
    try:
        res = requests.get(url, headers=get_headers(), timeout=15)
        soup = BeautifulSoup(res.text, "html.parser")

        price = None

        # Look for common price patterns in the page
        for tag in soup.find_all(["span", "div", "p"], class_=re.compile(r"price", re.I)):
            candidate = clean_price(tag.get_text())
            if candidate and candidate > 0:
                price = candidate
                break

        in_stock = "out of stock" not in soup.get_text().lower()
        return price, in_stock

    except Exception as e:
        log_activity(f"Generic scrape error: {str(e)}", "ERROR")
        return None, False


def get_product_data(url, platform="auto"):
    """Route to the appropriate platform scraper."""
    if platform == "auto":
        platform = detect_platform(url)

    scrapers = {
        "amazon": scrape_amazon,
        "flipkart": scrape_flipkart,
        "snapdeal": scrape_snapdeal,
        "other": scrape_generic,
    }

    scraper = scrapers.get(platform, scrape_generic)
    return scraper(url)


# ═══════════════════════════════════════════════════════════════
# Background Monitor Thread
# ═══════════════════════════════════════════════════════════════
def monitor_loop():
    """Background thread that continuously scrapes all tracked products."""
    global monitor_running

    log_activity("Monitor started — beginning scrape cycle.", "ALERT")

    while monitor_running:
        conn = get_db()
        cursor = conn.cursor()

        try:
            cursor.execute("SELECT id, name, url, target_price, platform FROM products")
            products = cursor.fetchall()

            if not products:
                log_activity("No products to monitor. Add products to begin tracking.")
            else:
                log_activity(f"Scraping {len(products)} product(s)...")

            for product in products:
                if not monitor_running:
                    break

                pid = product["id"]
                name = product["name"]
                url = product["url"]
                target = product["target_price"]
                platform = product["platform"] or "auto"

                detected = detect_platform(url) if platform == "auto" else platform
                log_activity(f"Scraping [{detected.upper()}] {name}...")

                price, in_stock = get_product_data(url, platform)

                # Small delay between scrapes to avoid rate limiting
                time.sleep(2)

                if price is not None:
                    log_activity(f"  → ₹{price:,.2f} {'(In Stock)' if in_stock else '(Out of Stock)'}")

                    # Save to history
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    cursor.execute(
                        "INSERT INTO history (product_id, price, stock, timestamp) VALUES (?, ?, ?, ?)",
                        (pid, price, 1 if in_stock else 0, timestamp)
                    )
                    conn.commit()

                    # Price drop alert
                    if price <= target:
                        alert_msg = f"🔥 {name} dropped to ₹{price:,.2f} (target: ₹{target:,.2f})!"
                        log_activity(f"ALERT: {alert_msg}", "ALERT")
                        # Send WhatsApp notification
                        send_whatsapp(f"💰 PricePulse Alert!\n\n{alert_msg}\n\n🔗 {url}")
                else:
                    log_activity(f"  → Could not extract price (site may be blocking)", "WARN")

                if in_stock:
                    # Only alert if it was previously out of stock
                    cursor.execute(
                        "SELECT stock FROM history WHERE product_id=? ORDER BY rowid DESC LIMIT 1 OFFSET 1",
                        (pid,)
                    )
                    prev = cursor.fetchone()
                    if prev and prev["stock"] == 0:
                        stock_msg = f"✅ {name} is back in stock!"
                        log_activity(f"ALERT: {stock_msg}", "ALERT")
                        # Send WhatsApp notification
                        send_whatsapp(f"📦 PricePulse Alert!\n\n{stock_msg}\n\n🔗 {url}")

        except Exception as e:
            log_activity(f"Monitor cycle error: {str(e)}", "ERROR")
        finally:
            conn.close()

        # Wait for next cycle (check every second to allow quick stop)
        for _ in range(CHECK_INTERVAL):
            if not monitor_running:
                break
            time.sleep(1)

    log_activity("Monitor stopped.", "ALERT")


def start_monitor():
    """Start the background monitor thread."""
    global monitor_running, monitor_thread

    if monitor_running:
        return False, "Monitor is already running."

    monitor_running = True
    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()
    return True, "Monitor started."


def stop_monitor():
    """Stop the background monitor thread."""
    global monitor_running

    if not monitor_running:
        return False, "Monitor is not running."

    monitor_running = False
    return True, "Monitor stopping..."


# ═══════════════════════════════════════════════════════════════
# Flask Routes — Pages
# ═══════════════════════════════════════════════════════════════
@app.route("/")
def index():
    """Serve the main dashboard."""
    return render_template("index.html")


# ═══════════════════════════════════════════════════════════════
# Flask Routes — API
# ═══════════════════════════════════════════════════════════════
@app.route("/api/products", methods=["GET"])
def api_get_products():
    """List all products with their latest scraped price and stock status."""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT id, name, url, target_price, platform FROM products ORDER BY id DESC")
    products = cursor.fetchall()

    result = []
    for p in products:
        pid = p["id"]

        # Get the latest history entry for this product
        cursor.execute(
            "SELECT price, stock, timestamp FROM history WHERE product_id=? ORDER BY rowid DESC LIMIT 1",
            (pid,)
        )
        latest = cursor.fetchone()

        result.append({
            "id": pid,
            "name": p["name"],
            "url": p["url"],
            "target_price": p["target_price"],
            "platform": p["platform"] or "auto",
            "price": latest["price"] if latest else None,
            "stock": bool(latest["stock"]) if latest else True,
            "last_updated": latest["timestamp"] if latest else None,
        })

    conn.close()
    return jsonify(result)


@app.route("/api/products", methods=["POST"])
def api_add_product():
    """Add a new product to track."""
    data = request.get_json()

    name = data.get("name", "").strip()
    url = data.get("url", "").strip()
    target_price = data.get("target_price")
    platform = data.get("platform", "auto").strip()

    if not name or not url or target_price is None:
        return jsonify({"success": False, "error": "Name, URL, and target price are required."}), 400

    try:
        target_price = float(target_price)
    except (ValueError, TypeError):
        return jsonify({"success": False, "error": "Target price must be a number."}), 400

    # Auto-detect platform if set to auto
    if platform == "auto":
        platform = detect_platform(url)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO products (name, url, target_price, platform) VALUES (?, ?, ?, ?)",
        (name, url, target_price, platform)
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()

    log_activity(f"Added product: {name} [{platform.upper()}] (target: ₹{target_price:,.2f})", "ALERT")

    return jsonify({"success": True, "id": new_id, "platform": platform})


@app.route("/api/products/<int:product_id>", methods=["DELETE"])
def api_delete_product(product_id):
    """Delete a product and all its price history."""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT name FROM products WHERE id=?", (product_id,))
    product = cursor.fetchone()

    if not product:
        conn.close()
        return jsonify({"success": False, "error": "Product not found."}), 404

    cursor.execute("DELETE FROM history WHERE product_id=?", (product_id,))
    cursor.execute("DELETE FROM products WHERE id=?", (product_id,))
    conn.commit()
    conn.close()

    log_activity(f"Deleted product: {product['name']} (ID: {product_id})", "ALERT")

    return jsonify({"success": True})


@app.route("/api/products/<int:product_id>/history", methods=["GET"])
def api_get_history(product_id):
    """Get price history for a specific product."""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT price, stock, timestamp FROM history WHERE product_id=? ORDER BY rowid ASC",
        (product_id,)
    )
    history = cursor.fetchall()
    conn.close()

    return jsonify([
        {
            "price": h["price"],
            "stock": bool(h["stock"]),
            "timestamp": h["timestamp"],
        }
        for h in history
    ])


@app.route("/api/monitor/start", methods=["POST"])
def api_start_monitor():
    """Start the background scraping monitor."""
    success, message = start_monitor()
    return jsonify({"success": success, "message": message})


@app.route("/api/monitor/stop", methods=["POST"])
def api_stop_monitor():
    """Stop the background scraping monitor."""
    success, message = stop_monitor()
    return jsonify({"success": success, "message": message})


@app.route("/api/monitor/status", methods=["GET"])
def api_monitor_status():
    """Get monitor running status and activity logs."""
    with log_lock:
        logs = list(activity_logs)

    return jsonify({
        "running": monitor_running,
        "logs": logs,
    })


@app.route("/api/stats", methods=["GET"])
def api_stats():
    """Get dashboard statistics."""
    conn = get_db()
    cursor = conn.cursor()

    # Total products
    cursor.execute("SELECT COUNT(*) as total FROM products")
    total = cursor.fetchone()["total"]

    # Products with price drops (current price <= target)
    drops = 0
    in_stock_count = 0

    cursor.execute("SELECT id, target_price FROM products")
    products = cursor.fetchall()

    for p in products:
        cursor.execute(
            "SELECT price, stock FROM history WHERE product_id=? ORDER BY rowid DESC LIMIT 1",
            (p["id"],)
        )
        latest = cursor.fetchone()
        if latest:
            if latest["price"] and latest["price"] <= p["target_price"]:
                drops += 1
            if latest["stock"]:
                in_stock_count += 1

    conn.close()

    return jsonify({
        "total_products": total,
        "price_drops": drops,
        "in_stock": in_stock_count,
    })


# ═══════════════════════════════════════════════════════════════
# Flask Routes — WhatsApp Settings API
# ═══════════════════════════════════════════════════════════════
@app.route("/api/whatsapp/config", methods=["GET"])
def api_get_whatsapp_config():
    """Get current WhatsApp notification settings (token masked)."""
    config = load_whatsapp_config()
    # Mask sensitive fields for the frontend
    safe_config = {
        "enabled": config.get("enabled", False),
        "account_sid": config.get("account_sid", ""),
        "auth_token_set": bool(config.get("auth_token", "")),
        "from_number": config.get("from_number", ""),
        "to_number": config.get("to_number", ""),
    }
    return jsonify(safe_config)


@app.route("/api/whatsapp/config", methods=["POST"])
def api_save_whatsapp_config():
    """Save WhatsApp notification settings."""
    data = request.get_json()

    config = load_whatsapp_config()
    config["enabled"] = data.get("enabled", config["enabled"])
    config["account_sid"] = data.get("account_sid", config["account_sid"])
    if data.get("auth_token"):  # Only update if provided (not masked)
        config["auth_token"] = data["auth_token"]
    config["from_number"] = data.get("from_number", config["from_number"])
    config["to_number"] = data.get("to_number", config["to_number"])

    save_whatsapp_config(config)
    log_activity(f"WhatsApp settings updated. Notifications {'enabled' if config['enabled'] else 'disabled'}.", "ALERT")

    return jsonify({"success": True})


@app.route("/api/whatsapp/test", methods=["POST"])
def api_test_whatsapp():
    """Send a test WhatsApp message."""
    config = load_whatsapp_config()
    if not config.get("enabled"):
        return jsonify({"success": False, "error": "WhatsApp notifications are disabled. Enable them first."})

    success, message = send_whatsapp(
        "🧪 PricePulse Test Message\n\nYour WhatsApp notifications are working! "
        "You'll receive alerts when tracked product prices drop below your target."
    )
    return jsonify({"success": success, "message": message})


# ═══════════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    init_db()
    log_activity("PricePulse server initialized. Ready to track prices.", "ALERT")
    app.run(debug=True, host="0.0.0.0", port=5000)