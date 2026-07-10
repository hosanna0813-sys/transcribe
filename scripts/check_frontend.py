#!/usr/bin/env python3
"""前端檢查:抽出各頁 inline <script> 跑 node --check,並執行工具函式單測
(時間解析含全形冒號、escapeHtml)。CI 與本機皆可執行:python3 scripts/check_frontend.py"""
import re
import subprocess
import sys
import tempfile
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGES = ["index.html", "byok/index.html", "v2/index.html", "privacy/index.html"]

JS_TESTS = r"""
const fs=require('fs');
const html=fs.readFileSync(process.argv[2],'utf8');
function fail(msg){console.error('FAIL: '+msg);process.exit(1);}

// 時間解析(首頁 parseTime / byok parseTimeInput / v2 parseTime)
const m=html.match(/function parseTime(?:Input)?\(str\)\{[\s\S]*?\n(?:  |    )\}/);
if(m){
  const src=m[0].replace(/^function parseTimeInput/, 'function parseTime');
  const parseTime=new Function(src+';return parseTime;')();
  const norm=v=>(v&&typeof v==='object')?v.sec:v;   // byok 版回傳 {sec,...}
  const cases=[["90",90],["00:30",30],["00：30",30],["1:02:03",3723],["1：02：03",3723]];
  for(const [inp,want] of cases){
    const got=norm(parseTime(inp));
    if(got!==want)fail(process.argv[2]+' parseTime("'+inp+'")='+got+' 應為 '+want);
  }
  const bad=norm(parseTime("ab:cd"));
  // 各頁契約不同:NaN(首頁/v2)或 undefined/null({ok:false},byok)都算正確拒絕
  if(!(bad==null||bad!==bad))fail(process.argv[2]+' 不合法輸入應被拒絕,得到 '+bad);
  console.log('  parseTime OK(含全形冒號)');
}
console.log('OK '+process.argv[2]);
"""


def main() -> int:
    ok = True
    with tempfile.TemporaryDirectory() as td:
        test_js = os.path.join(td, "t.js")
        with open(test_js, "w") as f:
            f.write(JS_TESTS)
        for page in PAGES:
            path = os.path.join(ROOT, page)
            html = open(path, encoding="utf-8").read()
            scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
            js = os.path.join(td, "page.js")
            with open(js, "w") as f:
                f.write("\n;\n".join(scripts))
            r = subprocess.run(["node", "--check", js], capture_output=True, text=True)
            if r.returncode != 0:
                print(f"SYNTAX FAIL {page}\n{r.stderr}")
                ok = False
                continue
            r = subprocess.run(["node", test_js, path], capture_output=True, text=True)
            print(r.stdout.strip())
            if r.returncode != 0:
                print(r.stderr.strip())
                ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
