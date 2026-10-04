# Using OmniRoute (one local address for many AI providers)

This is optional. The app works with an OpenAI key alone (`OPENAI_API_KEY`), or with no
key at all for the offline search baseline (`EMBEDDING_MODEL=hash`). OmniRoute is for when
you want one local address that can reach many AI providers, including free ones, and
switch between them without changing the app.

> **What I could and could not test.** The app side is tested against a fake gateway that
> behaves like OmniRoute (`backend/tests/fake_gateway.py`): the address, the key, JSON
> mode, tool calls, embeddings and every error message. I could not start the real
> OmniRoute from my cloud workspace, so the dashboard steps below come from OmniRoute's own
> documentation. If a button is named differently on your screen, trust your screen, then
> run `python -m scripts.check_llm` (step 6), which tells you whether the connection works.

## 1. What it is, in plain words

OmniRoute is a small program that runs on your computer. Your code talks to it exactly as
it would talk to OpenAI (same "OpenAI-compatible" API). OmniRoute then forwards each request
to a provider you connected (a free one, or one you have an account with) and can fall back
to another provider when one fails or runs out of quota.

```
this app  ->  http://localhost:20128/v1  ->  OmniRoute  ->  provider A (or B, or C ...)
```

The app needs only two settings for this: where OmniRoute is (`LLM_BASE_URL`) and its key
(`LLM_API_KEY`). Nothing else in the code changes. That is also why you can drop OmniRoute
later, or swap it for another gateway (LiteLLM, Ollama), by editing `.env`.

## 2. How the settings combine

| Setting | Meaning | If empty |
| --- | --- | --- |
| `OPENAI_API_KEY` | Key for OpenAI itself. | OpenAI is not used. |
| `LLM_BASE_URL` | Address of a gateway, ending in `/v1`. | Calls go to OpenAI. |
| `LLM_API_KEY` | The gateway's key. | Falls back to `OPENAI_API_KEY`. |
| `EMBEDDING_BASE_URL` | Address for embeddings only. | Same place as chat. |
| `EMBEDDING_API_KEY` | Key for that address. | Same key as chat (or `OPENAI_API_KEY` when `EMBEDDING_BASE_URL` is set). |
| `LLM_MODEL_FAST`, `LLM_MODEL_SMART` | Model names for quick and careful jobs. | The Phase 4 agents need both. |
| `EMBEDDING_MODEL` | Embedding model name, or `hash` for the offline baseline. | `text-embedding-3-small`. |

