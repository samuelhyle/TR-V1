#!/usr/bin/env python3
"""Package only public UI assets. Never publish the checkout or data directory.

Three things happen here that the local development server does not need.

**Content addressing.** Every script and stylesheet is written under a name that
contains a digest of its own bytes, and every reference to it in the HTML and in the
other modules is rewritten to match. That is what makes a long cache safe: a URL that
includes its content cannot serve old bytes under a new name, so `/static/*` can be
declared immutable for a year instead of revalidated on every load. The hosted site
previously served `Cache-Control: no-store` on everything, so a 187 KB shell and a
339 KB script were re-downloaded in full on every single page load.

**Bundling and minification.** Twenty-five ES modules are twenty-five round trips on
a first visit, and the modules are read once and never again. esbuild inlines them
into one minified script when it is installed, and the build falls back to the plain
copy when it is not, so a missing optional tool costs size rather than correctness.

**Stylesheet extraction.** The shell carries its CSS in an inline `<style>` block and
seventy inline `style=` attributes, which is the only reason the content security
policy has to allow `style-src 'unsafe-inline'`. An inline style block is the one
thing that policy allows that can also be used to exfiltrate a page: an injected
`<style>` can probe attribute values one character at a time. Moving the rules to a
same-origin stylesheet lets the directive be dropped entirely.

Example:
  python3 bin/build_netlify.py --mode full
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'dist' / 'netlify'
WEB = ROOT / 'toriresearch' / 'web'
# The modules a published page loads by URL. Everything else is inlined into one of
# these, which is why only these are published as separate files.
ENTRY_POINTS = {'developer.js', 'viewer.js', 'login.js', 'cloud.js'}

# What the reduced seller workspace and the login page need. Published in both modes,
# because bundling the developer shell does not bundle those two pages.
REDUCED_ASSETS = {'api.js', 'auth-client.js', 'login.js', 'cloud.js', 'format.js'}

# `/static/name.js`, optionally with a query string. Only same-origin asset
# references are rewritten; a URL that is not one is left alone so nothing can be
# redirected by this pass.
REFERENCE = re.compile(r'(?P<quote>["\'])/static/(?P<path>[\w./-]+?\.(?:js|css))(?P<rest>[^"\']*)(?P=quote)')

# Every inline style in the shell, and the class that replaces it.
#
# The list is exhaustive and checked: `tests/test_netlify_build.py` builds the page
# and fails if an inline style survives, so a new one cannot be added to the markup
# without either being mapped here or relaxing the policy that depends on the answer.
UTILITIES = {
    'margin-top:4px': 'u-mt-4', 'margin-top:5px': 'u-mt-5', 'margin-top:7px': 'u-mt-7',
    'margin-top:8px': 'u-mt-8', 'margin-top:12px': 'u-mt-12', 'margin-top:14px': 'u-mt-14',
    'margin-top:15px': 'u-mt-15', 'margin-top:16px': 'u-mt-16', 'margin-top:18px': 'u-mt-18',
    'margin-top:20px': 'u-mt-20', 'margin-top:22px': 'u-mt-22', 'margin-top:24px': 'u-mt-24',
    'margin-bottom:16px': 'u-mb-16', 'margin-bottom:20px': 'u-mb-20',
    'margin-bottom:24px': 'u-mb-24',
    'margin:0': 'u-m-0', 'margin:20px 0': 'u-m-20x', 'margin:16px 24px': 'u-m-16x24',
    'margin:-10px 0 18px': 'u-note-tight',
    'padding:24px 24px 0': 'u-p-top', 'padding-left:20px': 'u-pl-20',
    'white-space:pre-wrap': 'u-pre-wrap',
    'max-height:380px;overflow:auto': 'u-scroll-box',
    'width:auto': 'u-w-auto', 'font-size:11px': 'u-fs-11',
    'font-size:12px': 'u-fs-12', 'font-size:12px;margin-top:24px': 'u-fs-12-mt-24',
    'border:0;padding:0': 'u-reset', 'margin-top:10px': 'u-mt-10',
    'margin-top:6px': 'u-mt-6', 'margin-top:9px': 'u-mt-9',
    'margin:10px 0 22px': 'u-m-10x22',
    'max-width:180px': 'u-max-180', 'padding-left:16px': 'u-pl-16',
    'display:flex;flex-direction:column;align-items:flex-start;gap:8px': 'u-field-stack',
    'background:var(--bg);padding:2px 6px;border-radius:4px;font-size:11px': 'u-chip',
    'white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;font-size:14px;line-height:1.65': 'u-prose',
}


def digest(text):
    """A short content address. Eight hex characters is 32 bits, which is ample for
    the number of builds a site accumulates and short enough to read in a URL."""
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def rewrite(text, mapping):
    """Point every same-origin asset reference at its content-addressed name."""
    def replace(match):
        target = mapping.get(match.group('path'))
        if not target:
            return match.group(0)
        return f"{match.group('quote')}/static/{target}{match.group('rest')}{match.group('quote')}"
    return REFERENCE.sub(replace, text)


def bundle(source, target):
    """Inline and minify the module graph into one script.

    Optional: without esbuild the caller copies the plain modules and every `/static/`
    reference still resolves. The failure being tolerated is a larger download, never
    a broken page, so a build machine without the tool still produces a working site.
    """
    node = shutil.which('node')
    if not node:
        return False
    result = subprocess.run([node, str(ROOT / 'bin/bundle.mjs'), str(source), str(target)],
                            capture_output=True, text=True, cwd=ROOT)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        print('bundling unavailable, shipping plain modules: '
              + (detail[-1][:160] if detail else 'no detail'))
        return False
    if result.stderr.strip():
        print('  ' + result.stderr.strip().splitlines()[-1])
    return True


def swap_inline_styles(text):
    """Replace every mapped inline `style` with a utility class of the same effect.

    Applied to the markup and to the built script alike, so the substitution is one
    rule rather than two, and a style written inside a template literal in a view is
    cleaned up by the same mechanism as one written in the HTML. That matters because
    the source keeps inline styles where they read better -- a template literal that
    builds a paragraph is easier to follow with the spacing next to the text than
    with a class name -- and the published artifact does not have to.
    """
    introduced = set()

    def replace(match):
        name = UTILITIES.get(match.group('declaration'))
        if not name:
            return match.group(0)
        introduced.add(name)
        return match.group('prefix') + f'class="{name}"'

    text = re.sub(r'(?P<prefix>[\s"\'])(?:\s)?style="(?P<declaration>[^"\'\\]{1,160})"',
                  replace, text)
    return text, introduced


def leftover_inline_styles(text):
    """The style attributes that survived, which the policy has to keep allowing.

    The attribute is delimited by double quotes, so a single quote is ordinary
    content -- and every bar renderer builds one: `style="left:'+left+'%;width:..."`.
    Excluding quotes from the character class as well would silently miss all of
    them, and the manifest would claim the output is free of unmapped inline styles
    while the bundle is full of them.
    """
    return sorted(set(re.findall(r'style="([^"\\]{1,160})"', text)))


def utility_css(names):
    """Rules for exactly the utility classes that were introduced."""
    declarations = {name: declaration for declaration, name in UTILITIES.items() if name in names}
    if not declarations:
        return ''
    rules = ['/* Utility classes replacing the inline styles the sources carried.',
             '   Prefixed `u-` so a clash with a component class is obvious. */']
    rules += [f'.{name}{{{declaration}}}' for name, declaration in sorted(declarations.items())]
    return '\n'.join(rules)


def extract_styles(html):
    """Move the shell's inline `<style>` block out into a same-origin stylesheet.

    The block is 75 KB of real rules and it is the reason `style-src` has to allow
    inline styles. Moving it is what lets that directive become `style-src 'self'`,
    which in turn blocks an injected `<style>` element -- the vector that takes data
    off the page by probing an attribute value one character at a time through a
    background image.
    """
    blocks = re.findall(r'<style[^>]*>(.*?)</style>', html, re.S)
    if not blocks:
        return html, ''
    css = '\n'.join(blocks)
    return re.sub(r'<style[^>]*>.*?</style>\s*', '', html, flags=re.S), css



def build(mode=None, *, strict=False):
    """Assemble the published site.

    The order of the steps is load-bearing and is the reason this function is a
    function rather than a script: a content address is only true if it is taken over
    the bytes that are actually published, and every step below changes bytes. So all
    the content work happens first, then the naming, then the reference rewriting --
    in that order, because each one depends on the one before it having finished.
    """
    mode = mode or os.environ.get('TORI_DEPLOY_MODE', 'full')
    if mode not in {'basic', 'full'}:
        raise ValueError('Choose basic or full deployment mode.')
    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    static = OUTPUT / 'static'
    static.mkdir(parents=True)
    manifest = {'mode': mode, 'assets': {}, 'style_utilities': [], 'unmapped_inline_styles': [],
                'bundled': False, 'modules': 0, 'entries': []}

    # -- The three pages -------------------------------------------------------
    source = ROOT / 'toriresearch'
    (OUTPUT / 'index.html').write_text((source / 'login.html').read_text())
    # Sellers get the reduced interface in both modes. The original seller workspace
    # reads /api/viewer/snapshot, whose seller projection is computed by the research
    # engine, so shipping it beside a Blobs-backed deployment would hand sellers a
    # page whose every panel refuses. The reduced view is the one this deployment can
    # answer, and it is the same view they have today.
    (OUTPUT / 'viewer.html').write_text((source / 'cloud.html').read_text())

    if mode == 'full':
        html = (source / 'dashboard.html').read_text()
        html = html.replace('<html lang="en"',
                            '<html data-deploy-mode="full" data-hosted="1" data-live-streams="0" lang="en"')
        html = html.replace('Local workspace', 'Shared workspace')
        html = html.replace('Loading local data', 'Loading workspace data')
        html = html.replace('src="/static/dashboard.js"', 'src="/static/developer.js"')
        html = html.replace('Loading local data', 'Loading workspace data')
        # The bootstrap establishes the session and CSRF token before importing
        # the dashboard. It also supplies account controls; bypassing it breaks writes.
        candidates = list(sorted(WEB.rglob('*')))
        (OUTPUT / 'developer.html').write_text(html)
        (OUTPUT / 'seller.html').write_text(html.replace('<html ', '<html data-workspace-role="seller" ', 1))
        (OUTPUT / 'reader.html').write_text((source / 'viewer.html').read_text())
    else:
        (OUTPUT / 'developer.html').write_text(
            (OUTPUT / 'viewer.html').read_text().replace('data-role="viewer"', 'data-role="developer"'))
        candidates = [asset for asset in sorted(WEB.iterdir())
                      if asset.name in REDUCED_ASSETS or asset.suffix == '.css']

    js_files = [asset for asset in candidates
                if asset.suffix == '.js' and asset.is_file() and not asset.is_symlink()]
    css_files = [asset for asset in candidates if asset.suffix == '.css']
    manifest['modules'] = len(js_files)

    # -- Bundling --------------------------------------------------------------
    # Each entry point is bundled on its own rather than sharing chunks, which is
    # what makes every published script self-contained. A published script that
    # imported another by URL would have its own bytes changed by the rewrite that
    # renames the other one, so its digest would stop matching its contents and the
    # immutable caching policy would be promising something untrue. Duplicating about
    # fifteen kilobytes of shared helpers across three bundles is the cheap way to
    # keep that promise.
    entries = [asset for asset in js_files if asset.name in ENTRY_POINTS]
    manifest['entries'] = sorted(asset.name for asset in entries)
    results = [bundle(asset, static / asset.name) for asset in entries]
    manifest['bundled'] = bool(entries) and all(results)
    if strict and not manifest['bundled']:
        raise RuntimeError('Every entry point must bundle for a deployment. Run npm ci first.')

    if not manifest['bundled']:
        # The fallback: publish the modules as they are and let the entry point
        # import its siblings. A build machine without esbuild gets a larger site,
        # never a broken one.
        for asset in js_files:
            destination = static / asset.relative_to(WEB)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(asset, destination)

    for asset in css_files:
        destination = static / asset.relative_to(WEB)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(asset, destination)

    # -- Content work ----------------------------------------------------------
    # Inline styles become utility classes. The mapping is a fixed table so the class
    # names are stable across builds, and anything the table does not cover is
    # reported rather than dropped -- a silently discarded style leaves the page
    # subtly wrong with nothing in the logs to say so.
    introduced, leftover = set(), set()
    pages = []
    for page in sorted(OUTPUT.glob('*.html')):
        text, css = extract_styles(page.read_text()) if mode == 'full' else (page.read_text(), '')
        text, used = swap_inline_styles(text)
        introduced |= used
        leftover |= set(leftover_inline_styles(text))
        pages.append([page, text, css])

    scripts = {}
    for path in sorted(static.rglob('*.js')):
        text, used = swap_inline_styles(path.read_text())
        introduced |= used
        leftover |= set(leftover_inline_styles(text))
        scripts[path] = text

    for index, (page, text, css) in enumerate(pages):
        if css:
            # The utility rules belong to whichever stylesheet was extracted, so they
            # are appended before the digest is taken: a change to the mapping is a
            # change to the stylesheet's address, not a silent edit.
            pages[index][2] = css + '\n' + utility_css(introduced)

    # -- Naming ----------------------------------------------------------------
    names = {}
    for path, text in scripts.items():
        path.write_text(text)
        names[str(path.relative_to(static))] = f'{path.stem}.{digest(text)}{path.suffix}'
    for index, entry in enumerate(pages):
        page, text, css = entry
        if not css:
            continue
        target = f'shell.{digest(css)}.css'
        (static / target).write_text(css)
        names['shell.css'] = target
        pages[index][1] = text.replace('</head>', f'<link rel="stylesheet" href="/static/{target}"></head>', 1)
    for path in sorted(static.rglob('*.css')):
        if path.name in names.values():
            continue
        names[str(path.relative_to(static))] = f'{path.stem}.{digest(path.read_text())}{path.suffix}'
    for relative, target in names.items():
        source_path = static / relative
        # `names` doubles as the reference map, and the extracted stylesheet is
        # written under its final name rather than renamed into it, so a key here is
        # not always a file waiting to be moved.
        if source_path.is_file():
            source_path.rename(static / target)

    # -- Publishing the references ---------------------------------------------
    for entry in pages:
        entry[0].write_text(rewrite(entry[1], names))
    for path in sorted(static.rglob('*.js')):
        text = path.read_text()
        if '/static/' not in text:
            continue
        # The unbundled fallback, where a module still imports its siblings by URL.
        # Rewriting changes the bytes, so the address is recomputed from them.
        text = rewrite(text, names)
        path.write_text(text)
        target = f'{path.stem.rsplit(".", 1)[0] if "." in path.stem else path.stem}.{digest(text)}{path.suffix}'
        for relative, current in list(names.items()):
            if current == path.name:
                names[relative] = target
        path.rename(path.with_name(target))

    manifest['assets'] = names
    manifest['style_utilities'] = sorted(introduced)
    manifest['unmapped_inline_styles'] = sorted(leftover)
    # The same source fingerprint is reported by the Python backend, including
    # when a dirty checkout is deployed. It never reads data or private settings.
    sys.path.insert(0, str(ROOT))
    from toriresearch import release
    manifest['release'] = release.describe()
    # A fresh function artifact also refreshes deploy-scoped configuration even
    # when only settings change and all application source bytes stay the same.
    manifest['build_id'] = str(uuid.uuid4())
    (OUTPUT / 'build-manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')

    total = sum(path.stat().st_size for path in OUTPUT.rglob('*') if path.is_file())
    print(f'Packaged {mode} workspace: {len(names)} content-addressed assets '
          f'({total:,} bytes) into {OUTPUT}')
    if manifest['bundled']:
        print(f'  {len(entries)} entry points bundled and minified: '
              + ', '.join(manifest['entries']))
    if manifest['style_utilities']:
        print(f'  {len(manifest["style_utilities"])} inline styles replaced by utility classes')
    if manifest['unmapped_inline_styles']:
        print(f'  {len(manifest["unmapped_inline_styles"])} inline style(s) unmapped: '
              f'{manifest["unmapped_inline_styles"]}; style-src-attr must still allow them')
    return OUTPUT


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('basic', 'full'))
    parser.add_argument('--strict', action='store_true', help='Require every page to bundle.')
    args = parser.parse_args()
    build(args.mode, strict=args.strict)
