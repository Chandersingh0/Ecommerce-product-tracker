from flask import Flask, render_template, request, jsonify, session, redirect, url_for
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
import random
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from playwright.sync_api import sync_playwright
from werkzeug.security import generate_password_hash, check_password_hash

# Database path (configurable via environment variable, e.g., for Hugging Face persistent storage mount)
DATABASE_PATH = os.environ.get("DATABASE_PATH", "products.db")

# ═══════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "pricepulse_secure_session_secret_key_12345")

# Enable cross-site cookies for Hugging Face iframe compatibility in production
if os.environ.get("PORT"):  
    app.config.update(
        SESSION_COOKIE_SAMESITE='None',
        SESSION_COOKIE_SECURE=True
    )

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


def load_whatsapp_config(user_id):
    """Load WhatsApp config from SQLite database."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT whatsapp_enabled as enabled, whatsapp_account_sid as account_sid,
               whatsapp_auth_token as auth_token, whatsapp_from_number as from_number,
               whatsapp_to_number as to_number FROM user_configs WHERE user_id=?
    """, (user_id,))
    row = cursor.fetchone()
    conn.close()

    if row:
        config = dict(row)
        config["enabled"] = bool(config["enabled"])
        return config
    return DEFAULT_WHATSAPP_CONFIG.copy()


def save_whatsapp_config(config, user_id):
    """Save WhatsApp config to SQLite database."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE user_configs SET
            whatsapp_enabled=?,
            whatsapp_account_sid=?,
            whatsapp_auth_token=?,
            whatsapp_from_number=?,
            whatsapp_to_number=?
        WHERE user_id=?
    """, (
        1 if config.get("enabled") else 0,
        config.get("account_sid", ""),
        config.get("auth_token", ""),
        config.get("from_number", "whatsapp:+14155238886"),
        config.get("to_number", ""),
        user_id
    ))
    conn.commit()
    conn.close()


def send_whatsapp(message, user_id):
    """Send a WhatsApp message via Twilio API for the specified user."""
    config = load_whatsapp_config(user_id)

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
            log_user_activity(user_id, f"WhatsApp sent: {message[:60]}...", "ALERT")
            return True, "Message sent successfully."
        else:
            error_msg = response.json().get("message", response.text[:200])
            log_user_activity(user_id, f"WhatsApp send failed: {error_msg}", "ERROR")
            return False, error_msg

    except Exception as e:
        log_activity(f"WhatsApp error: {str(e)}", "ERROR")
        return False, str(e)

# ═══════════════════════════════════════════════════════════════
# Email Notification Config (Simplified)
# ═══════════════════════════════════════════════════════════════
# SMTP credentials are set ONCE via environment variables (or .env file).
# Users only need to enter their email address in the UI.
#
# Required env vars:
#   SMTP_SERVER   — e.g. smtp.gmail.com  (default: smtp.gmail.com)
#   SMTP_PORT     — e.g. 587             (default: 587)
#   SMTP_EMAIL    — sender email address
#   SMTP_PASSWORD — sender app password  (Gmail: use App Password)

# Try loading .env file if python-dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed, rely on system env vars

SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_EMAIL = os.environ.get("SMTP_EMAIL", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")


def is_smtp_configured():
    """Check if the app-level SMTP credentials are set."""
    return bool(SMTP_EMAIL and SMTP_PASSWORD)


def load_email_config(user_id):
    """Load user's notification email from database."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT email_enabled as enabled, email_recipient as recipient_email
        FROM user_configs WHERE user_id=?
    """, (user_id,))
    row = cursor.fetchone()
    conn.close()

    if row:
        return {
            "enabled": bool(row["enabled"]),
            "recipient_email": row["recipient_email"] or "",
        }
    return {"enabled": False, "recipient_email": ""}


def save_email_config(config, user_id):
    """Save user's notification email to database."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE user_configs SET
            email_enabled=?,
            email_recipient=?
        WHERE user_id=?
    """, (
        1 if config.get("enabled") else 0,
        config.get("recipient_email", ""),
        user_id
    ))
    conn.commit()
    conn.close()


def send_email(subject, body, user_id):
    """Send an email notification via SMTP.
    
    SMTP credentials come from environment variables.
    Only the recipient email is per-user (stored in DB).
    """
    config = load_email_config(user_id)

    if not config.get("enabled"):
        return False, "Email notifications are disabled."

    recipient = config.get("recipient_email", "").strip()
    if not recipient:
        return False, "No notification email set."

    if not is_smtp_configured():
        return False, "Email service not configured. Ask the admin to set SMTP_EMAIL and SMTP_PASSWORD environment variables."

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = f"PricePulse <{SMTP_EMAIL}>"
        msg["To"] = recipient
        msg["Subject"] = subject

        # Plain text version
        msg.attach(MIMEText(body, "plain"))

        # HTML version with styled email
        html_body = f"""
        <div style="font-family: 'Segoe UI', Arial, sans-serif; max-width: 520px; margin: 0 auto;
                    background: #0f1419; border-radius: 12px; overflow: hidden; border: 1px solid #2a2f38;">
            <div style="background: linear-gradient(135deg, #8b5cf6, #7c3aed); padding: 20px 24px;">
                <h2 style="color: white; margin: 0; font-size: 18px;">PricePulse Alert</h2>
            </div>
            <div style="padding: 24px; color: #e5e7eb; line-height: 1.6;">
                {body.replace(chr(10), '<br>')}
            </div>
            <div style="padding: 12px 24px; background: #1a1f27; color: #6b7280; font-size: 12px;
                        border-top: 1px solid #2a2f38; text-align: center;">
                Sent by PricePulse Price Tracker
            </div>
        </div>
        """
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            server.sendmail(SMTP_EMAIL, recipient, msg.as_string())

        log_user_activity(user_id, f"Email sent: {subject}", "ALERT")
        return True, "Email sent successfully."

    except smtplib.SMTPAuthenticationError:
        error_msg = "SMTP authentication failed. Admin should check SMTP_EMAIL/SMTP_PASSWORD env vars."
        log_user_activity(user_id, f"Email auth error: {error_msg}", "ERROR")
        return False, error_msg
    except Exception as e:
        log_user_activity(user_id, f"Email error: {str(e)}", "ERROR")
        return False, str(e)

# Realistic browser headers for scraping
HEADERS_POOL = [
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Connection": "keep-alive",
        "DNT": "1",
    },
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",
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


