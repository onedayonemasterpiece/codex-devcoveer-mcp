# Операционное качество DevCoveer и межпроектное переиспользование

Дата: 3 октября 2026. Тип: аудит и постановка; не релиз runtime.
Владелец результата: `codex-devcoveer-mcp`.
Источник постановки: приватная intake-сессия IdeaHub `voice-20261003-083638-766e61b7` и явный запрос владельца на аудит.

## Итог

Главный дефицит — не отсутствие общего кода. Общий Live runtime уже используется в нескольких продуктах, ресурсный SDK существует отдельно, есть журнал инцидентов и восстановление операций. Не хватает достоверной и компактной навигации: **что уже решено, где находится актуальная реализация, какие версии/возможности доступны, что реально эксплуатируется и какие ограничения выявлены**.

Рекомендуется расширить существующий DevCoveer небольшим контекстом переиспользования и связями с имеющейся историей, а не строить новый оркестратор, approval engine, универсальный агент или вторую систему журналирования. Семантическое решение остаётся за работающей моделью; схема данных, права, идемпотентность и продуктовые инварианты проверяются детерминированно.

Поставляемые документы: [целевая схема](../cross-project-reuse.md), [каталог](../quality/reuse-catalog.yaml), [задачи и приёмка](../quality/implementation-plan.yaml). Каталог — заполненный стартовый документ, **не уже работающая автоматическая функция MCP**.

## Методика и границы

Проверены GitHub-источники, выбранные рабочие деревья DevCoveer, зависимости и адаптеры, инструкции, существующие контракты требований, журналы инцидентов и ограниченные runtime-пробы. Прочитано последнее релевантное голосовое ревью целиком. Для Wonderful прочитаны восемь карточек каталога инцидентов, связанные исторические отчёты и сохранённые доказательства; они не копируются в этот аудит.

Это целевой архитектурно-операционный аудит, не построчная ревизия всех репозиториев и не полноценная production-приёмка всех продуктов. Не выполнялись публикации, вызовы платных моделей, изменение секретов, остановка сервисов, deployment, переустановка окружений или очистка чужих worktree. Исходный код продуктов не менялся. Исторические PASS ниже не являются тестами этого прогона.

Наблюдения привязаны к времени и конкретному источнику. `main`, выбранный checkout, опубликованный пакет и работающий процесс — разные сущности. Старый checkout не позволяет утверждать отсутствие функции в новом main; пустой поиск по main не позволяет утверждать отсутствие кода в рабочих ветках.

## Срез источников

| Репозиторий | Основание чтения | Что исследовано / ограничение |
|---|---|---|
| codex-devcoveer-mcp | main `ddd688b434ce809f735e77940fcd6b465ccb87af` | Requirements Guard, Direct Ops/recovery, incident registry, оператор edge, установленный manifest |
| idea-hub | main `c346ac7a526870501350c0277678276429224e0a` | Последнее voice review, intake index, правила канонической материализации |
| events-bot-new | индекс GitHub `8105d069f2d91306c13db02b976e5fa2921226f6` | Live adapter, зависимости, Kaggle helpers, Smart Update caller inventory; не заявляется свежий deployed SHA |
| projects-hub | checkout и main `9f740303120a0efd673ed6661237768d63a1ddee` | `live_runtime.py`, pyproject, продуктовый контракт |
| street-story | checkout `d575b3bd0d45330414e2dfdda20a7e9a44c63dd9` | `live_socket.py`, `quota.py`, requirements; это рабочая ветка, не доказательство deployed release |
| wonderful-lections | main `d129b15b1d570aabe4efab44e29b3c0992fd7e9c` | Shared Live integration, package declaration, вся зарегистрированная история инцидентов |
| my-data-hub | checkout/main `e7972f7339b0abe28634bd1782c0d517c65ab06e` | Kaggle adapter/contracts, control-plane документация, lifecycle/identity |
| vibepublish | main `53575a9befe22f05301348917f003b1a7440dead` | routes.yml, image ingress, shared service boundary, scheduling/flood incidents |
| live-interaction | README checkout `e05fac8bbb6fdcc463d53fff4c554083cb3d27e7`; remote main `129edcd38208e3d3daa4fb033c75288d83575dea` | Два состояния намеренно не смешиваются; browser/Python/Node transport и границы product adapters |
| ai-resource-control | checkout/main `f31738077dc4ad3f1e141a9eb252f84bce7d5379` | README, собственная authority и SDK; private implementation не переносится в public package |

