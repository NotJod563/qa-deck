# Про QA Deck

## Що це за проєкт

QA Deck - це розширювана платформа для підготовки та обслуговування QA-середовищ. Вона потрібна QA/QC-інженерам, які регулярно перемикають конфігурації, відновлюють початковий стан продукту, збирають діагностичні дані або повторюють однакові ручні дії.

QA Deck не запускає автоматизовані тести. Це також не лише інструмент аналізу поточного стану. Його головна цінність - швидко підготувати потрібний стан тестового продукту й автоматизувати пов’язану з цим рутину.

Проєкт розробляється як індивідуальний магістерський проєкт із Software Engineering. Код і документація будуть відкритими.

## Поточний стан

Працює базове керування Products, локальне JSON-сховище та плагінна основа.
Доступні чотири вбудовані плагіни: Executable Inspector, License Manager,
Log Collector і Windows Registry. License Manager перевіряє, резервує,
приховує та відновлює налаштовані ліцензійні файли. Log Collector перевіряє
джерела логів і формує тимчасовий ZIP-архів для завантаження. Windows Registry
працює лише з явно налаштованими values і branches та підтримує presets,
preview, stale-state validation, typed value writes і reversible branch rename.

Snapshot Capture фіксує поточний стан доступних providers, Snapshot Diff
порівнює збережені або поточні стани, а Snapshot Restore виконує незалежно
підтверджені provider entries із повторною перевіркою Current. Product-scoped
Environment Profiles посилаються на поточний Registry preset і налаштовані
license identities, автоматично порівнюються з Current та можуть бути
застосовані через server-side preview, one-time intent і PRG result.
Product Setup Package переносить метадані Product і підтримувані конфігурації
плагінів у versioned JSON. Export доступний для License Manager, Log Collector і
Windows Registry. Product Setup Bundle впорядковує кілька таких пакетів в одному
versioned JSON. Імпорт автоматично розпізнає обидва формати та поєднує вибір,
перевірку конфліктів і local adaptation на одній сторінці. Після server-side
validation одноразовий intent підтверджується в діалозі. Створюються лише нові
Product і підтримувані plugin configurations. Runtime state середовища при
цьому не змінюється.

Product Setup не є частиною Snapshot history або Environment Profile. Snapshot
зберігає фактичний стан, Profile описує бажаний стан, а Product Setup переносить
конфігурацію QA Deck. Він також не виконує runtime operations плагінів. Підтримка
Product Setup є optional capability плагіна, тому відсутність такого provider
не зупиняє export або import незалежних секцій.

Підтверджене видалення Product очищає його plugin configurations, snapshots і
Environment Profiles. Append-only Operation Logs зберігаються для аудиту.
Executable, Registry, license files, backups і source logs не змінюються.

Проєкт містить 217 автоматичних тестів основного функціоналу.

## Product у центрі системи

`Product` - це програма або система, яку тестують. Усі ресурси, плагіни та операції налаштовуються в контексті конкретного Product.

Для Product користувач зможе вказати:

- executable та параметри запуску;
- конфігураційні файли й settings;
- ліцензійні файли;
- Registry keys;
- каталоги логів;
- інші артефакти, які потрібно знаходити, копіювати, зберігати, експортувати або пакувати;
- підключені плагіни та їхню окрему конфігурацію;
- готові presets та environment profiles.

Плагіни надають універсальні можливості. Наприклад, реалізований Windows
Registry працює з Registry keys, а запланований File Operations має працювати з
файлами. Користувач окремо налаштовує ці можливості для кожного Product.

## Основний сценарій

1. Користувач додає тестовий Product.
2. Вказує його файли, settings, Registry keys, логи та інші артефакти.
3. Підключає потрібні плагіни й налаштовує їх для цього Product.
4. Створює preset або environment profile.
5. Запускає підготовку, перемикання, відновлення або збір діагностичних даних.
6. За потреби створює snapshot, порівнює стани або переносить Product Setup.

## Основні поняття

### Product

Тестовий програмний продукт або система. Це центральна сутність QA Deck.

### Plugin

Незалежне розширення з універсальними можливостями. Плагін може працювати з файлами, Registry, логами, Jira або іншими частинами середовища.

