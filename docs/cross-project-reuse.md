# Межпроектное переиспользование: целевая схема

Статус: **Phase 1 implemented in source; live rollout/acceptance recorded separately**, 03.10.2026.
Основание: [аудит](audits/2026-10-03-operational-quality.md).
Входы: [каталог возможностей](quality/reuse-catalog.yaml), [постановка](quality/implementation-plan.yaml).
Это расширение существующего DevCoveer, а не отдельный сервис согласований.

## Продуктовый результат

Перед разработкой модель получает короткий ответ: какие близкие решения уже существуют, где их исходники и договор интеграции, кто их использует, какие ограничения подтверждены эксплуатацией. При исправлении общего компонента становится понятно, какие потребители нуждаются в проверке или обновлении. Пользователь не переносит контекст между окнами вручную.

Модель сама выбирает reuse / extend / adapt pattern / independent implementation и кратко объясняет существенное отклонение. Выбор не требует нового согласования. Существующие права, подтверждения внешних действий и Requirements Guard продолжают действовать. Изменение продуктового требования по-прежнему требует явного намерения владельца; обычный выбор реализации — нет.

## Минимальные компоненты

1. **Каталог в Git**: stable capability ID, owner repository, короткие назначения, canonical paths, вид интеграции, границы, доказательства и потребители. В первой версии достаточно заполненного YAML; не нужны новая БД, векторный индекс или фоновый LLM.
2. **Компактный advisory context** в существующих Direct Ops / task entrypoints. Один и тот же контекст доступен ChatGPT и OpenCode. Сначала модель видит короткую карту, затем раскрывает до пяти релевантных карточек и нужные исходники. Подбор по смыслу выполняет текущая модель; поиск по словам — навигационный помощник, не семантический арбитр.
3. **Ссылки на существующие receipts и инциденты**. Каталог не копирует логи, транскрипты, требования и историю коммитов. Текущие карточки инцидентов остаются каноническими у владельцев.

При недоступности каталога обычная разработка продолжается с явным `reuse_context_unavailable`. Это не отменяет проверку прав или продуктового контракта. Данные из репозиториев и журналов не могут сами санкционировать действие.

## Договор карточки и источников

`kind` различает `shared_library`, `product_service`, `reusable_pattern` и `extraction_candidate`. Наличие похожего кода ещё не делает его библиотекой.

Для каждого наблюдения отдельно хранятся source ref, scope и дата. Версия в requirements, установленная версия backend, обслуживаемая browser-сборка и результат acceptance не подменяют друг друга. Неизвестное значение остаётся `null/unknown`. Если snapshot устарел, модель читает текущий owning document перед решением, а не делает вывод по старой метке.

Текущий YAML — стартовый срез, а не live inventory. В дальнейшем build/deploy receipt обновляет сведения автоматически; ручной текст не должен утверждать runtime PASS без доказательства. Предлагаемые предупреждения о возрасте: 7 дней для активного rollout и 30 дней для устойчивого интерфейса; это настраиваемые сроки обзора, не блокировка работы и не измеренный SLA.

## Совместимость и перенос исправлений

Потребитель договаривается о версии интерфейса и необходимых capabilities. SHA/digest фиксируют целостность пакета или конкретный deployment, но не требуют, чтобы соседний сервис работал на одной навязанной ревизии. Численно новая версия не автоматически лучше для всех runtime/transport вариантов.

Для общей библиотеки: контракт API, versioned artifact, список исправленных дефектов и минимальные consumer tests. Для продуктового сервиса: endpoint/schema/capability contract и scoped authorization. Для паттерна: сохраняются инварианты и тестовые ситуации, а не копируется предметная модель.

Внедрение проходит через одного реального потребителя, затем второго с другим адаптером. Результат первого не доказывает второй. Отдельно фиксируются `declared`, `installed`, `served`, `accepted`; rollback возвращает предыдущий совместимый артефакт без удаления receipts. Не вводится межрепозиторный all-or-nothing релиз для независимых сервисов.

## Границы ответственности

Live владеет транспортом, порядком PCM, lifecycle, остановкой, ACK и переходами capabilities. Приложения владеют пользователем, tenant/resource authorization, инструментами, UI и идемпотентностью содержательных изменений.

AI Resource Control остаётся владельцем собственной authority/SDK. Live lease и обычный reserve/mark-sent/finalize — разные режимы. Private SDK не копируется в public package. Google admission не объявляется универсальным бюджетом VK/Kaggle.

VibePublish — продуктовый сервис публикации и media store; новые приложения интегрируются с его контрактом, не копируют worker/provider adapters. Scheduling остаётся provider-native. Capability не даёт право на произвольную публикацию.

Kaggle pilot ограничивается transport/status/error/reconciliation. Политика protected master и права My Data Hub остаются в My Data Hub. Smart Update даёт паттерны provenance/accepted outcome/exact replay, но не заменяет POI identity и LLM-first работу Миры. ADB остаётся локальным инструментом пользователя, не зависимостью DevCoveer.