Полные SHAs нужны здесь для воспроизводимости аудита. Они **не предлагаются как межсервисный договор совместимости**.

## Подтверждённые находки

### F01. Ошибка resources и неполная диагностика — P0

03.10, 07:08:24Z, `host_probe(resources, largest=0)` завершился `FileNotFoundError` на отсутствующем зарегистрированном каталоге worktrees. Диагностика ресурсов не должна целиком пропадать из-за необязательного подкаталога. Причина отсутствия каталога и фактический текущий объём свободного диска этим вызовом не установлены.

`log_search(mcp_tunnel)` за 72 часа и повторно за час вернул ноль совпадений с `complete=false, source_truncated=true`. Это **не доказательство отсутствия ошибок**. В частности, уменьшение окна до часа не восстановило полноту. Требуется курсор/сегментация источника и явное покрытие диапазона, а не просто увеличение вывода. Задача OPQ-01.

### F02. Разные источники выглядят как один проект — P0

Канонический checkout DevCoveer был на 83 коммита позади tracking main и содержал изменения; IdeaHub — на 94 и также с незавершённой работой; VibePublish — на 34. Эти деревья оставлены нетронутыми. Сам установленный DevCoveer при этом сообщил **`3.30.0-requirements-guard`, 23 инструмента**, Python 3.12.3. Следовательно, возраст основного checkout не равен возрасту runtime.

У Street Story свежий remote main `431d783d3d27798d272a68b532099d3fdb803a80` содержал только README и `.devcoveer`, тогда как backend находился в выбранной рабочей ветке. Поиск GitHub по default branch и чтение backend без ref возвращали пустоту/404. Список worktree был усечён на 30 записях: это минимум 30, а не точное число и не разрешение удалить их.

Нужен единый компактный ответ о main / working source / installed / served artifact с явным `unknown`, а не новый обязательный ритуал ручной сверки SHA перед каждым чтением. Задача OPQ-02.

### F03. Общий Live уже есть; не хватает карты внедрения — P1

Projects Hub импортирует общий `LiveSocketSessionHost`, запускает provider через `ai_resource_control.run_guarded`; Events использует shared transport и сохраняет event retrieval у себя; Street использует общие socket/protocol/ticket primitives; Wonderful потребляет общий browser/Node/Python контур.

Декларации зависимостей различаются: Wonderful — локальный vendor archive 0.3.11; Events и выбранный Street — 0.3.7-rc.1; Projects Hub — Git SHA. Это **наблюдаемое различие деклараций**, не доказанная несовместимость и не инвентаризация установленных пакетов. В README Live отдельно описан Python/native-WSS candidate: нельзя считать численно старшую browser-версию автоматической заменой каждому потребителю.

Нужно вести capabilities/transport/runtime, диапазон совместимости после проверки, версии потребителей и известные исправления; не обновлять всех до «latest» вслепую. OPQ-03/04.

### F04. Документация может направлять в уже устаревшее состояние — P1

В актуальном VibePublish `docs/routes.yml` для social operations сохранено `partial_remote_source_not_runnable`. При этом канонический инцидент `inc_009a4680f1220529da42e68a` закрыт с подтверждённым provider-native scheduled readback и deployment receipt. Это конфликт источников статуса, а не основание объявить весь продукт исправным.

У ai-resource-control первый блок README по-прежнему описывает статус 27 сентября и версию 0.1.6, а ниже присутствуют изменения 0.1.11. Документ сам помечает дату; проблема — отсутствие машинно-читаемого current vs historical контекста.

Нужны ссылки на owning document и receipt, `observed_at`, scope проверки и статус freshness; исторические статусы не удаляются из истории и не выдают себя за current. OPQ-02/05.

### F05. Опыт исправлений не должен оставаться внутри одного потребителя — P1

История Wonderful содержит несколько разных классов проблем:

- Исправленный source не гарантировал доставку новой browser-сборки. Проверять надо и обслуживаемые байты/версию.
- Модель сообщала о выполненной правке без соответствующего tool-call/mutation receipt. Это не сетевой timeout и не лечится увеличением буфера.
- Смена capability теряла владение микрофоном; после исправления отдельно проверялось физическое прекращение захвата.
- Resource-budget отказ после успешной мутации мог дать неверное сообщение, будто правка не началась. Нужна раздельная судьба мутации и необязательного изображения.
- Недостаток прав reviewer/editor был продуктовым доступом, не общим дефектом транспорта.

