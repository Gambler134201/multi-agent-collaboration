"""Install a small, reversible global rule without replacing existing user guidance.

The repository ships portable guide templates that address their own location as
``@ROOT@``.  A global rule must never carry an unresolved ``@ROOT@``, so this
module binds every path - the rule text *and* the operation guides it links to -
to the actual installation directory before anything is written.  Existing
unmanaged instructions are preserved verbatim, and nothing outside the managed
markers or the managed guide directory is modified.
"""
from datetime import datetime
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUIDE_TEMPLATES = ROOT / '.local-worker/workflow-guides'
# Copies of the linked guides, written next to the installed rule and rewritten
# on every install so they always match this installation.
GUIDES_DIR_NAME = 'model-desk-workflow-guides'
START = '<!-- model-desk-workflow:start -->'
END = '<!-- model-desk-workflow:end -->'
# The templates address their own directory with this literal token.
GUIDE_TOKEN = '@ROOT@\\.local-worker\\workflow-guides'


def bind(text, root):
    """Replace the ``@ROOT@`` template token with one concrete installation path."""
    return text.replace('@ROOT@', str(root))


def guides_dir(home):
    return Path(home) / GUIDES_DIR_NAME


def sync_guides(home, root=ROOT):
    """Write ``@ROOT@``-resolved copies of the linked operation guides.

    The rule tells an agent to read those guides for the exact operation it is
    performing, so they have to be readable with real paths.  Only this managed
    directory and the guide file names shipped in this repository are written.
    """
    home = Path(home)
    target = guides_dir(home)
    if target.exists() and not target.is_dir():
        raise ValueError('Guide destination exists but is not a directory: ' + str(target))
    templates = sorted(GUIDE_TEMPLATES.glob('*.md'))
    if not templates:
        raise ValueError('No workflow guide templates found under ' + str(GUIDE_TEMPLATES))
    written = []
    for template in templates:
        body = bind(template.read_text(encoding='utf-8'), root).strip() + '\n'
        if '@ROOT@' in body:
            raise ValueError('Guide template still has an unresolved @ROOT@: ' + template.name)
        target.mkdir(parents=True, exist_ok=True)
        path = target / template.name
        temporary = target / (template.name + '.model-desk.tmp')
        temporary.write_text(body, encoding='utf-8')
        os.replace(temporary, path)
        written.append(path)
    return written


def block(guides=None, root=ROOT):
    """Managed rule text with every path bound to a real installation.

    ``guides`` optionally points at the resolved guide copies installed for this
    rules directory; without it the templates inside ``root`` are addressed.
    """
    text = (Path(root) / '.local-worker/workflow-guides/core.md').read_text(encoding='utf-8')
    if guides is not None:
        text = text.replace(GUIDE_TOKEN, str(Path(guides)))
    text = bind(text, root).strip()
    if '@ROOT@' in text:
        raise ValueError('Rule template still has an unresolved @ROOT@; refusing to install.')
    return START + '\n' + text + '\n' + END


def install(home):
    home = Path(home)
    sync_guides(home)
    rule = block(guides=guides_dir(home))
    path = home / ('AGENTS.override.md' if (home / 'AGENTS.override.md').exists() else 'AGENTS.md')
    old = path.read_text(encoding='utf-8') if path.exists() else ''
    if (START in old) != (END in old):
        raise ValueError('Existing managed instruction markers are incomplete; preserve file and inspect it.')
    if START in old:
        before, rest = old.split(START, 1)
        _, after = rest.split(END, 1)
        new = before + rule + after
    else:
        new = old.rstrip() + ('\n\n' if old.strip() else '') + rule + '\n'
    if new != old:
        home.mkdir(parents=True, exist_ok=True)
        if old:
            backup = path.with_name(path.name + '.before-model-desk-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
            backup.write_text(old, encoding='utf-8')
        # Same filesystem atomic replace; keep unrelated instructions verbatim.
        temporary = path.with_name(path.name + '.model-desk.tmp')
        temporary.write_text(new, encoding='utf-8')
        os.replace(temporary, path)
    return path


if __name__ == '__main__':
    import argparse
    import json
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', help='Rules directory (default: CODEX_HOME, else the user profile .codex)')
    parser.add_argument('--check', action='store_true', help='Report the binding without writing anything')
    arguments = parser.parse_args()
    target = Path(arguments.home) if arguments.home else Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    if arguments.check:
        rule_file = target / ('AGENTS.override.md' if (target / 'AGENTS.override.md').exists() else 'AGENTS.md')
        installed = rule_file.is_file() and START in rule_file.read_text(encoding='utf-8')
        print(json.dumps({'installed': installed, 'rule_file': str(rule_file), 'install_root': str(ROOT),
                          'guides': str(guides_dir(target)), 'scope': 'current Windows user, all local projects'},
                         ensure_ascii=False))
    else:
        print(install(target))
