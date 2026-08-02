# QA Deck

QA Deck — локальний Flask-застосунок для роботи з тестовими продуктами та їхніми QA-середовищами. Проєкт перебуває на ранньому етапі розробки.

## Що вже працює

- створення тестового продукту через вебформу;
- список збережених продуктів;
- окрема картка продукту;
- локальне JSON-сховище у форматі UTF-8;
- збереження назви, опису, шляху до executable, робочого каталогу й аргументів запуску;
- автоматичне створення id;
- темний адаптивний вебінтерфейс;
- health endpoint `/health`;
- Plugin API, Plugin Manager і виявлення вбудованих плагінів;
- read-only перевірка executable через Executable Inspector;
- окремі конфігурації плагінів для кожного продукту;
- License Manager із перевіркою стану, Change Plan, backup, приховуванням і відновленням ліцензійних файлів;
- Log Collector із перевіркою джерел та формуванням ZIP-архіву;
- локальна історія операцій QA Deck;
- перевірка коду за допомогою `pytest` і Ruff.

Дані зберігаються локально в JSON-файлах усередині Flask instance-каталогу. Ці файли не додаються до Git.

Workflows, snapshots, Registry operations, Jira Integration і запуск executable ще не реалізовані.

Проєкт містить 40 автоматичних тестів основного функціоналу.

## Встановлення

Потрібен Python 3.11 або новіший.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

## Запуск

```powershell
python run.py
```

Застосунок буде доступний за адресою `http://127.0.0.1:5000/`.

## Перевірка

```powershell
python -m pytest
python -m ruff check .
```

`pytest` перевіряє сам QA Deck. Проєкт не є test runner для тестів інших програм.

## Документація

- [Контекст проєкту](docs/PROJECT_CONTEXT.md)
- [Архітектурні рішення](docs/DECISIONS.md)
- [Дорожня карта](docs/ROADMAP.md)
- [Звіт за тиждень 1](docs/reports/week-01.md)
- [Звіт за тиждень 2](docs/reports/week-02.md)