Hall incident остаётся открытым: сохранённые серверные события и синтетический тест sender не доказывают качество реальной сети, слышимость и полный путь до динамика. Нельзя закрывать его только зелёным source CI. Необходимо связывать общий компонент и затронутые версии с исходным инцидентом, оставляя продуктовую причину продуктовой. OPQ-05/08.

### F06. Kaggle — кандидат на ограниченное выделение, не готовая единая библиотека

У Events имеются registry, recovery, status/callback helpers и несколько прямых вспомогательных вызовов Kaggle. У My Data Hub уже есть собственный типизированный provider adapter, run identity, effect journal, protected resources и правила неизвестного результата. Общим кандидатом являются транспорт, ограниченная пагинация, классификация ошибок и reconciliation; политика master/checkpoint, ACL, предметные задания — не общий код.

Наличие похожих имён файлов ещё не измеряет дублирование. Не выполнен полный AST/clone-анализ; процент сокращения кода не заявляется. Начинать с двух конкретных потребителей и сохранения их контрактов, а не общего переписывания. OPQ-06.

### F07. Повторно использовать семантическую схему, а не подменять модель регулярками

Smart Update содержит полезный опыт: только типизированный accepted outcome допускает downstream side effect; диагностический ID и строковый статус не означают успешное изменение; exact replay остаётся идемпотентным. Эти принципы полезны Street Story.

Однако event identity, POI identity, атомарный исторический факт и арбитраж противоречий — разные предметные сущности. LLM-first контракт Street сохраняется: семантическое извлечение, сопоставление, дедупликация и объяснение противоречий выполняет модель. Детерминированный слой валидирует schema, provenance, полномочия, запись и idempotency, но не имитирует смысл. OPQ-07.

### F08. Общий control-plane уже переиспользуется через продуктовый класс

`runtime/projects_hub_edge_operator.py` наследует `StreetStoryOperator`; это реальное переиспользование, а не простое копирование. Но зависимость generic edge механизма от класса другого продукта усложняет границы. При следующей доработке разумно выделить нейтральные receipt/lock/edge primitives, оставив жёсткие отдельные allowlist и readiness checks. Не превращать это в произвольный deploy-anywhere инструмент. OPQ-09.

## Проверки безопасности ChatGPT: отдельный вывод

### Что известно

Канонические инциденты DevCoveer `inc_453f4acbb881a4bd4864a087` и `inc_5ba055f5f05a4dd10f9a69fd` остаются открытыми. Первый фиксирует сообщения владельца о дополнительных проверках ChatGPT. Второй содержит ранее подтверждённые восемь потерянных ответов tunnel 28 сентября и два восстановившихся control-poll timeout 29 сентября. Это исторические наблюдения из журнала, не новый подсчёт за 1–3 октября.

Глобальный журнал Direct Ops имел retention cutoff 28 сентября 20:49:48.607250Z; более ранняя корреляция неполна. Свежий 72-часовой поиск по OpenCode не нашёл совпадений при `complete=true`, но проверяет только соответствующий журнал OpenCode. Он не видит внутренние классификаторы или интерфейс ChatGPT.

Установленная версия DevCoveer и компактные Direct Ops доступны. Но свежий tunnel search неполон, а resources сломан. Надёжность расследования пока недостаточна, чтобы обещать устранение повторов.

### Чего нельзя утверждать

Нет подтверждённого машинного сигнала о причине каждой проверки ChatGPT, полного клиентского журнала таких событий или достоверного знаменателя рабочих сессий. Поэтому нельзя вывести частоту, доказать её снижение, назвать конкретный trigger или сказать, что серверный патч «отключил проверки». Корреляция длинных/широких tool calls и сбоев — гипотеза для измерения, не установленная причина.

Нужно различать: user-reported platform review; явный отказ пользователя; отказ серверной авторизации; schema/capability mismatch; policy gate продукта; runtime/provider error; timeout; потерянный ответ; частичную выдачу. В этом прогоне `remote_denied` для непривязанного remote IdeaHub и `not_found` по неверно выбранной версии — диагностические результаты, не доказательства проверки ChatGPT.

