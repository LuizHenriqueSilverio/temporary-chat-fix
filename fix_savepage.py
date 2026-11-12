#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_savepage.py -- conserta paginas do ChatGPT salvas com SavePage WE.

O BUG
-----
A extensao as vezes grava o <input name="conversationState"> do campo de
digitacao com o JSON de estado da conversa colado cru dentro do value=,
sem escapar as aspas duplas. O navegador le value="{" , trata o resto do
JSON como atributos inventados e fecha a tag no primeiro '>' que existe
dentro do texto. Todo o resto vaza como texto visivel: aquele paredao
com \\n\\n e ** por cima da conversa.

A CORRECAO
----------
Escapar as aspas do atributo (" -> &quot;) para a tag voltar a fechar no
lugar certo. Nada mais e tocado: fora esse atributo, o arquivo de saida
fica byte a byte identico ao original -- CSS embutido, icones em base64,
tudo continua funcionando offline. Rodar duas vezes nao causa dano.

USO
---
    python3 fix_savepage.py conversa.html
    python3 fix_savepage.py conversa.html saida.html
    python3 fix_savepage.py ~/conversas/                 # pasta inteira
    python3 fix_savepage.py ~/conversas/ -r              # com subpastas
    python3 fix_savepage.py ~/conversas/ -n              # so diagnostica
    python3 fix_savepage.py ~/conversas/ -i              # sobrescreve (.bak)
    python3 fix_savepage.py ~/conversas/ -o ~/corrigidas # joga em outra pasta

Sem opcoes, grava "nome-corrigido.html" ao lado de cada original.
Codigo de saida: 0 = tudo certo, 1 = erro de leitura/escrita em algum
arquivo, 2 = modo -n encontrou arquivos quebrados.
"""
import argparse
import json
import os
import re
import shutil
import sys


def short(path):
    """Caminho relativo quando faz sentido, absoluto quando nao faz."""
    try:
        rel = os.path.relpath(path)
    except ValueError:
        return path
    return rel if not rel.startswith('..') else path

# um atributo que abre com {" ou [{" e JSON que deveria estar escapado
SUSPECT = re.compile(r'\svalue="(?=[\[{]")')
SUFFIX = '-corrigido'
EXTS = ('.html', '.htm', '.xhtml')


def escape_attr(value):
    return (value
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;'))


def find_breaks(html):
    """Devolve [(inicio, fim, valor_cru)] de cada atributo quebrado."""
    decoder = json.JSONDecoder()
    breaks = []
    for m in SUSPECT.finditer(html):
        vstart = m.end()
        try:
            # raw_decode le exatamente ate o fim do objeto JSON
            _, vend = decoder.raw_decode(html, vstart)
        except json.JSONDecodeError:
            continue
        raw_value = html[vstart:vend]
        # so esta quebrado se o JSON contiver aspas cruas
        if '"' not in raw_value:
            continue
        # o caractere seguinte tem de ser a aspa que fecha o atributo
        if vend >= len(html) or html[vend] != '"':
            continue
        breaks.append((vstart, vend, raw_value))
    return breaks


def fix(html):
    """Devolve (html_corrigido, n_atributos, n_caracteres_recuperados)."""
    breaks = find_breaks(html)
    if not breaks:
        return html, 0, 0

    out = []
    cursor = 0
    recovered = 0
    for vstart, vend, raw_value in breaks:
        out.append(html[cursor:vstart])
        out.append(escape_attr(raw_value))
        cursor = vend
        recovered += len(raw_value)
    out.append(html[cursor:])
    return ''.join(out), len(breaks), recovered


def collect(paths, recursive):
    """Expande arquivos e pastas numa lista de arquivos html."""
    files = []
    for path in paths:
        if os.path.isdir(path):
            if recursive:
                for root, _dirs, names in os.walk(path):
                    for n in sorted(names):
                        if n.lower().endswith(EXTS):
                            files.append(os.path.join(root, n))
            else:
                for n in sorted(os.listdir(path)):
                    full = os.path.join(path, n)
                    if os.path.isfile(full) and n.lower().endswith(EXTS):
                        files.append(full)
        else:
            files.append(path)
    # ignora saidas de execucoes anteriores
    return [f for f in files
            if not os.path.splitext(os.path.basename(f))[0].endswith(SUFFIX)]


def destination(src, args, single_out):
    if single_out:
        return single_out
    if args.in_place:
        return src
    base, ext = os.path.splitext(os.path.basename(src))
    name = base + SUFFIX + ext
    out_dir = args.out_dir or os.path.dirname(src) or '.'
    return os.path.join(out_dir, name)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Conserta paginas do ChatGPT salvas com SavePage WE.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Sem opcoes, grava "nome-corrigido.html" ao lado de cada original.')
    ap.add_argument('paths', nargs='+', metavar='ARQUIVO_OU_PASTA')
    ap.add_argument('-o', '--out-dir', metavar='PASTA',
                    help='grava os corrigidos nesta pasta')
    ap.add_argument('-i', '--in-place', action='store_true',
                    help='sobrescreve o original (guarda copia .bak)')
    ap.add_argument('--no-backup', action='store_true',
                    help='com -i, nao guarda o .bak')
    ap.add_argument('-r', '--recursive', action='store_true',
                    help='desce nas subpastas')
    ap.add_argument('-n', '--dry-run', action='store_true',
                    help='so diagnostica, nao grava nada')
    ap.add_argument('-q', '--quiet', action='store_true',
                    help='so mostra o que estava quebrado')
    args = ap.parse_args(argv)

    # forma antiga de 2 argumentos: entrada.html saida.html
    single_out = None
    if (len(args.paths) == 2 and os.path.isfile(args.paths[0])
            and args.paths[1].lower().endswith(EXTS)
            and not os.path.exists(args.paths[1])):
        single_out = args.paths[1]
        args.paths = args.paths[:1]

    if args.out_dir:
        os.makedirs(args.out_dir, exist_ok=True)

    files = collect(args.paths, args.recursive)
    if not files:
        print('nenhum arquivo html encontrado.')
        return 0

    broken = fine = failed = 0
    for src in files:
        label = short(src)
        try:
            with open(src, encoding='utf-8') as fh:
                html = fh.read()
        except (OSError, UnicodeDecodeError) as err:
            print('  !! %s: nao consegui ler (%s)' % (label, err))
            failed += 1
            continue

        out, n_attrs, recovered = fix(html)

        if n_attrs == 0:
            fine += 1
            if not args.quiet:
                print('  ok %s: integro' % label)
            continue

        broken += 1
        detail = '%d atributo(s), %d caracteres de JSON recuperados' % (
            n_attrs, recovered)

        if args.dry_run:
            print('  ** %s: QUEBRADO -- %s' % (label, detail))
            continue

        dst = destination(src, args, single_out)
        try:
            if args.in_place and not args.no_backup:
                shutil.copy2(src, src + '.bak')
            with open(dst, 'w', encoding='utf-8') as fh:
                fh.write(out)
        except OSError as err:
            print('  !! %s: nao consegui gravar (%s)' % (label, err))
            failed += 1
            broken -= 1
            continue
        print('  -> %s: %s  [%s]' % (label, detail, short(dst)))

    print('\n%d arquivo(s): %d quebrado(s), %d ja integro(s)%s'
          % (len(files), broken, fine,
             ', %d com erro' % failed if failed else ''))

    if failed:
        return 1
    if args.dry_run and broken:
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
