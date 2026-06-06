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
# Email Notification Config (SMTP)
# ═══════════════════════════════════════════════════════════════
EMAIL_CONFIG_FILE = "email_config.json"

DEFAULT_EMAIL_CONFIG = {
    "enabled": False,
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 587,
    "sender_email": "",
    "sender_password": "",     # App password for Gmail
    "recipient_email": "",
}


def load_email_config(user_id):
    """Load email config from SQLite database."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT email_enabled as enabled, email_smtp_server as smtp_server,
               email_smtp_port as smtp_port, email_sender as sender_email,
               email_password as sender_password, email_recipient as recipient_email
        FROM user_configs WHERE user_id=?
    """, (user_id,))
    row = cursor.fetchone()
    conn.close()

    if row:
        config = dict(row)
        config["enabled"] = bool(config["enabled"])
        return config
    return DEFAULT_EMAIL_CONFIG.copy()


def save_email_config(config, user_id):
    """Save email config to SQLite database."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE user_configs SET
            email_enabled=?,
            email_smtp_server=?,
            email_smtp_port=?,
            email_sender=?,
            email_password=?,
            email_recipient=?
        WHERE user_id=?
    """, (
        1 if config.get("enabled") else 0,
        config.get("smtp_server", "smtp.gmail.com"),
        int(config.get("smtp_port", 587)),
        config.get("sender_email", ""),
        config.get("sender_password", ""),
        config.get("recipient_email", ""),
        user_id
    ))
    conn.commit()
    conn.close()