def create_scraper():
    """Create a requests session with rotating headers (for non-Amazon sites)."""
    session = requests.Session()
    session.headers.update(get_headers())
    return session


# ═══════════════════════════════════════════════════════════════
# Playwright Browser Manager (for Amazon)
# ═══════════════════════════════════════════════════════════════
_pw_lock = threading.Lock()
_pw_instance = None     # Playwright context manager
_pw_browser = None      # Browser instance


def _get_browser():
    """Get or create a shared Playwright browser instance (thread-safe)."""
    global _pw_instance, _pw_browser
    with _pw_lock:
        if _pw_browser is None or not _pw_browser.is_connected():
            log_activity("Launching headless Chromium for Amazon scraping...")
            _pw_instance = sync_playwright().start()
            _pw_browser = _pw_instance.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-infobars",
                    "--window-size=1920,1080",
                    "--disable-extensions",
                ],
            )
            log_activity("Chromium browser launched successfully.")
        return _pw_browser


def _create_stealth_context(browser):
    """Create a browser context with stealth settings to avoid detection."""
    context = browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        locale="en-IN",
        timezone_id="Asia/Kolkata",
        java_script_enabled=True,
        bypass_csp=True,
    )
    # Remove the 'webdriver' property that flags automation
    context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        // Override plugins to look like a real browser
        Object.defineProperty(navigator, 'plugins', {
            get: () => [1, 2, 3, 4, 5],
        });
        // Override languages
        Object.defineProperty(navigator, 'languages', {
            get: () => ['en-IN', 'en-US', 'en'],
        });
    """)
    return context


# ═══════════════════════════════════════════════════════════════
# Database Setup
# ═══════════════════════════════════════════════════════════════
def get_db():
    """Get a thread-local database connection."""
    db_dir = os.path.dirname(os.path.abspath(DATABASE_PATH))
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialize the database schema and run migrations."""
    conn = get_db()
    cursor = conn.cursor()

    # Create users table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    # Create user_configs table (stores notifications credentials per user)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_configs (
            user_id INTEGER PRIMARY KEY,
            whatsapp_enabled INTEGER DEFAULT 0,
            whatsapp_account_sid TEXT DEFAULT '',
            whatsapp_auth_token TEXT DEFAULT '',
            whatsapp_from_number TEXT DEFAULT 'whatsapp:+14155238886',
            whatsapp_to_number TEXT DEFAULT '',
            email_enabled INTEGER DEFAULT 0,
            email_smtp_server TEXT DEFAULT 'smtp.gmail.com',
            email_smtp_port INTEGER DEFAULT 587,
            email_sender TEXT DEFAULT '',
            email_password TEXT DEFAULT '',
            email_recipient TEXT DEFAULT '',
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # Create user-specific activity_logs table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            level TEXT DEFAULT 'INFO',
            timestamp TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # Create products table with user_id and is_active flag
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL DEFAULT 1,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            target_price REAL NOT NULL,
            platform TEXT DEFAULT 'auto',
            is_active INTEGER DEFAULT 1,
            FOREIGN KEY (user_id) REFERENCES users(id)
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

    # Migration: add user_id column to products if it doesn't exist
    try:
        cursor.execute("SELECT user_id FROM products LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE products ADD COLUMN user_id INTEGER DEFAULT 1")
        log_activity("DB: Migrated — added 'user_id' column to products.")

    # Migration: add is_active column to products if missing
    try:
        cursor.execute("SELECT is_active FROM products LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE products ADD COLUMN is_active INTEGER DEFAULT 1")
        log_activity("DB: Migrated — added 'is_active' column to products.")

    # Check and create default user if none exists (for legacy single-user data migration)
    cursor.execute("SELECT COUNT(*) as count FROM users")
    if cursor.fetchone()["count"] == 0:
        default_username = "admin"
        default_password = "admin"
        hashed = generate_password_hash(default_password)
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (default_username, hashed, created_at)
        )
        default_uid = cursor.lastrowid
        
        # Prepopulate default config for this user
        cursor.execute(
            "INSERT INTO user_configs (user_id) VALUES (?)",
            (default_uid,)
        )
        conn.commit()
        log_activity(f"DB: Created default user '{default_username}' with password '{default_password}'.")

    # Set user_id for any legacy products that are NULL or 0
    cursor.execute("UPDATE products SET user_id = (SELECT id FROM users ORDER BY id ASC LIMIT 1) WHERE user_id IS NULL OR user_id = 0")
    
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