Two safety rules are built in. A key is only ever sent to the address it belongs to (the
gateway's key is never sent to `EMBEDDING_BASE_URL`). And no message, log line or error
ever prints a key or a password written inside an address.

## 3. Step by step

Everything below is typed in the IntelliJ terminal (PowerShell). Steps 1 to 4 start in the
project's top folder (`ORM_AI`, the one that contains `docker-compose.yml`).

### Step 1. Start OmniRoute

```powershell
docker compose --profile gateway up -d omniroute
docker compose ps
```

The first run downloads the image, which takes a minute or two. `docker compose ps` should
list `omniroute` as `running`. It is published on `127.0.0.1` only, so only your own
computer can reach it.

### Step 2. Open the dashboard

Open <http://localhost:20128> in your browser. If it asks for a password, OmniRoute's
documentation gives `CHANGEME` as the initial one; change it straight away in
**Settings → Security**. If it does not ask, you are in.

### Step 3. Connect one provider

In the dashboard open **Providers** and connect one:

- **OpenCode Free** needs no account, so it is the quickest way to see everything work.
- Or connect a provider where you already have an API key, and paste the key there.

Read the next section (caveats) before you connect anything you pay for.

### Step 4. Copy the key

Open **Endpoints** in the dashboard and copy the API key shown there.

### Step 5. Edit `.env`

Open `backend/.env` (create it from `.env.example` if you have not yet; the file is in
`backend/` next to `pyproject.toml`, or in the top folder, either works) and set:

```dotenv
LLM_BASE_URL=http://localhost:20128/v1
LLM_API_KEY=paste-the-key-from-step-4
LLM_MODEL_FAST=auto/fast
LLM_MODEL_SMART=auto/smart
EMBEDDING_MODEL=hash
```

`auto/fast` and `auto/smart` are OmniRoute's own "pick a good provider for me" names. To use
one specific model instead, run `python -m scripts.check_llm --list` (step 6) and copy a
name from the list, for example one that looks like `provider/model-name`.

`EMBEDDING_MODEL=hash` keeps search working offline for now; step 7 explains the better
options. Never paste a key into a chat or commit `.env` (it is already ignored by git).

### Step 6. Test it

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
python -m scripts.check_llm
```

It sends about ten tiny requests and prints one line each:

```
[ ok ] Model list           14 models, for example auto, auto/fast, auto/cheap, auto/smart
[ ok ] Chat                 auto/fast replied 'ready'
[ ok ] JSON mode            auto/fast returned valid JSON
[WARN] Tool calling         auto/fast answered in text instead of calling the tool.
       -> The Phase 4 agents depend on tool calling: choose a model that supports it.
[ skip ] Embeddings         hash is the offline embedder.
```

(The lines above are an example of the format, not a recording of OmniRoute.)

- `ok` means it works. `skip` means nothing to test.
- `WARN` means it works but has a limit. A model that ignores tools cannot run the Phase 4
  agents, so choose another model (`--model provider/model-name` tests one by name). A model
  without JSON mode is fine as long as you keep `RERANKER=heuristic` (the default).
- `FAIL` means something must be fixed; the line says what. The table in section 5 covers
  the usual ones.

Useful variations:

```powershell
python -m scripts.check_llm --model auto          # test one chat model by name
python -m scripts.check_llm --list                # every model the gateway offers
python -m scripts.check_llm --list embed          # only names containing "embed"
```

### Step 7. Choose where embeddings come from (knowledge search)

Search needs an embedding model. You have three choices:

1. **Keep `hash`.** Free and offline, matches shared words but not meanings. This is the
   baseline the tests use (hit@5 0.95 on the evaluation set).
2. **Use an embedding model your gateway offers.** Run
   `python -m scripts.check_llm --list embed`. If something is listed, set
   `EMBEDDING_MODEL=` to that exact name and run `python -m scripts.check_llm` again.
3. **Use OpenAI for embeddings and the gateway for chat.** Add to `.env`:

   ```dotenv
   EMBEDDING_BASE_URL=https://api.openai.com/v1
   EMBEDDING_API_KEY=your-openai-key
   EMBEDDING_MODEL=text-embedding-3-small
   ```

After you change `EMBEDDING_MODEL`, build the new search index and measure it (each model
keeps its own index, so nothing is overwritten):

```powershell
python -m scripts.ingest
python -m scripts.eval_retrieval
```

A different embedding model scores texts on a different scale. The evaluation prints the
range to choose from; set `RETRIEVAL_MIN_SIMILARITY` in `.env` to the value it suggests.

## 4. Before you rely on it

- **Provider rules.** Some providers' terms do not allow their accounts or subscriptions to
  be used through a gateway. OmniRoute's dashboard has a terms-risk catalogue that marks
  providers to avoid. Read it, and connect only providers you are allowed to use that way.
- **Privacy.** Whatever the app sends (shop names, stock, customer names, credit
  balances) goes to the provider that answers. Free tiers may log prompts or train on them.
  The demo data is invented, so this costs nothing now. Before real customers, use a provider
  with a no-training promise, and note that Phase 5 adds masking of personal details.
- **`auto` changes the model.** Automatic routing may answer one request with one model and
  the next with another, so answers, and evaluation scores, vary from run to run. For the
  Phase 8 evaluation, pin one specific `provider/model-name`.
- **Models differ.** JSON mode, tool calling and quality depend on the model behind the
  name. `check_llm` shows what each one supports.
- **Keep it on this computer.** The compose file publishes OmniRoute on `127.0.0.1` only. If
  you ever make it reachable from other machines, require a key (OmniRoute's `REQUIRE_API_KEY`
  setting) and put `https://` in front of it. `check_llm` warns when `LLM_BASE_URL` uses
  plain `http://` to a public address.
- **It is someone else's software.** OmniRoute is an open-source project (MIT licence)
  that stores your provider keys. The compose file pins its version (`OMNIROUTE_VERSION` in
  `.env`); upgrade on purpose: change the number, then run
  `docker compose --profile gateway pull omniroute` and the `up -d` command again.

## 5. When something is wrong

| What you see | What it means | What to do |
| --- | --- | --- |
| `Could not reach the gateway ... Is it running?` | Nothing answers at `LLM_BASE_URL`. | Run `docker compose ps`. Start it with the command in step 1. Check the port in `LLM_BASE_URL` matches `OMNIROUTE_PORT`. |
| `rejected the API key` | Wrong or missing `LLM_API_KEY`. | Copy the key again from **Endpoints** (step 4). |
| `HTTP 404 (not found) ... ends in /v1` | The address or the model name is wrong. | The address must end in `/v1`. Check the model name with `--list`. |
| `rate limit or quota reached` | The provider behind the gateway ran out of free quota. | Wait, connect another provider, or use a combo such as `auto`. |
| `answered in text instead of calling the tool` | That model does not do tool calls. | Test another model with `--model`. |
| `cannot be read` / `Is the address an OpenAI-style endpoint` | The address is not an OpenAI-style API. | Use the `/v1` address from the dashboard. |
| `LLM_BASE_URL is ... not a web address` | The address is missing `http://`. | Write it in full: `http://localhost:20128/v1`. |

Search errors use the same wording, for example when you run `python -m scripts.ingest`
with an embedding model the gateway does not have.

## 6. Later: when the backend itself runs in Docker

Inside a container, `localhost` means the container, not your computer. If the backend runs
in Docker Compose (Phase 9), use the service name instead:
`LLM_BASE_URL=http://omniroute:20128/v1`. While you run the backend with `uvicorn` on your
computer, keep `http://localhost:20128/v1`.

## 7. Stop it, or remove it

```powershell
docker compose --profile gateway stop omniroute     # stop; settings are kept
docker compose --profile gateway rm -f omniroute    # remove the container; the data volume stays
docker volume rm orm_ai_omniroute_data              # also delete its saved providers and keys
```

Without Docker, OmniRoute can also run with `npm install -g omniroute` and then
`omniroute` (it needs a recent Node.js; see its README). The address is the same.
