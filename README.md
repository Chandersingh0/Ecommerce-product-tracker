# 🛒 PricePulse — Multi-Platform Price Tracker

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.8+-blue?style=for-the-badge&logo=python&logoColor=white"/>
  <img src="https://img.shields.io/badge/Flask-3.0-black?style=for-the-badge&logo=flask&logoColor=white"/>
  <img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge"/>
  <img src="https://img.shields.io/badge/WhatsApp-Alerts-25D366?style=for-the-badge&logo=whatsapp&logoColor=white"/>
</p>

<p align="center">
  Track product prices across <strong>Amazon</strong>, <strong>Flipkart</strong>, and <strong>Snapdeal</strong> — all from a single sleek dashboard. Get instant <strong>WhatsApp alerts</strong> when prices drop to your target! 🔥
</p>

---

## ✨ Features

| Feature | Description |
|---|---|
| 🛍️ **Multi-Platform** | Scrapes Amazon India, Flipkart & Snapdeal simultaneously |
| 🤖 **Auto-Detection** | Paste any product URL — platform is detected automatically |
| 📉 **Price History** | Interactive Chart.js graphs showing price trends over time |
| 📦 **Stock Alerts** | Get notified when out-of-stock items become available |
| 💬 **WhatsApp Alerts** | Real-time WhatsApp messages via Twilio on price drops |
| 🌙 **Dark UI** | Premium glassmorphism dashboard — looks stunning |
| 🔄 **Auto Monitor** | Background thread scrapes all products every 60 seconds |
| 💾 **Persistent DB** | All data stored locally in SQLite — no cloud dependency |

---

## 📸 Screenshot

> Dashboard showing product tracking, price history graph, and WhatsApp alert panel.

```
[Paste your screenshot here after running the app]
```

---

## 🚀 Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/YOUR_USERNAME/pricepulse.git
cd pricepulse
```

### 2. Create a virtual environment (recommended)

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
```

### 4. Run the app

```bash
python app.py
```

### 5. Open your browser

```
http://localhost:5000
```

That's it! 🎉

---

## 📲 WhatsApp Notifications (Optional)

PricePulse can send you WhatsApp messages when a price drops. It uses **Twilio's free sandbox** — no paid plan needed for personal use.

### Setup Steps

1. **Create a free account** at [twilio.com](https://www.twilio.com)
2. In the Twilio Console, go to **Messaging → Try it out → Send a WhatsApp message**
3. Follow the instructions — send `join <sandbox-code>` from your WhatsApp to the Twilio number
4. Copy your **Account SID** and **Auth Token** from the Twilio Console
5. In the PricePulse dashboard, click **"WhatsApp Alerts"** in the left panel
6. Enter your credentials and your WhatsApp number (with country code, e.g. `+91XXXXXXXXXX`)
7. Click **Save**, then **Test** to verify it works ✅

> **Note:** The Twilio free trial includes enough credits for personal use. Your credentials are stored **locally only** in `whatsapp_config.json` and are never sent anywhere.

---

## 🗂️ Project Structure

```
pricepulse/
│
├── app.py                  # Flask server + scrapers + background monitor
├── requirements.txt        # Python dependencies
├── .gitignore              # Excludes DB, credentials, cache
├── LICENSE                 # MIT License
├── README.md               # This file
│
└── templates/
    └── index.html          # Full dashboard UI (HTML + CSS + JS)
```

---

## 🔧 How It Works

```
User adds product URL
        │
        ▼
Platform auto-detected (Amazon / Flipkart / Snapdeal)
        │
        ▼
Background monitor thread scrapes prices every 60s
        │
        ├── Price ≤ Target? ──► WhatsApp alert + dashboard toast
        │
        ├── Back in stock?  ──► WhatsApp alert + dashboard toast
        │
        └── Save price to SQLite history
                │
                ▼
        Chart.js renders price trend graph
```

---

## ⚠️ Important Notes

- **Amazon & Flipkart actively block scrapers.** This tool works on a best-effort basis using rotating User-Agent headers. For heavy use, consider adding a proxy or using official APIs.
- **This is for personal/educational use only.** Always respect a website's Terms of Service.
- Prices are scraped at regular intervals — not real-time. For real-time tracking, official APIs are needed.

---

## 🛠️ Tech Stack

- **Backend:** Python 3, Flask
- **Scraping:** requests + BeautifulSoup4
- **Database:** SQLite (via Python's built-in `sqlite3`)
- **Frontend:** Vanilla HTML/CSS/JS, Chart.js, Font Awesome
- **Notifications:** Twilio WhatsApp API (via direct REST calls)

---

## 📝 License

This project is licensed under the **MIT License** — see the [LICENSE](LICENSE) file for details.

---

## 🤝 Contributing

Pull requests are welcome! For major changes, please open an issue first to discuss what you'd like to change.

1. Fork the repository
2. Create your feature branch: `git checkout -b feature/amazing-feature`
3. Commit your changes: `git commit -m 'Add amazing feature'`
4. Push to the branch: `git push origin feature/amazing-feature`
5. Open a Pull Request

---

<p align="center">Made with ❤️ | Star ⭐ this repo if it helped you!</p>
