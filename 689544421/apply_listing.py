# -*- coding: utf-8 -*-
"""
把文案和截图套进 Partner Center 导出的 Store 一览 CSV，生成可以直接导入的文件夹。

用法：
    python apply_listing.py <导出的 CSV> <内容 JSON> [输出文件夹]

    输出文件夹默认是 ./ListingImport。导入时在 Partner Center 选择这个文件夹，
    文件夹名会自动作为图片路径的前缀（Partner Center 要求路径从所选文件夹名开始写）。

内容 JSON 的格式见 listing-template.json：
    - 键是 CSV 第一列的字段名（Description、ReleaseNotes、ShortTitle ……）
    - 值为字符串：直接写入该字段
    - 值为列表：展开成带编号的字段，比如 "Feature": [...] 写入 Feature1、Feature2 ……，
      多出来的编号会被清空
    - 图片字段（导出文件里类型为“相对路径或 URL”的行）可以写本地图片路径（相对 JSON 文件），
      脚本会把图片复制进输出文件夹并改写成正确的相对路径；写 http 开头的网址则保持原样
    - JSON 里没写的字段保持导出时的原值

只依赖 Python 3 标准库；装了 Pillow 时会额外检查 Xbox 截图尺寸。
"""
import csv
import hashlib
import io
import json
import os
import re
import shutil
import sys

# Partner Center 网页表单上的长度上限（maxlength），超过会导入失败
LIMITS = {
    'Description': 10000,
    'ReleaseNotes': 1500,
    'ShortTitle': 50,
    'VoiceTitle': 255,
    'ShortDescription': 1000,
    'DevStudio': 255,
    'CopyrightTrademarkInformation': 200,
    'AdditionalLicenseTerms': 10000,
}
PREFIX_LIMITS = {'Feature': 200}
SEARCH_TERM_MAX_COUNT = 7
SEARCH_TERM_MAX_WORDS = 21
XBOX_SCREENSHOT_SIZES = {(1920, 1080), (3840, 2160)}


def read_csv(path):
    raw = open(path, 'rb').read()
    text = raw.decode('utf-8-sig')
    rows = list(csv.reader(io.StringIO(text, newline='')))
    return rows, raw.startswith(b'\xef\xbb\xbf'), raw.endswith(b'\n')


def write_csv(path, rows, bom, trailing_newline):
    # 保持和导出文件一致：UTF-8（带 BOM）、CRLF、最少引号，末尾是否换行也一致
    buf = io.StringIO()
    csv.writer(buf, lineterminator='\r\n', quoting=csv.QUOTE_MINIMAL).writerows(rows)
    out = buf.getvalue()
    if not trailing_newline:
        out = out[:-2]
    with open(path, 'w', encoding='utf-8-sig' if bom else 'utf-8', newline='') as f:
        f.write(out)


