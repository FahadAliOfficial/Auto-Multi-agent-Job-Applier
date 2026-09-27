# Indeed Easy Apply Bot 🚀

A personal automation assistant for Indeed Easy Apply job applications. Built with Python + Playwright.

## Features

- **Semi-auto mode**: Bot finds jobs, fills forms — you review and approve each one
- **Full-auto mode**: Bot applies to all matching jobs automatically
- **Smart form filling**: Detects and fills form fields using your config
- **Learn-as-you-go screening questions**: First encounter → asks you → saves answer → auto-fills next time
- **Multiple resume profiles**: Picks the best resume based on job title keywords
- **Duplicate prevention**: SQLite database tracks all applications
- **Anti-detection**: Persistent sessions, stealth scripts, human-like delays
- **Web dashboard**: View applied jobs, stats, and screening questions at `localhost:5000`

## to run the dashboard:
Use a second terminal for the dashboard:
```bash
$env:PYTHONUTF8=1; python -m src.main dashboard --host 127.0.0.1 --port 5000
```
Then open:
```
http://127.0.0.1:5000
```
If port 5000 is busy, run:

```bash
python -m src.main dashboard --port 5001
```
and open http://127.0.0.1:5001.

If you want, I can also add a single command mode that starts both bot + dashboard together.


## Quick Start

### 1. Install

```bash
# Clone/navigate to this directory
cd indeed

# Install dependencies
pip install -e .

# Install Playwright browsers
playwright install chromium
```

### 2. Configure

Edit `config/config.yaml` with your:
- Personal info (name, email, phone, address)
- Search criteria (job titles, location, filters)
- Bot preferences (mode, max applications, delays)

Place your resume PDF(s) in `config/resumes/`:
```
config/resumes/general.pdf      # Default resume
config/resumes/technical.pdf    # For tech roles (optional)
config/resumes/leadership.pdf   # For management roles (optional)
```

### 3. Login

```bash
python -m src.main login
```

This opens Indeed in the browser. Log in manually (handle CAPTCHA/2FA yourself). The session is saved for future runs.

### Use your normal Chrome profile (optional)

The original Playwright-managed browser remains the default. To use a dedicated
tab in your everyday Chrome profile instead:

1. Set `bot.browser.mode: "extension"` in `config/config.yaml`.
2. Start the bot once so it creates `data/extension_pairing_token`.
3. Open `chrome://extensions`, enable Developer mode, select **Load unpacked**,
   and choose `extension/chrome`.
4. Open the extension Options, paste the pairing token, and save.

The local bridge binds only to `127.0.0.1`. Indeed access is granted by default;
company-site access and screenshots are separate, explicit Chrome permissions.
Return `bot.browser.mode` to `"legacy"` at any time to use the original flow.

Run the safe local-only integration check with:

```bash
python scripts/smoke_test_extension.py
```

### 4. Run

```bash
# Semi-auto mode (review each job before applying)
python -m src.main run --mode semi

# Full-auto mode (apply to all matching jobs)
python -m src.main run --mode auto

# Use default mode from config
python -m src.main run
```

## All Commands

| Command | Description |
|---|---|
| `python -m src.main run` | Start applying to jobs |
| `python -m src.main run --mode auto` | Full-auto mode |
| `python -m src.main run --mode semi` | Semi-auto mode (default) |
| `python -m src.main login` | Login and save session |
| `python -m src.main search` | Search and list jobs (no applying) |
| `python -m src.main search -q "Data Engineer"` | Search with custom query |
| `python -m src.main status` | Show application statistics |
| `python -m src.main dashboard` | Launch web dashboard |
| `python -m src.main resume-maker` | Launch standalone AI resume maker |

## Resume Maker (Standalone)

Launch:

```bash
python -m src.main resume-maker --host 127.0.0.1 --port 5050
```

Open:

```
http://127.0.0.1:5050
```

Set your API key before launch:

```bash
$env:OPENAI_API_KEY="your_key_here"
```

The app:
- loads/saves a structured master resume at `config/resume_master.json`
- tailors it against a pasted job description
- shows changes + estimated token usage
- exports tailored files to `data/resume_outputs/`

## Project Structure

```
indeed/
├── config/
│   ├── config.yaml          # Your settings
│   ├── answers.yaml         # Default screening question answers
│   └── resumes/             # Your resume PDFs
├── src/
│   ├── main.py              # CLI entry point
│   ├── bot.py               # Main orchestrator
│   ├── browser.py           # Playwright browser setup + stealth
│   ├── database.py          # SQLite data layer
│   ├── pages/               # Page Objects (login, search, job, apply form)
│   ├── handlers/            # Form detection, filling, question matching
│   ├── utils/               # Logging, delays, screenshots
│   └── dashboard/           # Flask web dashboard
├── data/
│   ├── indeed_bot.db        # SQLite database (auto-created)
│   ├── sessions/            # Saved browser sessions
│   ├── screenshots/         # Application screenshots
│   └── logs/                # Log files
├── pyproject.toml
└── README.md
```

## How It Works

1. **Login**: Opens Indeed in a real browser. You log in once manually. Session is saved.
2. **Search**: Builds Indeed search URLs from your config (job title, location, filters).
3. **Parse**: Extracts job listings from search results (title, company, salary, Easy Apply badge).
4. **Filter**: Skips already-applied, non-Easy-Apply, and blacklisted jobs.
5. **Review** (semi-auto): Shows you each job summary. You decide: apply, skip, or quit.
6. **Apply**: Clicks "Apply now", detects form fields, fills them from your config.
7. **Learn**: For unknown screening questions, asks you once and remembers your answer.
8. **Track**: Every action is logged to SQLite. View stats via CLI or web dashboard.

## Screening Questions

The bot handles Indeed's screening questions with a "learn-as-you-go" approach:

- **First encounter**: Bot pauses and asks you to answer in the terminal
- **Your answer is saved** to the database
- **Next time** the same (or similar) question appears: auto-filled from your saved answer
- **Edit answers** anytime via the web dashboard (`python -m src.main dashboard` → Questions tab)

Default answers for common questions (work auth, experience, etc.) are in `config/answers.yaml`.

## ⚠️ Disclaimer

This tool is for **personal use only**. Indeed's Terms of Service prohibit automated access. Use responsibly:
- Run in semi-auto mode to review applications
- Use reasonable delays (configured by default)
- Don't spam — quality over quantity
- The author is not responsible for any consequences of using this tool
