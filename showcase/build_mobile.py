#!/usr/bin/env python3
"""从 index.html 生成手机版 mobile.html。逻辑只有 index.html 一份，这里只加手机外壳和内嵌模型，别手改 mobile.html。

  python3 showcase/build_mobile.py           生成（第一次要联网下载手势模型，约 8 MB）
  python3 showcase/build_mobile.py --check   只核对 mobile.html 有没有过期
"""
import base64
import re
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "mobile.html"
MODEL_CACHE = HERE.parent / ".tmp" / "hand_landmarker.task"
MODEL_URL = "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task"
MODEL_SIZE = 7819105
MAX_MB = 16
CDN_OK = ("https://cdn.jsdelivr.net/npm/", "https://unpkg.com/", "https://cdnjs.cloudflare.com/")
DATA_TAG = '<script src="data.js"></script><!-- show.py 现读你的数据；直接开文件时没有它，用示例数据 -->\n'
MODEL_LINE = f"const MODEL='{MODEL_URL}';\n"
MODEL_RE = re.compile(r'<script type="text/plain" id="model">(.*?)</script>', re.S)

HEAD = """<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<style>
  html,body{touch-action:none;overscroll-behavior:none;-webkit-user-select:none;user-select:none}
  #gl{mix-blend-mode:normal}
  [hidden]{display:none!important}
  #bar{position:fixed;top:8px;right:8px;display:flex;gap:6px;z-index:5}
  #bar button,#tip button{font:600 13px "PingFang SC",sans-serif;color:#eaf8ff;background:rgba(6,16,28,.72);border:1px solid rgba(120,220,255,.55);border-radius:10px;min-width:44px;height:44px;padding:0 6px}
  #bAct{position:absolute;top:50px;right:0}
  #boot{position:fixed;left:0;right:0;top:42%;padding:0 24px;text-align:center;font:600 16px "PingFang SC",sans-serif;color:#bfe6ff;z-index:4;pointer-events:none}
  #tip{position:fixed;left:16px;right:16px;top:50%;transform:translateY(-50%);max-width:460px;margin:auto;padding:18px;background:rgba(6,16,28,.92);border:1px solid #ffb02e;border-radius:14px;z-index:6;text-align:center}
  #tip p{margin:0 0 14px;font:700 17px/1.5 "PingFang SC",sans-serif;color:#ffb02e}
  #tip button{margin:4px;width:calc(100% - 8px)}
</style>
"""

SHELL = """<div id="bar"><button id="bAct" hidden>召唤</button><button id="bFlip">前后</button><button id="bFull">全屏</button><button id="bHide">文字</button></div>
<div id="boot">加载中，第一次要等十几秒。画面只在这台手机上处理，不上传</div>
<div id="tip" hidden><p id="tipText"></p><button id="bRetry">再试一次</button><button id="bDemo">不用摄像头，先看效果</button><button id="bSave">另存这个文件（用电脑 Chrome 打开）</button></div>
<script>window.COACH_MOBILE=1;setTimeout(function(){if(!window.S)document.getElementById('boot').textContent='页面没加载起来：网络被拦，或系统太旧（要 iOS 16.4、Chrome 89 以上）。用 Safari / Chrome 直接打开再试'},30000)</script>
"""


def model_b64():
    if not MODEL_CACHE.exists():
        MODEL_CACHE.parent.mkdir(exist_ok=True)
        with urllib.request.urlopen(MODEL_URL, timeout=120) as r:
            MODEL_CACHE.write_bytes(r.read())
    raw = MODEL_CACHE.read_bytes()
    assert len(raw) == MODEL_SIZE, f"模型大小不对：{len(raw)}"
    return base64.b64encode(raw).decode()


def render(b64):
    html = (HERE / "index.html").read_text(encoding="utf-8")
    for old, new in [
        ("</title>\n", "</title>\n" + HEAD),
        (DATA_TAG, SHELL),
        (MODEL_LINE, "const MODEL='';\n"),
        ("</body>", f'<script type="text/plain" id="model">{b64}</script>\n</body>'),
    ]:
        assert html.count(old) == 1, f"index.html 里找不到或不止一处：{old[:40]}"
        html = html.replace(old, new)
    return html


def problems(html):
    """托管平台只放行三家 CDN，版本要写死；也不许带上任何本机数据。"""
    body = MODEL_RE.sub("", html)
    urls = set(re.findall(r"https?://[^\s'\"`)]+", body))
    bad = [f"外链不在白名单：{u}" for u in urls if not u.startswith(CDN_OK)]
    bad += [f"外链没写死版本：{u}" for u in urls if not re.search(r"@\d+\.\d+\.\d+", u)]
    if "/Users/" in body or "COACH_DATA=" in body or "data.js" in body:
        bad.append("混进了本机路径或用户数据")
    if len(html.encode()) > MAX_MB * 1024 * 1024:
        bad.append(f"超过 {MAX_MB} MB")
    return bad


def fresh():
    """mobile.html 是不是还对得上现在的 index.html（模型直接用文件里那份，不联网）。"""
    if not OUT.exists():
        return False
    old = OUT.read_text(encoding="utf-8")
    m = MODEL_RE.search(old)
    return bool(m) and render(m.group(1)) == old


def main():
    if "--check" in sys.argv:
        ok = fresh()
        print("mobile.html 是最新的" if ok else "mobile.html 过期了，跑一遍 build_mobile.py")
        return 0 if ok else 1
    html = render(model_b64())
    bad = problems(html)
    assert not bad, bad
    OUT.write_text(html, encoding="utf-8")
    print(f"{OUT}  {OUT.stat().st_size / 1024 / 1024:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
