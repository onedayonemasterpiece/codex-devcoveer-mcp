# Operational Quality Phase 1 — Acceptance Report

Дата: 2026-10-03
Репозиторий: `onedayonemasterpiece/codex-devcoveer-mcp`
Статус поставки: **IMPLEMENTED_AND_DEPLOYED — AWAITING_CONTROL_REVIEW**
Статус независимой проверки: `awaiting_control_review`

## 1. Что изменилось

До этой поставки один отсутствующий зарегистрированный resource root мог уронить весь `resources`; `log_search` мог вернуть ноль совпадений при `complete=false/source_truncated=true`; `read_text` не отличал завершённость порции от завершённости запрошенного диапазона. Source checkout, remote main, installed runtime и served/accepted состояние приходилось собирать вручную, а карта межпроектного переиспользования существовала только как документ.

После поставки:

- `resources` возвращает результат по каждому root отдельно; missing optional root остаётся typed `missing`, а остальные измерения сохраняются.
- `log_search` имеет frozen observation upper bound, cursor continuation, requested/covered interval, cumulative match count и явный `coverage_complete`. Пустой неполный ответ не является доказательством отсутствия ошибок.
- `read_text` возвращает `actual_range`, `requested_range`, `eof`, `requested_range_complete`, `next_start_line` и причину неполноты.
- `quality_inventory` различает remote/main, выбранный checkout, declarations, installed, served и accepted, сохраняя неизвестное как `unknown`.
- `reuse_context` автоматически доступен Direct Ops и агентному пути; каталог остаётся advisory, а semantic donor selection делает работающая модель.
- `reuse_compatibility`, `reuse_incident_impact`, `reuse_portfolio` и `reuse_portfolio_record` дают исполняемые bounded проверки версий/capabilities, межпроектного incident impact и drift без новой БД/daemon.
- Requirements Guard, authorization, durable recovery и idempotency не заменены новой системой согласований.

Короткие live-примеры:

- `resources`: `codex_worktrees -> status=missing`, при этом source/runtime/projects и остальные roots — `status=ok`; весь probe не падает.
- `log_search`: один frozen 180-second interval прочитан последовательно за 14 страниц; final `coverage_complete=true`, `matches_seen=7`.
- `read_text runtime/direct_actions.py`: 1346 строк восстановлены порциями 1–200 … 1201–1346; только последняя порция имеет `eof=true` и `requested_range_complete=true`.
- `reuse_context`: компактная summary-карта содержит 8 capability IDs; раскрытие четырёх карточек вернуло разные kinds `shared_library / extraction_candidate / reusable_pattern` и их явные exclusions.

## 2. D01–D12