def log_user_activity(user_id, message, level="INFO"):
    """Log an activity for a specific user to the database."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO activity_logs (user_id, message, level, timestamp) VALUES (?, ?, ?, ?)",
            (user_id, message, level, timestamp)
        )
        conn.commit()
    except Exception as e:
        log_activity(f"Failed to write user log: {str(e)}", "ERROR")
    finally:
        conn.close()

    # Also log to system-wide console logs
    log_activity(f"[User {user_id}] {message}", level)


from functools import wraps

def login_required(f):
    """Decorator to require user authentication on routes."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"success": False, "error": "Unauthorized. Please log in."}), 401
            return redirect(url_for("login_route"))
        return f(*args, **kwargs)
    return decorated_function


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
def _extract_json_ld_price(soup):
    """Extract price from JSON-LD structured data (schema.org Product markup).
    
    This is the most reliable extraction method since sites serve structured
    data for search engines (Google, Bing) and rarely block it.
    Returns (price, in_stock) or (None, None) if not found.
    """
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            if not script.string:
                continue
            ld_data = json.loads(script.string)
            items = ld_data if isinstance(ld_data, list) else [ld_data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_type = item.get("@type", "")
                if item_type not in ("Product", "IndividualProduct"):
                    # Check inside @graph arrays
                    if "@graph" in item:
                        for graph_item in item["@graph"]:
                            if isinstance(graph_item, dict) and graph_item.get("@type") in ("Product", "IndividualProduct"):
                                item = graph_item
                                break
                        else:
                            continue
                    else:
                        continue

                offers = item.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                if isinstance(offers, dict):
                    # AggregateOffer → get lowPrice
                    if offers.get("@type") == "AggregateOffer":
                        p = offers.get("lowPrice") or offers.get("price")
                    else:
                        p = offers.get("price") or offers.get("lowPrice")
                    if p is not None:
                        price = float(p)
                        # Check stock from JSON-LD availability
                        availability = str(offers.get("availability", "")).lower()
                        in_stock = "outofstock" not in availability and "discontinued" not in availability
                        return price, in_stock
        except (json.JSONDecodeError, ValueError, TypeError, KeyError):
            continue
    return None, None


def _extract_price_from_html(soup, page_text=""):
    """Fallback: extract price by scanning HTML for ₹ currency patterns.
    Returns (price, in_stock) or (None, True).
    """
    price = None

    # Look for common price-related classes/selectors
    for tag in soup.find_all(["span", "div", "p"], class_=re.compile(r"price|Price|amount|Amount", re.I)):
        candidate = clean_price(tag.get_text())
        if candidate and candidate > 0:
            price = candidate
            break

    # Regex scan for ₹ price patterns in entire HTML
    if price is None and page_text:
        price_pattern = re.findall(r'[₹₨]\s*([\d,]+(?:\.\d{1,2})?)', page_text)
        if price_pattern:
            from collections import Counter
            price_counts = Counter(price_pattern)
            most_common = price_counts.most_common(1)[0][0]
            price = clean_price(most_common)

    # Stock detection (generic)
    text_lower = soup.get_text().lower()
    in_stock = (
        "out of stock" not in text_lower
        and "sold out" not in text_lower
        and "currently unavailable" not in text_lower
    )

    return price, in_stock


def scrape_amazon(url, retries=2):
    """Scrape product price and stock from Amazon.
    
    Strategy order:
    1. Try fast requests-based scrape first (works when not CAPTCHA'd)
    2. Fall back to Playwright headless browser if requests fails
    """
    # ─── Phase 1: Try fast requests-based scrape ───
    try:
        log_activity(f"  → [Amazon] Trying fast requests-based scrape...")
        session = create_scraper()
        res = session.get(url, timeout=15)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            title_tag = soup.find("title")
            title_text = (title_tag.get_text().lower() if title_tag else "").strip()

            # Check if we're blocked (CAPTCHA/challenge page)
            is_blocked = (
                "sorry" in title_text
                or "robot" in title_text
                or "validatecaptcha" in res.text[:3000].lower()
                or (title_text in ("amazon", "amazon.in", "amazon.com") and len(res.text) < 15000)
            )

            if not is_blocked:
                # Try JSON-LD first
                price, in_stock = _extract_json_ld_price(soup)
                if price is not None:
                    log_activity(f"  → [Amazon/requests JSON-LD] Price: ₹{price:,.2f}")
                    return price, in_stock

                # Try standard Amazon selectors
                price = None
                for selector_name, finder in [
                    ("a-offscreen", lambda: soup.select_one(".a-price .a-offscreen")),
                    ("priceblock_dealprice", lambda: soup.find("span", {"id": "priceblock_dealprice"})),
                    ("priceblock_ourprice", lambda: soup.find("span", {"id": "priceblock_ourprice"})),
                    ("corePrice", lambda: (soup.find("div", {"id": "corePrice_feature_div"}) or soup.new_tag("x")).find("span", {"class": "a-offscreen"})),
                    ("a-price-whole", lambda: soup.find("span", {"class": "a-price-whole"})),
                    ("price_inside_buybox", lambda: soup.find("span", {"id": "price_inside_buybox"})),
                    ("newBuyBoxPrice", lambda: soup.find("span", {"id": "newBuyBoxPrice"})),
                ]:
                    tag = finder()
                    if tag:
                        candidate = clean_price(tag.get_text())
                        if candidate:
                            price = candidate
                            log_activity(f"  → [Amazon/requests {selector_name}] Price: ₹{price:,.2f}")
                            break

                if price is not None:
                    # Stock check
                    in_stock = True
                    avail_div = soup.find("div", {"id": "availability"})
                    if avail_div:
                        avail_text = avail_div.get_text().lower()
                        if "unavailable" in avail_text or "out of stock" in avail_text:
                            in_stock = False
                    if in_stock:
                        unavail_span = soup.find("span", string=re.compile(r"currently unavailable", re.I))
                        if unavail_span:
                            in_stock = False
                    return price, in_stock

                log_activity(f"  → [Amazon/requests] Page loaded but no price found, falling back to Playwright.", "WARN")
            else:
                log_activity(f"  → [Amazon/requests] Blocked by CAPTCHA, falling back to Playwright.")
    except Exception as e:
        log_activity(f"  → [Amazon/requests] Error: {str(e)[:100]}, falling back to Playwright.", "WARN")

    # ─── Phase 2: Playwright headless browser ───
    last_error = None

    for attempt in range(retries + 1):
        context = None
        page = None
        try:
            browser = _get_browser()
            context = _create_stealth_context(browser)
            page = context.new_page()

            # Block unnecessary resources to speed up loading
            page.route("**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf}", lambda route: route.abort())
            page.route("**/ads/**", lambda route: route.abort())
            page.route("**/analytics/**", lambda route: route.abort())

            log_activity(f"  → [Playwright] Loading page (attempt {attempt+1}/{retries+1})...")

            # Navigate and wait for the page to be fully loaded
            page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # Wait a moment for any JS redirects / dynamic content
            page.wait_for_timeout(random.randint(2000, 4000))

            # ─── Handle Amazon challenge pages ───
            continue_btn = page.query_selector('form[action="/errors/validateCaptcha"] button[type="submit"]')
            if continue_btn:
                log_activity(f"  → [Playwright] 'Continue shopping' challenge detected, clicking...")
                continue_btn.click()
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                    page.wait_for_timeout(random.randint(3000, 5000))
                    log_activity(f"  → [Playwright] Challenge bypassed, title: '{page.title()[:60]}'")
                except Exception as nav_err:
                    log_activity(f"  → [Playwright] Post-challenge timeout: {nav_err}", "WARN")

            # Check if still on CAPTCHA page
            current_title = page.title().lower()
            page_html_snippet = page.content()[:2000].lower()
            is_captcha = (
                "sorry" in current_title
                or "robot" in current_title
                or (("amazon" == current_title.strip() or "amazon.in" == current_title.strip())
                    and len(page.content()) < 15000
                    and "validatecaptcha" in page_html_snippet)
            )

            if is_captcha:
                log_activity(f"  → [Playwright] Still on challenge page (attempt {attempt+1})", "WARN")
                if attempt < retries:
                    time.sleep(random.uniform(5, 10))
                    continue
                log_activity("Amazon is blocking after all retries.", "ERROR")
                return None, False

            # Try to wait for a price element
            try:
                page.wait_for_selector(
                    ".a-price-whole, #priceblock_ourprice, #priceblock_dealprice, "
                    "#corePrice_feature_div, #price_inside_buybox, #newBuyBoxPrice",
                    timeout=8000
                )
            except Exception:
                log_activity("  → [Playwright] No price selector within timeout, continuing...", "WARN")

            # Get fully rendered HTML
            page_text = page.content()
            soup = BeautifulSoup(page_text, "html.parser")

            page_title = soup.find("title")
            page_title_text = page_title.get_text()[:60] if page_title else "unknown"
            log_activity(f"  → [Playwright] Page: '{page_title_text}' ({len(page_text)} bytes)")

            price = None

            # Strategy 0: JSON-LD
            ld_price, ld_stock = _extract_json_ld_price(soup)
            if ld_price is not None:
                price = ld_price
                log_activity(f"  → [JSON-LD] Price: ₹{price:,.2f}")

            # Strategy 1-8: Amazon-specific selectors
            if price is None:
                for selector_name, finder in [
                    ("dealprice", lambda: soup.find("span", {"id": "priceblock_dealprice"})),
                    ("ourprice", lambda: soup.find("span", {"id": "priceblock_ourprice"})),
                    ("corePrice", lambda: (soup.find("div", {"id": "corePrice_feature_div"}) or soup.new_tag("x")).find("span", {"class": "a-offscreen"})),
                    ("a-price", lambda: next((a.find("span", {"class": "a-offscreen"}) for a in soup.find_all("span", {"class": "a-price"}) if not a.find_parent(class_=re.compile(r"a-text-strike|priceBlockStrikePrice")) and a.find("span", {"class": "a-offscreen"})), None)),
                    ("a-price-whole", lambda: soup.find("span", {"class": "a-price-whole"})),
                    ("apex", lambda: soup.select_one("#apex_offerDisplay_desktop .a-offscreen")),
                    ("buybox", lambda: soup.find("span", {"id": "price_inside_buybox"})),
                    ("newBuyBox", lambda: soup.find("span", {"id": "newBuyBoxPrice"})),
                ]:
                    tag = finder()
                    if tag:
                        candidate = clean_price(tag.get_text())
                        if candidate:
                            price = candidate
                            log_activity(f"  → [{selector_name}] Price: ₹{price:,.2f}")
                            break

            # Strategy 9: JS evaluation
            if price is None:
                try:
                    js_price = page.evaluate("""() => {
                        const selectors = [
                            '.a-price .a-offscreen',
                            '#priceblock_ourprice',
                            '#priceblock_dealprice',
                            '#price_inside_buybox',
                            '#newBuyBoxPrice',
                            '.a-price-whole',
                        ];
                        for (const sel of selectors) {
                            const el = document.querySelector(sel);
                            if (el && el.textContent.trim()) {
                                return el.textContent.trim();
                            }
                        }
                        return null;
                    }""")
                    if js_price:
                        price = clean_price(js_price)
                        if price:
                            log_activity(f"  → [JS eval] Price: ₹{price:,.2f}")
                except Exception:
                    pass

            # Strategy 10: Regex fallback
            if price is None:
                price_pattern = re.findall(r'[₹₨]\s*([\d,]+(?:\.\d{1,2})?)', page_text)
                if price_pattern:
                    from collections import Counter
                    price_counts = Counter(price_pattern)
                    most_common = price_counts.most_common(1)[0][0]
                    price = clean_price(most_common)
                    if price:
                        log_activity(f"  → [regex fallback] Price: ₹{price:,.2f}")

            # Stock detection
            in_stock = True
            if ld_price is not None and ld_stock is not None:
                in_stock = ld_stock
            else:
                availability = soup.find("div", {"id": "availability"})
                if availability:
                    avail_text = availability.get_text().lower()
                    if "unavailable" in avail_text or "out of stock" in avail_text:
                        in_stock = False
                if in_stock:
                    unavail_span = soup.find("span", string=re.compile(r"currently unavailable", re.I))
                    if unavail_span:
                        in_stock = False

            if price is None:
                log_activity(f"  → All strategies failed. Title: '{page_title_text}'", "WARN")
                log_activity(f"  → Page size: {len(page_text)} bytes. Has 'a-price': {'a-price' in page_text}", "WARN")

            return price, in_stock

        except Exception as e:
            last_error = str(e)
            log_activity(f"Amazon scrape error (attempt {attempt+1}): {last_error}", "ERROR")
            if attempt < retries:
                time.sleep(random.uniform(3, 6))
                continue
        finally:
            try:
                if page:
                    page.close()
                if context:
                    context.close()
            except Exception:
                pass

    log_activity(f"Amazon scrape failed after {retries+1} attempts: {last_error}", "ERROR")
    return None, False


def scrape_flipkart(url):
    """Scrape product price and stock status from Flipkart.
    
    Strategy order:
    1. JSON-LD structured data (most reliable, Flipkart always serves this)
    2. Rupee symbol regex from HTML (class names change frequently)
    3. Playwright fallback for JS-rendered content
    """
    # ─── Phase 1: requests-based scrape ───
    try:
        log_activity(f"  → [Flipkart] Trying requests-based scrape...")
        session = create_scraper()
        res = session.get(url, timeout=15)

        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            title_tag = soup.find("title")
            title_text = title_tag.get_text()[:60] if title_tag else "unknown"
            log_activity(f"  → [Flipkart] Page: '{title_text}' ({len(res.text)} bytes)")

            # Check if blocked (Flipkart may serve challenge pages)
            if res.status_code == 429 or "access denied" in res.text[:2000].lower():
                log_activity(f"  → [Flipkart] Rate limited or blocked, trying Playwright.", "WARN")
            else:
                # Strategy 1: JSON-LD (MOST RELIABLE)
                price, in_stock = _extract_json_ld_price(soup)
                if price is not None:
                    log_activity(f"  → [Flipkart/JSON-LD] Price: ₹{price:,.2f}")
                    return price, in_stock

                # Strategy 2: First ₹ price on the page (typically the product price)
                rupee_tags = soup.find_all(string=re.compile(r'₹'))
                for tag_text in rupee_tags:
                    text = tag_text.strip()
                    # Look for clean price patterns like "₹56,900" — skip long texts
                    if len(text) < 20 and re.match(r'^₹[\d,]+(?:\.\d{1,2})?$', text.replace(' ', '')):
                        candidate = clean_price(text)
                        if candidate and candidate > 0:
                            price = candidate
                            log_activity(f"  → [Flipkart/rupee-tag] Price: ₹{price:,.2f}")
                            break

                if price is not None:
                    page_text = soup.get_text().lower()
                    in_stock = "sold out" not in page_text and "currently unavailable" not in page_text
                    return price, in_stock

                # Strategy 3: Regex scan for ₹ patterns in raw HTML
                price_pattern = re.findall(r'₹\s*([\d,]+(?:\.\d{1,2})?)', res.text)
                if price_pattern:
                    # Take the first reasonable price (product prices are typically shown first)
                    for p_str in price_pattern:
                        candidate = clean_price(p_str)
                        if candidate and candidate > 10:  # Filter out tiny noise values
                            price = candidate
                            log_activity(f"  → [Flipkart/regex] Price: ₹{price:,.2f}")
                            break

                if price is not None:
                    page_text = soup.get_text().lower()
                    in_stock = "sold out" not in page_text and "currently unavailable" not in page_text
                    return price, in_stock

                log_activity(f"  → [Flipkart/requests] No price found, trying Playwright fallback.", "WARN")

    except Exception as e:
        log_activity(f"  → [Flipkart/requests] Error: {str(e)[:100]}", "WARN")

    # ─── Phase 2: Playwright fallback ───
    context = None
    page = None
    try:
        log_activity(f"  → [Flipkart/Playwright] Launching browser scrape...")
        browser = _get_browser()
        context = _create_stealth_context(browser)
        page = context.new_page()

        # Block heavy resources
        page.route("**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf}", lambda route: route.abort())
        page.route("**/ads/**", lambda route: route.abort())

        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(random.randint(2000, 4000))

        page_text = page.content()
        soup = BeautifulSoup(page_text, "html.parser")

        # Try JSON-LD first
        price, in_stock = _extract_json_ld_price(soup)
        if price is not None:
            log_activity(f"  → [Flipkart/Playwright JSON-LD] Price: ₹{price:,.2f}")
            return price, in_stock

        # Try JS evaluation for price
        try:
            js_price = page.evaluate("""() => {
                // Look for price elements containing ₹
                const all = document.querySelectorAll('div, span');
                for (const el of all) {
                    const text = el.textContent.trim();
                    if (/^₹[\\d,]+(\\.\\d{1,2})?$/.test(text) && text.length < 20) {
                        return text;
                    }
                }
                return null;
            }""")
            if js_price:
                price = clean_price(js_price)
                if price:
                    log_activity(f"  → [Flipkart/JS eval] Price: ₹{price:,.2f}")
        except Exception:
            pass

        # Regex fallback on rendered HTML
        if price is None:
            price, in_stock_html = _extract_price_from_html(soup, page_text)
            if price:
                log_activity(f"  → [Flipkart/Playwright HTML] Price: ₹{price:,.2f}")
                return price, in_stock_html

        # Stock detection
        text_lower = soup.get_text().lower()
        in_stock = "sold out" not in text_lower and "currently unavailable" not in text_lower

        if price is None:
            log_activity(f"  → [Flipkart] All strategies failed.", "WARN")

        return price, in_stock

    except Exception as e:
        log_activity(f"Flipkart Playwright error: {str(e)}", "ERROR")
        return None, False
    finally:
        try:
            if page:
                page.close()
            if context:
                context.close()
        except Exception:
            pass


def scrape_snapdeal(url):
    """Scrape product price and stock status from Snapdeal.
    
    Strategy order:
    1. JSON-LD structured data
    2. Known CSS selectors (payBlkBig, pdp-final-price, etc.)
    3. Rupee regex fallback
    """
    try:
        session = create_scraper()
        res = session.get(url, timeout=15)
        soup = BeautifulSoup(res.text, "html.parser")

        # Strategy 1: JSON-LD
        price, in_stock = _extract_json_ld_price(soup)
        if price is not None:
            log_activity(f"  → [Snapdeal/JSON-LD] Price: ₹{price:,.2f}")
            return price, in_stock

        price = None

        # Strategy 2: Known Snapdeal selectors
        for selector_name, finder in [
            ("payBlkBig", lambda: soup.find("span", {"class": "payBlkBig"})),
            ("pdp-final-price", lambda: soup.find("span", {"class": "pdp-final-price"})),
            ("pdp-e-i-P-r", lambda: soup.find("span", {"class": "pdp-e-i-P-r"})),
            ("price-payable", lambda: soup.select_one(".pdp-e-i-PAY-r span")),
        ]:
            tag = finder()
            if tag:
                candidate = clean_price(tag.get_text())
                if candidate:
                    price = candidate
                    log_activity(f"  → [Snapdeal/{selector_name}] Price: ₹{price:,.2f}")
                    break

        # Strategy 3: Regex fallback
        if price is None:
            price, _ = _extract_price_from_html(soup, res.text)
            if price:
                log_activity(f"  → [Snapdeal/regex] Price: ₹{price:,.2f}")

        # Stock detection
        in_stock = "sold out" not in soup.get_text().lower()

        return price, in_stock

    except Exception as e:
        log_activity(f"Snapdeal scrape error: {str(e)}", "ERROR")
        return None, False


def scrape_generic(url):
    """Generic fallback scraper — works for any e-commerce site.
    
    Strategy order:
    1. JSON-LD structured data (most sites serve this for SEO)
    2. Open Graph / meta tag prices
    3. Price-class CSS selectors
    4. Rupee regex scan
    5. Playwright fallback for JS-rendered sites
    """
    # ─── Phase 1: requests-based scrape ───
    try:
        log_activity(f"  → [Generic] Trying requests-based scrape...")
        session = create_scraper()
        res = session.get(url, timeout=15)
        soup = BeautifulSoup(res.text, "html.parser")

        # Strategy 1: JSON-LD
        price, in_stock = _extract_json_ld_price(soup)
        if price is not None:
            log_activity(f"  → [Generic/JSON-LD] Price: ₹{price:,.2f}")
            return price, in_stock

        # Strategy 2: Meta tags (og:price, product:price:amount, etc.)
        price = None
        for meta in soup.find_all("meta"):
            prop = (meta.get("property", "") + meta.get("name", "")).lower()
            content = meta.get("content", "")
            if any(kw in prop for kw in ["price:amount", "price", "amount"]):
                candidate = clean_price(content)
                if candidate:
                    price = candidate
                    log_activity(f"  → [Generic/meta] Price: ₹{price:,.2f}")
                    break

        if price is not None:
            text_lower = soup.get_text().lower()
            in_stock = "out of stock" not in text_lower and "sold out" not in text_lower
            return price, in_stock

        # Strategy 3: Price-class CSS selectors
        price, in_stock = _extract_price_from_html(soup, res.text)
        if price is not None:
            log_activity(f"  → [Generic/HTML] Price: ₹{price:,.2f}")
            return price, in_stock

        log_activity(f"  → [Generic/requests] No price found, trying Playwright.", "WARN")

    except Exception as e:
        log_activity(f"  → [Generic/requests] Error: {str(e)[:100]}", "WARN")

    # ─── Phase 2: Playwright fallback for JS-heavy sites ───
    context = None
    page = None
    try:
        log_activity(f"  → [Generic/Playwright] Launching browser scrape...")
        browser = _get_browser()
        context = _create_stealth_context(browser)
        page = context.new_page()

        page.route("**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf}", lambda route: route.abort())
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(random.randint(2000, 4000))

        page_text = page.content()
        soup = BeautifulSoup(page_text, "html.parser")

        # Try JSON-LD from rendered page
        price, in_stock = _extract_json_ld_price(soup)
        if price is not None:
            log_activity(f"  → [Generic/Playwright JSON-LD] Price: ₹{price:,.2f}")
            return price, in_stock

        # Try HTML extraction from rendered page
        price, in_stock = _extract_price_from_html(soup, page_text)
        if price is not None:
            log_activity(f"  → [Generic/Playwright HTML] Price: ₹{price:,.2f}")
            return price, in_stock

        log_activity(f"  → [Generic] All strategies failed.", "WARN")
        return None, True

    except Exception as e:
        log_activity(f"Generic Playwright error: {str(e)}", "ERROR")
        return None, False
    finally:
        try:
            if page:
                page.close()
            if context:
                context.close()
        except Exception:
            pass


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
            cursor.execute("SELECT id, name, url, target_price, platform, user_id FROM products WHERE is_active = 1")
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
                uid = product["user_id"]

                # Check when it was last successfully scraped (history entries only exist for non-null/successful scrapes)
                cursor.execute(
                    "SELECT timestamp FROM history WHERE product_id=? ORDER BY rowid DESC LIMIT 1",
                    (pid,)
                )
                last_scrape = cursor.fetchone()
                if last_scrape:
                    try:
                        last_time = datetime.strptime(last_scrape["timestamp"], "%Y-%m-%d %H:%M:%S")
                        elapsed = (datetime.now() - last_time).total_seconds()
                        if elapsed < 3600:
                            # Skip scraping this product in this cycle
                            print(f"[Monitor] Skipping {name} (last successful scrape was {int(elapsed // 60)} minutes ago)")
                            continue
                    except Exception as parse_err:
                        print(f"[Monitor] Error parsing timestamp: {parse_err}")

                detected = detect_platform(url) if platform == "auto" else platform
                log_user_activity(uid, f"Scraping [{detected.upper()}] {name}...")

                price, in_stock = get_product_data(url, platform)

                # Small delay between scrapes to avoid rate limiting
                time.sleep(2)

                if price is not None:
                    log_user_activity(uid, f"  → ₹{price:,.2f} {'(In Stock)' if in_stock else '(Out of Stock)'}")

                    # Save to history
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    cursor.execute(
                        "INSERT INTO history (product_id, price, stock, timestamp) VALUES (?, ?, ?, ?)",
                        (pid, price, 1 if in_stock else 0, timestamp)
                    )
                    conn.commit()

                    # Price drop alert (only alert and stop tracking if product is in stock)
                    if price <= target and in_stock:
                        alert_msg = f"🔥 {name} dropped to ₹{price:,.2f} (target: ₹{target:,.2f})!"
                        log_user_activity(uid, f"ALERT: {alert_msg}", "ALERT")
                        # Send notifications
                        send_whatsapp(f"💰 PricePulse Alert!\n\n{alert_msg}\n\n🔗 {url}", uid)
                        send_email(
                            f"Price Drop: {name}",
                            f"{alert_msg}\n\nProduct: {name}\nLink: {url}",
                            uid
                        )
                        # Deactivate product since alert is sent
                        cursor.execute("UPDATE products SET is_active = 0 WHERE id = ?", (pid,))
                        conn.commit()
                else:
                    log_user_activity(uid, f"  → Could not extract price (site may be blocking)", "WARN")

                if in_stock:
                    # Only alert if it was previously out of stock
                    cursor.execute(
                        "SELECT stock FROM history WHERE product_id=? ORDER BY rowid DESC LIMIT 1 OFFSET 1",
                        (pid,)
                    )
                    prev = cursor.fetchone()
                    if prev and prev["stock"] == 0:
                        stock_msg = f"✅ {name} is back in stock!"
                        log_user_activity(uid, f"ALERT: {stock_msg}", "ALERT")
                        # Send notifications
                        send_whatsapp(f"📦 PricePulse Alert!\n\n{stock_msg}\n\n🔗 {url}", uid)
                        send_email(
                            f"Back In Stock: {name}",
                            f"{stock_msg}\n\nProduct: {name}\nLink: {url}",
                            uid
                        )

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
# Flask Routes — Authentication
# ═══════════════════════════════════════════════════════════════
@app.route("/login", methods=["GET", "POST"])
def login_route():
    """Handle user login."""
    if "user_id" in session:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        if not username or not password:
            return render_template("login.html", error="Username and password are required.")

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT id, password_hash FROM users WHERE username=?", (username,))
        user = cursor.fetchone()
        conn.close()

        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["username"] = username
            log_user_activity(user["id"], "User logged in.")
            return redirect(url_for("index"))
        else:
            return render_template("login.html", error="Invalid username or password.")

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register_route():
    """Handle user registration."""
    if "user_id" in session:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        confirm_password = request.form.get("confirm_password", "").strip()

        if not username or not password or not confirm_password:
            return render_template("register.html", error="All fields are required.")

        if password != confirm_password:
            return render_template("register.html", error="Passwords do not match.")

        if len(password) < 6:
            return render_template("register.html", error="Password must be at least 6 characters long.")

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE username=?", (username,))
        if cursor.fetchone():
            conn.close()
            return render_template("register.html", error="Username is already taken.")

        # Create user
        hashed = generate_password_hash(password)
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            cursor.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                (username, hashed, created_at)
            )
            user_id = cursor.lastrowid
            
            # Create config for user
            cursor.execute(
                "INSERT INTO user_configs (user_id) VALUES (?)",
                (user_id,)
            )
            conn.commit()
            
            log_user_activity(user_id, f"Account registered for '{username}'.")
            session["user_id"] = user_id
            session["username"] = username
            return redirect(url_for("index"))
        except Exception as e:
            return render_template("register.html", error=f"Registration error: {str(e)}")
        finally:
            conn.close()

    return render_template("register.html")


