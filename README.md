# jobservice

Сборщик вакансий с [Jobs.cz](https://www.jobs.cz) и [StartupJobs.cz](https://www.startupjobs.cz) в SQLite.

## Установка

```sh
uv sync
cp .env.example .env   # ключевые слова, город, путь к БД
```

## Запуск

```sh
uv run jobservice collect                         # параметры из .env
uv run jobservice collect -k python -k django --city Praha --max-pages 2
uv run jobservice collect --source startupjobs --refresh
```

Повторный запуск обновляет существующие записи (ключ — `source` + `external_id`) и не
скачивает заново описания, которые уже есть в БД (`--refresh` — скачать заново).

## Таблица `vacancies`

| поле | |
|---|---|
| `source`, `external_id` | `jobscz` / `startupjobs` и id вакансии на сайте |
| `title`, `company`, `city`, `url` | |
| `description_text`, `description_html` | описание текстом и в исходном HTML |
| `published_at` | ISO-дата публикации, если её удалось определить |
| `published_raw` | дата как её показал сайт (`10. listopadu`, `Končí zítra`…) |
| `first_seen_at`, `last_seen_at` | когда вакансию впервые / последний раз видел сборщик |

## Источники

- **StartupJobs** — JSON API, которым пользуется сам сайт (`back.startupjobs.cz`).
- **Jobs.cz** — HTML выдачи + страница вакансии. У вакансий на шаблонах работодателей
  (`*.jobs.cz/detail-pozice`) текст рендерится JavaScript'ом, поэтому для них сохраняется
  только краткое описание из `og:description`, а `description_html` пустой.

## Разработка

```sh
uv run pytest
uv run ruff check . && uv run ruff format .
```
