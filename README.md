# aigw-mock-llm

A mock LLM API (Anthropic Messages and OpenAI Chat Completions, Responses and Models) on an AWS Lambda Function URL, for seeing
exactly what an AI gateway forwards upstream. Point a gateway target at it with `custom_host`, send requests through the gateway,
and read the CloudWatch log.

> Lab test tool, not a product. No support. Unofficial.

| Path | What |
|---|---|
| `handler/handler.py` | The Lambda (Python stdlib only). Answers canned `ok` replies (JSON, or SSE when `"stream": true`) for the endpoints below, and logs one JSON line per request: header names with masked values, model, metadata, tool names, message block types, body hash, and `leaks` |
| `infra/` | Terraform for the Lambda, its Function URL, IAM and the log group |

| Provider | Endpoints (after `<URL><token>/v1`) |
|---|---|
| Anthropic | `POST /messages`, `POST /messages/count_tokens` |
| OpenAI | `POST /chat/completions`, `POST /responses`, `GET /models` |

`leaks` lists identity-provider data found in any header or the body: JWT-shaped strings, `keycloak`, `portkey_oid`,
`portkey_workspace`, and the strings in `leak_needles`. `MOCK_TOKENS=<n>` anywhere in the request body sets the reported input
tokens (`input_tokens` / `prompt_tokens`), for exhausting gateway usage limits. Requests whose path doesn't start with the secret
token get a 404.

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
