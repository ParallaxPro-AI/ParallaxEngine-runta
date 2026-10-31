"""Make ParallaxEngine games with a Runta Cloud Agent."""

import argparse
import os
import sys
from pathlib import Path

from . import config, gallery, runner
from .cloud import Cloud


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(prog='pxr', description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('providers', help='show model providers connected to Runta')
    base = commands.add_parser(
        'base', help='build a runtime checkpoint with the engine installed so game runs start faster'
    )
    base.add_argument('--engine-ref', default='main', help='engine commit, tag or branch')
    base.add_argument('--provider', required=True, help='Runta managed-provider ID')
    run = commands.add_parser('run', help='make a game from a prompt')
    run.add_argument('--config', type=Path, help='prompts and profiles file (default: pxr.toml if present)')
    run.add_argument('--prompt', help='game idea; overrides saved prompts')
    run.add_argument('--provider', help='Runta provider ID; use this instead of a saved profile')
    run.add_argument('--profile', action='append', help='saved provider setup; repeat to run each one')
    run.add_argument('--checkpoint', help='override the saved base checkpoint')
    run.add_argument('--samples', type=positive, default=1)
    run.add_argument('--parallel', type=positive, default=2)
    run.add_argument(
        '--timeout',
        type=positive,
        default=7200,
        help='agent time limit in seconds; on timeout the work so far is checked and saved',
    )
    run.add_argument('--dry-run', action='store_true')
    view = commands.add_parser('gallery', help='rebuild and open the local gallery')
    view.add_argument('--no-open', action='store_true')
    attach = commands.add_parser(
        'attach', help='wait again for agents pxr lost track of, then check and save their games'
    )
    attach.add_argument(
        '--timeout',
        type=positive,
        default=7200,
        help='agent time limit in seconds, counted from when the run started',
    )
    pull = commands.add_parser('pull', help='download game source from a Runta runtime')
    down = commands.add_parser('down', help='delete Runta runtimes for saved generations')
    down.add_argument('--yes', action='store_true', help='allow deletion even without a current local backup')
    down.add_argument(
        '--old-bases',
        action='store_true',
        help='delete base checkpoints that are no longer current or in use, instead of runtimes',
    )
    for command in (attach, pull, down):
        command.add_argument('run', nargs='?', help='run folder or id; omitted means all saved runs')
    for command in (base, run, view, attach, pull, down):
        command.add_argument('--out', type=Path, default=Path('results'))
    args = parser.parse_args(argv)
    try:
        if args.command == 'providers':
            with Cloud() as cloud:
                for provider in cloud.providers():
                    name = provider.get('display_name', 'provider')
                    model = provider.get('default_model') or 'model not set'
                    print(f'{provider["id"]}\t{name}\t{model}')
        elif args.command == 'base':
            runner.setup(args)
        elif args.command == 'run':
            profiles, prompts = config.load(args.config, args.prompt, args.profile, args.provider)
            results = runner.generate(args, profiles, prompts)
            if not args.dry_run:
                print(f'Gallery: {gallery.render(args.out)}')
            return int(any(run['status'] != 'passed' for run in results))
        elif args.command == 'gallery':
            print(gallery.render(args.out, not args.no_open))
        elif args.command == 'attach':
            failed = runner.attach(args)
            gallery.render(args.out)
            return int(failed > 0)
        elif args.command == 'pull':
            failed = runner.pull(args)
            gallery.render(args.out)
            return int(failed > 0)
        elif args.command == 'down' and args.old_bases:
            return int(runner.prune_bases(args) > 0)
        elif args.command == 'down':
            skipped = runner.down(args)
            gallery.render(args.out)
            return int(skipped > 0)
    except KeyboardInterrupt:
        print('Interrupted. Recorded runtimes are retained; use pxr pull or pxr down.', file=sys.stderr)
        # Run threads may still be inside a long remote command; don't wait for them to return.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(130)
    except Exception as error:
        print(f'pxr: {error}', file=sys.stderr)
        return 1
    return 0
