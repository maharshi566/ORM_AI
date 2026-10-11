# Monitoring: knowing when something breaks

Four pieces, each optional, none needing anything to be installed besides the app:

| Piece | What it tells you | Set up with |
| --- | --- | --- |
| Error alerts (Sentry) | An unhandled error happened: stack trace, which endpoint, which release | `SENTRY_DSN` |
| Metrics (Prometheus) | Request rates and timings, agent outcomes, model tokens, tool failures, WhatsApp results, logins, approvals | `GET /metrics`, `METRICS_TOKEN` |
| Website errors | A visitor's browser hit an error | nothing: sent to the backend, then to Sentry |
| Health checks | The process is up; the database and Redis answer | `GET /api/health/live`, `GET /api/health` |

The logs were already structured (one JSON line per request with `LOG_JSON=true`, phone
numbers and emails masked); these add alerts and numbers on top.

## Error alerts with Sentry

1. Create a free account at <https://sentry.io>, then a project of type **FastAPI**.
2. Copy its DSN into the backend's settings: `SENTRY_DSN=https://...@....ingest.sentry.io/...`.
3. Restart. Each unhandled error now arrives in Sentry, and Sentry emails you.

What is **never** sent: request bodies (they hold customers' names and amounts), cookies,
the `Authorization`, `X-Cron-Secret` and webhook signature headers, and phone numbers or
emails in error messages (masked, as in the logs). Expected answers (401, 403, 404,
409, 422, 429) are not errors and are not sent. `SENTRY_TRACES_SAMPLE_RATE=0.1` also
sends timings for one request in ten (performance monitoring); it is 0 by default.

Errors in the website reach Sentry too: the website reports them to
`POST /api/client-errors` (the message, the page's path and the stack; once per error,
at most five per page load), and the backend logs them and forwards them. The website
needs no Sentry key of its own.

## Metrics for Prometheus or Grafana Cloud

`GET /metrics` (on the backend, outside `/api`) answers in Prometheus' text format:

| Metric | Labels | For example |
| --- | --- | --- |
| `orm_http_requests_total` | method, route, status | 5xx answers by route |
| `orm_http_request_seconds` | method, route | 95th percentile time per route |
| `orm_workflows_total` | outcome | completed, needs_clarification, blocked, failed |
| `orm_agent_steps_total` | agent, status | which agent fails most |
| `orm_llm_tokens_total` | model, direction | tokens per model, in and out (cost) |
| `orm_tool_calls_total` | tool, status | a supplier API that keeps timing out |
| `orm_messages_total` | purpose, result | WhatsApp sent, failed, or refused |
| `orm_logins_total` | method, result | password and code logins, failed and locked |
| `orm_approval_decisions_total` | decision | approve, modify, reject |
| `orm_client_errors_total` | | errors visitors hit |
| `orm_build_info` | version, env | which build is running |

Routes are templates (`/workflows/{workflow_id}`), never raw paths, so the number of
series stays small.

**Access.** With `METRICS_TOKEN` set, only `Authorization: Bearer <token>` gets the
metrics. Without a token the endpoint is open on a developer's machine and switched off
(404) in production, so it is never public by accident.

**Grafana Cloud** (free tier) can scrape it: add a Prometheus "metrics endpoint"
integration with the URL `https://<backend>/metrics` and the token as a bearer token.
Useful first panels: requests per minute and the share of 5xx, the 95th percentile of
`orm_http_request_seconds` for `/chat`, `orm_workflows_total` by outcome, and
`rate(orm_llm_tokens_total[1d])` by model.

Each backend process keeps its own counts (they restart at zero with the process);
Prometheus handles that with `rate()` and `increase()`.

## Health checks

- `GET /api/health/live`: 200 while the process runs; it touches nothing else. Use it
  for the hosting platform's health check (Render: *Health Check Path*), so a short
  database hiccup does not get the server restarted.
- `GET /api/health`: 200 when PostgreSQL and Redis answer, 503 with the details when
  one is down. Use it for an uptime monitor such as UptimeRobot or Better Stack (free),
  which alerts you by email when it fails twice in a row.
