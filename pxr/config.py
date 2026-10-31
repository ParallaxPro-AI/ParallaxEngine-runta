"""Prompt and managed-provider configuration."""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Profile:
    provider: str
    checkpoint: str | None = None

    def __post_init__(self):
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ValueError('profile provider must be a Runta managed-provider ID')
        if self.checkpoint is not None and (
            not isinstance(self.checkpoint, str) or not self.checkpoint.strip()
        ):
            raise ValueError('checkpoint must be a nonempty string')


DEFAULT = Path('pxr.toml')


def read(path: Path | None):
    """An explicit --config must exist; the default pxr.toml is optional."""
    if path is None:
        path = DEFAULT if DEFAULT.is_file() else None
    elif not path.is_file():
        raise ValueError(f'config not found: {path}')
    return tomllib.loads(path.read_text()) if path else {}


def checkpoints(path: Path | None):
    return {
        definition['checkpoint']
        for definition in read(path).get('profiles', {}).values()
        if isinstance(definition, dict) and definition.get('checkpoint')
    }


def load(path: Path | None, text: str | None, selected: list[str] | None, provider: str | None = None):
    data = read(path)
    definitions = data.get('profiles', {})
    names = selected or (list(definitions) if not provider else [])
    if selected and len(set(selected)) != len(selected):
        raise ValueError('choose distinct profiles')
    if provider and selected and len(selected) != 1:
        raise ValueError('--provider requires exactly one profile')
    profiles = {'run': Profile(provider)} if provider and not selected else {}
    for name in names:
        if name not in definitions:
            raise ValueError(f'unknown profile: {name}')
        definition = dict(definitions[name])
        if provider:
            definition['provider'] = provider
        profiles[name] = Profile(**definition)
    if not profiles:
        raise ValueError('provide --provider or choose a configured profile')
    entries = [{'id': slug(text), 'text': text}] if text else data.get('prompts', [])
    prompts = {}
    for entry in entries:
        ident, prompt = entry.get('id'), entry.get('text')
        if not isinstance(ident, str) or not ident or ident in prompts:
            raise ValueError('each prompt needs a distinct nonempty id')
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(f'prompt {ident!r} needs text')
        prompts[ident] = prompt
    if not prompts:
        raise ValueError('provide --prompt or a config containing [[prompts]]')
    return profiles, prompts


def slug(value):
    return re.sub(r'[^a-z0-9]+', '-', value.lower()).strip('-')[:32] or 'run'
