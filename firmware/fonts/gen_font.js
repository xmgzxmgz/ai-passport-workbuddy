#!/usr/bin/env node
/**
 * 用 lv_font_conv 从思源黑体生成 WorkBuddy 中文字库。
 *
 * 关键点：走 lib/cli.js 的 run(argv) 编程接口，不经过 shell ——
 * 6992 个字符若用命令行传参会撞上 cmd.exe 的 8191 字符上限。
 *
 *   ASCII 用 -r 0x20-0x7E 覆盖（避免 --symbols 以 '-' 开头被误判为选项）
 *   非 ASCII 用 --symbols= 传（开头是汉字，安全）
 */
'use strict';

const fs = require('fs');
const path = require('path');

// lv_font_conv 的定位顺序：环境变量 LV_FONT_CONV -> 常规 require 解析。
function loadCli() {
  if (process.env.LV_FONT_CONV) {
    return require(process.env.LV_FONT_CONV);
  }
  try {
    return require('lv_font_conv/lib/cli.js');
  } catch (e) {
    throw new Error(
      '找不到 lv_font_conv。请先 `npm i lv_font_conv`，' +
      '或用 LV_FONT_CONV=<path/to/lv_font_conv/lib/cli.js> 指定。'
    );
  }
}
const cli = loadCli();

// 本脚本位于 <engine>/fonts/，引擎根即上一级
const ROOT = path.resolve(__dirname, '..');
const OTF = path.join(ROOT, 'managed_components/lvgl__lvgl/scripts/built_in_font/SourceHanSansSC-Normal.otf');
const SYMBOLS_TXT = path.join(ROOT, 'fonts/symbols.txt');
const OUT_DIR = path.join(ROOT, 'main/fonts');

// sizes: [px, 变量名, bpp]
const TARGETS = [
  [16, 'lv_font_wb_16', 4],
  [14, 'lv_font_wb_14', 4],
];

async function main() {
  if (!fs.existsSync(OTF)) {
    throw new Error('源字体缺失: ' + OTF);
  }
  if (!fs.existsSync(SYMBOLS_TXT)) {
    throw new Error('字符集缺失，请先跑 gen_charset.py: ' + SYMBOLS_TXT);
  }

  const all = fs.readFileSync(SYMBOLS_TXT, 'utf8');
  // 拆成 ASCII / 非 ASCII
  const ascii = [...all].filter(c => c.codePointAt(0) >= 0x20 && c.codePointAt(0) <= 0x7E);
  const wide = [...all].filter(c => c.codePointAt(0) > 0x7E);

  console.log(`字符集: 总 ${[...all].length}  ASCII ${ascii.length}  非ASCII ${wide.length}`);
  console.log(`源字体: ${(fs.statSync(OTF).size / 1048576).toFixed(1)} MB`);
  console.log('');

  fs.mkdirSync(OUT_DIR, { recursive: true });

  for (const [size, varname, bpp] of TARGETS) {
    const out = path.join(OUT_DIR, varname + '.c');
    const t0 = Date.now();
    console.log(`[${size}px ${bpp}bpp] 生成中 -> ${path.basename(out)}`);

    await cli.run([
      '--font', OTF,
      '--size', String(size),
      '--bpp', String(bpp),
      '--format', 'lvgl',
      '--no-compress',
      '--no-prefilter',
      '--force-fast-kern-format',
      '--lv-font-name', varname,
      // 项目里一律 #include "lvgl.h"（simple include），
      // 不指定的话生成的是 "lvgl/lvgl.h"，ESP-IDF 的 include 路径找不到。
      '--lv-include', 'lvgl.h',
      '-r', '0x20-0x7E',
      '--symbols=' + wide.join(''),
      '-o', out,
    ]);

    const kb = (fs.statSync(out).size / 1024).toFixed(0);
    console.log(`          完成 ${kb} KB 源码，耗时 ${((Date.now() - t0) / 1000).toFixed(1)}s\n`);
  }

  console.log('全部完成。产物:');
  for (const f of fs.readdirSync(OUT_DIR)) {
    console.log('  main/fonts/' + f,
      ((fs.statSync(path.join(OUT_DIR, f)).size / 1024 / 1024)).toFixed(2) + ' MB');
  }
}

main().catch(e => { console.error('生成失败:', e.message); process.exit(1); });