### Plugin Configuration

Налаштування конкретного плагіна для конкретного Product. Один плагін може мати різну конфігурацію для різних продуктів.

### Resource

Файл, setting, Registry key, лог або інший артефакт, пов’язаний із Product.

### Action

Одна операція, яку надає плагін. Наприклад, копіювання файла, очищення логів або читання версії executable.

### Environment Profile або Preset

Збережений варіант стану й налаштувань Product. Приклади: `clean`, `trial`, `licensed` або `production-like`.

### Product Setup Package

Переносимий опис Product і підтримуваних конфігурацій плагінів для передачі між
інсталяціями або користувачами. Він не містить runtime state, snapshots,
Operation Logs, backup чи вміст локальних файлів. Environment Profiles,
Registry presets і launch arguments у поточній версії не включаються, щоб не
створювати зайві зв'язки між плагінами й не переносити потенційно чутливі
значення.

### Product Setup Bundle

Переносимий versioned контейнер для впорядкованої передачі кількох
`ProductSetupPackage`. Bundle не дублює plugin serialization. Під час import
можна вибрати окремі Product, адаптувати локальні значення та підтвердити
створення нових записів QA Deck. Наявні Product не оновлюються і не
перезаписуються.

### Snapshot

Зафіксований стан Product і пов’язаного з ним середовища.

### Snapshot Diff

Порівняння двох станів Product або його середовища.

### Operation Log

Історія запусків операцій та їхніх результатів.

### Change Plan

Опис запланованих змін до їх застосування. Він показує дії та рівень ризику.

## Приклади

### Clean Trial State

**Product:** Sample Desktop Application
**Preset:** Clean Trial State

- зберегти поточний файл ліцензії;
- тимчасово прибрати `license.dat`;
- змінити `settings.json`;
- очистити старі логи;
- запустити застосунок.

## Плагіни та заплановані напрями

Реалізовані плагіни:

- **Executable Inspector** - уже виконує базову read-only перевірку executable; читання Windows version resources заплановане пізніше.
- **License Manager** - перевіряє, резервує, тимчасово приховує та відновлює налаштовані ліцензійні файли.
- **Log Collector** - перевіряє джерела логів і збирає їх у ZIP; фільтрація чутливих даних ще не реалізована.
- **Windows Registry** - читає й безпечно змінює вибрані Registry keys.

Заплановані плагіни та напрями:

- **File Operations** - має знаходити, копіювати, тимчасово приховувати,
  відновлювати та пакувати файли.
- **Jira Integration** - має передавати snapshots і зібрані артефакти до задач
  Jira.

Snapshot і diff доповнюють основні сценарії. Запланована Jira Integration має
додати передачу діагностичних даних.

Jira Integration і запуск executable залишаються майбутньою роботою. Глобальна
cross-plugin transaction, persistent execution jobs і фінальний UX polish також
ще не реалізовані.

## Безпека

- Перед змінами користувач має бачити Change Plan.
- Ризиковані та руйнівні дії потребують явного підтвердження.
- Помилка одного плагіна не повинна зупиняти весь застосунок.
- Snapshots і Operation Logs не повинні містити паролі, API tokens, private keys або довільний конфіденційний вміст файлів.
- Плагіни повинні вміти приховувати чутливі значення.
- У MVP локально встановлені плагіни вважаються довіреними.
- Повна sandbox-ізоляція плагінів не входить до scope дипломного MVP.

## Технічна основа

- Python і Flask application factory;
- `src-layout`;
- невелике ядро та незалежні плагіни;
- `pytest` для тестування самого QA Deck;
- `pyproject.toml`;
- явне виявлення вбудованих плагінів і реєстрація переданих plugin factories;
- Python entry points як майбутній напрям для зовнішніх встановлених плагінів;
- Windows-специфічний код поза ядром;
- type hints;
- простий код без зайвих абстракцій.

## Межі MVP

До MVP входять базова робота з Products, ресурсами, конфігураціями плагінів,
profiles, Product Setup, заплановані офіційні плагіни, Change Plan, Operation
Log, snapshots і diff, тести, пакування та документація. Повна ізоляція
недовірених плагінів на рівні операційної системи залишається поза scope.