def send_email(subject, body, user_id):
    """Send an email notification via SMTP for the specified user."""
    config = load_email_config(user_id)

    if not config.get("enabled"):
        return False, "Email notifications are disabled."

    smtp_server = config.get("smtp_server", "").strip()
    smtp_port = int(config.get("smtp_port", 587))
    sender = config.get("sender_email", "").strip()
    password = config.get("sender_password", "").strip()
    recipient = config.get("recipient_email", "").strip()

    if not all([smtp_server, sender, password, recipient]):
        return False, "Email config is incomplete."

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = f"PricePulse <{sender}>"
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

        with smtplib.SMTP(smtp_server, smtp_port, timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(sender, password)
            server.sendmail(sender, recipient, msg.as_string())

        log_user_activity(user_id, f"Email sent: {subject}", "ALERT")
        return True, "Email sent successfully."

    except smtplib.SMTPAuthenticationError:
        error_msg = "SMTP authentication failed. Check your email/password (use App Password for Gmail)."
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

    # Create products table with user_id
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL DEFAULT 1,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            target_price REAL NOT NULL,
            platform TEXT DEFAULT 'auto',
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
def scrape_amazon(url, retries=2):
    """Scrape product price and stock from Amazon using Playwright headless browser.
    
    Uses a real Chromium browser to bypass Amazon's JS-based bot detection.
    The browser executes JavaScript, handles redirects, and renders the full page
    before we extract the price from the DOM.
    """
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

            log_activity(f"  -> [Playwright] Loading page (attempt {attempt+1}/{retries+1})...")

            # Navigate and wait for the page to be fully loaded
            page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # Wait a moment for any JS redirects / dynamic content
            page.wait_for_timeout(random.randint(2000, 4000))

            # ─── Handle Amazon challenge pages ───
            # Amazon has two types of bot checks:
            # 1. "Continue shopping" button (simple challenge, no image CAPTCHA)
            # 2. Full CAPTCHA with image (harder block)

            # Check for "Continue shopping" challenge (most common)
            continue_btn = page.query_selector('form[action="/errors/validateCaptcha"] button[type="submit"]')
            if continue_btn:
                log_activity(f"  -> [Playwright] 'Continue shopping' challenge detected, clicking button...")
                continue_btn.click()
                # Wait for navigation to complete and real page to load
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                    page.wait_for_timeout(random.randint(3000, 5000))
                    log_activity(f"  -> [Playwright] Challenge bypassed, page title: '{page.title()[:60]}'")
                except Exception as nav_err:
                    log_activity(f"  -> [Playwright] Post-challenge navigation timeout: {nav_err}", "WARN")

            # Check if we're still on a CAPTCHA/challenge page after attempting bypass
            current_title = page.title().lower()
            page_html_snippet = page.content()[:2000].lower()
            is_captcha = (
                "sorry" in current_title
                or "robot" in current_title
                or ("amazon" == current_title.strip() or "amazon.in" == current_title.strip())
                and len(page.content()) < 15000
                and "validatecaptcha" in page_html_snippet
            )

            if is_captcha:
                log_activity(f"  -> [Playwright] Still on challenge page after bypass attempt (attempt {attempt+1})", "WARN")
                if attempt < retries:
                    time.sleep(random.uniform(5, 10))
                    continue
                log_activity("Amazon is blocking after all retries.", "ERROR")
                return None, False

            # Try to wait for a price element to appear
            try:
                page.wait_for_selector(
                    ".a-price-whole, #priceblock_ourprice, #priceblock_dealprice, "
                    "#corePrice_feature_div, #price_inside_buybox, #newBuyBoxPrice",
                    timeout=8000
                )
            except Exception:
                log_activity("  → [Playwright] No price selector found within timeout, continuing with page as-is...", "WARN")

            # Get the fully rendered HTML
            page_text = page.content()
            soup = BeautifulSoup(page_text, "html.parser")

            page_title = soup.find("title")
            page_title_text = page_title.get_text()[:60] if page_title else "unknown"
            log_activity(f"  → [Playwright] Page loaded: '{page_title_text}' ({len(page_text)} bytes)")

            price = None

            # ─── Strategy 0: JSON-LD structured data (MOST RELIABLE) ───
            for script in soup.find_all("script", {"type": "application/ld+json"}):
                try:
                    ld_data = json.loads(script.string)
                    items = ld_data if isinstance(ld_data, list) else [ld_data]
                    for item in items:
                        if item.get("@type") in ("Product", "IndividualProduct"):
                            offers = item.get("offers", {})
                            if isinstance(offers, list):
                                offers = offers[0] if offers else {}
                            p = offers.get("price") or offers.get("lowPrice")
                            if p:
                                price = float(p)
                                log_activity(f"  → [JSON-LD] Extracted price: ₹{price:,.2f}")
                                break
                    if price:
                        break
                except (json.JSONDecodeError, ValueError, TypeError, KeyError):
                    continue

            # ─── Strategy 1: Deal price ───
            if price is None:
                tag = soup.find("span", {"id": "priceblock_dealprice"})
                if tag:
                    price = clean_price(tag.get_text())
                    if price:
                        log_activity(f"  → [dealprice] Extracted price: ₹{price:,.2f}")

            # ─── Strategy 2: Our price ───
            if price is None:
                tag = soup.find("span", {"id": "priceblock_ourprice"})
                if tag:
                    price = clean_price(tag.get_text())
                    if price:
                        log_activity(f"  → [ourprice] Extracted price: ₹{price:,.2f}")

            # ─── Strategy 3: corePrice_feature_div .a-offscreen ───
            if price is None:
                price_div = soup.find("div", {"id": "corePrice_feature_div"})
                if price_div:
                    offscreen = price_div.find("span", {"class": "a-offscreen"})
                    if offscreen:
                        price = clean_price(offscreen.get_text())
                        if price:
                            log_activity(f"  → [corePrice offscreen] Extracted price: ₹{price:,.2f}")

            # ─── Strategy 4: .a-price .a-offscreen (first non-struck-out) ───
            if price is None:
                for a_price in soup.find_all("span", {"class": "a-price"}):
                    if a_price.find_parent(class_=re.compile(r"a-text-strike|priceBlockStrikePrice")):
                        continue
                    offscreen = a_price.find("span", {"class": "a-offscreen"})
                    if offscreen:
                        price = clean_price(offscreen.get_text())
                        if price:
                            log_activity(f"  → [a-price offscreen] Extracted price: ₹{price:,.2f}")
                            break

            # ─── Strategy 5: .a-price-whole + .a-price-fraction ───
            if price is None:
                tag = soup.find("span", {"class": "a-price-whole"})
                if tag:
                    whole = tag.get_text().replace(".", "").replace(",", "")
                    fraction_tag = tag.find_next_sibling("span", {"class": "a-price-fraction"})
                    fraction = fraction_tag.get_text() if fraction_tag else "00"
                    try:
                        price = float(f"{whole.strip()}.{fraction.strip()}")
                        log_activity(f"  → [a-price-whole] Extracted price: ₹{price:,.2f}")
                    except ValueError:
                        pass

            # ─── Strategy 6: apex_offerDisplay_desktop ───
            if price is None:
                tag = soup.select_one("#apex_offerDisplay_desktop .a-offscreen")
                if tag:
                    price = clean_price(tag.get_text())
                    if price:
                        log_activity(f"  → [apex_offerDisplay] Extracted price: ₹{price:,.2f}")

            # ─── Strategy 7: price_inside_buybox ───
            if price is None:
                tag = soup.find("span", {"id": "price_inside_buybox"})
                if tag:
                    price = clean_price(tag.get_text())
                    if price:
                        log_activity(f"  → [buybox] Extracted price: ₹{price:,.2f}")

            # ─── Strategy 8: newBuyBoxPrice ───
            if price is None:
                tag = soup.find("span", {"id": "newBuyBoxPrice"})
                if tag:
                    price = clean_price(tag.get_text())
                    if price:
                        log_activity(f"  → [newBuyBox] Extracted price: ₹{price:,.2f}")

            # ─── Strategy 9: Playwright direct JS evaluation ───
            if price is None:
                try:
                    js_price = page.evaluate("""() => {
                        // Try multiple selectors via JS
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
                            log_activity(f"  → [JS eval] Extracted price: ₹{price:,.2f}")
                except Exception:
                    pass

            # ─── Strategy 10: Regex scan for ₹ price in HTML ───
            if price is None:
                price_pattern = re.findall(r'[₹₨]\s*([\d,]+(?:\.\d{1,2})?)', page_text)
                if price_pattern:
                    from collections import Counter
                    price_counts = Counter(price_pattern)
                    most_common = price_counts.most_common(1)[0][0]
                    price = clean_price(most_common)
                    if price:
                        log_activity(f"  → [regex fallback] Extracted price: ₹{price:,.2f}")

            # ─── Stock detection ───
            in_stock = True
            availability = soup.find("div", {"id": "availability"})
            if availability:
                avail_text = availability.get_text().lower()
                if "unavailable" in avail_text or "out of stock" in avail_text:
                    in_stock = False

            if in_stock:
                unavail_span = soup.find("span", string=re.compile(r"currently unavailable", re.I))
                if unavail_span:
                    in_stock = False

            # ─── Diagnostics if price still None ───
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
            cursor.execute("SELECT id, name, url, target_price, platform, user_id FROM products")
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

                    # Price drop alert
                    if price <= target:
                        alert_msg = f"🔥 {name} dropped to ₹{price:,.2f} (target: ₹{target:,.2f})!"
                        log_user_activity(uid, f"ALERT: {alert_msg}", "ALERT")
                        # Send notifications
                        send_whatsapp(f"💰 PricePulse Alert!\n\n{alert_msg}\n\n🔗 {url}", uid)
                        send_email(
                            f"Price Drop: {name}",
                            f"{alert_msg}\n\nProduct: {name}\nLink: {url}",
                            uid
                        )
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

    cursor.execute("SELECT id, name, url, target_price, platform FROM products WHERE user_id=? ORDER BY id DESC", (uid,))
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
    cursor.execute(
        "INSERT INTO products (user_id, name, url, target_price, platform) VALUES (?, ?, ?, ?, ?)",
        (uid, name, url, target_price, platform)
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()

    log_user_activity(uid, f"Added product: {name} [{platform.upper()}] (target: ₹{target_price:,.2f})", "ALERT")

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
# Flask Routes — Email Settings API
# ═══════════════════════════════════════════════════════════════
@app.route("/api/email/config", methods=["GET"])
@login_required
def api_get_email_config():
    """Get current email notification settings for the logged-in user (password masked)."""
    uid = session["user_id"]
    config = load_email_config(uid)
    safe_config = {
        "enabled": config.get("enabled", False),
        "smtp_server": config.get("smtp_server", ""),
        "smtp_port": config.get("smtp_port", 587),
        "sender_email": config.get("sender_email", ""),
        "password_set": bool(config.get("sender_password", "")),
        "recipient_email": config.get("recipient_email", ""),
    }
    return jsonify(safe_config)


@app.route("/api/email/config", methods=["POST"])
@login_required
def api_save_email_config():
    """Save email notification settings for the logged-in user."""
    data = request.get_json()
    uid = session["user_id"]

    config = load_email_config(uid)
    config["enabled"] = data.get("enabled", config["enabled"])
    config["smtp_server"] = data.get("smtp_server", config["smtp_server"])
    config["smtp_port"] = int(data.get("smtp_port", config["smtp_port"]))
    config["sender_email"] = data.get("sender_email", config["sender_email"])
    if data.get("sender_password"):  # Only update if provided
        config["sender_password"] = data["sender_password"]
    config["recipient_email"] = data.get("recipient_email", config["recipient_email"])

    save_email_config(config, uid)
    log_user_activity(uid, f"Email settings updated. Notifications {'enabled' if config['enabled'] else 'disabled'}.", "ALERT")

    return jsonify({"success": True})


@app.route("/api/email/test", methods=["POST"])
@login_required
def api_test_email():
    """Send a test email for the logged-in user."""
    uid = session["user_id"]
    config = load_email_config(uid)
    if not config.get("enabled"):
        return jsonify({"success": False, "error": "Email notifications are disabled. Enable them first."})

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
    log_activity("PricePulse server initialized. Ready to track prices.", "ALERT")
    app.run(debug=True, host="0.0.0.0", port=5000)