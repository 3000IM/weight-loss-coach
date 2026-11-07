#!/usr/bin/env python3
"""手势花活的启动器：读你的减肥数据，在本机开网页，用 Chrome 打开。

  python3 showcase/show.py               读 ~/weight-loss-data，数据不够就用示例
  python3 showcase/show.py --data 路径    换数据目录
  python3 showcase/show.py --sample      强制用示例数据（录屏不想露真实体重时）
  python3 showcase/show.py --no-open     只开服务，地址自己贴到浏览器

网页服务只听本机（127.0.0.1），数据每次刷新现读，不写任何文件。
"""
import argparse
import functools
import http.server
import json
import sys
import tempfile
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from coach import DEFAULT_DATA, load_log, load_plan, num, stats  # noqa: E402

PORT = 8765


def payload(data):
    """页面要的数据。计划没写全或体重不到 3 条画不出曲线，返回 None，页面就用示例。"""
    plan, log = load_plan(data), load_log(data)
    weighed = [r for r in log if r["weight"] is not None]
    start, goal = num((plan or {}).get("起始体重 kg")), num((plan or {}).get("目标体重 kg"))
    if not (plan and start and goal and start > goal and len(weighed) >= 3):
        return None
    s = stats(data, weighed[-1]["date"])
    return {
        "label": "SAMPLE DATA 示例数据" if "示例" in plan.get("数据来源", "") else "YOUR DATA 你的数据",
        "log": [[r["date"].isoformat(), r["weight"], r["waist"], r["note"]] for r in log],
        "plan": {
            "start": start,
            "goal": goal,
            "waist0": num(plan.get("起始腰围 cm")),
            "rate": num(plan.get("每周该掉 kg")),
            "kcal": num(plan.get("每天热量 kcal")),
            "protein": plan.get("每天蛋白 g", ""),
            "lineDay": plan.get("计划线起点日期"),
            "lineKg": num(plan.get("计划线起点体重 kg")),
            "week": s.get("week_no") or len(weighed) // 7 + 1,
            "pace": s.get("pace"),
        },
    }


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] != "/data.js":
            return super().do_GET()
        d = None if self.server.data is None else payload(self.server.data)
        body = (f"window.COACH_DATA={json.dumps(d, ensure_ascii=False)};" if d else "").encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def open_page(url):
    print(f"已开好：{url}\n没自动打开就把这个地址贴到 Chrome。关掉这个窗口或按 Ctrl+C 结束。", flush=True)
    try:
        if webbrowser.get("chrome").open(url):
            return
    except webbrowser.Error:
        pass
    webbrowser.open(url)


def selftest():
    ex = payload(HERE.parent / "examples" / "three-weeks")
    assert ex and ex["label"].startswith("SAMPLE") and len(ex["log"]) == 19 and ex["plan"]["goal"] == 75 and ex["plan"]["pace"], ex
    with tempfile.TemporaryDirectory() as tmp:
        assert payload(tmp) is None, "空目录要退回示例"
        (Path(tmp) / "plan.md").write_text("- 起始体重 kg：90\n- 目标体重 kg：80\n", encoding="utf-8")
        (Path(tmp) / "log.md").write_text("| 2026-01-01 | 90 |  |  |\n| 2026-01-02 | 89.5 |  |  |\n", encoding="utf-8")
        assert payload(tmp) is None, "体重不到 3 条要退回示例"
        with open(Path(tmp) / "log.md", "a", encoding="utf-8") as f:
            f.write("| 2026-01-03 | 89.2 |  | 备注 |\n")
        mine = payload(tmp)
        assert mine["label"].startswith("YOUR") and mine["plan"]["rate"] is None and mine["log"][2][3] == "备注", mine
    print("show.py selftest ok")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA, help="数据目录，默认 ~/weight-loss-data")
    ap.add_argument("--sample", action="store_true", help="用示例数据")
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()

    url = f"http://127.0.0.1:{args.port}/"
    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), functools.partial(Handler, directory=str(HERE)))
    except OSError:  # 上次开的还在跑，直接用
        return open_page(url) if not args.no_open else print(url)
    server.data = None if args.sample else args.data
    if args.no_open:
        print(url, flush=True)
    else:
        open_page(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
