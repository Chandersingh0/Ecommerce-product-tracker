---
title: Price Tracking
emoji: 🛒
colorFrom: indigo
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
---

# 🛒 PricePulse — Premium Multi-Platform Price Tracker

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10-blue?style=for-the-badge&logo=python&logoColor=white"/>
  <img src="https://img.shields.io/badge/Flask-3.0-black?style=for-the-badge&logo=flask&logoColor=white"/>
  <img src="https://img.shields.io/badge/Docker-Compatible-blue?style=for-the-badge&logo=docker&logoColor=white"/>
  <img src="https://img.shields.io/badge/Playwright-Bypass-green?style=for-the-badge&logo=playwright&logoColor=white"/>
  <img src="https://img.shields.io/badge/WhatsApp-Alerts-25D366?style=for-the-badge&logo=whatsapp&logoColor=white"/>
</p>

<p align="center">
  Track product prices across <strong>Amazon</strong>, <strong>Flipkart</strong>, and <strong>Snapdeal</strong> — all from a single sleek, professional dashboard. Get instant <strong>WhatsApp & Email alerts</strong> when prices drop to your target! 🔥
</p>

---

## ✨ Features

| Feature | Description |
|---|---|
| 🛍️ **Multi-Platform** | Scrapes Amazon India, Flipkart & Snapdeal simultaneously |
| 🤖 **Auto-Detection** | Paste any product URL — platform is detected automatically |
| 📈 **Price History** | Interactive Chart.js bezier-curve graphs showing price trends over time |
| 📦 **Stock Alerts** | Dynamic available-to-out ratio bar meter and stock recovery alerts |
| 💬 **WhatsApp Alerts** | Real-time WhatsApp notifications via Twilio REST API on price drops |
| ✉️ **Email Alerts** | Custom SMTP email notifications (works with Gmail App Passwords) |
| 🎨 **Premium UI** | Modern light-mode tech dashboard with left sidebar navigation tabs |
| 🔄 **Auto Monitor** | Background thread scrapes all products at regular intervals |
| 💾 **Persistent DB** | All data stored locally/persistently in SQLite — no complex cloud setup |
| 🐳 **Docker Ready** | Fully dockerized and optimized for Hugging Face Spaces deployment |

---

## 🚀 Quick Start (Local Run)

### 1. Clone the repository

```bash
git clone https://github.com/Chandersingh0/Ecommerce-product-tracker.git
cd pricepulse
```

### 2. Create a virtual environment

```bash
# Windows
python -m venv venv
venv\Scripts\activate

# macOS / Linux
python3 -m venv venv
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
playwright install-deps
```

### 4. Run the app

```bash
python app.py
```

### 5. Open your browser

Navigate to `http://localhost:5000` (Default credentials: **admin / admin**).

---

## 📲 Notification Setup (Optional)

Configure your alert channels directly inside the **"Alert Settings"** tab in the sidebar:

### A. WhatsApp Setup (via Twilio Sandbox)
1. Register a free account at [twilio.com](https://www.twilio.com).
2. Go to **Messaging → Try WhatsApp** and opt-in by sending `join <sandbox-code>` from your WhatsApp.
3. Paste your **Account SID**, **Auth Token**, **From number** (sandbox), and **Recipient number** in the Settings panel.

### B. Email Setup (via SMTP / Gmail)
1. Go to your Google Account Settings &rarr; Security.
2. Enable **2-Step Verification**.
3. Generate a 16-character **App Password** for "Mail".
4. Configure the SMTP server (`smtp.gmail.com`), port (`587`), sender email, and App Password in the Settings panel.

---

## 🐳 Hugging Face Spaces Deployment

This project is configured out-of-the-box to run as a **Hugging Face Space** using the custom Docker SDK. 

1. Create a new Space on Hugging Face, select **Docker** as the SDK, and choose **Blank** template.
2. Add your Space remote and push the codebase:
   ```bash
   git remote add hf https://huggingface.co/spaces/your-username/your-space-name
   git push -f hf main
   ```
3. To persist your database across Space restarts, go to the Space's **Settings** and attach a free **Persistent Storage** mount. The database will automatically be stored on the persistent `/data` partition.

---

## 🗂️ Project Structure

```
pricepulse/
│
├── app.py                  # Flask webapp + scrapers + background monitor threads
├── Dockerfile              # Playwright, Chromium & Hugging Face optimized build file
├── requirements.txt        # python packages
├── products.db             # Local SQLite database (auto-generated)
│
└── templates/
    ├── index.html          # Core responsive light-theme dashboard UI
    ├── login.html          # Polished sign-in interface
    └── register.html       # Polished account registration interface
```

---

## 🛠️ Tech Stack

- **Backend:** Python 3, Flask
- **Scraping:** Requests, BeautifulSoup4, Playwright Chromium (for anti-bot rendering)
- **Database:** SQLite (via standard library `sqlite3`)
- **Frontend:** HTML5, CSS3 Grid/Flexbox, Vanilla JS, Chart.js, Font Awesome
- **Notifications:** Twilio REST API, SMTP SSL/TLS

---

## 📝 License

This project is licensed under the **MIT License**.