@app.route("/logout", methods=["GET", "POST"])
def logout_route():
    """Log out the current user."""
    user_id = session.get("user_id")
    if user_id:
        log_user_activity(user_id, "User logged out.")
    session.clear()
    return redirect(url_for("login_route"))


@app.route("/api/user-status", methods=["GET"])
def api_user_status():
    """Get active user status."""
    if "user_id" in session:
        return jsonify({
            "authenticated": True,
            "username": session["username"],
            "user_id": session["user_id"]
        })
    return jsonify({"authenticated": False})


# ═══════════════════════════════════════════════════════════════
# Flask Routes — Pages
# ═══════════════════════════════════════════════════════════════
@app.route("/")
@login_required
def index():
    """Serve the main dashboard."""
    return render_template("index.html")


# ═══════════════════════════════════════════════════════════════
# Flask Routes — API
# ═══════════════════════════════════════════════════════════════
@app.route("/api/products", methods=["GET"])
@login_required
def api_get_products():
    """List all products for the logged-in user with their latest scraped price."""
    uid = session["user_id"]
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT id, name, url, target_price, platform, is_active FROM products WHERE user_id=? ORDER BY id DESC", (uid,))
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
            "is_active": bool(p["is_active"]),
            "price": latest["price"] if latest else None,
            "stock": bool(latest["stock"]) if latest else True,
            "last_updated": latest["timestamp"] if latest else None,
        })

    conn.close()
    return jsonify(result)


