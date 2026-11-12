#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
inline_assets.py -- junta uma pagina salva pelo navegador como "pagina
completa" (pagina.html + pasta pagina_files/) num unico .html que abre
sozinho, sem a pasta do lado.

O navegador grava o HTML ja renderizado e espalha CSS, fontes, imagens e
JS pela pasta. Este script embute no HTML o que a pagina precisa para ser
lida e descarta o que so atrapalha offline:

  <link rel="stylesheet">   vira <style> com o CSS dentro
  url(...) e @import        viram data: URI (fontes, fundos, CSS)
  img, source, video...     src, srcset e poster viram data: URI
  <iframe> local            vira srcdoc, processado do mesmo jeito
  <script>, preload, CSP    removidos: offline o app so tentaria se
                            religar e baguncaria a pagina estatica

O html e a pasta originais nao sao tocados. So usa a biblioteca padrao.

USO
---
    python3 inline_assets.py wrapper.html          # gera wrapper-unico.html
    python3 inline_assets.py wrapper.html -o conversa.html
    python3 inline_assets.py ~/salvas/             # todos os .html da pasta
"""
import argparse
import base64
import html as htmllib
import mimetypes
import os
import re
import sys
from urllib.parse import unquote

SUFFIX = '-unico'
EXTS = ('.html', '.htm', '.xhtml')
MAX_DEPTH = 6

# o html salvo pelo navegador e bem formado: regex basta e preserva o resto
_ATTR = r'''\s+[^\s"'>/=]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+))?'''
ATTR = re.compile(
    r'''\s+([^\s"'>/=]+)(?:\s*=\s*("[^"]*"|'[^']*'|[^\s"'=<>`]+))?''')
TOKEN = re.compile(
    r'(?P<sopen><style\b[^>]*>)(?P<css>.*?)(?P<sclose></style\s*>)'
    r'|<(?P<name>[a-zA-Z][a-zA-Z0-9:-]*)(?P<attrs>(?:' + _ATTR + r')*)'
    r'\s*(?P<slash>/?)>', re.S | re.I)
SCRIPT = re.compile(r'<script\b[^>]*>.*?</script\s*>', re.S | re.I)
CSS_URL = re.compile(
    r'''url\(\s*(?:"([^"]*)"|'([^']*)'|([^)\s"']*))\s*\)''', re.I)
CSS_IMPORT = re.compile(r'''@import\s+(["'])([^"']*)\1''', re.I)
CHARSET = re.compile(r'''^\s*@charset\s+["'][^"']*["']\s*;''', re.I)
CSS_FILE = re.compile(r'\.css(\.download)?$', re.I)
SCHEME = re.compile(r'^[a-zA-Z][a-zA-Z0-9+.-]*:')

DROP_REL = {'preload', 'modulepreload', 'prefetch', 'preconnect',
            'dns-prefetch', 'prerender', 'manifest'}
ICON_REL = {'icon', 'apple-touch-icon', 'mask-icon'}
URL_ATTRS = {'img': ('src',), 'source': ('src',), 'video': ('src', 'poster'),
             'audio': ('src',), 'track': ('src',), 'input': ('src',),
             'image': ('href', 'xlink:href')}
WATCH = set(URL_ATTRS) | {'link', 'meta', 'iframe'}

MIME = {'.css': 'text/css', '.png': 'image/png', '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp',
        '.avif': 'image/avif', '.svg': 'image/svg+xml', '.ico': 'image/x-icon',
        '.woff2': 'font/woff2', '.woff': 'font/woff', '.ttf': 'font/ttf',
        '.otf': 'font/otf'}
MAGIC = ((b'\x89PNG', 'image/png'), (b'\xff\xd8\xff', 'image/jpeg'),
         (b'GIF8', 'image/gif'), (b'wOF2', 'font/woff2'),
         (b'wOFF', 'font/woff'), (b'OTTO', 'font/otf'),
         (b'\x00\x01\x00\x00', 'font/ttf'))


# ----------------------------------------------------------------- utilidades
def escape_attr(value):
    return (value
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;'))


def read_text(path):
    with open(path, encoding='utf-8', errors='replace') as fh:
        return fh.read().lstrip('\ufeff')


def local_path(url, base_dir):
    """Caminho no disco de uma referencia relativa, ou (None, '')."""
    url = (url or '').strip()
    if not url or url.startswith(('#', '/', '\\')) or SCHEME.match(url):
        return None, ''        # ancora, raiz do site, http:, data:, blob:...
    url, _, frag = url.partition('#')
    url = url.split('?', 1)[0]
    if not url:
        return None, ''
    path = os.path.normpath(os.path.join(base_dir, unquote(url)))
    return path, ('#' + frag if frag else '')


def guess_mime(path, data):
    name = path.lower()
    if name.endswith('.download'):       # o Chrome grava x.js como x.js.download
        name = name[:-len('.download')]
    mime = (MIME.get(os.path.splitext(name)[1])
            or mimetypes.guess_type(name)[0])
    if mime:
        return mime
    for magic, kind in MAGIC:
        if data.startswith(magic):
            return kind
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    if b'<svg' in data[:2048].lower():
        return 'image/svg+xml'
    return 'application/octet-stream'


def split_srcset(value):
    """[(url, descritor)] -- aguenta data: URI, que tem virgula no meio."""
    out, i, n = [], 0, len(value)
    while i < n:
        while i < n and (value[i].isspace() or value[i] == ','):
            i += 1
        j = i
        while j < n and not value[j].isspace():
            j += 1
        url, desc = value[i:j], ''
        if url.endswith(','):
            url = url.rstrip(',')
            i = j
        else:
            k = value.find(',', j)
            k = n if k < 0 else k
            desc = value[j:k].strip()
            i = k + 1
        if url:
            out.append((url, desc))
    return out


class Attrs(object):
    """Atributos de uma tag, na ordem original, ja sem entidades."""

    def __init__(self, raw):
        self.items, self.changed = [], False
        for m in ATTR.finditer(raw):
            val = m.group(2)
            if val is not None:
                if val[:1] in ('"', "'"):
                    val = val[1:-1]
                val = htmllib.unescape(val)
            self.items.append([m.group(1), val])

    def get(self, key):
        for k, v in self.items:
            if k.lower() == key:
                return v
        return None

    def put(self, key, value):
        for item in self.items:
            if item[0].lower() == key:
                if item[1] != value:
                    item[1], self.changed = value, True
                return
        self.items.append([key, value])
        self.changed = True

    def drop(self, *keys):
        kept = [it for it in self.items if it[0].lower() not in keys]
        if len(kept) != len(self.items):
            self.items, self.changed = kept, True

    def render(self, name, slash):
        parts = [name] + [k if v is None else '%s="%s"' % (k, escape_attr(v))
                          for k, v in self.items]
        return '<%s%s>' % (' '.join(parts), ' /' if slash else '')


# ----------------------------------------------------------------- empacotador
class Packer(object):
    def __init__(self):
        self.cache, self.missing = {}, set()
        self.files = self.sheets = self.scripts = self.frames = 0

    def embed(self, url, base_dir, depth=0):
        """Troca uma referencia local pelo data: URI do arquivo."""
        path, frag = local_path(url, base_dir)
        if path is None or depth > MAX_DEPTH:
            return url
        if not os.path.isfile(path):
            self.missing.add(os.path.basename(path))
            return url
        if path not in self.cache:
            if CSS_FILE.search(path):        # @import: o css tambem e processado
                css = self.fix_css(read_text(path), os.path.dirname(path),
                                   depth + 1)
                data, mime = css.encode('utf-8'), 'text/css;charset=utf-8'
            else:
                with open(path, 'rb') as fh:
                    data = fh.read()
                mime = guess_mime(path, data)
            self.cache[path] = 'data:%s;base64,%s' % (
                mime, base64.b64encode(data).decode('ascii'))
            self.files += 1
        return self.cache[path] + frag

    def fix_css(self, css, base_dir, depth=0):
        css = CHARSET.sub('', css)
        css = CSS_IMPORT.sub(r'@import url(\1\2\1)', css)

        def repl(m):
            raw = next((g for g in m.groups() if g is not None), '')
            new = self.embed(raw, base_dir, depth)
            return m.group(0) if new == raw else 'url("%s")' % new
        return CSS_URL.sub(repl, css)

    def fix_html(self, page, base_dir, depth=0):
        page, n = SCRIPT.subn('', page)
        self.scripts += n

        def route(m):
            if m.group('sopen'):
                return (m.group('sopen')
                        + self.fix_css(m.group('css'), base_dir, depth)
                        + m.group('sclose'))
            return self.fix_tag(m, base_dir, depth)
        return TOKEN.sub(route, page)

    def stylesheet(self, a, base_dir, depth):
        """<link rel=stylesheet> local -> <style>; None se nao der."""
        path, _ = local_path(a.get('href'), base_dir)
        if path is None:
            return None
        if not os.path.isfile(path):
            self.missing.add(os.path.basename(path))
            return None
        self.sheets += 1
        css = self.fix_css(read_text(path), os.path.dirname(path), depth + 1)
        media = a.get('media')
        head = ('<style media="%s">' % escape_attr(media)
                if media and media.strip().lower() != 'all' else '<style>')
        return head + css.replace('</style', '<\\/style') + '</style>'

    def fix_tag(self, m, base_dir, depth):
        name, raw = m.group('name').lower(), m.group('attrs')
        if name not in WATCH and 'url(' not in raw.lower():
            return m.group(0)
        a = Attrs(raw)

        if name == 'meta':
            csp = (a.get('http-equiv') or '').lower()
            return '' if csp == 'content-security-policy' else m.group(0)

        if name == 'link':
            rel = set((a.get('rel') or '').lower().split())
            if rel & DROP_REL:
                return ''
            if 'stylesheet' in rel and 'alternate' not in rel:
                inlined = self.stylesheet(a, base_dir, depth)
                if inlined is not None:
                    return inlined
            elif rel & ICON_REL and a.get('href'):
                a.put('href', self.embed(a.get('href'), base_dir, depth))

        style = a.get('style')
        if style and 'url(' in style.lower():
            a.put('style', self.fix_css(style, base_dir, depth))

        for key in URL_ATTRS.get(name, ()):
            if a.get(key):
                a.put(key, self.embed(a.get(key), base_dir, depth))

        if name in ('img', 'source') and a.get('srcset'):
            cands = [(self.embed(u, base_dir, depth), d)
                     for u, d in split_srcset(a.get('srcset'))]
            if (a.get('src') or '').startswith('data:'):
                a.drop('srcset', 'sizes')      # o src ja basta; nao duplica
            elif cands and all(u.startswith('data:') for u, _ in cands):
                a.put('srcset', ', '.join((u + ' ' + d).strip()
                                          for u, d in cands))
            elif name == 'img':
                a.drop('srcset', 'sizes')      # candidato remoto quebra offline
            else:
                return ''                      # <source> sem copia: fica o <img>

        if name == 'iframe' and depth < MAX_DEPTH:
            path, _ = local_path(a.get('src'), base_dir)
            if path and os.path.isfile(path) and path.lower().endswith(EXTS):
                inner = self.fix_html(read_text(path), os.path.dirname(path),
                                      depth + 1)
                a.drop('src')
                a.put('srcdoc', inner)
                self.frames += 1

        return (a.render(m.group('name'), m.group('slash'))
                if a.changed else m.group(0))


# ----------------------------------------------------------------- cli
def collect(paths):
    files = []
    for path in paths:
        if os.path.isdir(path):
            files += [os.path.join(path, n) for n in sorted(os.listdir(path))
                      if n.lower().endswith(EXTS)
                      and os.path.isfile(os.path.join(path, n))]
        else:
            files.append(path)
    # ignora saidas de execucoes anteriores
    return [f for f in files
            if not os.path.splitext(os.path.basename(f))[0].endswith(SUFFIX)]


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Junta pagina.html + pagina_files/ num unico .html.')
    ap.add_argument('paths', nargs='+', metavar='ARQUIVO_OU_PASTA')
    ap.add_argument('-o', '--out', metavar='DESTINO',
                    help='arquivo de saida, ou pasta se houver varios')
    ap.add_argument('-q', '--quiet', action='store_true')
    opts = ap.parse_args(argv)

    files = collect(opts.paths)
    if not files:
        print('nenhum arquivo html encontrado.')
        return 0
    out_is_dir = bool(opts.out) and (len(files) > 1 or os.path.isdir(opts.out))
    if out_is_dir:
        os.makedirs(opts.out, exist_ok=True)

    total = failed = 0
    for src in files:
        try:
            with open(src, encoding='utf-8') as fh:
                page = fh.read()
        except (OSError, UnicodeDecodeError) as err:
            print('  !! %s: nao consegui ler (%s)' % (src, err))
            failed += 1
            continue

        packer = Packer()
        result = packer.fix_html(page, os.path.dirname(os.path.abspath(src)))

        base, ext = os.path.splitext(os.path.basename(src))
        if out_is_dir:
            dst = os.path.join(opts.out, base + SUFFIX + ext)
        elif opts.out:
            dst = opts.out
        else:
            dst = os.path.join(os.path.dirname(src), base + SUFFIX + ext)
        try:
            with open(dst, 'w', encoding='utf-8') as fh:
                fh.write(result)
        except OSError as err:
            print('  !! %s: nao consegui gravar (%s)' % (dst, err))
            failed += 1
            continue
        total += 1

        if not opts.quiet:
            extra = ', %d frame(s)' % packer.frames if packer.frames else ''
            print('  -> %s: %d css, %d arquivo(s) embutido(s), '
                  '%d script(s) removido(s)%s, %.0f KB -> %.0f KB  [%s]'
                  % (os.path.basename(src), packer.sheets, packer.files,
                     packer.scripts, extra, len(page.encode('utf-8')) / 1024,
                     len(result.encode('utf-8')) / 1024, dst))
        if packer.missing:
            names = sorted(packer.missing)
            print('     faltando na pasta: %s%s' % (
                ', '.join(names[:6]), ' ...' if len(names) > 6 else ''))
            if not (packer.sheets or packer.files):
                print('     nada foi embutido -- a pasta _files esta ao lado '
                      'do html, com o nome de quando foi salva?')

    if not opts.quiet:
        print('\n%d arquivo(s) empacotado(s)%s'
              % (total, ', %d com erro' % failed if failed else ''))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())