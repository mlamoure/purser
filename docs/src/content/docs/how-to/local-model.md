---
title: Use a local model
description: Run Purser's optional AI categorization on a model you host (Ollama, LM Studio, vLLM, a llama.cpp server or a LiteLLM gateway), so your transactions stay on your network.
---

Purser's optional AI categorization can use a model you run yourself. Purser
talks to it through the OpenAI-compatible API that most local model servers
offer, one transaction per request
([what Purser sends](../../explanation/privacy-and-agents/#what-purser-itself-sends-to-a-model-provider)).
With the model on your own hardware, none of it leaves your network.

The model is optional. Rules and your earlier choices categorize most new
rows without one; the model is asked only about the rows they miss.

## Before you start

- A model server running where Purser can reach it: on the same machine, on
  another machine on your network, or in the same Compose project.
- A model that follows instructions. Purser asks for a one-line JSON answer;
  small instruction-tuned models (7 to 8 billion parameters) usually manage.
- An admin account in Purser.

## Find your server's base URL

The base URL is the server's OpenAI-compatible address, which usually ends in
`/v1`. Purser adds `/chat/completions` to send a prompt and `/models` to list
the models.

| Server | Default base URL | Model names look like |
| --- | --- | --- |
| Ollama | `http://HOST:11434/v1` | `llama3.1:8b`, `qwen2.5:7b` (as `ollama list` shows them) |
| LM Studio | `http://HOST:1234/v1` | The identifier LM Studio shows for the loaded model |
| llama.cpp (`llama-server`) | `http://HOST:8080/v1` | Any name: the server answers with the model it loaded |
| vLLM | `http://HOST:8000/v1` | The model it serves, such as `Qwen/Qwen2.5-7B-Instruct` |
| LiteLLM gateway | `http://HOST:4000/v1` | The `model_name` entries in its configuration |

vLLM's default port is also Purser's. If both run on one machine, start one of
them on another port.

## Reach the server from Purser's container

With Docker Compose, Purser runs in a container, and `localhost` inside it is
the container itself, not your machine. Pick the address by where the server
runs:

- **On the same machine as Docker:** use `http://host.docker.internal:PORT/v1`.
  Docker Desktop (macOS and Windows) provides that name. On Linux, add it to
  Purser's service in your
  [override file](../docker-compose/#keep-your-changes-in-an-override-file):

  ```yaml
  # docker-compose.override.yml
  services:
    purser:
      extra_hosts:
        - "host.docker.internal:host-gateway"
  ```

  On Linux the server must also listen on an address the container can
  reach, not only on `127.0.0.1`. Ollama, for example, listens on
  `127.0.0.1` unless you set `OLLAMA_HOST=0.0.0.0:11434`; keep its port closed
  to other machines with your firewall if you do not want them to use it.
- **On another machine on your network:** use that machine's address, such as
  `http://192.168.1.20:11434/v1`, and make sure the server listens on the
  network rather than only on `127.0.0.1`.
- **In the same Compose project:** use the service name, such as
  `http://ollama:11434/v1`.
- **Purser runs directly on the machine (no container):**
  `http://localhost:11434/v1` works.

## Set it up in Purser

1. Open **Settings -> Categories** and its **AI categorization** tab.
2. Choose the provider **Local model (OpenAI-compatible)**.
3. Enter the **Base URL** from above. There is no default: Purser never
   fills in a cloud address for a local model, and refuses to save one
   without its own address.
4. Leave **API key (optional)** empty unless your server expects a key (a
   LiteLLM gateway with keys, for example). If a key from a hosted provider
   is still stored, Purser sends it to your server once you save: select
   **Clear key** to remove it.
5. Select **Find models** to ask the server for its model list, then pick
   one in the **Model** field. If the server has no list, type the model's
   name.
6. Set the **Timeout**, how long Purser waits for one answer. The default is
   30 seconds; a model on a CPU or a small GPU can need more, and 120 is a
   reasonable start.
7. Select **Test connection**, then **Save**.

**Find models** and **Test connection** use what is in the form and save
nothing, so you can try an address before you keep it. A stored key goes
only to the saved provider and address: when you check anything else, Purser
sends the key you typed in the form, or no key, and the result says when the
stored key was left out. The audit log records the host each check reached.

## What Test connection checks

It makes at most two requests:

1. It asks the server for its model list. This shows whether the server is
   reachable and whether it accepts the key (or needs one).
2. It sends one short prompt to the chosen model and checks that the answer
   is the JSON Purser asks for.

It reports how long the model took and where the server's address is: this
machine, your private network or the public internet. A local provider whose
address is on the public internet gets a warning, because what Purser sends
would leave your network. Messages never contain your key or the server's own
error text.

| Message | What to do |
| --- | --- |
| Could not reach ... (ConnectError) | The server is not running, the address or port is wrong, or it listens only on `127.0.0.1`. In Docker, use `host.docker.internal` instead of `localhost`. |
| ... did not answer within N s | Raise the timeout, or choose a smaller model. |
| The server needs an API key / The server refused the API key | Enter the key the server expects, or check it. |
| The server answered HTTP 404 | Check that the base URL ends in `/v1` and that the server has the model. |
| The model answered, but not with the JSON Purser asks for | Choose an instruction-tuned model. |

## What happens next

With a provider saved, Purser categorizes after every bank sync: rules first,
then your earlier choices for the same payee, then the model for what is
left. Each answer is recorded with the model's name and confidence and can be
changed like any category. A failing server never breaks a sync: Purser stops
asking it for the rest of that pass and leaves those rows uncategorized.

An answer below the confidence floor (`ai_min_confidence`, 0.7 by default;
**Minimum confidence** on the same tab, as a percentage) is not applied
either: the row stays uncategorized and, for a bank row,
[needs review](../../how-to/review-new-transactions/). Raise the floor when
a small model guesses too freely.

## Configure it with environment variables

The same settings can come from the environment, for example in your
override file. A value saved in the settings card wins over the environment.

```yaml
# docker-compose.override.yml
services:
  purser:
    environment:
      PURSER_AI_PROVIDER: local
      PURSER_AI_BASE_URL: http://host.docker.internal:11434/v1
      PURSER_AI_MODEL: llama3.1:8b
      PURSER_AI_TIMEOUT_SECONDS: "120"
```

See [Configuration](../../reference/configuration/#runtime-editable) for
every variable. Admins can also check a setup over the REST API with
`POST /config/ai/models` and `POST /config/ai/test`.

## If you already use the OpenAI-compatible choice with a local server

Earlier versions had a single "OpenAI-compatible" choice, which also worked
with a local server. An install set up that way keeps working exactly as
before after an upgrade; the card now names that choice **Hosted
OpenAI-compatible service**. Switching it to **Local model** sends the same
requests, and adds the local privacy wording, the empty default address and
the public-address warning.