def scrape_single_product_background(product_id, user_id, name, url, target_price, platform):
    """Scrape a newly added product immediately in the background."""
    try:
        log_activity(f"Background scrape triggered for new product: {name}")
        price, in_stock = get_product_data(url, platform)
        
        conn = get_db()
        cursor = conn.cursor()
        
        if price is not None:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute(
                "INSERT INTO history (product_id, price, stock, timestamp) VALUES (?, ?, ?, ?)",
                (product_id, price, 1 if in_stock else 0, timestamp)
            )
            conn.commit()
            
            # Check price drop (only alert and stop tracking if product is in stock)
            if price <= target_price and in_stock:
                alert_msg = f"🔥 {name} dropped to ₹{price:,.2f} (target: ₹{target_price:,.2f})!"
                log_user_activity(user_id, f"ALERT: {alert_msg}", "ALERT")
                send_whatsapp(f"💰 PricePulse Alert!\n\n{alert_msg}\n\n🔗 {url}", user_id)
                send_email(
                    f"Price Drop: {name}",
                    f"{alert_msg}\n\nProduct: {name}\nLink: {url}",
                    user_id
                )
                # Deactivate product since alert is sent
                cursor.execute("UPDATE products SET is_active = 0 WHERE id = ?", (product_id,))
            else:
                # Scrape succeeded, but price did not drop or it's out of stock: activate product for periodic checking
                cursor.execute("UPDATE products SET is_active = 1 WHERE id = ?", (product_id,))
            conn.commit()
            log_user_activity(user_id, f"Initial scrape completed for {name}: ₹{price:,.2f}")
        else:
            # Scrape failed (blocked/etc.): set to active so it retries in the main loop
            cursor.execute("UPDATE products SET is_active = 1 WHERE id = ?", (product_id,))
            conn.commit()
            log_user_activity(user_id, f"Initial scrape: could not extract price for {name} (site may be blocking)", "WARN")
            
        conn.close()
    except Exception as e:
        log_activity(f"Error in background scrape for product {product_id}: {str(e)}", "ERROR")
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("UPDATE products SET is_active = 1 WHERE id = ?", (product_id,))
            conn.commit()
            conn.close()
        except Exception:
            pass


