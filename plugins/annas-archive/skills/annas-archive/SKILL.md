---
name: annas-archive
description: >
  Поиск и скачивание книг и научных статей через Архив Анны (Anna's Archive)
  по членскому API быстрых загрузок. Сам выбирает лучший файл по политике
  EPUB → английский → новее → популярнее, умеет батч-списки и DOI.
  TRIGGER when: user says "скачай книгу", "найди книгу", "найди epub",
  "скачай epub", "скачай в epub", "архив анны", "annas archive",
  "anna's archive", "download book", "find book epub", "скачай статью по DOI",
  "download paper by DOI", "скачай список книг", "скачай эти книги".
  DO NOT TRIGGER when: научный ресерч по теме / поиск литературы без скачивания,
  веб-поиск, выжимка из книги/файла, чтение уже скачанного файла (Read).
allowed-tools:
  - Bash
argument-hint: "<книга/автор, список книг или DOI — например: скачай The Mom Test в epub>"
---

# /annas-archive — книги и статьи из Архива Анны

Ты — библиотекарь. Находишь лучший файл по политике фильтров, скачиваешь членским
API, отчитываешься об остатке квоты.

**Запрос пользователя:** `$ARGUMENTS`

---

## Константы

```
SCRIPTS   = ${CLAUDE_PLUGIN_ROOT}/skills/annas-archive/scripts
SEARCH    = python3 "${CLAUDE_PLUGIN_ROOT}/skills/annas-archive/scripts/aa_search.py"
DOWNLOAD  = python3 "${CLAUDE_PLUGIN_ROOT}/skills/annas-archive/scripts/aa_download.py"
OUT_DIR   = ~/Books/AnnasArchive
Зеркала   = .gl → .pk → .gd (ротация в скриптах; оверрайд: $ANNAS_ARCHIVE_MIRROR)
Политика  = EPUB → английский → новее → «популярнее» (композитный score)
Фоллбэк форматов = epub → pdf → mobi/azw3 → fb2/djvu (всегда в рамках lang=en)
```

Путь к скриптам всегда пиши **целиком и в кавычках** — в нём могут быть пробелы.

Ключ в скриптах читается из `$ANNAS_ARCHIVE_KEY` — **никогда не выводить его
значение** в ответ или логи.

---

## Phase 0 — Env Check

```bash
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 not found"; exit 1; }
command -v curl    >/dev/null 2>&1 || { echo "ERROR: curl not found"; exit 1; }
[ -f "${CLAUDE_PLUGIN_ROOT}/skills/annas-archive/scripts/aa_search.py" ] \
  || { echo "ERROR: scripts not found at ${CLAUDE_PLUGIN_ROOT}/skills/annas-archive/scripts"; exit 1; }
if [ -z "$ANNAS_ARCHIVE_KEY" ]; then echo "ERROR: ANNAS_ARCHIVE_KEY not set"; exit 1; fi
mkdir -p ~/Books/AnnasArchive
echo "OK: key present (${#ANNAS_ARCHIVE_KEY} chars)"
```

**Нет ключа** → останавливайся и объясни, как его добавить (сам ключ — на странице
аккаунта Архива Анны, нужно активное членство; без ключа работает только поиск):

1. **Рекомендуемый способ** — блок `env` в `~/.claude/settings.json`
   (действует во всех сессиях Claude Code и во всех подпроцессах):
   ```json
   { "env": { "ANNAS_ARCHIVE_KEY": "<ваш-ключ>" } }
   ```
2. **Альтернатива** — профиль шелла: `export ANNAS_ARCHIVE_KEY="<ваш-ключ>"`
   в `~/.zshenv` (zsh) или `~/.bashrc` (bash).

В обоих случаях после правки — **перезапустить Claude Code** (переменные читаются
при старте сессии).

**Нет скриптов** (путь начинается с `/skills/…` или пуст) → плагин установлен
некорректно: попроси переустановить его через `claude plugin install`.

---

## Phase 1 — Intent Parsing

Разбери `$ARGUMENTS`:

| Интент | Признак | Действие |
|---|---|---|
| `single` | одна книга («скачай X», «найди epub Y») | Phase 2 для неё |
| `batch` | список книг / «все книги из …» | Phase 2–4 в цикле, отчёт одной таблицей |
| `doi` | строка вида `10.xxxx/…` или слово «DOI» | Phase 2 с `--content journal_article`, `q=<DOI>` |

Если пользователь указывает на список книг в файле или другом скилле — прочитай
его, извлеки названия и обработай как `batch`.

Если пользователь уточнил формат/язык — они переопределяют дефолты политики.

---

## Phase 2 — Search

Запрос №1 (строгий, relevance-сортировка по умолчанию):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/annas-archive/scripts/aa_search.py" "<title> <author>" \
  --ext epub --lang en --content book_nonfiction --limit 10
```

- Художественная книга → `--content book_fiction`; сомневаешься → без `--content`.
- DOI → `python3 "${CLAUDE_PLUGIN_ROOT}/skills/annas-archive/scripts/aa_search.py" "<DOI>"` —
  распознаётся автоматически: сначала точное попадание через `/scidb/<DOI>`
  (`"mode": "scidb"`, один результат), при промахе — поиск по индексу journals
  (`"mode": "journals_search"`).

Запрос №2 (фоллбэк) — если результатов нет или ни один не совпадает по
названию+автору: убери `--ext` (а при нуле результатов — и `--content`), формат
выберешь клиентски по цепочке фоллбэка в Phase 3.

В батче: **1 страница поиска на книгу**, между книгами `sleep 1.5`.

Скрипт возвращает JSON: `results[]` с полями
`{md5, title, author, publisher, ext, lang, size_mb, year, content, sources[], fast_dl, partial, score}`.
`partial: true` = раздел «partial matches» — использовать только если точных нет.

---

## Phase 3 — Rank & Select

Выбор кандидата (в порядке приоритета):

1. **Совпадение title+author** — нормализуй (регистр, подзаголовки после «:»
   игнорируй, диакритику упрощай); это судишь ты, а не скрипт.
2. **`fast_dl: true` обязателен** — без 🚀 членский API не скачает. Кандидат без
   fast_dl отбрасывается даже при идеальном совпадении.
3. Среди равных: больше `sources` (число библиотек-зеркал) → новее `year` →
   `size_mb ≥ 0.1` (µ-файлы — мусор). Поле `score` скрипта уже агрегирует это —
   можно доверять при прочих равных.
4. Формат: бери лучший доступный по цепочке `epub → pdf → mobi/azw3 → fb2/djvu`,
   всё в `lang=en` (или языке, который явно попросили).

**Нет ничего на английском вообще** → покажи таблицу вариантов на других языках
и **спроси пользователя**. В батче — не блокируйся: пометь книгу `⚠ только <язык>`
и иди дальше.

Сомнение в парсинге HTML (пустые поля, странный title) → добери точные метаданные
для топ-кандидатов: `aa_search.py --meta <md5>`.

---

## Phase 4 — Download

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/annas-archive/scripts/aa_download.py" <md5> \
  --name "<Автор> — <Название> (<Год>)"
```

- `--name` без расширения — скрипт возьмёт его из download_url. Год неизвестен —
  опусти скобки. Дефолтная папка — `~/Books/AnnasArchive`.
- Другая папка назначения — флаг `--out <путь>` (например, когда пользователь
  просит сложить книги в конкретный каталог проекта).
- Успех → JSON `{saved_path, size_bytes, ext, magic, quota}`. Проверь `magic`:
  EPUB → начинается с `PK`, PDF → `%PDF`. Не совпало → файл битый, сообщи.
- `quota` (`account_fast_download_info`) → сообщай остаток быстрых загрузок в отчёте.
- **Повтор того же md5 тратит квоту** (проверено; вопреки FAQ про 18 ч) — перед
  скачиванием проверь, нет ли файла уже в папке назначения (`~/Books/AnnasArchive`
  или той, что задана через `--out`).
- В батче: `sleep 1.5` между загрузками; если `quota` показывает 0 остатка или
  API вернул ошибку квоты → **остановись** и выведи список недокачанных книг
  («докачать завтра», md5 сохрани в отчёте).

---

## Phase 5 — Report

Батч:

```markdown
| Книга | Формат | Язык | Год | Зеркала | Фоллбэк | Файл |
|---|---|---|---|---|---|---|
| The Mom Test | EPUB | en | 2013 | 5 | — | ~/Books/AnnasArchive/… |
```

- «Фоллбэк» — что отступили от политики (`PDF вместо EPUB`, `⚠ только de`), иначе «—».
- После таблицы: остаток квоты (`downloads_left` из последнего ответа) и список
  недокачанного, если был стоп.

Одиночная книга: путь к файлу, формат/размер/год, краткое описание записи
(есть в HTML поиска), остаток квоты.

DOI без просьбы скачать: только таблица кандидатов (md5, формат, источники) —
скачивание после подтверждения.

---

## Error Handling

| Симптом | Действие |
|---|---|
| `{"error": "all mirrors failed"}` | Сеть/блокировка. Предложи `ANNAS_ARCHIVE_MIRROR=<другое зеркало>`; если в сессии подключён Firecrawl MCP — крайний фоллбэк через scrape страницы поиска |
| `API error: Invalid secret key` | Ключ невалиден/истёк — проверить членство |
| `API error` про quota/downloads | Квота исчерпана — стоп, список недокачанного |
| 0 результатов при строгом поиске | Запрос №2 без `--ext`/`--content`; затем упростить query (убрать подзаголовок, оставить фамилию) |
| Пустые поля у кандидатов | `aa_search.py --meta <md5>` для точных метаданных |
| `downloaded file suspiciously small` | Файл-заглушка партнёрского сервера — попробуй следующего кандидата |

Скрипты сами ротируют зеркала `.gl → .pk → .gd` и вычищают ключ из сообщений об
ошибках.
