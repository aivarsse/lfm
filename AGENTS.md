# lfm — project notes for agents

General workflow rules live in the global `~/.pi/agent/AGENTS.md`. This file covers only this repository.

## What this project is

A small CLI that finds similar artists via the Last.fm API: one read-only method
(`artist.getSimilar`) plus lookups to enrich results, all cached in SQLite, with a
Textual explorer and local playback through mpv.

`PLAN.md` is the current source of truth for what is done and what is left; keep it
in step when a feature lands or a decision changes.

## Architecture

Four layers under `src/lfm/`, each with one job, plus two modules at the root that
cross all of them.

```
cli.py        the entry point: Typer commands, one per verb
config.py     settings resolution: flag > environment > config.toml > default
paths.py      where lfm keeps its files: the config directory and the cache one

api/          talking to Last.fm and describing what it says
  client.py     the ONLY module that touches the network (httpx, async)
  models.py     Pydantic models absorbing Last.fm's quirks
  rank.py       pure functions: rank, blend, deep cuts, suggestions, shared tags
  flows.py      the API sequences both interfaces need, in one place
  protocols.py  the read half as Protocols: what the flows and the screens take
store/          small local state, beside the cache
  cache.py      SQLite TTL cache, keyed by method + params
  history.py    recent artists, for the prompt's completion
  sources.py    the source chosen per track (a watch URL, so it does not expire)
playback/       the child processes, and what is playing
  player.py     owns ONE mpv process over its JSON IPC socket; resolves with yt-dlp
  visualizer.py cava in raw output mode, for the header's EQ strip
  scrobble.py   outbox, ledger, drain: what counts as a play and how it is sent
ui/             everything the user sees
  render.py     Rich tables, plain and json output (also what the CLI prints)
  theme.py      three Textual themes and the one stylesheet they dress
  shortcuts.py  the key sheet, generated from BINDINGS
  widgets.py    row builders, the prompt, the player bar, small app helpers
  screens.py    similar artists, a tag, and the source picker
  artist.py     the artist page: stats, tags, albums, tracks, radio, playback
  app.py        LfmApp: screens, bindings, theme, the key sheet that reads them
```

The rules that keep the layers worth having:

- Only `api/client.py` talks to the network. Nothing else imports httpx.
- A sequence of calls both the CLI and the TUI need goes in `api/flows.py` —
  never in both. It is either one call with a rule attached (the whole similar
  list, then rank locally) or two or three calls that must agree. Written twice,
  it is tuned twice, and the interface that was not tuned keeps the old rule.
- Only `playback/` spawns a child process, and only it knows mpv or cava exist.
- `ui/` may import any layer; nothing outside `ui/` imports `ui/`. `render.py` is
  in `ui/` because it formats for a person, not for a caller.
- `store/` is best effort and never fatal: a cache that cannot be read is a cache
  miss, not an error.
- `playback/` reaches into `store/` with an import inside the function, because
  playback is imported by everything and the store is not its business until it
  records what played.
- The CLI imports its layer *inside* each command, so `lfm --help` never pays for
  Textual or mpv. Keep that: a module-level import in `cli.py` that pulls in `ui/`
  makes every `--help` slow.
- `screens.py` imports the artist page where it pushes it, inside the two
  methods that do — the one deliberate import in a function body. `artist.py`
  imports `screens.py` at module level, so the reverse would be a cycle.

## Commands

| Task | Command |
|---|---|
| Install / sync env | `uv sync` |
| All quality gates | `make check` |
| Lint only | `make lint` |
| Types only | `make types` |
| Tests only | `make test` |
| Tests, in parallel | `make test-fast` |
| The guard, locally | `make guard` (any two refs: `BASE=… HEAD=… make guard`) |
| Run the app | `uv run lfm similar "Burial"` / `uv run lfm tui "Burial"` |

`make check` is lint + types + tests; CI runs it as separate jobs and adds two the
Makefile does not have: `diff-cover coverage.xml --compare-branch=origin/main
--fail-under=80` (pull requests only) and a `security` job. Run the diff-cover
line before pushing, regenerating `coverage.xml` in the same command — a stale
file prints the previous diff's numbers and reads as a pass or a failure that has
nothing to do with the change.