## Межпроектное качество

При инциденте фиксируются component/capability, observed version, confidence, effect outcome и ссылочный список затронутых потребителей. Статусы `suspected`, `confirmed`, `not_affected`, `unknown` обязательны к различению. Успех synthetic fixture не закрывает field incident. Исправление обновляет ту же карточку, а не создаёт новый независимый журнал.

Предлагаемый монитор запускается существующим CI/операционным механизмом после изменения общего компонента и, после отдельного включения, периодически проверяет только изменившиеся receipts/routes. Он не запускает публикации, дорогие модели и весь портфель тестов. Сообщает о материальном drift: пропущенный фикс, устаревший статус, несовместимая capability, повтор инцидента. В этом прогоне монитор **не включён**.

## Безопасность и восстановление

Не пытаться предотвращать платформенные проверки через маскировку действий. Проверять реальные annotations tools/list: read-only означает отсутствие внешних изменений; idempotent не означает read-only; общий gateway не становится read-only из-за наличия в нём probe операций. Annotations не заменяют серверную авторизацию.

Для каждой операции сохраняются bounded correlation ID, effect class, terminal outcome и coverage. Не сохраняются секреты, raw prompts, аудио и полные пользовательские payload в общем каталоге. После timeout/потерянного ответа читается существующий receipt; неизвестный исход не разрешает повтор мутации другим способом. Неуспешный optional шаг не переписывает уже подтверждённый результат основного действия.

Классы ошибок: user-reported platform review; user denial; authorization denial; schema mismatch; product policy; provider/runtime failure; transport timeout/lost response; incomplete evidence. Без клиентского события причина проверки ChatGPT остаётся unknown.

## Приёмка и показатели

OPQ-01–09 содержат проверяемые сценарии. Обязательный сквозной набор: смысловой поиск без совпадения ключевых слов; неверный donor; stale main/dirty checkout; недоступный каталог; JS и Python Live consumers; Stop и повреждённый turn; успешная мутация с отказавшим optional image; неподходящий token audience; потерянный ответ; чтение недоверенного текста; старые browser assets; отсутствие каталога ресурсов; неполные логи.

Первые метрики собираются как baseline, текущие значения не выдумываются:

| Метрика | Определение |
|---|---|
| Уместность reuse | Доля независимо проверенных eligible задач, где reuse/отказ обоснован предметным контрактом |
| Перенос исправления | Время от verified shared fix до подтверждённого внедрения в затронутый consumer |
| Повторяемость | Повторы одного подтверждённого defect class на число сопоставимых операций |
| Доставка результата | Завершённые пользовательские сценарии / начатые, с отдельными interrupted/unknown |
| Полнота диагностики | Доля запросов с complete coverage; отдельно ложные выводы из пустой partial-выдачи |
| Проверки ChatGPT | Только наблюдаемые сообщения пользователя/клиента на покрытые сессии, с долей unknown |

Безопасность — guardrail, не показатель для «оптимизации» отключением защит. Приёмочные fixtures требуют отсутствия несанкционированных эффектов и утечек, но не доказывают нулевой риск всей эксплуатации. Экономия LOC не является самостоятельным критерием качества.


## Phase 1 runtime contract — 03.10.2026

Phase 1 implements the minimal advisory layer inside the existing DevCoveer runtime; it does not add a database, daemon, approval engine or LLM router.

- Every project probe response carries a compact `reuseContext` summary when the canonical catalog is readable. The same bounded navigation map is prepended to OpenCode and native task prompts. Catalog content is explicitly untrusted advisory data; Requirements Guard and the user's task remain authoritative.
- `reuse_context` expands only explicit capability IDs (maximum five cards) under an output byte budget. The current working model performs semantic selection; the runtime does not route by keyword or capability ID.
- `quality_inventory` keeps `main/source`, selected checkout, dependency declaration, installed, served and acceptance state distinct. Missing evidence stays `unknown`.
- `reuse_compatibility` evaluates interface/version/capabilities/transport. Source SHA is provenance, not a cross-service lock; artifact SHA-256 remains an integrity check.
- `reuse_incident_impact` projects an explicit incident scope/confidence across catalog consumers without changing canonical incident records. Local failures remain local, suspected stays suspected, and synthetic PASS cannot close a real field incident.
- `reuse_portfolio` plus `reuse_portfolio_record` provides a small file-backed drift baseline across all catalog cards. The hook is advisory: failure cannot roll back a successful commit/deploy or invent a PASS.
- The canonical catalog is copied into the installed MCP runtime so task context does not depend on a dirty source checkout.

The implementation deliberately leaves OPQ-06, OPQ-07 and OPQ-09 as backlog. Those are extraction/refactor projects, not prerequisites for Phase 1 operational quality.
