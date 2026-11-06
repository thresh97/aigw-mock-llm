# aigw-mock-llm

A mock LLM API (Anthropic Messages and OpenAI Chat Completions, Responses and Models) on an AWS Lambda Function URL or in a
container, for seeing exactly what an AI gateway forwards upstream. Point a gateway target at it with `custom_host`, send
requests through the gateway, and read the log (CloudWatch, or the container's stdout).

> Lab test tool, not a product. No support. Unofficial.

| Path | What |
|---|---|
| `handler/handler.py` | The Lambda (Python stdlib only). Answers canned `ok` replies (JSON, or SSE when `"stream": true`) for the endpoints below, and logs one JSON line per request: header names with masked values, model, metadata, tool names, message block types, body hash, and `leaks` |
| `handler/server.py` | Serves `handler.py` over plain HTTP (stdlib), for a container or a local run. Same replies and log line |
| `Dockerfile` | Container image running `server.py` |
| `infra/` | Terraform for the Lambda, its Function URL, IAM and the log group |

| Provider | Endpoints (after `<URL><token>/v1`) |
|---|---|
| Anthropic | `POST /messages`, `POST /messages/count_tokens` |
| OpenAI | `POST /chat/completions`, `POST /responses`, `GET /models` |

`leaks` lists identity-provider data found in any header or the body: JWT-shaped strings, `keycloak`, `portkey_oid`,
`portkey_workspace`, and the strings in `leak_needles`. `MOCK_TOKENS=<n>` anywhere in the request body sets the reported input
tokens (`input_tokens` / `prompt_tokens`), for exhausting gateway usage limits. Requests whose path doesn't start with the secret
token get a 404.

## Simulate errors and delays

For routing, fallback and load-balancing demos: pick the behavior with the model name (so each gateway target or route can
point at its own fake model) or with a directive in the prompt (per request). Works on every inference endpoint.

| Directive | Model name | Anywhere in the request body | Effect |
|---|---|---|---|
| Error | `-error-<status>` | `MOCK_ERROR=<status>` | Answers that 4xx/5xx instead of `ok`, in the provider's error format (`rate_limit_error` / `rate_limit_exceeded` for 429, `overloaded_error` for Anthropic 529, ...) |
| Fixed delay | `-delay-<ms>` | `MOCK_DELAY=<ms>` | Waits that long before answering |
| Random delay | `-delay-<min>-<max>` | `MOCK_DELAY=<min>-<max>` | Waits a random time in the range, drawn per request |

```bash
# well-known names with a suffix, so they look right in gateway config and metrics
gpt-4o-error-429                       claude-sonnet-4-5-error-529
gpt-4o-delay-2000                      claude-sonnet-4-5-delay-500-3000
gpt-4o-mini-delay-1000-5000-error-503  # delay, then error
# or per request
curl -s "$URL${MOCK_TOKEN}/v1/chat/completions" -d '{"model":"gpt-4o","messages":[{"role":"user","content":"MOCK_DELAY=100-800 MOCK_ERROR=503"}]}'
```

The prompt wins over the model name. The delay runs first, then the error, if any. The log line's `simulate` field shows what was
applied (`delay_s`, `error`). On Lambda, set `timeout` (seconds, default 60) above your longest delay: `terraform apply -var timeout=120`.
A delay longer than the gateway's `request_timeout` tests the gateway's timeout handling.

## Deploy

```bash
echo 'leak_needles = "<jwt-sub>,<org-id>"' > infra/terraform.tfvars   # optional, gitignored
terraform -chdir=infra init
terraform -chdir=infra apply
URL=$(terraform -chdir=infra output -raw url)          # ends in /
MOCK_TOKEN=$(terraform -chdir=infra output -raw token)
curl -s "$URL${MOCK_TOKEN}/v1/messages" -d '{"model":"x"}'            # canned Anthropic message
curl -s "$URL${MOCK_TOKEN}/v1/chat/completions" -d '{"model":"x"}'    # canned OpenAI completion
```

Gateway targets:

```json
{"provider": "anthropic", "custom_host": "<URL><token>/v1", "forward_headers": ["..."], "request_timeout": 60000}
{"provider": "openai",    "custom_host": "<URL><token>/v1", "forward_headers": ["..."], "request_timeout": 60000}
```

The function name, IAM role and log group come from `var.name` (default `aigw-mock-llm`). To keep an existing deployment made
under the old name (`aigw-mock-anthropic`), apply with `-var name=aigw-mock-anthropic`; otherwise Terraform replaces the function
and the URL and token change.

## Read the log

```bash
aws logs tail "$(terraform -chdir=infra output -raw log_group)" --since 10m --format short | grep '"mock": "request"'
```

## Run as a container

```bash
docker build -t aigw-mock-llm .
MOCK_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
docker run -d --name aigw-mock-llm -e TOKEN="$MOCK_TOKEN" -e LEAK_NEEDLES="<jwt-sub>,<org-id>" -p 8080:8080 aigw-mock-llm
curl -s "http://localhost:8080/${MOCK_TOKEN}/v1/messages" -d '{"model":"x"}'
docker logs -f aigw-mock-llm | grep '"mock": "request"'
```

`TOKEN` is required; `LEAK_NEEDLES` and `PORT` (default 8080) are optional. `GET /healthz` answers 200 without the token, for
liveness and readiness probes. The gateway's `custom_host` is `<base-url>/<token>/v1`, where the base URL is whatever reaches the
container from the gateway (a Kubernetes Service, for example). Plain HTTP: put TLS in front (ingress, load balancer) if the
gateway needs HTTPS. Without Docker: `cd handler && TOKEN=<secret> python3 server.py`.

## Test locally

```bash
python3 -m unittest discover tests
```

Stdlib only. With `pip install openai anthropic`, it also calls the handler through the official clients over local HTTP and
validates every response and stream event against the SDK models.

## Teardown

```bash
terraform -chdir=infra destroy
```
