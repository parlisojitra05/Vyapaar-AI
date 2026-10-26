# Vyapaar AI 🚀

> **Know what your business needs. Recover money faster. Keep stock under control.**

Vyapaar AI is a business assistant application designed for Indian retail and wholesale businesses. It combines local deterministic financial ledgers, inventory tracking, and safety approval workflows with an AI Assistant powered by **Ollama** (`llama3.1:8b`).

---

## ✨ Features

- **📊 Intelligent Ledger & Receivables**: Track customer balances, overdue invoices, debtor prioritization, and calculate aging and risk factors.
- **📦 Inventory & Low-Stock Alerts**: Monitor warehouse stock against safety minimums with automatic reorder alerts.
- **🤖 Ollama-Powered Business Assistant**: Natural language business queries routed directly to live database tools via function calling with fallback to deterministic keywords.
- **🛡️ Human-in-the-Loop Safety Contract**: Consequential actions (e.g. sending payment reminders or creating sales orders) are staged as proposals and require explicit user approval before execution.
- **🔍 Transparent Action Trail**: Inspect exact tables queried, findings, and recommendations for full auditability.
- **🌐 Zero Cloud AI Dependency**: Runs completely locally with Ollama for data privacy and zero API costs.

---

## 🛠️ Tech Stack

- **Backend**: Python 3.12, Flask 3.0+
- **Database**: SQLite 3 with strict foreign keys and transactional audit logging
- **AI / LLM**: [Ollama](https://ollama.com/) running local `llama3.1:8b` (or `gemma3:4b`)
- **Frontend**: Responsive UI with Vanilla CSS and Jinja2 templates

---

## 🚀 Getting Started

### 1. Prerequisites
- **Python 3.10+**
- **Git**
- **[Ollama](https://ollama.com/)** installed and running on your system

### 2. Clone the Repository
```bash
git clone https://github.com/parlisojitra05/Vyapaar-AI.git
cd Vyapaar-AI
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

### 4. Setup Ollama
Make sure the Ollama daemon is running and pull the default model:
```bash
ollama serve
ollama pull llama3.1:8b
```

### 5. Environment Configuration
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```
*(Default settings connect to `http://localhost:11434` with model `llama3.1:8b`)*

### 6. Run the Application
```bash
python app.py
```
Visit **`http://127.0.0.1:5000`** in your browser.

---

## 🧪 Running Tests

The test suite covers database migrations, idempotent operations, agent fallback, and UI state machines:
```bash
python -m unittest discover tests
```

---

## 📂 Project Structure

```text
├── actions.py           # Action proposals, validation, and approval transitions
├── agent.py             # Agent query routing, fast-paths, and conversational logic
├── app.py               # Flask application routes, CSRF, context processors
├── config.py            # Global application constants and settings
├── db.py                # SQLite schema, seed data, and audit event logger
├── domain.py            # Business domain rules, currency formatting (INR)
├── llm.py               # Ollama client, live DB tool definitions, and reasoning loop
├── requirements.txt     # Python package requirements
├── services.py          # Business domain services with transactional integrity
├── tools.py             # Deterministic data extraction tools for ledger & inventory
├── static/              # CSS stylesheets, UI components, animations
├── templates/           # Jinja2 HTML templates for dashboard, assistant, approvals
├── tests/               # Unit and regression test suites
└── vyapaar_agent_ui/    # UI state machines, agent pulse status, and action trail
```

---

## 📄 License
MIT License

## 👾Team Members..

- **Parli Sojitra**
- **Pari Gupta**
- **Tanmay Deore**
- **Swarup Surwase**
