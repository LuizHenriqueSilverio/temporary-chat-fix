#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
chat2md.py -- extrai a conversa de uma pagina do ChatGPT salva em HTML
e grava um markdown limpo, pronto para colar numa conversa nova.

O HTML salvo tem ~800 KB porque carrega o CSS, os icones em base64 e a
interface inteira. A conversa em si sao uns 30 KB. Este script joga fora
a interface e mantem o texto, com os turnos marcados, preservando
titulos, listas, citacoes, blocos de codigo e os links das fontes.

Funciona tanto no HTML ja corrigido quanto no quebrado pelo SavePage --
a extracao nao depende do campo de digitacao, que e onde mora o bug.

USO
---
    python3 chat2md.py conversa.html              # gera conversa.md
    python3 chat2md.py conversa.html -o saida.md
    python3 chat2md.py ~/conversas/ -r            # pasta inteira
    python3 chat2md.py conversa.html --stdout     # joga na tela / pipe
    python3 chat2md.py conversa.html --sem-fontes # descarta links de fonte
    python3 chat2md.py conversa.html --titulos-originais

Precisa de beautifulsoup4:  pip install beautifulsoup4
"""
import argparse
import base64
import binascii
import datetime
import json
import os
import re
import sys

try:
    from bs4 import BeautifulSoup, NavigableString, Tag
except ImportError:
    sys.exit("faltou a dependencia: pip install beautifulsoup4")

EXTS = ('.html', '.htm', '.xhtml')

# elementos de interface que nunca entram no markdown
SKIP_TAGS = {'script', 'style', 'svg', 'button', 'template', 'noscript',
             'dialog', 'form', 'input', 'use', 'defs', 'symbol'}
SKIP_CLASS = re.compile(r'srOnly|messageActions|messageActionButton')

ROLE_LABEL = {'user': 'Você', 'assistant': 'ChatGPT', 'system': 'Sistema'}


# ----------------------------------------------------------------- fontes
def clean_url(url):
    """Tira o utm_source=chatgpt.com e amigos."""
    if '?' not in url:
        return url
    base, query = url.split('?', 1)
    keep = [p for p in query.split('&')
            if p and not p.split('=')[0].lower().startswith('utm_')]
    return base + ('?' + '&'.join(keep) if keep else '')


def render_sources(node):
    """Converte o chip de fontes do ChatGPT em links markdown."""
    seen, links = set(), []
    for btn in node.find_all(attrs={'data-assistant-sources-payload': True}):
        try:
            payload = json.loads(btn['data-assistant-sources-payload'])
        except (ValueError, TypeError):
            continue
        for src in payload:
            url = clean_url(src.get('url', ''))
            if not url or url in seen:
                continue
            seen.add(url)
            label = (src.get('title') or src.get('attribution')
                     or url).strip().replace(']', ')')
            links.append('[%s](%s)' % (label, url))
    if not links:
        return ''
    return '(' + ', '.join(links) + ')'


def append_citation(out, piece):
    """Cola o bloco de fontes ao texto anterior com um unico espaco."""
    if not piece:
        return
    while out and out[-1][-1:] in (' ', '\t', '\n'):
        out[-1] = out[-1][:-1]
        if not out[-1]:
            out.pop()
    out.append(' ' + piece if out else piece)


# ----------------------------------------------------------------- inline
def skippable(node):
    if node.name in SKIP_TAGS:
        return True
    cls = ' '.join(node.get('class') or [])
    return bool(cls and SKIP_CLASS.search(cls))


def inline_tag(child, opts):
    """Renderiza UM elemento inline, incluindo os proprios marcadores."""
    # chip de fontes: vira link, o texto do chip e descartado
    if child.has_attr('data-assistant-content-reference'):
        return '' if opts.sem_fontes else render_sources(child)
    if skippable(child):
        return ''

    name = child.name
    if name == 'br':
        return '\n'
    if name in ('strong', 'b'):
        inner = inline(child, opts).strip()
        return '**%s**' % inner if inner else ''
    if name in ('em', 'i'):
        inner = inline(child, opts).strip()
        return '*%s*' % inner if inner else ''
    if name in ('del', 's', 'strike'):
        inner = inline(child, opts).strip()
        return '~~%s~~' % inner if inner else ''
    if name == 'code':
        text = child.get_text()
        fence = '`'
        while fence in text:
            fence += '`'
        pad = ' ' if text.startswith('`') or text.endswith('`') else ''
        return '%s%s%s%s%s' % (fence, pad, text, pad, fence)
    if name == 'a':
        text = inline(child, opts).strip()
        href = clean_url(child.get('href', '') or '')
        return '[%s](%s)' % (text, href) if href else text
    if name == 'img':
        alt = (child.get('alt') or '').strip()
        src = child.get('data-savepage-src') or child.get('src') or ''
        if src.startswith('data:') or not src:
            return '![%s]' % alt if alt else ''
        return '![%s](%s)' % (alt, src)
    return inline(child, opts)


def inline(node, opts):
    """Renderiza o conteudo inline de um no (negrito, codigo, links...)."""
    out = []
    for child in node.children:
        if isinstance(child, NavigableString):
            out.append(str(child))
        elif isinstance(child, Tag):
            if child.has_attr('data-assistant-content-reference'):
                append_citation(out, inline_tag(child, opts))
            else:
                out.append(inline_tag(child, opts))
    return ''.join(out)


# ----------------------------------------------------------------- blocos
def code_block(node):
    code = node.find('code')
    text = (code.get_text() if code else node.get_text()).rstrip('\n')
    lang = ''
    for cls in ((code or node).get('class') or []):
        if cls.startswith('language-'):
            lang = cls[len('language-'):]
            break
    if lang in ('text', 'plaintext', 'none'):
        lang = ''
    fence = '```'
    while fence in text:
        fence += '`'
    return '%s%s\n%s\n%s' % (fence, lang, text, fence)


def list_block(node, opts, depth):
    ordered = node.name == 'ol'
    try:
        n = int(node.get('start', 1))
    except ValueError:
        n = 1
    lines = []
    for item in node.find_all('li', recursive=False):
        marker = ('%d. ' % n) if ordered else '- '
        n += 1
        body = blocks(item, opts, depth + 1)
        if not body.strip():
            continue
        pad = ' ' * len(marker)
        chunk = body.split('\n')
        first = marker + chunk[0]
        rest = [(pad + ln if ln.strip() else '') for ln in chunk[1:]]
        lines.append('\n'.join([first] + rest))
    sep = '\n' if depth else '\n'
    return sep.join(lines)


def quote_block(node, opts, depth):
    body = blocks(node, opts, depth)
    return '\n'.join(('> ' + ln).rstrip() for ln in body.split('\n'))


def blocks(node, opts, depth=0):
    """Renderiza conteudo de bloco, devolvendo markdown com linhas em branco."""
    parts = []
    buffer_inline = []

    def flush():
        if buffer_inline:
            txt = ''.join(buffer_inline).strip()
            if txt:
                parts.append(txt)
            buffer_inline[:] = []

    for child in node.children:
        if isinstance(child, NavigableString):
            buffer_inline.append(str(child))
            continue
        if not isinstance(child, Tag):
            continue
        if child.has_attr('data-assistant-content-reference'):
            if not opts.sem_fontes:
                append_citation(buffer_inline, render_sources(child))
            continue
        if skippable(child):
            continue

        name = child.name
        if name in ('p',):
            flush()
            txt = inline(child, opts).strip()
            if txt:
                parts.append(txt)
        elif re.fullmatch(r'h[1-6]', name or ''):
            flush()
            level = int(name[1])
            if not opts.titulos_originais:
                level = min(level + 2, 6)
            txt = inline(child, opts).strip()
            if txt:
                parts.append('#' * level + ' ' + txt)
        elif name in ('ul', 'ol'):
            flush()
            txt = list_block(child, opts, depth)
            if txt.strip():
                parts.append(txt)
        elif name == 'blockquote':
            flush()
            txt = quote_block(child, opts, depth)
            if txt.strip('> \n'):
                parts.append(txt)
        elif name == 'pre':
            flush()
            parts.append(code_block(child))
        elif name == 'hr':
            flush()
            parts.append('---')
        elif name == 'table':
            flush()
            txt = table_block(child, opts)
            if txt:
                parts.append(txt)
        elif name in ('div', 'section', 'article', 'span', 'main', 'figure'):
            inner = blocks(child, opts, depth)
            if inner.strip():
                flush()
                parts.append(inner)
        else:
            buffer_inline.append(inline_tag(child, opts))

    flush()
    return '\n\n'.join(p for p in parts if p.strip())


def table_block(node, opts):
    rows = []
    for tr in node.find_all('tr'):
        cells = [inline(td, opts).strip().replace('|', r'\|').replace('\n', ' ')
                 for td in tr.find_all(['th', 'td'])]
        if cells:
            rows.append(cells)
    if not rows:
        return ''
    width = max(len(r) for r in rows)
    rows = [r + [''] * (width - len(r)) for r in rows]
    head, body = rows[0], rows[1:]
    out = ['| ' + ' | '.join(head) + ' |',
           '| ' + ' | '.join(['---'] * width) + ' |']
    out += ['| ' + ' | '.join(r) + ' |' for r in body]
    return '\n'.join(out)


# ----------------------------------------------------------------- conversa
def saved_at(html):
    """Le a data de salvamento escondida no token da pagina."""
    m = re.search(r'data-conversation-document-affinity="([^."]+)\.', html)
    if not m:
        return None
    token = m.group(1) + '=' * (-len(m.group(1)) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(token))
        stamp = data.get('i')
        if stamp:
            return datetime.datetime.fromtimestamp(
                stamp / 1000, datetime.timezone.utc)
    except (ValueError, binascii.Error, TypeError, OSError):
        return None
    return None


def extract(html, title, opts):
    soup = BeautifulSoup(html, 'lxml')
    turns = soup.select('li[data-message-role]')
    if not turns:
        return None, 0

    when = saved_at(html)
    head = ['# ' + title, '']
    origem = 'Conversa com o ChatGPT'
    if when:
        origem += ', salva em ' + when.strftime('%d/%m/%Y')
    head.append('*%s. Extraída da página salva em HTML.*' % origem)

    out = list(head)
    count = 0
    for turn in turns:
        role = turn.get('data-message-role', '')
        holder = (turn.select_one('[data-assistant-markdown]')
                  or turn.select_one('[data-user-message-copy]'))
        if holder is None:
            continue
        if role == 'user':
            # mensagem do usuario e texto puro com quebras de linha reais
            body = holder.get_text().strip()
        else:
            body = blocks(holder, opts).strip()
        if not body:
            continue
        count += 1
        out += ['', '## ' + ROLE_LABEL.get(role, role or 'Mensagem'), '', body]

    text = '\n'.join(out).rstrip() + '\n'
    text = re.sub(r'\n{4,}', '\n\n\n', text)
    return text, count


# ----------------------------------------------------------------- cli
def collect(paths, recursive):
    files = []
    for path in paths:
        if os.path.isdir(path):
            walker = (os.walk(path) if recursive
                      else [(path, [], sorted(os.listdir(path)))])
            for root, _dirs, names in walker:
                for n in sorted(names):
                    full = os.path.join(root, n)
                    if os.path.isfile(full) and n.lower().endswith(EXTS):
                        files.append(full)
        else:
            files.append(path)
    return files


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Extrai a conversa de paginas do ChatGPT salvas em HTML.')
    ap.add_argument('paths', nargs='+', metavar='ARQUIVO_OU_PASTA')
    ap.add_argument('-o', '--out', metavar='DESTINO',
                    help='arquivo .md de saida, ou pasta se houver varios')
    ap.add_argument('-r', '--recursive', action='store_true',
                    help='desce nas subpastas')
    ap.add_argument('--stdout', action='store_true',
                    help='escreve na saida padrao em vez de gravar arquivo')
    ap.add_argument('--sem-fontes', action='store_true',
                    help='nao inclui os links das fontes citadas')
    ap.add_argument('--titulos-originais', action='store_true',
                    help='mantem os niveis de titulo do ChatGPT (h1 vira #)')
    ap.add_argument('-q', '--quiet', action='store_true')
    opts = ap.parse_args(argv)

    files = collect(opts.paths, opts.recursive)
    if not files:
        print('nenhum arquivo html encontrado.')
        return 0

    out_is_dir = bool(opts.out) and (len(files) > 1 or os.path.isdir(opts.out))
    if out_is_dir:
        os.makedirs(opts.out, exist_ok=True)

    failed = total = 0
    for src in files:
        title = os.path.splitext(os.path.basename(src))[0]
        title = re.sub(r'-corrigido$', '', title)
        try:
            with open(src, encoding='utf-8') as fh:
                html = fh.read()
        except (OSError, UnicodeDecodeError) as err:
            print('  !! %s: nao consegui ler (%s)' % (src, err))
            failed += 1
            continue

        md, turns = extract(html, title, opts)
        if md is None:
            print('  -- %s: nenhuma conversa encontrada aqui' % src)
            failed += 1
            continue

        if opts.stdout:
            sys.stdout.write(md)
            total += 1
            continue

        if out_is_dir:
            dst = os.path.join(opts.out, title + '.md')
        elif opts.out:
            dst = opts.out
        else:
            dst = os.path.splitext(src)[0] + '.md'
        try:
            parent = os.path.dirname(dst)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(dst, 'w', encoding='utf-8') as fh:
                fh.write(md)
        except OSError as err:
            print('  !! %s: nao consegui gravar (%s)' % (dst, err))
            failed += 1
            continue
        total += 1
        if not opts.quiet:
            kb_in = len(html.encode('utf-8')) / 1024
            kb_out = len(md.encode('utf-8')) / 1024
            print('  -> %s: %d turnos, %.0f KB -> %.0f KB  [%s]'
                  % (os.path.basename(src), turns, kb_in, kb_out, dst))

    if not opts.stdout and not opts.quiet:
        print('\n%d arquivo(s) convertido(s)%s'
              % (total, ', %d com erro' % failed if failed else ''))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
