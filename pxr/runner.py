"""Create, save, inspect, and remove ParallaxEngine Cloud Agent runs."""

import hashlib
import json
import secrets
import shlex
import sys
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

import httpx
from runta import ApiError, RuntaError

from . import config
from .cloud import Cloud, Interrupted, pause, retry, shell
from .config import slug
from .runtime.worker import digest

PYTHON = '/usr/bin/python3'
WORKER = '/opt/pxr/worker.py'
# Uploaded fresh on every run, so local edits apply without rebuilding the base.
# Only setup.sh is baked into the checkpoint.
TOOLS = ('worker.py', 'capture.mjs')
SYSTEM_PROMPT = (
    'Create ParallaxEngine games in /workspace/game. Read and follow '
    '/opt/parallax/skills/parallax-game/SKILL.md. pxr runs the project checks '
    'after you finish, so focus on implementing the requested game.'
)
STOP = threading.Event()
# Baseline and maximum memory for the base setup runtime, recorded on its checkpoint so every game runtime gets it.
MEMORY_MIB = 4096
EDITOR_PORT = 5180


def stamp():
    return datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ') + '-' + secrets.token_hex(3)


def save(path, value):
    temporary = path.with_name(f'{path.name}.{secrets.token_hex(3)}.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def asset(name):
    return files('pxr').joinpath('runtime', name).read_text()


def sha256(text):
    return hashlib.sha256(text.encode()).hexdigest()


def upload_tools(runtime):
    for name in TOOLS:
        runtime.files.write('/opt/pxr/' + name, asset(name))


def say(meta, message):
    print(f'{meta["id"]}: {message}', flush=True)


def stopped():
    if STOP.is_set():
        raise Interrupted('interrupted by Ctrl-C; runtime retained')


def load_base(out):
    return json.loads((out / 'base.json').read_text()) if (out / 'base.json').exists() else {}


def provider_by_id(cloud, provider_id):
    providers = cloud.providers()
    for provider in providers:
        if provider['id'] == provider_id:
            return provider
    known = ', '.join(f'{item.get("display_name", item["id"])} ({item["id"]})' for item in providers)
    raise ValueError(f'Runta managed provider {provider_id!r} not found. Available: {known or "none"}')


def watch_setup(runtime, engine_ref):
    """Runs setup.sh, printing its latest log line every 20 seconds."""
    started = time.monotonic()
    future = runtime.exec_detached(
        ['bash', '-c', 'set -o pipefail; bash /opt/pxr/setup.sh 2>&1 | tee /opt/pxr/setup.log'],
        env={'ENGINE_REF': engine_ref},
        timeout=3600,
        max_output_bytes=200_000,
    )
    last = None
    while not wait([future], timeout=20).done:
        try:
            line = shell(runtime, 'tail -n 1 /opt/pxr/setup.log 2>/dev/null', timeout=30).strip()
        except (RuntimeError, RuntaError):
            continue  # Only progress output; the setup command itself is still being watched.
        if line and line != last:
            minutes, seconds = divmod(int(time.monotonic() - started), 60)
            print(f'  [{minutes}m{seconds:02d}s] {line[:150]}', flush=True)
            last = line
    return future.result()


def setup(args):
    provider = args.provider
    if not provider:
        raise ValueError('pxr base requires --provider with a Runta managed-provider ID')
    args.out.mkdir(parents=True, exist_ok=True)
    state = args.out / ('base-' + stamp() + '.json')
    with Cloud() as cloud:
        selected = provider_by_id(cloud, provider)
        agent = cloud.create_agent(provider, 'ParallaxEngine base setup')
        info = {
            'agent_id': agent['id'],
            'runtime_id': agent['runtime_id'],
            'engine_ref': args.engine_ref,
            'provider_id': provider,
            'model': selected.get('default_model'),
        }
        save(state, info)
        print(f'Preparing Cloud Agent base {agent["id"]}', flush=True)
        try:
            runtime = cloud.sdk.runtimes.get(agent['runtime_id'])
            print('Waiting for the runtime to start', flush=True)
            runtime.wait_until_ready(timeout=300)
            print(f'Restarting it with {MEMORY_MIB} MiB of memory', flush=True)
            cloud.set_memory(runtime, MEMORY_MIB)
            runtime.resume(timeout=300)
            shell(runtime, 'mkdir -p /opt/pxr /workspace')
            script = asset('setup.sh')
            runtime.files.write('/opt/pxr/setup.sh', script)
            upload_tools(runtime)
            print('Installing ParallaxEngine (several minutes)', flush=True)
            setup_result = watch_setup(runtime, args.engine_ref)
            state.with_suffix('.log').write_text(setup_result.stdout_text + setup_result.stderr_text)
            if setup_result.exit_code:
                raise RuntimeError(
                    f'base setup failed ({setup_result.exit_code}); see {state.with_suffix(".log")}'
                )
            info['versions'] = json.loads(runtime.files.read('/opt/pxr/versions.json'))
            info['setup_sha256'] = sha256(script)
            print('Saving the checkpoint (can take a while)', flush=True)
            checkpoint = cloud.checkpoint(runtime, 'parallax-base-' + stamp(), MEMORY_MIB)
            info['memory_mib'] = MEMORY_MIB
            info['checkpoint'] = checkpoint.id
            save(state, info)
            save(args.out / 'base.json', info)
            cloud.delete_agent(agent['id'])
            print(f'Checkpoint {checkpoint.id} ready; engine {info["versions"]["engine"]}')
        except Exception:
            print(f'Base Cloud Agent retained for inspection: {agent["id"]}', file=sys.stderr)
            raise


def records(out, selection=None):
    if selection:
        path = Path(selection)
        paths = [path / 'meta.json'] if path.is_dir() else list(out.glob(f'batches/*/{selection}/meta.json'))
        if not paths or any(not p.is_file() for p in paths):
            raise ValueError(f'run not found: {selection}')
    else:
        paths = sorted(out.glob('batches/*/*/meta.json'))
    return [(path, json.loads(path.read_text())) for path in paths]


def backup_ok(path, meta, expected=None):
    backup = meta.get('backup')
    if not backup or (expected and backup['digest'] != expected):
        return False
    game = path.parent / backup['path']
    try:
        return (game / 'game/main.py').is_file() and digest(game) == backup['digest']
    except (OSError, ValueError):
        return False


def copy_game(runtime, path, meta):
    """Returns (game folder, whether a new copy was downloaded)."""
    for attempt in range(3):
        try:
            snapshot = json.loads(shell(runtime, f'{PYTHON} {WORKER} pack', timeout=180))
            break
        except RuntimeError:
            # An agent that could not be cancelled may change the game mid-pack; try again.
            if attempt == 2:
                raise
            time.sleep(10)
    if backup_ok(path, meta, snapshot['digest']):
        return path.parent / meta['backup']['path'], False
    dest = path.parent / 'copies' / stamp()
    dest.mkdir(parents=True)
    archive = dest / 'game.tar.gz'
    runtime.files.download('/workspace/game.tar.gz', str(archive))
    with tarfile.open(archive) as bundle:
        bundle.extractall(dest, filter='data')
    game = dest / 'game'
    if not (game / 'game/main.py').is_file() or digest(game) != snapshot['digest']:
        raise RuntimeError('downloaded game does not match the remote snapshot')
    meta['backup'] = {
        'path': game.relative_to(path.parent).as_posix(),
        'archive': archive.relative_to(path.parent).as_posix(),
        'digest': snapshot['digest'],
    }
    save(path, meta)
    return game, True


def command(runtime, args, timeout, output):
    result = runtime.exec(args, timeout=timeout, max_output_bytes=1_000_000)
    output.write_text(result.stdout_text + result.stderr_text)
    return result.exit_code


def run_checks(runtime, dest, meta):
    result = meta.get('result', {})
    for label, argv, timeout in (
        ('check', ['parallax', 'check', '/workspace/game', '--json'], 300),
        ('test', ['parallax', 'test', '/workspace/game', '--json'], 600),
    ):
        stopped()
        try:
            result[label + '_exit'] = command(runtime, argv, timeout, dest / f'{label}.log')
        except Exception as error:
            result[label + '_exit'] = -1
            result[label + '_error'] = str(error)
    meta['result'] = result


def start_editor(cloud, runtime, meta):
    """Starts the editor as its own service, so it outlives this command, then opens its port."""
    unit = 'pxr-editor-' + secrets.token_hex(4)
    editor = [
        'parallax',
        'edit',
        '/workspace/game',
        '--no-open',
        '--host',
        '0.0.0.0',
        '--port',
        str(EDITOR_PORT),
    ]
    shell(
        runtime, shlex.join(['systemd-run', '--quiet', f'--unit={unit}', '-p', 'OOMPolicy=continue', *editor])
    )
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            shell(runtime, f'curl -fsS http://127.0.0.1:{EDITOR_PORT}/ >/dev/null')
            break
        except (RuntaError, RuntimeError, TimeoutError):
            pause(STOP, 1)
    else:
        raise TimeoutError('editor did not become ready')
    meta['result']['editor_ready'] = True
    cloud.set_ingress(runtime.id, EDITOR_PORT)
    meta['url'] = f'https://{EDITOR_PORT}-{runtime.id}.runta.dev/'


def take_screenshot(runtime, dest, meta):
    capture = runtime.exec(
        ['node', '/opt/pxr/capture.mjs', f'http://127.0.0.1:{EDITOR_PORT}/', '/workspace/screenshot.png'],
        timeout=150,
        max_output_bytes=20_000,
    )
    if capture.exit_code == 0:
        runtime.files.download('/workspace/screenshot.png', str(dest / 'screenshot.png'))
    meta['result']['capture_ok'] = capture.exit_code == 0
    if capture.exit_code:
        meta['result']['capture_error'] = capture.stderr_text[-2000:]


def prepare_game(cloud, runtime, dest, meta):
    """Checks and tests the game, then serves it in the editor and screenshots it."""
    say(meta, 'running parallax check and test')
    run_checks(runtime, dest, meta)
    result = meta['result']
    outcome = {label: 'ok' if result.get(label + '_exit') == 0 else 'failed' for label in ('check', 'test')}
    say(meta, f'check {outcome["check"]}, test {outcome["test"]}; starting the editor')
    try:
        start_editor(cloud, runtime, meta)
        stopped()
        say(meta, 'taking a screenshot')
        take_screenshot(runtime, dest, meta)
    except Interrupted:
        raise
    except Exception as error:
        meta['result']['editor_error'] = str(error)


def agent_error(cloud, agent, run, timeout, dest):
    """The error to record for the agent's run. A run that timed out is cancelled first."""
    if run['status'] != 'timed_out':
        return run.get('error') if run['status'] != 'finished' else None
    if run['id']:
        try:
            cloud.cancel_run(agent['id'], run['id'])
        except (httpx.HTTPError, TimeoutError) as error:
            return (
                f'agent exceeded --timeout {timeout}s and could not be cancelled ({error}); '
                f'it may still be running. Use pxr pull {dest} for later changes.'
            )
    return (
        f'agent exceeded --timeout {timeout}s and was cancelled; the game so far '
        'was checked and saved, and the runtime is kept.'
    )


def final_status(run, meta):
    if run['status'] == 'timed_out':
        return 'timed_out'
    result = meta.get('result', {})
    passed = (
        run['status'] == 'finished'
        and result.get('check_exit') == 0
        and result.get('test_exit') == 0
        and meta.get('backup')
        and meta.get('url')
    )
    return 'passed' if passed else 'failed'


def run_one(folder, prompt_id, prompt, name, profile, sample, checkpoint, provider, args):
    ident = f'{slug(prompt_id)}-{slug(name)}-{sample}-{secrets.token_hex(3)}'
    dest = folder / ident
    dest.mkdir()
    path = dest / 'meta.json'
    instruction = f'{prompt}\n\nCreate the complete game at /workspace/game. Work independently.'
    meta = {
        'id': ident,
        'prompt_id': prompt_id,
        'prompt': prompt,
        'profile': name,
        'sample': sample,
        'checkpoint': checkpoint,
        'provider_id': provider['id'],
        'model_used': provider.get('default_model'),
        'started': time.time(),
        'status': 'starting',
        'runtime_id': None,
        'agent_id': None,
        'backup': None,
    }
    save(path, meta)
    with Cloud() as cloud:
        try:
            stopped()
            agent = cloud.create_agent(provider['id'], f'pxr {ident}', instruction, checkpoint, SYSTEM_PROMPT)
            meta['agent_id'], meta['runtime_id'], meta['status'] = agent['id'], agent['runtime_id'], 'running'
            save(path, meta)
            say(meta, f'agent {agent["id"]} started')
        except Exception as error:
            meta['status'] = 'interrupted' if isinstance(error, Interrupted) else 'failed'
            meta['error'], meta['finished'] = str(error), time.time()
            save(path, meta)
            return meta
        return finish_run(cloud, path, meta, args.timeout)


def finish_run(cloud, path, meta, timeout):
    """Waits for the run's agent, then checks, tests, screenshots and saves its game."""
    dest = path.parent
    agent = {'id': meta['agent_id'], 'runtime_id': meta['runtime_id']}
    try:
        run = cloud.wait_for_run(agent['id'], timeout, STOP)
        minutes = int(time.time() - meta['started']) // 60
        say(meta, f'agent {run["status"]} after {minutes}m')
        meta['agent_run_id'] = run['id']
        meta['agent_result'] = run.get('result')
        meta['error'] = agent_error(cloud, agent, run, timeout, dest)
        save(path, meta)
        runtime = retry(lambda: cloud.sdk.runtimes.get(agent['runtime_id']))
        retry(lambda: runtime.wait_until_ready(timeout=300))
        retry(lambda: upload_tools(runtime))
        meta['versions'] = json.loads(retry(lambda: runtime.files.read('/opt/pxr/versions.json')))
        prepare_game(cloud, runtime, dest, meta)
        say(meta, 'saving the game source')
        try:
            copy_game(runtime, path, meta)
        except (RuntaError, OSError, RuntimeError, ValueError, tarfile.TarError) as error:
            meta['backup_error'] = str(error)
        meta['status'] = final_status(run, meta)
    except Interrupted as error:
        # Keep the agent's own error if it already reported one.
        meta['status'], meta['error'] = 'interrupted', meta.get('error') or str(error)
        if meta['agent_id']:
            try:
                cloud.cancel_active(meta['agent_id'])
            except httpx.HTTPError as cancel_error:
                meta['cancel_error'] = str(cancel_error)
    except Exception as error:
        meta['status'], meta['error'] = 'failed', str(error)
        if 'agent_run_id' not in meta:
            # pxr stopped waiting, not the agent: it may still be building on Runta.
            meta['error'] += (
                f'\nThe agent may still be running on Runta; '
                f'`pxr attach {meta["id"]}` waits for it again and saves its game.'
            )
    finally:
        meta['finished'] = time.time()
        save(path, meta)
    return meta


def interrupted(folder):
    """After Ctrl-C: mark runs still in flight and list the agents that were started."""
    for path in sorted(folder.glob('*/meta.json')):
        meta = json.loads(path.read_text())
        if meta['status'] in ('starting', 'running'):
            meta['status'] = 'interrupted'
            meta['error'] = meta.get('error') or 'interrupted by Ctrl-C; runtime retained'
            save(path, meta)
        if meta['status'] == 'interrupted' and meta.get('agent_id'):
            print(
                f'{meta["id"]}: interrupted; asked Runta to stop agent {meta["agent_id"]}, runtime kept',
                file=sys.stderr,
            )


def generate(args, profiles, prompts):
    base = load_base(args.out)
    if base:
        engine = base.get('versions', {}).get('engine', 'unknown')[:12]
        print(f'Base checkpoint {base.get("checkpoint")} has engine {engine}', flush=True)
        if base.get('setup_sha256') != sha256(asset('setup.sh')):
            print(
                'WARNING: the base was built with a different (or unrecorded) runtime/setup.sh; '
                'rerun `pxr base` to apply it.',
                file=sys.stderr,
            )
    jobs = [
        (pid, text, name, profile, sample)
        for pid, text in prompts.items()
        for name, profile in profiles.items()
        for sample in range(1, args.samples + 1)
    ]
    if args.dry_run:
        for pid, _, name, profile, sample in jobs:
            chosen = profile.checkpoint or args.checkpoint or base.get('checkpoint', '(run pxr base first)')
            print(f'{pid} / {name} / sample {sample} / checkpoint {chosen}')
        return []
    with Cloud() as cloud:
        providers = {name: provider_by_id(cloud, profile.provider) for name, profile in profiles.items()}
        checkpoints = {}
        for name, profile in profiles.items():
            chosen = profile.checkpoint or args.checkpoint or base.get('checkpoint')
            if not chosen:
                raise ValueError('run `pxr base --provider ID` first, or set a profile checkpoint')
            checkpoints[name] = cloud.sdk.checkpoints.get(chosen).id
    folder = args.out / 'batches' / stamp()
    folder.mkdir(parents=True)
    print(f'{len(jobs)} runs, at most {args.parallel} in parallel → {folder}', flush=True)
    results = []
    pool = ThreadPoolExecutor(max_workers=args.parallel)
    futures = [
        pool.submit(
            run_one, folder, pid, text, name, profile, sample, checkpoints[name], providers[name], args
        )
        for pid, text, name, profile, sample in jobs
    ]
    try:
        for future in as_completed(futures):
            meta = future.result()
            results.append(meta)
            outcome = meta.get('url') or meta.get('error', 'see run metadata')
            print(f'{meta["id"]}: {meta["status"]} → {outcome}', flush=True)
    except KeyboardInterrupt:
        STOP.set()
        pool.shutdown(wait=False, cancel_futures=True)
        # Futures cancelled by shutdown never count as done for wait(), so leave them out.
        wait([future for future in futures if not future.cancelled()], timeout=15)
        interrupted(folder)
        raise
    pool.shutdown()
    return results


def attach(args):
    """Waits again for agents pxr lost track of, then checks, tests and saves their games."""
    lost = [
        (path, meta)
        for path, meta in records(args.out, args.run)
        if meta.get('agent_id') and 'agent_run_id' not in meta and not meta.get('deleted')
    ]
    if not lost:
        print('No runs are waiting on an agent; every recorded agent has already finished.')
        return 0
    failed = 0
    with Cloud() as cloud:
        for path, meta in lost:
            say(meta, f'reattaching to agent {meta["agent_id"]}')
            # Keep the original time limit, but always look once so an overdue run gets cancelled.
            remaining = max(args.timeout - (time.time() - meta['started']), 60)
            meta = finish_run(cloud, path, meta, remaining)
            outcome = meta.get('url') or meta.get('error', 'see run metadata')
            print(f'{meta["id"]}: {meta["status"]} → {outcome}', flush=True)
            failed += meta['status'] != 'passed'
    return failed


def pull(args):
    failed = 0
    with Cloud() as cloud:
        for path, meta in records(args.out, args.run):
            if not meta.get('runtime_id') or meta.get('deleted'):
                continue
            try:
                runtime = cloud.sdk.runtimes.get(meta['runtime_id'])
                upload_tools(runtime)
                game, fresh = copy_game(runtime, path, meta)
                print(game if fresh else f'{game} (already up to date)')
            except Exception as error:
                print(f'{meta["id"]}: {error}', file=sys.stderr)
                failed += 1
    return failed


def backup_problem(path, meta, runtime):
    if not meta.get('backup'):
        return 'the generated game has not been copied down'
    if not backup_ok(path, meta):
        return 'the local backup is missing or has changed'
    try:
        upload_tools(runtime)
        remote = shell(runtime, f'{PYTHON} {WORKER} digest', timeout=180).strip()
    except (RuntaError, RuntimeError, TimeoutError):
        return 'the remote game could not be compared with its local backup'
    if remote != meta['backup']['digest']:
        return 'the cloud game has changed since it was copied down'
    return None


def confirm(question, yes):
    return yes or (sys.stdin.isatty() and input(f'{question} [y/N] ').lower() == 'y')


def find_runtime(cloud, runtime_id):
    """The runtime, or None if Runta has already deleted it."""
    try:
        return cloud.sdk.runtimes.get(runtime_id)
    except ApiError as error:
        if error.status_code != 404:
            raise
        return None


def delete_run(cloud, path, meta, yes):
    """Deletes the run's agent and runtime. Returns False if the user chose to keep it."""
    runtime = find_runtime(cloud, meta['runtime_id'])
    problem = runtime and backup_problem(path, meta, runtime)
    if problem:
        print(f'WARNING: {meta["id"]}: {problem}. Use pxr pull {path.parent} to save it.', file=sys.stderr)
        if not confirm('Delete this runtime anyway?', yes):
            return False
    try:
        cloud.delete_agent(meta['agent_id'])
    except httpx.HTTPStatusError as error:
        # An agent whose runtime is already gone may be gone too.
        if runtime or error.response.status_code != 404:
            raise
    meta['deleted'] = True
    save(path, meta)
    if runtime:
        print(f'Deletion requested for {meta["runtime_id"]}; local files retained')
    else:
        print(f'{meta["id"]}: runtime was already gone; marked deleted')
    return True


def down(args):
    skipped = 0
    with Cloud() as cloud:
        for path, meta in records(args.out, args.run):
            if not meta.get('agent_id') or meta.get('deleted'):
                continue
            try:
                if not delete_run(cloud, path, meta, args.yes):
                    skipped += 1
            except Exception as error:
                print(f'{meta["id"]}: {error}', file=sys.stderr)
                skipped += 1
    return skipped


def prune_bases(args):
    """Deletes pxr base checkpoints except the current base, profile checkpoints, and ones live runs use."""
    keep = {load_base(args.out).get('checkpoint')} | config.checkpoints(None)
    keep |= {meta.get('checkpoint') for _, meta in records(args.out) if not meta.get('deleted')}
    with Cloud() as cloud:
        old = [
            item
            for item in cloud.sdk.checkpoints.list()
            if item.display_name.startswith('parallax-base-') and item.id not in keep
        ]
        if not old:
            print('No old base checkpoints')
            return 0
        for item in old:
            print(f'{item.id}\t{item.display_name}')
        if not confirm(f'Delete these {len(old)} checkpoints?', args.yes):
            print('Nothing deleted', file=sys.stderr)
            return len(old)
        failed = 0
        for item in old:
            try:
                cloud.sdk.checkpoints.delete(item.id)
                print(f'Deleted {item.id}')
            except RuntaError as error:
                print(f'{item.id}: {error}', file=sys.stderr)
                failed += 1
        return failed