| ID | Status | Evidence / result | Граница применимости |
|---|---|---|---|
| D01 | PASS | Source regression: `tests/test_operational_quality_phase1.py`. Live 3.31.1: `resources(largest=0)` вернул typed missing для `codex_worktrees` и достоверные результаты остальных roots. | Production-каталоги не удалялись и не создавались для теста. |
| D02 | PASS | Fixtures: multi-page match, invalid cursor, rotation/retention gap, empty complete interval. Первичный live 3.31.0 выявил реальный `cursor_gap`; исправлено PR #119. Live 3.31.1: 14 страниц одного frozen interval, cumulative `matches_seen=7`, final `coverage_complete=true`. | Первая неудачная live-проверка сохранена как evidence, а не скрыта. |
| D03 | PASS | Source regression восстанавливает длинный файл continuation-цепочкой. Live `runtime/direct_actions.py`: 1346 строк, 7 порций, interim `complete=false`, final EOF/range complete. | Legacy `start_line/end_line/text` сохранены. |
| D04 | PASS | Live `quality_inventory`: DevCoveer показывает remote main `aae1a47…` отдельно от stale dirty checkout `36ab2e…`; Projects Hub и Wonderful показывают declarations отдельно от installed/served/accepted. Unknown не превращается в PASS. | Для consumer runtime, где нет зарегистрированного receipt, installed/served остаются unknown. |
| D05 | PASS | Direct `reuse_context` readback + автоматический `reuseContext` в probe responses. OpenCode read-only task `dvt_f2564aff1d0c477fb1532d705b7630bd` и continuation одной сессии получили тот же navigation context. | Не создаётся новый approval gate; catalog unavailable должен деградировать advisory. |
| D06 | PASS | Семантический smoke из 4 случаев: paraphrased realtime microphone/ordered audio → `live.audio-session`; Kaggle lifecycle → `kaggle.provider-operations`; POI provenance → `facts.accepted-outcome-provenance` с отказом от общего semantic engine; lost response/unknown effect → `operations.durable-recovery`. | Это scoped smoke текущих моделей, не массовый benchmark. |
| D07 | PASS | Прочитаны реальные Wonderful incidents. Shared hall case `inc_79e1bb…` с confidence=suspected распространяется как suspected на 4 consumers; local permissions `inc_e0cdf…` остаётся только Wonderful; unknown не повышается; synthetic PASS не разрешает closure. | Incident records не переписаны как “fixed”; hall incident остаётся open. |
| D08 | PASS | Реальный `reuse_portfolio`: 8 карточек, sources, no coverage gaps, digest `99819523…`. `reuse_portfolio_record` повторён без изменений — no alert duplication. Hook фактически сработал при source commit/push. Source fixtures покрывают changed receipt и unavailable source gap. | Нет нового периодического daemon/external notifier. |
| D09 | PASS | Live contract: разные source SHA при совместимых interface/version/capabilities/transport → compatible; missing capability → incompatible; browser/native transport mismatch → incompatible; artifact checksum mismatch → integrity failure. | Межсервисный SHA не является compatibility lock. |
| D10 | PASS | Full source suite после live-fix: **341 passed, 4 skipped**. Есть regressions на mixed effect annotations, authorization denial, untrusted catalog/log text, lost response/concurrent retry, Requirements Guard и redaction. 900-second bounded tunnel search после финального restart: 0 NameError/TypeError при `coverage_complete=true`. | Частота/причина platform ChatGPT safety reviews остаётся unknown: клиентского machine-readable сигнала/denominator нет; намеренно safety alert не вызывался. |
| D11 | PASS | PR #117 docs → merge `9eeeda6…`; PR #118 implementation → merge `bf70a6e…`; PR #119 live journal fix → merge **`aae1a47…`**. Installed `3.31.1-operational-quality`; bridge SHA `22c42d72…`; rollback locator сохранён. Final MCP tunnel PID 446620, active/running. | Было два tunnel restart: первый для 3.31.0, второй стал необходим после обязательной live D02 выявила и потребовала исправить собственную регрессию. Product services не перезапускались. |
| D12 | PASS | Canonical docs/tool entrypoints/catalog обновлены; этот report записан на source-line. IdeaHub replacement PR #92 merged `f069024…`; selected session validator PASS; full registry по-прежнему имеет те же 25 исторических/посторонних ошибок и не объявлен исправленным. | IdeaHub session `overall=open`: Direct Ops запрещает staging path segment `registry/sessions`; запрет не обходился. Routing/processing/materialization evidence находятся в merged routing record и source packet. |

## 3. Source, merge, install и rollback

- Audit/docs: PR #117, merge `9eeeda6e146cc525432829369e1ec5fe52716d76`.
- Phase-1 implementation: PR #118, merge `bf70a6e64f182bcdb526c2cf15b39d3f2d61aab8`.
- Live D02 fix: PR #119; deployed runtime release source `aae1a47de980acfbe8e3a79d0b8621b5c72e662e`. Финальный acceptance-report merge выполняется позже как docs-only и не требует reinstall.
- Installed version: `3.31.1-operational-quality`.
- Installed bridge SHA-256: `22c42d729cc83ffa3afd30318eb09301c10b5574020d1f394a7cc520186869fb`.
- Final deployment evidence: `/home/dev/artifacts/codex-devcoveer-mcp/20261003T181152Z-install-current-runtime`.
- Rollback locator: `/home/dev/artifacts/codex-devcoveer-mcp/20261003T181152Z-install-current-runtime/previous-runtime`.
- Final service: `codex-devserver-tunnel.service`, PID `446620`, start `2026-10-03 18:12:24 UTC`.
- Canonical dirty checkout `/home/dev/projects/codex-devcoveer-mcp` не reset/clean/stash; он остаётся отдельным stale working source.\n- После merge этого отчёта remote `main` может быть новее installed source только за счёт docs-only acceptance commit; runtime identity остаётся явно привязанной к `aae1a47…`.

## 4. Read-only smoke для контрольного окна

Через действующий `direct_ops_v2`:

1. D01: project `codex-devcoveer-mcp`, plane `host_probe`, operation `resources`, payload `{"largest":0}`.
2. D02: plane `host_probe`, operation `log_search`, безопасный payload: `log=mcp_tunnel`, query=`operation_finished`, `since_seconds=180`, `page_lines=20`; повторять с возвращённым `cursor` до `coverage_complete=true`. `requested_interval.end` должен оставаться frozen.
3. D03: plane `project_probe`, operation `read_text`, `source=local_commit`, `ref=origin/main`, `path=runtime/direct_actions.py`; идти по `next_start_line`.
4. D04: plane `project_probe`, operation `quality_inventory`; повторить для `codex-devcoveer-mcp`, `projects-hub`, `wonderful-lections`.
5. D05: plane `project_probe`, operation `reuse_context`; без IDs возвращается компактная navigation summary, с `capability_ids` — bounded expansion (не более 5 карточек).
6. D08: plane `project_probe`, operation `reuse_portfolio`; затем read-only сравнение digest/baseline. Mutating baseline action — `project_action/reuse_portfolio_record`, использовать только если контрольному окну нужна проверка эффекта.
7. D09: plane `project_probe`, operation `reuse_compatibility`; compatibility определяется interface/version/capabilities/transport, не совпадением SHA.

Fresh-window top-level schema может быть клиентски закэширована; stable `direct_ops_v2` envelope намеренно позволяет вызывать новые server operations без reinstall/redeploy. Fresh native top-level catalogue остаётся допустимой внешней контрольной проверкой, но не требует нового rollout.

## 5. OpenCode smoke

- Task: `dvt_f2564aff1d0c477fb1532d705b7630bd`.
- Provider/model: `opencode/nemotron-3-ultra-free`.
- Access: read-only.
- Первая задача выбрала четыре ожидаемые capability cards по смыслу, включая microphone handoff без слов “Live/WSS”.
- Continuation выполнен в той же session (`continued_same_session`), не потерял advisory context и подтвердил, что facts donor — pattern, а не общий deterministic semantic engine; при недоступном каталоге работа должна продолжаться как advisory unknown без ослабления Requirements Guard.
- Evidence SHA после continuation: `d7c71f6bb1e525f217fed12ae4bf2a9f72dc00653ed10fb450a916b13fe27704`.
- Raw private transcript в этот report не копируется.

## 6. Tests и ограничения

Source verification после финального live-fix:

- `scripts/verify-source.sh`: **341 passed, 4 skipped**.
- Clean-host runtime builder: PASS.
- Focused operational-quality suite: 14 PASS; focused regression set после fixture alignment: 16 PASS.
- Runtime 900-second bounded search после финального restart: `NameError|TypeError` — 0 matches, complete coverage.

IdeaHub:

- PR #92 merge: `f069024fc157768ab0853139aa4fe84779c6e482`.
- Selected validation: `python tools/registry.py validate --session-id voice-20261003-083638-766e61b7` → PASS.
- Full registry validation: FAIL с **25** историческими/посторонними ошибками — тот же размер baseline, их устранение не входит в эту поставку.
- Старый PR #91 конфликтует с более новым IdeaHub main и superseded merged PR #92; он не использовался как source для overwrite новых изменений.

Observability/safety:

- Client-side platform safety-review classifier/reason/denominator не доступен, поэтому нельзя честно заявить снижение частоты ChatGPT checks.
- Existing incidents `inc_453f4acbb881a4bd4864a087` и `inc_5ba055f5f05a4dd10f9a69fd` не закрывались.
- Попытка добавить verification note с work checkpoint была отклонена `incident_conflict`; запись не выполнялась и не обходилась альтернативным mutation route.

## 7. Намеренно не сделано

Не закрываются этой phase-1 поставкой:

- OPQ-06 — extraction общей Kaggle-библиотеки.
- OPQ-07 — дальнейшая работа Street Story facts; LLM-first semantics сохраняется.
- OPQ-09 — neutral edge primitives refactor.
- Массовая миграция Live consumers на одну версию.
- Полные consumer JS/Python rollout suites за пределами scoped compatibility contracts OPQ-04.
- Новый safety classifier/попытки отключить платформенные проверки OPQ-08.
- Новая vector DB, LLM-router, daemon, approval engine, UI или periodic notifier.
- Исправление 25 исторических IdeaHub registry errors.

## Итог

Phase 1 реализована, интегрирована в main, установлена и live-проверена. Собственная регрессия journal continuation была обнаружена обязательной production acceptance, исправлена отдельным PR #119, повторно установлена и проверена до полного coverage. Все D01–D12 имеют evidence в пределах заявленного scope.

**IMPLEMENTED_AND_DEPLOYED — AWAITING_CONTROL_REVIEW**