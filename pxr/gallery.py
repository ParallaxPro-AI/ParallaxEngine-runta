"""A local, static gallery. All data comes from the saved run records."""

import html
import json
import webbrowser
from pathlib import Path
from string import Template

from .config import slug
from .runner import asset, records


def escape(value):
    return html.escape(str(value), quote=True)


def title(meta):
    # One-off --prompt runs used to be saved as 'prompt' / 'run'; name them by prompt text and model.
    name = slug(meta['prompt']) if meta['prompt_id'] == 'prompt' else meta['prompt_id']
    who = meta['profile']
    if who == 'run':
        who = meta.get('model_used') or who
    return f'{name} · {who}'


def image(folder, rel):
    if not (folder / 'screenshot.png').exists():
        return '<div class="empty">No screenshot</div>'
    return f'<img loading="lazy" src="{escape(rel)}/screenshot.png" alt="Game screenshot">'


def links(meta, rel):
    found = [f'<a href="{escape(rel)}/meta.json">Details</a>']
    if meta.get('url') and not meta.get('deleted'):
        found.append(f'<a href="{escape(meta["url"])}" target="_blank" rel="noopener">Play / editor</a>')
    if meta.get('backup'):
        found.append(f'<a href="{escape(rel + "/" + meta["backup"]["archive"])}" download>Download game</a>')
    return ' · '.join(found)


def error_text(meta):
    result = meta.get('result', {})
    return (
        meta.get('error')
        or result.get('editor_error')
        or result.get('capture_error')
        or meta.get('backup_error')
    )


def card(path, meta, out):
    folder = path.parent
    rel = folder.relative_to(out).as_posix()
    model = meta.get('model_used') or 'model not reported'
    versions = meta.get('versions', {})
    result = meta.get('result', {})
    engine = versions.get('engine', 'unknown')[:12]
    checks = f'Check {result.get("check_exit", "not run")} · tests {result.get("test_exit", "not run")}'
    error = error_text(meta)
    search = ' '.join(
        [
            meta['prompt'],
            meta['profile'],
            model,
            str(meta.get('provider_id', '')),
            str(versions),
            meta['status'],
        ]
    ).lower()
    return f'''\
    <article data-search="{escape(search)}">
      {image(folder, rel)}
      <div class="info">
        <h2>{escape(title(meta))} #{meta['sample']}</h2>
        <p>{escape(meta['prompt'])}</p>
        <small>
          Pi · {escape(model)}<br>
          Provider {escape(meta.get('provider_id', 'unknown'))}<br>
          Engine {escape(engine)} · {escape(meta['status'])}<br>
          {escape(checks)}
        </small>
        {f'<p class="failed">{escape(error)}</p>' if error else ''}
        <p>{links(meta, rel)}</p>
        <details>
          <summary>Configuration and results</summary>
          <pre>{escape(json.dumps(meta, indent=2))}</pre>
        </details>
      </div>
    </article>'''


def render(out: Path, open_browser=False):
    out.mkdir(parents=True, exist_ok=True)
    cards = [card(path, meta, out) for path, meta in reversed(records(out))]
    page = out / 'index.html'
    page.write_text(Template(asset('gallery.html')).substitute(count=len(cards), cards='\n'.join(cards)))
    if open_browser:
        webbrowser.open(page.resolve().as_uri())
    return page
