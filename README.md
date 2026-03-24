# ⚡ MediaFire Bulk Downloader

A **super-fast**, multi-threaded bulk downloader for MediaFire links with a stunning dark-mode web interface. Built with Python Flask.

## Features

- ⚡ **Chunked Parallel Downloads** — Large files are split into 8 chunks downloaded simultaneously, then merged for maximum bandwidth
- 🔄 **Connection Pooling** — Reusable HTTP sessions with keep-alive for reduced latency
- 📥 **Resume Support** — Automatic HTTP Range detection for resumable downloads
- 🧵 **Adjustable Threads** — 1 to 20 simultaneous downloads via slider
- ⏯️ **Pause / Resume / Cancel** — Full control over each download
- 📊 **Real-Time Stats** — Rolling-window speed, ETA, progress, and file-type icons
- � **Auto Retry** — Exponential backoff on failures (up to 3 retries)
- 🧹 **Clear Completed** — One-click cleanup of finished downloads
- 🌙 **Premium Dark UI** — Glassmorphism, gradient animations, toast notifications
- 📱 **Responsive** — Works on desktop and mobile
- ⌨️ **Keyboard Shortcut** — Ctrl+Enter to start downloads

## Quick Start

### Prerequisites
- Python 3.7+
- pip

### Installation

```bash
git clone https://github.com/aamirxs/Mediafire-downloader
cd Mediafire-downloader
pip install -r requirements.txt
```

### Run

```bash
python app.py
```

Open **http://localhost:5000** in your browser.

### Usage

1. Paste MediaFire URLs (one per line)
2. Adjust the thread count slider
3. Click **Start Download** (or press Ctrl+Enter)
4. Monitor progress, speed, and ETA in real-time
5. Use pause/resume/cancel controls per download
6. Click **Clear Done** to remove completed downloads

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Backend | Python Flask |
| Download Engine | `requests` + `ThreadPoolExecutor` + chunked parallel I/O |
| Frontend | Vanilla HTML/CSS/JS, Inter font, Font Awesome |

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Web UI |
| `POST` | `/api/download` | Start downloads (`urls`, `max_workers`) |
| `GET` | `/api/status` | Get all download statuses |
| `POST` | `/api/download/control` | Pause/resume/cancel (`download_id`, `action`) |
| `POST` | `/api/clear` | Clear completed/failed/cancelled downloads |

## Created By

**Aamir** — [github.com/aamirxs](https://github.com/aamirxs)
