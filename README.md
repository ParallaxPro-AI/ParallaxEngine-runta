# Make ParallaxEngine games on Runta

Runta is the execution layer for AI agents: it provides secure cloud runtimes,
protects credentials, and controls network access. This repo contains `pxr`, a
Python CLI for making ParallaxEngine games with Runta. It sends your prompt to
a coding agent running on Runta cloud, checks the game, saves its source
locally, and gives you a URL to play it.

## Connect Runta

You need Python 3.12+ and [uv](https://docs.astral.sh/uv/getting-started/installation/).

In Runta:

1. Create a [Runta API key](https://runta.com/docs/cloud-agents/quickstart/).
   `pxr` uses it to create and manage Cloud Agents.
2. In [Dashboard → Secrets](https://dashboard.runta.com/), add a provider.
   Configure its API credential, endpoint, and default model there. Runta keeps
   and routes the provider credential.

On your computer, set the Runta API key and list the providers you added:

```sh
export RUNTA_TOKEN=rt_...
uv run pxr providers
```

`pxr providers` prints each provider's ID, name, and default model.

## Make your first game

Use the provider ID from the previous command:

```sh
uv run pxr base --provider YOUR_PROVIDER_ID
uv run pxr run --provider YOUR_PROVIDER_ID \
  --prompt "Make a 2D snake game"
```

`pxr base` installs ParallaxEngine in a temporary Cloud Agent runtime and saves
a checkpoint. Later runs restore it, so they don't have to install the engine
again. Runta requires a provider when creating a Cloud Agent; the setup agent
doesn't receive a game prompt or make a model call.

`pxr run` asks the agent to build the game, then runs ParallaxEngine's checks and
tests. It downloads the source to `results/` and leaves the runtime running so
the play URL works.

An agent gets two hours (`--timeout` seconds). If it runs over, `pxr` cancels
the agent's run (the runtime and its play URL stay up), then still checks, tests
and downloads what it built so far and marks the run `timed_out`.
Ctrl-C stops a batch: queued runs never start, started agents are stopped, and
their runtimes are kept.

If Runta's API fails while `pxr` is waiting on an agent, the run is marked
`failed`, but the agent may still be building. Reattach to it instead of
rerunning:

```sh
uv run pxr attach RUN_ID
```

`RUN_ID` is the name at the start of each line `pxr run` prints, such as
`farming-default-1-fa03c9` in `farming-default-1-fa03c9: failed → ...`. It is
also the run's folder name under `results/batches/`, and the folder's path
works too.

`attach` waits for the agent again, then checks, tests, screenshots and saves
its game like `pxr run` would. Omit `RUN_ID` to reattach every run `pxr` lost
track of. The two-hour limit still counts from when the run started.

The checkpoint is a snapshot of the engine and of `pxr/runtime/setup.sh`. Rerun
`pxr base` to pick up a newer engine or a changed `setup.sh`; `pxr run` prints
the base's engine commit and warns when `setup.sh` has changed since.

## Save your provider ID

Uncomment the profile at the top of [`pxr.toml`](pxr.toml) and add your
provider ID so `pxr run` doesn't need `--provider`.

## Find and save your games

```sh
uv run pxr gallery
uv run pxr pull
uv run pxr down
```

The gallery is a page on your computer showing each prompt, model, screenshot,
check result, source download, and play URL. Each game is copied to your
computer when its run finishes. If you change a game afterwards in its cloud
editor, `pull` saves the latest version next to the earlier copies.

`down` deletes the Runta runtimes. Their play URLs stop working, but local source
copies and gallery records stay.

Rerunning `pxr base` makes a new checkpoint but keeps the old ones on Runta.
`pxr down --old-bases` deletes the old ones that nothing uses anymore.

## Run many prompts at once

The sample game prompts are in [`pxr.toml`](pxr.toml). Edit them to save your
own prompts, or use `--prompt` for a one-off idea. Without `--prompt`, `pxr run`
makes a game for every `[[prompts]]` entry in `pxr.toml`:

```sh
uv run pxr run --parallel 4
```

`--samples 3` makes three independent versions of each prompt. `--parallel`
sets how many runs go at once (default 2); the rest wait their turn. Add
`--dry-run` to list the runs without starting them.