@app.route("/api/products", methods=["POST"])
@login_required
def api_add_product():
    """Add a new product to track for the logged-in user."""
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

    uid = session["user_id"]
    conn = get_db()
    cursor = conn.cursor()
    # Insert with is_active = 2 (initial scraping in progress) to avoid loop race conditions
    cursor.execute(
        "INSERT INTO products (user_id, name, url, target_price, platform, is_active) VALUES (?, ?, ?, ?, ?, 2)",
        (uid, name, url, target_price, platform)
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()

    log_user_activity(uid, f"Added product: {name} [{platform.upper()}] (target: ₹{target_price:,.2f})", "ALERT")

    # Start immediate scrape in background thread
    threading.Thread(
        target=scrape_single_product_background,
        args=(new_id, uid, name, url, target_price, platform),
        daemon=True
    ).start()

    return jsonify({"success": True, "id": new_id, "platform": platform})


@app.route("/api/products/<int:product_id>", methods=["DELETE"])
@login_required
def api_delete_product(product_id):
    """Delete a product and all its price history if owned by the user."""
    uid = session["user_id"]
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT name FROM products WHERE id=? AND user_id=?", (product_id, uid))
    product = cursor.fetchone()

    if not product:
        conn.close()
        return jsonify({"success": False, "error": "Product not found or unauthorized."}), 404

    cursor.execute("DELETE FROM history WHERE product_id=?", (product_id,))
    cursor.execute("DELETE FROM products WHERE id=?", (product_id,))
    conn.commit()
    conn.close()

    log_user_activity(uid, f"Deleted product: {product['name']} (ID: {product_id})", "ALERT")

    return jsonify({"success": True})


@app.route("/api/products/<int:product_id>/history", methods=["GET"])
@login_required
def api_get_history(product_id):
    """Get price history for a specific product if owned by the user."""
    uid = session["user_id"]
    conn = get_db()
    cursor = conn.cursor()

    # Verify ownership
    cursor.execute("SELECT id FROM products WHERE id=? AND user_id=?", (product_id, uid))
    if not cursor.fetchone():
        conn.close()
        return jsonify({"success": False, "error": "Unauthorized."}), 401

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
@login_required
def api_start_monitor():
    """Start the background scraping monitor."""
    success, message = start_monitor()
    return jsonify({"success": success, "message": message})


@app.route("/api/monitor/stop", methods=["POST"])
@login_required
def api_stop_monitor():
    """Stop the background scraping monitor."""
    success, message = stop_monitor()
    return jsonify({"success": success, "message": message})


@app.route("/api/monitor/status", methods=["GET"])
@login_required
def api_monitor_status():
    """Get monitor running status and user-specific activity logs."""
    uid = session["user_id"]
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT message, level, timestamp FROM activity_logs WHERE user_id=? ORDER BY id DESC LIMIT 100",
        (uid,)
    )
    db_logs = cursor.fetchall()
    conn.close()

    # Format logs to match visual display "[timestamp] prefix: message"
    logs = []
    for log in reversed(db_logs):
        # Format date time to HH:MM:SS
        try:
            dt = datetime.strptime(log["timestamp"], "%Y-%m-%d %H:%M:%S")
            ts = dt.strftime("%H:%M:%S")
        except ValueError:
            ts = log["timestamp"]
        
        level = log["level"]
        prefix = ""
        if level == "ALERT":
            prefix = "ALERT: "
        elif level == "ERROR":
            prefix = "Error: "
        elif level == "WARN":
            prefix = "Warning: "
        
        logs.append(f"[{ts}] {prefix}{log['message']}")

    return jsonify({
        "running": monitor_running,
        "logs": logs,
    })


