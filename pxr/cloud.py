"""Runta Cloud Agents and the runtime operations they do not expose."""

import os
import time
from urllib.parse import quote

import httpx
from runta import ApiError, Runta

# Runta hiccups worth retrying rather than failing a run over.
TRANSIENT = (502, 503, 504)


class Interrupted(Exception):
    """Ctrl-C was pressed; the run stops waiting and keeps its runtime."""


def pause(stop, seconds):
    if stop.wait(seconds):
        raise Interrupted('interrupted by Ctrl-C; runtime retained')


def retry(call, attempts=5):
    """Runs an idempotent Runta SDK call, retrying 502/503/504s and network timeouts."""
    for attempt in range(attempts):
        try:
            return call()
        except (ApiError, httpx.TransportError) as error:
            if attempt == attempts - 1 or (
                isinstance(error, ApiError) and error.status_code not in TRANSIENT
            ):
                raise
            time.sleep(5)


def api(*parts):
    """An API path with each ID quoted, e.g. api('agents', agent_id, 'runs')."""
    return '/v2/' + '/'.join(quote(part, safe='') for part in parts)


class Cloud:
    def __init__(self):
        token = os.getenv('RUNTA_TOKEN')
        endpoint = os.getenv('RUNTA_API_URL', 'https://api.runta.com')
        if not token:
            raise ValueError('set RUNTA_TOKEN to a Runta API key')
        self.sdk = Runta(token=token, endpoint=endpoint)
        self.http = httpx.Client(
            base_url=endpoint, headers={'Authorization': f'Bearer {token}'}, timeout=30, trust_env=False
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.sdk.close()
        self.http.close()

    def request(self, method, path, **kwargs):
        # Reads are safe to repeat, so ride out brief Runta outages; writes fail fast.
        for attempt in range(5 if method == 'GET' else 1):
            try:
                response = self.http.request(method, path, **kwargs)
            except httpx.TransportError:
                if method != 'GET' or attempt == 4:
                    raise
                time.sleep(5)
                continue
            if method == 'GET' and response.status_code in TRANSIENT and attempt < 4:
                time.sleep(5)
                continue
            response.raise_for_status()
            return response.json() if response.content else None

    def providers(self):
        return self.request('GET', api('model-providers')).get('model_providers', [])

    def create_agent(self, provider_id, name, message=None, checkpoint=None, system_prompt=None):
        runtime = {'type': 'checkpoint', 'checkpoint_id': checkpoint} if checkpoint else {'type': 'fresh'}
        body = {
            'name': name,
            'model_provider': {'type': 'managed', 'id': provider_id},
            'runtime': runtime,
        }
        if message:
            body['initial_message'] = message
        if system_prompt:
            body['system_prompt'] = system_prompt
        return self.request('POST', '/v2/agents', json=body)

    def runs(self, agent_id):
        data = self.request('GET', api('agents', agent_id, 'runs'))
        return data if isinstance(data, list) else data.get('runs', [])

    def run(self, agent_id, run_id):
        return self.request('GET', api('agents', agent_id, 'runs', run_id))

    def wait_for_run(self, agent_id, timeout, stop):
        """The finished run, or {'status': 'timed_out'} once timeout passes."""
        deadline = time.monotonic() + timeout
        run_id = None
        while time.monotonic() < deadline:
            try:
                runs = self.runs(agent_id)
                if runs:
                    run_id = runs[0]['id']
                    break
            except httpx.HTTPStatusError as error:
                if error.response.status_code not in (409, *TRANSIENT):
                    raise
            except httpx.TransportError:
                pass  # Runta answered too slowly or dropped the connection; ask again.
            pause(stop, 5)

        while run_id and time.monotonic() < deadline:
            try:
                result = self.run(agent_id, run_id)
            except httpx.HTTPStatusError as error:
                if error.response.status_code not in TRANSIENT:
                    raise
                pause(stop, 5)
                continue
            except httpx.TransportError:
                pause(stop, 5)
                continue
            if result['status'] in ('finished', 'failed', 'cancelled'):
                return result
            pause(stop, 5)
        return {'id': run_id, 'status': 'timed_out'}

    def cancel_run(self, agent_id, run_id):
        """Stops the agent's turn; the runtime, its files and its services stay up."""
        result = self.request('DELETE', api('agents', agent_id, 'runs', run_id))
        deadline = time.monotonic() + 120
        while result.get('status') not in ('finished', 'failed', 'cancelled'):
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f'Cloud Agent run {run_id} is still {result.get("status")} after cancelling'
                )
            time.sleep(2)
            result = self.run(agent_id, run_id)
        return result

    def cancel_active(self, agent_id):
        """Asks Runta to cancel any unfinished run without waiting; used on Ctrl-C."""
        for run in self.runs(agent_id):
            if run['status'] not in ('finished', 'failed', 'cancelled'):
                self.request('DELETE', api('agents', agent_id, 'runs', run['id']))

    def patch_runtime(self, runtime_id, body):
        path = api('runtimes', runtime_id)
        for _ in range(5):
            current = self.request('GET', path).get('data', {})
            response = self.http.patch(path, params={'expected_revision': current['revision']}, json=body)
            if response.status_code == 409:
                time.sleep(1)
                continue
            response.raise_for_status()
            return self.request('GET', path).get('data', {})
        raise RuntimeError('runtime kept changing while updating it; retry')

    def set_ingress(self, runtime_id, port):
        self.patch_runtime(runtime_id, {'ingress_specs': [{'protocol': 'https', 'runtime_port': port}]})

    def delete_agent(self, agent_id):
        self.request('DELETE', api('agents', agent_id), params={'delete_runtime': 'true'})

    def set_memory(self, runtime, memory_mib):
        """Shuts the runtime down and sets its baseline and maximum memory; resume it to use it."""
        runtime.shutdown()
        deadline = time.monotonic() + 300
        while runtime.status().value != 'shutdown':
            if time.monotonic() > deadline:
                raise TimeoutError('base Cloud Agent runtime did not shut down')
            time.sleep(2)
        # A running VM only has its baseline memory, and Runta's default 512 MiB baseline let the
        # kernel OOM-kill pnpm install, esbuild, pi and Runta's own guest agent.
        memory = {'memory_mib': memory_mib}
        self.patch_runtime(runtime.id, {'resources': {'requests': memory, 'limits': memory}})

    def checkpoint(self, runtime, name, memory_mib):
        # Agents restored from a cold checkpoint get the memory recorded on it.
        self.set_memory(runtime, memory_mib)
        path = api('runtimes', runtime.id, 'checkpoints')
        response = self.http.post(path, json={'name': name, 'kind': 'cold'})
        response.raise_for_status()
        return self.sdk.checkpoints.wait_until_ready(
            response.json()['data']['id'], timeout=1800, poll_interval=5
        )


def shell(runtime, script, timeout=60):
    result = runtime.exec(['bash', '-lc', script], timeout=timeout, max_output_bytes=100_000)
    if result.exit_code:
        raise RuntimeError(
            f'command exited {result.exit_code}: {(result.stderr_text or result.stdout_text)[-2000:]}'
        )
    return result.stdout_text