The `guard` job is `.gitea/guard.py`, which `make guard` runs here against any two
refs: a check nobody can run before pushing is a check nobody trusts. It reports
which test files were touched and fails only on substance — a test function that no
longer exists; a test whose **own** assertion count fell (per test, not the suite's
total, which let a gutted test hide behind a new one until #145's second pass); an
added skip/xfail/ignore; a `pyproject.toml` change that is anything but the version
line; or a change to `.gitea/`, the Makefile or another build entry point. Touching
a test file, adding tests to one, or moving tests between files is not itself a
failure — that was the old behaviour, and #145 replaced it. Neither is a version
bump on its own, for the same reason. Explain a real hit on the PR; never weaken a
test or move it to dodge it.

## Testing

- **Random order by default.** `pytest-randomly` reshuffles every run and prints
  `Using --randomly-seed=N` in the header: re-run with `--randomly-seed=N` to
  reproduce an order-dependent failure, `-p no:randomly` to rule one out. The suite
  is expected to pass in any order — a flake is a real bug, not noise.
- **`--cov-branch`**, not only line coverage. `if self._writer is None` is one of
  the branches the Enter bug lived in, and 100% of lines says nothing about it.
  `make test-fast` is the same suite across workers for when four minutes hurts.
- **Properties, not only examples**, for the pure functions (`progress_bar`,
  `volume_glyph`, `rank`, `blend`): `tests/test_properties.py` asserts the
  invariants — a bar is exactly as wide as it was asked for, a ranking follows the
  deciding column, a cap of zero returns zero. That last one was a real bug, found
  on `rank` the first time hypothesis ran, and fixed in the same change.
- **`tests/test_bar_layout.py`** holds the bar's thresholds at the sizes either
  side of them: 68 columns, where the seek buttons go, and the readout never being
  pushed off the row.
- **`tests/test_real_terminal.py`** runs the app in a real tmux terminal and
  asserts on the cells it actually has — the only place wide characters are
  checked for real, and this app's data is full of them. Skipped where tmux is
  absent, which is a skip and not a pass; `tests/run_in_terminal.py` is what it
  starts. The SVG snapshots record what Rich believes the screen to be: useful,
  diffable, and not the same claim.

## Working here

- **Cut the branch before the first edit**, as the first action of a task:
  `git switch -c agent/<N>-<slug> origin/main`. Committing to a branch that is
  already merged is the most common slip in this repo, and it always comes from
  starting work without a new branch — `git switch -c` aborts on a dirty tree and
  leaves you where you were, and in an `&&` chain that abort is easy to read past.
- Before the first push, check the base: `git log --oneline origin/main..HEAD` must
  list only this task's commits. If `git merge-base --is-ancestor HEAD~1 origin/main`
  succeeds, the branch is already merged: cherry-pick onto a fresh branch off
  `origin/main` instead of pushing more onto a dead one.
- **Leave the checkout on `main` when a task ends.** The human runs the app straight
  from this tree, so a session that stops on a feature branch means their next
  `uv run lfm` is missing everything that merged since — which looks exactly like a
  bug that was already fixed.
- Write multi-line commit messages to a file and use `git commit -F`: a backtick in
  prose is command-substituted by the shell first, and the word vanishes silently
  while the commit is still created.
- A merged PR is not a closed issue, and the PR's own `head` may name a commit the
  base never saw. Settle it against the tree (`git merge-base --is-ancestor <sha>
  origin/main`, or grep `origin/main` for the symbol the commit added) rather than
  the metadata.

## API and models

- Last.fm response quirks are absorbed in `models.py`; nothing downstream should
  defensive-parse. Ranking logic goes in `rank.py` as pure functions over the models.
- Retry only what a retry can fix: `RateLimited` (error 29), `ServerError` (5xx) and
  transport errors go through stamina; `ArtistNotFound` and other API errors fail
  immediately. Tests call `stamina.set_testing` so attempts stay real but the waits
  do not.
- Error 6 is documented as "invalid parameters" but real responses reuse it for an
  unknown artist; 11 and 16 are documented "try again" codes worth retrying.
- `artist.search` is a relevance search, not a prefix index: it echoes the query as a
  junk artist and often buries the act you mean. The deciding column is listener
  count, so a suggestion rule phrased over anything else hands the same report back.
- A fixture-pinned test keeps passing after the service stops sending a field. When a
  feature looks dead but its test passes, dump the *raw* response and compare its keys
  against the fields the models read — the stale fixture is the defect.
- A key comes from `LFM_API_KEY`, falling back to `api_key` in
  `~/.config/lfm/config.toml`. The write path adds `LFM_SECRET`/`LFM_SESSION_KEY`, or
  `secret`/`session_key` in the file. Never commit a credential, and never print one:
  `lfm config` reports where a setting came from, not its value.
- The write endpoints honour `format=json` (verified live: JSON body, HTTP 400 with a
  bogus session key); pylast uses XML exclusively. Last.fm checks the signature before
  it checks required parameters, so a wrong-secret probe proves the request reaches
  the service and nothing more — completeness rests on a test that re-signs exactly
  what went on the wire.

## Playback

- `player.py` owns a single mpv process (`--input-ipc-server`) and must stop it on
  quit/unmount; playing a track stops the previous one. Keep it free of Textual
  imports so it stays unit-testable.
- Never report "Playing" just because mpv started: it exposes metadata *before* it
  opens the audio device, so wait for `time-pos` to advance and report the last
  stderr line when it exits early. `--really-quiet` must not come back — it hides
  exactly the errors playback needs to surface.
- The end of a track comes from mpv (`eof-reached`), never from the process exiting:
  plenty of mpv configs set `keep-open`, which leaves mpv alive and paused at the
  end. Only a track that played through may hand over to the next one —
  `PlayerStatus.finished` is true for a clean end and false for a failure or a
  deliberate stop.
- The child mpv must never outlive lfm: `stop()` on quit plus `PR_SET_PDEATHSIG`
  (`spawn_kwargs()`, Linux-only) cover graceful and SIGKILLed exits. When testing
  this, SIGKILL the *python* process, not a shell wrapper — killing the wrapper
  leaves python (mpv's real parent) alive and the check lies.
- mpv is restarted for every track, so anything that must survive (volume) lives on
  `MpvPlayer` and is reapplied at spawn (`--volume=`).
- mpv is spawned with `--no-config`, as its manual asks when it is embedded ("to
  avoid conflicts with the normal mpv user configuration intended for CLI
  playback"). The cost is that a user's audio-output settings do not apply.
- mpv's `--ytdl-format` defaults to `bestvideo+bestaudio/best`, so a picked watch URL
  needs `--ytdl-format=bestaudio/best` or it fetches video we never show; the
  documented spelling is `--vid=no`; `--terminal=no` keeps mpv off our stdin; `seek` is relative by default;
  `--msg-level=all=warn` is the level that still shows the HTTP 403 (at `all=error`
  it vanishes, leaving only "mpv exited with status 2"). The level name is `warn`,
  not `warning`: the wrong spelling makes mpv exit 1 before the socket exists.
- A stream that fails to load is retried (`PLAY_ATTEMPTS`): YouTube intermittently
  answers HTTP 403 for a googlevideo URL it just handed out, so a fresh resolve
  usually fixes it. Verify player changes against **real mpv**, not only the fake:
  the fake cannot catch a bad CLI flag, and the 403 only appears against real
  YouTube. `--length=3` on the real mpv is the quick way to watch a track end and
hand over.
- Everything a play waits for is seconds long — a search, a prefetch, mpv coming up
  — so each runs as a task `_claim` can cancel, and the abandoned call unwinds
  through `_Superseded` and reports `superseded` rather than playing on. Cleanup
  after a cancellation goes *off* the critical path: awaiting a teardown while
  holding `_play_lock` is the wait the next track is stuck behind.
- Bound every yt-dlp call with a timeout, and kill it by **process group**
  (`start_new_session=True` plus `os.killpg`): `Process.wait()` waits for the pipes
  as well as the process, so a grandchild holding our stdout keeps it blocked long
  after the killed process is gone.
- yt-dlp's `--print` already implies `--quiet` and `--simulate`, so `--skip-download`
  is redundant; print one **tab-separated line** per result so a title containing a
  newline cannot look like the URL. `ytsearchN:` uses `--flat-playlist`: without it
  yt-dlp extracts every result just to fill the template (7.8s for five hits against
  1.2s).
- When a resolved match is wrong the user fixes it with the picker (`y`), stored per
  artist/track rather than guessed at. Keep the resolve fallback (artist+track, then
  the track alone) as the cheap path; the picker is the escape hatch, not the default.
- The visualiser is deliberately **not** part of `player.py`: mpv exposes no audio
  levels over IPC (`af-metadata` is null), so a spectrum would mean a second decode
  of the stream — an extra download per song that also drifts on seek. cava taps the
  audio monitor instead, which is free and in sync. cava reads `output:channels`, not
  `general:channels`; its config is parsed with iniparser, so notes cannot live inside
  a section.
- Scrobbling: the rule comes from the Scrobbling 2.0 spec (>30s **and** half the track
  or 4 minutes, whichever first), and a play is sent as soon as it is satisfied — read
  off the player's position, not hooked to the track ending, so a play survives a skip,
  a stop or a quit. It is off until `scrobble`, `secret` and `session_key` are all set.
  Undo is the outbox (`~/.cache/lfm/outbox.json`, `--dry-run`/`--drop`/`--off`) plus the
  ledger (`~/.cache/lfm/scrobbled.jsonl`) — the API has **no** unscrobble method, so the
  ledger is the list to delete from by hand. A transport error is never retried: the
  request may have been accepted, and a duplicate play is worse than a missing one.
  A source picked by title is not scrobbled, because a YouTube title is not metadata.

## The TUI

Textual is fast-moving: check the installed version's API (`.venv/...`) before using
a widget or message this repo has not used yet.

- A `ListView` mounted empty and filled later has no highlighted row, so Enter does
  nothing until its `.index` is set. `Screen` already binds Tab to `focus_next`, so a
  focusable list gets Tab navigation for free; set `can_focus = False` on the scroll
  container and on mouse-only buttons, or Tab stops on them.
- `ListView.Highlighted` carries no index; read `event.list_view.index`. A list that
  is not focused should not look selected — that is what
  `ListView:blur > ListItem.-highlight { background: transparent; }` is for.
- A row hidden with `display = False` is still in `ListView._nodes`, so `index` and
  `Selected.index` keep pointing at the same entries, but the arrow walk only skips
  *disabled* rows and will land on a hidden one. Two widgets with `dock: bottom` land
  on the same row.
- `run_worker(..., exclusive=True)` cancels the *other* workers in its group. The
  similar screen's hover explanation was cancelling the deep-cut lookups started from
  the same screen, which looked like a stuck "finding deep cuts…". Different kinds of
  background work get their own `group=`. `run_worker` with no group uses the default
  group, so it cancels other default-group workers on that screen.
- A list inside a scrollable page needs care: with `height: auto` it grows the page
  instead of scrolling, and Textual's "scroll to the selected row" stops at the list,
  so the highlight walks off the bottom. The artist page's columns are **ten rows
  each**, so the list scrolls itself; on a terminal too short for those ten rows the
  page is clipped, so the page also nudges. When a test asserts a row is visible,
  check it against *both* regions.
- Content must never sit on the chrome: check the gap by rendering and by asserting
  it between the two regions rather than trusting the CSS.
- The player bar has two halves: `#now-playing` (the title, ellipsised) and
  `#bar-info` (queue position, `↻`, volume) hard against the right edge. Anything
  *about* the track goes on the first line, anything that is a *control* on the second.
- The key sheet (`h`/`F1`, `ShortcutsScreen`) is **generated from `BINDINGS`**, so it
  cannot drift; `SHORTCUT_NOTES` carries the few things that are not bindings (the
  prompt, `Tab`, `Del`, `↑ ↓`). There is **no footer**: the sheet is the one place for
  hints. One cheat-sheet row per key, in two columns balanced by height (`shortcut_columns`) — a paragraph
  of `key description · key description` wrapped mid-pair and pushed the sheet below
  the fold.
- Playback state that must survive across tracks lives on the artist page: the queue
  index drives `n`/`p`, the auto-advance behaviour (or `_repeat`, which loops the same
  track) and the `3/10` label, and `action_stop` clears the index so a deliberate stop
  is not mistaken for a track ending. The app and its screens take one
  `ui.options.TuiOptions` rather than a handful of keywords each, and the app is the
  only place that unpacks it — a screen has no business knowing which flag set it.
  Scrobbling state lives on the *app*, because the
  player outlives any one screen. Anything optional the app owns (visualiser, volume
  saver) is reached with `getattr` + a callable check, so screens stay decoupled from
  the CLI.
- A plain `str` given to rich's `Console.print` or any Textual widget is **markup**,
  and unknown tags are dropped in silence: a YouTube title loses its "[Official
  Video]", a bio loses "[citation needed]", and a stray "[/]" makes rich raise
  `MarkupError`, taking `lfm info` down. Anything carrying an artist, track, tag, title
  or bio goes through `Text` (`ui.widgets.plain`) or `rich.markup.escape`, or
  `notify(..., markup=False)`.
- Textual drops whitespace at the **start** of a styled run: `append("q", "bold")`
  then `append(" Quit", "dim")` renders "qQuit". Put the space (and any ` · `
  separator) at the *end* of the run before it.
- A scrollable panel sized `height: auto` needs a **definite width**, or its content
  cannot work out how tall it is and it collapses to a few cells.
- Styling gotchas hit here: list rows are highlighted via the `ListItem.-highlight`
  class (single dash); `Button` is three rows tall by default, so a compact bar needs
  `border: none; height: 1` and no `padding` that eats its only row; scrollbar colours
  come from the `$scrollbar*` theme variables, not from `scrollbar-color`. Textual's vertical scrollbar default is **two cells** and
  `scrollbar-size-vertical` is **not inherited**, so lfm sets it with
  `* { scrollbar-size-vertical: 1 }`.
- A fixed-width button cannot centre labels of different lengths: the labels must be
  the same length *and* the box agree in parity with them (`Start`/`Pause`, five cells,
  in a nine-cell button). The test counts the blanks rather than trusting
  `content-align`.
- Numbers are formatted for their audience: the TUI passes `render.compact_count`
  to `stats_line` (exact under a million, `13.9M` above), and the CLI keeps `exact_count` because
  `plain`/`json` output is for scripts. Empty sections are `display = False`, not blank
  headings.
- Textual does not set the terminal window title; write OSC 2 through
  `app.driver.write` and reset it on unmount. `c` copies a link with
  `App.copy_to_clipboard` (OSC 52) and is deliberately silent: a terminal that refuses
  the sequence just does nothing, and success popups are not this app's style.
- `Screen.name` is a read-only Textual property — do not use `name` as a screen
  attribute.

## CLI, config and output

- Settings resolve as CLI flag > environment > `config.toml` > default, using `None`
  defaults on the Typer options so "not passed" is distinguishable from "passed the
  default".
- A flag that applies to the whole run goes on the context: the root callback puts a
  `RunOptions` on `ctx.obj` and `_client` reads it from there. Do not reach for
  module-level state — it survives between invocations and hides an input from the
  function that uses it.
- `config.save_value` rewrites one key through a temp file and a rename, so a crash
  can never truncate the config. Set the mode on the temp file *before* the rename
  lands, or it carries the umask default and replaces a mode the owner chose: a new
  file starts 0600, an existing one keeps its mode, and `lfm config` reports the mode
  rather than policing it.
- `lfm --version` reads `pyproject.toml` from the working tree when it is there and
  falls back to distribution metadata otherwise. Editable installs keep the version
  they were installed with, so pyproject.toml stays the single place it is written
  (no `__version__`; uv_build does not support `dynamic = ["version"]`).

## Tests

`tests/` drives the real app through Textual's pilot while staying offline: `respx`
mocks the Last.fm HTTP calls and fake `mpv`/`yt-dlp` shell scripts stand in for
playback. Nothing may touch the network — the suite has to pass behind an unroutable
proxy. The seams are `api.protocols.DetailClient` (the read surface the screens
use) and `ui.protocols.Player` (playback), injected as fakes; the flows take the
narrower `api.protocols.Reader`. CLI tests isolate `XDG_CONFIG_HOME`/`XDG_CACHE_HOME` so they never touch the
real config or cache, and `test_player.py` exercises the real IPC code against a fake
mpv script.

Patch where a name is *looked up*: with the layers split, `set_terminal_title` is
called from both the artist page and the app, so a test that patches one of them sees
only that module's calls. The same rule catches a method that has moved module.

- **Behaviour** — `async with app.run_test(size=(w, h)) as pilot:`, then
  `pilot.press(...)`, `pilot.pause()`, `app.workers.wait_for_complete()`, and assert
  on widget state (`query_one`, `ListView.index`, label text, `region`) rather than
  on pixels.
- **Layout** — `tests/test_snapshots.py` compares whole screens against committed SVGs
  through `snap_compare(app, press=[...], terminal_size=(w, h), run_before=...)`. A
  deliberate change is accepted with `uv run pytest tests/test_snapshots.py
  --snapshot-update`, and the resulting diff is read before committing — the files are
  Rich's SVG, so `<text>` runs and fill colours are greppable without a picture.
- **Screenshots** — `app.save_screenshot("x.svg")` inside `run_test`, then
  `rsvg-convert -w 1000 x.svg -o x.png` (`librsvg2-bin`) for a human or a model to look
  at. Look at the PNG before claiming a layout is fine: half the styling bugs only show
  up that way.
- **Live API** — `tests/test_live.py` runs only when `LFM_LIVE=1` and a key is
  configured, so the default suite stays offline and CI needs no secret. One opt-in
  test, not a suite: what it catches is Last.fm changing shape, which respx fixtures
  cannot.

Standing rules, whatever the size of the change: validation at trust boundaries and
error handling that prevents data loss are never traded for brevity, and neither are
security or accessibility. Non-trivial logic — a branch, a loop, a parser, anything
touching money or security — leaves one small runnable check behind; trivial
one-liners need none. Comments say *why*, and only where the reason is not in the
code: a trap in someone else's API, a thing verified against the real mpv or the real
Last.fm, a deliberate absence. A comment that narrates how the bug that prompted it
looked belongs in the issue, not in the file.