@app.route("/api/stats", methods=["GET"])
@login_required
def api_stats():
    """Get dashboard statistics for the logged-in user."""
    uid = session["user_id"]
    conn = get_db()
    cursor = conn.cursor()

    # Total products for this user
    cursor.execute("SELECT COUNT(*) as total FROM products WHERE user_id=?", (uid,))
    total = cursor.fetchone()["total"]

    # Products with price drops (current price <= target)
    drops = 0
    in_stock_count = 0

    cursor.execute("SELECT id, target_price FROM products WHERE user_id=?", (uid,))
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
@login_required
def api_get_whatsapp_config():
    """Get current WhatsApp notification settings for the logged-in user (token masked)."""
    uid = session["user_id"]
    config = load_whatsapp_config(uid)
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
@login_required
def api_save_whatsapp_config():
    """Save WhatsApp notification settings for the logged-in user."""
    data = request.get_json()
    uid = session["user_id"]

    config = load_whatsapp_config(uid)
    config["enabled"] = data.get("enabled", config["enabled"])
    config["account_sid"] = data.get("account_sid", config["account_sid"])
    if data.get("auth_token"):  # Only update if provided (not masked)
        config["auth_token"] = data["auth_token"]
    config["from_number"] = data.get("from_number", config["from_number"])
    config["to_number"] = data.get("to_number", config["to_number"])

    save_whatsapp_config(config, uid)
    log_user_activity(uid, f"WhatsApp settings updated. Notifications {'enabled' if config['enabled'] else 'disabled'}.", "ALERT")

    return jsonify({"success": True})