def main():
    # 输出里有中文，控制台编码不支持的字符用 ? 代替，避免在 GBK 控制台下报错退出
    sys.stdout.reconfigure(errors='replace')
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    export_path, content_path = sys.argv[1], sys.argv[2]
    out_dir = os.path.abspath(sys.argv[3] if len(sys.argv) > 3 else 'ListingImport')
    prefix = os.path.basename(out_dir) + '/'
    content_dir = os.path.dirname(os.path.abspath(content_path))

    rows, bom, trailing_newline = read_csv(export_path)
    header = rows[0]
    by_field = {r[0]: r for r in rows[1:] if r and r[0]}
    # 类型列是本地化的（中文导出是“文本”，英文是“Text”），用已知字段推断，不写死文字
    text_type = by_field['Description'][2]
    bool_types = {r[2] for r in rows[1:] if r[2] and 'True' in r[2]}
    is_path_field = {name: r[2] not in ('', text_type) and r[2] not in bool_types for name, r in by_field.items()}

    content = json.load(open(content_path, encoding='utf-8'))
    os.makedirs(out_dir, exist_ok=True)
    copied = {}  # 文件内容哈希 -> 输出文件名，同一张图只复制一次
    errors, warnings, changed = [], [], set()

    def place_image(lang, field, value):
        if not value or value.startswith(('http://', 'https://')):
            return value
        src = os.path.join(content_dir, value)
        if not os.path.isfile(src):
            errors.append(f'{lang} {field}: 找不到图片 {src}')
            return value
        data = open(src, 'rb').read()
        digest = hashlib.sha1(data).hexdigest()
        if digest not in copied:
            # 用“上级目录-文件名”命名，避免不同语言的 1.png 互相覆盖
            parent = os.path.basename(os.path.dirname(src))
            name = f'{parent}-{os.path.basename(src)}' if parent else os.path.basename(src)
            while name in copied.values():
                name = digest[:6] + '-' + name
            shutil.copyfile(src, os.path.join(out_dir, name))
            copied[digest] = name
            if re.match(r'XboxScreenshot\d+$', field):
                check_xbox_size(lang, field, src)
        return prefix + copied[digest]

    def check_xbox_size(lang, field, src):
        try:
            from PIL import Image
        except ImportError:
            return
        size = Image.open(src).size
        if size not in XBOX_SCREENSHOT_SIZES:
            errors.append(f'{lang} {field}: Xbox 截图必须是 1920×1080 或 3840×2160，当前 {size[0]}×{size[1]}')

    def set_field(lang, field, value):
        row = by_field.get(field)
        if row is None:
            errors.append(f'{lang}: CSV 里没有字段 {field}')
            return
        col = header.index(lang)
        if is_path_field.get(field):
            value = place_image(lang, field, value)
        if row[col] != value:
            row[col] = value
            changed.add(field)

    for lang, fields in content.items():
        if lang.startswith('_'):
            continue  # 以下划线开头的键是注释
        if lang not in header:
            errors.append(f'导出文件里没有语言列 {lang}（先在 Partner Center 添加该语言再导出）')
            continue
        for key, value in fields.items():
            if key.startswith('_'):
                continue
            if isinstance(value, list):
                numbered = sorted((int(m.group(1)), name) for name in by_field
                                  for m in [re.fullmatch(re.escape(key) + r'(\d+)', name)] if m)
                if len(value) > len(numbered):
                    errors.append(f'{lang} {key}: 最多 {len(numbered)} 项，给了 {len(value)} 项')
                for n, name in numbered:
                    set_field(lang, name, value[n - 1] if n <= len(value) else '')
            else:
                set_field(lang, key, value)

    # 校验长度
    for lang in header[4:]:
        col = header.index(lang)
        for field, row in by_field.items():
            value = row[col]
            limit = LIMITS.get(field) or next((v for p, v in PREFIX_LIMITS.items() if re.fullmatch(p + r'\d+', field)), None)
            if limit and len(value) > limit:
                errors.append(f'{lang} {field}: {len(value)} 字符，超过上限 {limit}')
        terms = [by_field[f'SearchTerm{i}'][col] for i in range(1, 8) if f'SearchTerm{i}' in by_field]
        terms = [t for t in terms if t]
        words = sum(len(t.split()) for t in terms)
        if len(terms) > SEARCH_TERM_MAX_COUNT or words > SEARCH_TERM_MAX_WORDS:
            warnings.append(f'{lang} 搜索关键字：{len(terms)} 个、共 {words} 个词，建议不超过 {SEARCH_TERM_MAX_COUNT} 个 / {SEARCH_TERM_MAX_WORDS} 个词')

    for w in warnings:
        print('[警告]', w)
    if errors:
        for e in errors:
            print('[错误]', e)
        print('有错误，没有生成 CSV，修正后重新运行')
        sys.exit(1)

    # 文件名保持导出时的原名（包含提交 ID）
    out_csv = os.path.join(out_dir, os.path.basename(export_path))
    write_csv(out_csv, rows, bom, trailing_newline)
    print(f'输出文件夹：{out_dir}')
    print(f'图片路径前缀：{prefix}（导入时必须选择这个文件夹，文件夹名不要改）')
    print(f'复制图片 {len(copied)} 张；修改字段 {len(changed)} 个')


if __name__ == '__main__':
    main()
