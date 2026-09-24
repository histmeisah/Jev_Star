"""Publish a compiled paper PDF and regenerate its GitHub page reader."""

import argparse
import hashlib
import json
import shutil
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pdf', type=Path, required=True)
    parser.add_argument('--date', required=True, help='Edition date, YYYY-MM-DD')
    parser.add_argument('--dpi', type=int, default=144)
    args = parser.parse_args()
    paper = ROOT / 'paper'
    target = paper / 'JEV-Star.pdf'
    if args.pdf.resolve() != target.resolve():
        shutil.copyfile(args.pdf, target)
    doc = pymupdf.open(target)
    pages = paper / 'pages'
    pages.mkdir(exist_ok=True)
    rendered = []
    for number, page in enumerate(doc, 1):
        image_path = pages / f'page-{number:02d}.png'
        pixmap = page.get_pixmap(dpi=args.dpi)
        pixmap.save(image_path)
        rendered.append({'page': number, 'file': image_path.relative_to(paper).as_posix(),
                         'width': pixmap.width, 'height': pixmap.height,
                         'bytes': image_path.stat().st_size, 'sha256': digest(image_path)})
    # Only obsolete generated page images in this exact output directory.
    expected = {Path(row['file']).name for row in rendered}
    for old in pages.glob('page-*.png'):
        if old.name not in expected:
            old.unlink()
    version = {'filename': target.name, 'updated': args.date, 'bytes': target.stat().st_size,
               'pages': len(doc), 'sha256': digest(target),
               'provenance': 'Compiled from the accompanying LaTeX source and figure assets.',
               'latex_source': 'main.tex', 'latex_source_sha256': digest(paper/'main.tex'),
               'online_reader': 'PAPER.md', 'page_render_dpi': args.dpi,
               'rendered_pages': rendered}
    (paper/'pdf-version.json').write_text(json.dumps(version,indent=2)+'\n',encoding='utf-8')
    reader = [
        '# JEV-Star — Read the paper / 在线阅读论文', '',
        '**JEV-Star: Fast, Low-Cost StarCraft II Control with Language-Model Planning**', '',
        'Weiyu Ma · Liangbing Zhao · Yongcheng Zeng · Jian Zhao', '',
        f'{args.date} edition · {len(doc)} pages · five macro configurations and realtime Lv7 evidence.', '',
        f'{args.date} 版，共 {len(doc)} 页。新增五种宏观配置、实时 Lv7 结果及逐请求时序证据。', '',
        '[PDF](JEV-Star.pdf) · [Source and data / 源码与数据](README.md) · '
        '[Configuration guide / 配置说明](../docs/macro-configurations.md) · '
        '[Citation / 引用](../README.md#citation)', '',
        'This PDF is compiled from the accompanying source. Pages below are rendered directly from it.', '',
        'PDF 与随附源码同步，下方页面直接由本版 PDF 渲染。', '', '---', '',
    ]
    for row in rendered:
        reader.extend([f"### Page {row['page']} / 第 {row['page']} 页", '',
                       f"![JEV-Star paper, page {row['page']}]({row['file']})", ''])
    (paper/'PAPER.md').write_text('\n'.join(reader),encoding='utf-8')
    print(json.dumps({'pages':len(doc),'pdf':str(target),'sha256':digest(target)}))


if __name__ == '__main__':
    main()