@app.route("/api/whatsapp/test", methods=["POST"])
@login_required
def api_test_whatsapp():
    """Send a test WhatsApp message for the logged-in user."""
    uid = session["user_id"]
    config = load_whatsapp_config(uid)
    if not config.get("enabled"):
        return jsonify({"success": False, "error": "WhatsApp notifications are disabled. Enable them first."})

    success, message = send_whatsapp(
        "🧪 PricePulse Test Message\n\nYour WhatsApp notifications are working! "
        "You'll receive alerts when tracked product prices drop below your target.",
        uid
    )
    return jsonify({"success": success, "message": message})


# ═══════════════════════════════════════════════════════════════
# Flask Routes — Email Settings API (Simplified)
# ═══════════════════════════════════════════════════════════════
@app.route("/api/email/config", methods=["GET"])
@login_required
def api_get_email_config():
    """Get user's notification email and whether SMTP is configured."""
    uid = session["user_id"]
    config = load_email_config(uid)
    return jsonify({
        "enabled": config.get("enabled", False),
        "recipient_email": config.get("recipient_email", ""),
        "smtp_configured": is_smtp_configured(),
    })


@app.route("/api/email/config", methods=["POST"])
@login_required
def api_save_email_config():
    """Save user's notification email. Just email + toggle — that's it."""
    data = request.get_json()
    uid = session["user_id"]

    recipient = data.get("recipient_email", "").strip()
    enabled = data.get("enabled", False)

    # Basic email validation
    if enabled and (not recipient or "@" not in recipient):
        return jsonify({"success": False, "error": "Please enter a valid email address."}), 400

    save_email_config({"enabled": enabled, "recipient_email": recipient}, uid)
    log_user_activity(uid, f"Email alerts {'enabled' if enabled else 'disabled'} for {recipient}.", "ALERT")

    return jsonify({"success": True})


@app.route("/api/email/test", methods=["POST"])
@login_required
def api_test_email():
    """Send a test email to the logged-in user."""
    uid = session["user_id"]
    config = load_email_config(uid)
    if not config.get("enabled"):
        return jsonify({"success": False, "error": "Turn on email alerts first."})

    if not is_smtp_configured():
        return jsonify({"success": False, "error": "Email service not configured. Set SMTP_EMAIL and SMTP_PASSWORD in .env file."})

    success, message = send_email(
        "PricePulse Test Email",
        "Your email notifications are working!\n\n"
        "You will receive alerts when tracked product prices drop below your target.",
        uid
    )
    return jsonify({"success": success, "message": message})


# ═══════════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    init_db()
    
    # Configure debug mode (defaults to True, but checks environment variables)
    debug_mode = os.environ.get("FLASK_DEBUG", "true").lower() in ("true", "1")
    app.debug = debug_mode
    
    # If Flask reloader is enabled, only start the monitor in the active child process
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
        start_monitor()
        
    log_activity("PricePulse server initialized. Ready to track prices.", "ALERT")
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=debug_mode, host="0.0.0.0", port=port)