### Безопасные улучшения

Сохранить действующие права, approvals, secret redaction и Requirements Guard. Не менять annotations на ложные read-only, не маскировать side effects и не искать альтернативный маршрут для отклонённой операции. После неизвестного исхода мутации читать durable receipt; повтор допустим только согласно её идемпотентному контракту. Возвращать небольшие семантические результаты, actionable validation errors и правдивое покрытие. Логировать correlation IDs и коды, а не токены, raw prompts или полные payload.

Текущий `mcp_manifest` не возвращает значения annotations. Аудит не доказывает их дефект; в OPQ-08 поставлена проверка фактического tools/list и поведения каждого effect class, включая смешанный `direct_ops_v2`.

## Рекомендации первичных источников

OpenAI рекомендует короткую карту репозитория, каноническое знание в документации и проверку инвариантов, а не огромный монолитный AGENTS. Это поддерживает небольшой каталог со ссылками, без копирования всех историй [S1].

Anthropic рекомендует минимальный достаточный контекст, выразительные инструменты и проверку на реальных многошаговых сценариях. Применение здесь: релевантная выборка capabilities, bounded responses и оценка правильности результата, а не навязывание единственной последовательности tool calls [S2, S3].

OpenAI требует правдивых effect annotations, server-side authorization и минимизации данных; MCP запрещает token passthrough. Поэтому общий код не означает общую сессию пользователя, универсальный сервисный токен или ослабление подтверждений [S4–S6].

## Порядок исполнения

Сначала OPQ-01/02: восстановить достоверную наблюдаемость и выбор source. Затем OPQ-03: сделать каталог доступным обоим путям — ChatGPT Direct Ops и OpenCode. OPQ-04/05: проверяемые версии/capabilities и влияние инцидентов. После этого ограниченные пилоты OPQ-06/07; OPQ-08 добавляет сквозные тесты качества и безопасности, OPQ-09 выполняется при естественном изменении edge-кода.

Создание документации не равно реализации этих задач. Автоматический монитор, фоновые задания и runtime injection данным аудитом не включены.

## Источники и воспроизведение

Пути файлов читать в репозиториях по ref из таблицы. Для incident ID использовать `project_probe incident_get` соответствующего проекта. Для runtime-находок повторить только read-only `host_probe`; отсутствие/наличие после исправления должно иметь новый timestamp.

Ключевые source paths:

- DevCoveer: `docs/requirements-guard.md`, `docs/process-recovery-20260930.md`, `runtime/direct_tool_catalog.py`, `runtime/projects_hub_edge_operator.py`.
- Projects Hub: `pyproject.toml`, `src/projects_hub/live_runtime.py`.
- Street: `backend/requirements.txt`, `backend/street_story/live_socket.py`, `backend/street_story/quota.py`.
- Events: `requirements.txt`, `static_site_live_search.py`, `kaggle_registry.py`, `kaggle_recovery.py`, `kaggle_status.py`, `docs/features/smart-event-update/caller-inventory.md`.
- Wonderful: `package.json`, `AGENTS.md`, `docs/operations/incident-management.md`, `docs/reports/incidents/catalog.json`, связанные карточки/history/evidence.
- My Data Hub: `docs/17-kaggle-control-plane.md`, `src/my_data_hub/providers/kaggle/contracts.py`.
- VibePublish: `docs/routes.yml`, `social_operations/assets.py`, указанные incident IDs.
- Live / Resource Control: `README.md`, `docs/integration.md`, `docs/native-wss.md`, `docs/rollout.md` соответственно.

Внешние источники проверены 3 октября 2026:

- [S1 OpenAI — Harness engineering, 11.02.2026](https://openai.com/index/harness-engineering/)
- [S2 Anthropic — Effective context engineering, 29.09.2025](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [S3 Anthropic — Writing effective tools, 11.09.2025](https://www.anthropic.com/engineering/writing-tools-for-agents)
- [S4 OpenAI — Plugins Security & Privacy](https://developers.openai.com/plugins/guides/security-privacy)
- [S5 OpenAI — Plugins Reference, annotations](https://developers.openai.com/plugins/reference)
- [S6 MCP — Security Best Practices, versioned 2025-11-25 page](https://modelcontextprotocol.io/docs/2025-11-25/tutorials/security/security_best_practices)
