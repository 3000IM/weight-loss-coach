#!/usr/bin/env python3
"""减肥教练的算账部分：定计划、算统计、出看板、发提醒。

只用标准库，Python 3.9+，Mac 和 Windows 都能跑。子命令：
  init        建数据目录，放好空模板（已有的文件不动）
  calc        按身高体重等算每天热量、蛋白、每周该掉多少、预计几周到
  stats       读数据，输出 7 天平均、本周变化、和计划比、解锁进度（JSON）
  import      补录几天的「日期 体重 腰围 备注」，文件或标准输入
  dashboard   记满 7 天后把数据渲成本地网页 dashboard.html
  remind      今天没记体重就弹一条系统通知（定时任务跑的就是它）
  reminder    on HH:MM / off / status，管每天定时提醒
  --selfcheck 跑自检
"""
import argparse
import json
import math
import os
import plistlib
import re
import shutil
import statistics
import subprocess
import sys
import webbrowser
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_DATA = Path.home() / "weight-loss-data"
MAC_LABEL = "com.weightloss.reminder"
WIN_TASK = "WeightLossReminder"
KCAL_PER_KG = 7700
UNLOCK_DAYS = 7  # 记满几天才解锁看板


def read(path):
    # utf-8-sig：Windows 上的编辑器可能写入 BOM
    return Path(path).read_text(encoding="utf-8-sig")


def num(text):
    m = re.search(r"-?\d+(?:\.\d+)?", str(text or ""))
    return float(m.group()) if m else None


def day(text):
    m = re.search(r"\d{4}-\d{2}-\d{2}", str(text or ""))
    return date.fromisoformat(m.group()) if m else None


def calc(sex, age, height, weight, target, by=None, activity=1.375, today=None):
    """Mifflin-St Jeor 算静息代谢，乘活动系数得维持热量，再按每周该掉多少扣缺口。"""
    today = today or date.today()
    bmi = lambda kg: kg / (height / 100) ** 2
    out = {"bmi_now": round(bmi(weight), 1), "bmi_target": round(bmi(target), 1), "warnings": []}
    if weight <= target:
        return {**out, "ok": False, "stop": "目标体重不低于现在的体重，不需要减重计划"}
    if bmi(target) < 18.5:
        low = math.ceil(18.5 * (height / 100) ** 2)
        return {**out, "ok": False, "stop": f"目标体重的 BMI 低于 18.5（偏瘦线）。这个身高的偏瘦线约 {low} kg，不按这个目标做计划，想减到这么低先问医生"}

    bmr = 10 * weight + 6.25 * height - 5 * age + (5 if sex == "m" else -161)
    maintenance = bmr * activity
    floor = 1500 if sex == "m" else 1200
    cap = min(1.0, weight * 0.01)  # 每周最多掉 1 kg 或体重的 1%，取小的

    days = (by - today).days if by else None
    if by is None:
        wanted = weight * 0.005
    else:
        wanted = (weight - target) / (days / 7) if days > 0 else float("inf")
    rate = min(wanted, cap)
    if wanted > cap:
        out["warnings"].append("date_too_tight")

    kcal = maintenance - rate * KCAL_PER_KG / 7
    if kcal < floor:
        kcal = floor
        rate = (maintenance - floor) * 7 / KCAL_PER_KG
        out["warnings"].append("hit_kcal_floor")
    if rate < 0.05:
        return {**out, "ok": False, "stop": "维持热量已经接近安全下限，靠少吃做不出缺口，先增加日常活动量或问医生、营养师"}

    days_needed = round((weight - target) / rate * 7)
    shown_maintenance, shown_kcal = round(maintenance / 50) * 50, max(round(kcal / 50) * 50, floor)
    return {
        **out,
        "ok": True,
        "bmr": round(bmr),
        "maintenance_kcal": shown_maintenance,
        "kcal": shown_kcal,
        "deficit_kcal": shown_maintenance - shown_kcal,
        "kcal_floor": floor,
        "protein_low": round(target * 1.6),
        "protein_high": round(target * 2.0),
        "weekly_loss_kg": round(rate, 2),
        "weeks": max(1, round(days_needed / 7)),
        "eta": (today + timedelta(days=days_needed)).isoformat(),
    }


def load_plan(data):
    """plan.md 里形如「- 标签：值」的行。「本周计划」一节原样留给看板。"""
    path = Path(data) / "plan.md"
    fields, week, section = {}, [], ""
    if not path.exists():
        return None
    for line in read(path).splitlines():
        if line.startswith("#"):
            section = line.lstrip("# ").strip()
            continue
        m = re.match(r"\s*-\s*([^：:]+)[：:]\s*(.*)", line)
        if not m:
            continue
        key, value = m.group(1).strip(), m.group(2).strip()
        if section.startswith("本周计划"):
            week.append([key, value])
        else:
            fields.setdefault(key, value)
    fields["_week"] = week
    return fields


def load_log(data):
    """log.md 的表格行。同一天写了两行以后一行为准，按日期排序。"""
    path = Path(data) / "log.md"
    rows = {}
    if not path.exists():
        return []
    for line in read(path).splitlines():
        m = re.match(r"^\|\s*(\d{4}-\d{2}-\d{2})\s*\|(.*)\|\s*$", line)
        if not m:
            continue
        cells = [c.strip() for c in m.group(2).split("|")]
        cells += [""] * (3 - len(cells))
        d = date.fromisoformat(m.group(1))
        rows[d] = {"date": d, "weight": num(cells[0]), "waist": num(cells[1]), "note": " | ".join(cells[2:]).strip()}
    return [rows[d] for d in sorted(rows)]


def import_log(data, text, to_today=False, today=None):
    """补录：每行「日期 体重 腰围 备注」，逗号或空格隔开。有一行不对就整批不写，免得账本进半截数据。"""
    today = today or date.today()
    plan, path = load_plan(data), Path(data) / "log.md"
    if not plan or not num(plan.get("目标体重 kg")) or not path.exists():
        sys.exit("还没定计划，先走第一次用的问答")
    new, bad = {}, []
    for n, line in enumerate(x for x in text.splitlines() if x.strip()):
        cells = re.split(r"[,，]", line, maxsplit=3) if re.search(r"[,，]", line) else line.split(None, 3)
        cells = [c.strip() for c in cells] + [""] * (4 - len(cells))
        try:
            weight = float(cells[1])
            if not 20 <= weight <= 400:
                raise ValueError
            d = date.fromisoformat(cells[0])
            new[d] = {"date": d, "weight": weight, "waist": float(cells[2]) if cells[2] else None, "note": cells[3]}
        except ValueError:
            if n or re.search(r"\d", cells[0]):  # 第一行第一格没有数字就是表头
                bad.append(line)
    if new and to_today:
        gap = today - max(new)
        new = {d + gap: {**r, "date": d + gap} for d, r in new.items()}
    bad += [f"{d}（未来日期）" for d in new if d > today]
    if bad or not new:
        sys.exit("没有写入。这几行有问题，要写成「YYYY-MM-DD 体重kg 腰围 备注」：\n" + "\n".join(bad))

    merged = {r["date"]: r for r in load_log(data)}
    for d, r in new.items():
        old = merged.get(d, {})  # 这次没报的腰围和备注不能冲掉已有的
        merged[d] = {**r, "waist": r["waist"] if r["waist"] is not None else old.get("waist"), "note": r["note"] or old.get("note", "")}
    head = [x for x in read(path).splitlines() if not re.match(r"^\|\s*\d{4}-\d{2}-\d{2}\s*\|", x)]
    cell = lambda v: "" if v is None else v
    rows = [f"| {d} | {cell(r['weight'])} | {cell(r['waist'])} | {r['note']} |" for d, r in sorted(merged.items())]
    path.write_text("\n".join(["\n".join(head).rstrip()] + rows) + "\n", encoding="utf-8")
    return len(new)


def window_mean(rows, end, key="weight", min_n=3):
    vals = [r[key] for r in rows if r[key] is not None and end - timedelta(days=6) <= r["date"] <= end]
    return round(statistics.mean(vals), 2) if len(vals) >= min_n else None


def window_last(rows, end, key="waist"):
    vals = [r[key] for r in rows if r[key] is not None and end - timedelta(days=6) <= r["date"] <= end]
    return vals[-1] if vals else None


def plan_at(plan, d):
    """计划线在 d 这天的值。7 天平均滞后约 3 天，这里同样后移 3 天，两条线才可比。"""
    start, weight, rate = day(plan.get("计划线起点日期")), num(plan.get("计划线起点体重 kg")), num(plan.get("每周该掉 kg"))
    target = num(plan.get("目标体重 kg"))
    if not (start and weight is not None and rate and target is not None):
        return None
    return round(max(target, weight - rate * max(0, (d - start).days - 3) / 7), 2)


def pace_of(change, rate):
    if change is None or not rate:
        return None
    if -change > rate * 1.5:
        return "fast"
    return "slow" if -change < rate * 0.5 else "on_track"


def stats(data, as_of=None):
    plan = load_plan(data)
    if plan is None:
        return None
    today = as_of or date.today()
    rows = [r for r in load_log(data) if r["date"] <= today]
    rate = num(plan.get("每周该掉 kg"))
    target = num(plan.get("目标体重 kg"))
    start = num(plan.get("起始体重 kg"))
    next_review = day(plan.get("下次复盘"))
    days = sum(r["weight"] is not None for r in rows)
    out = {
        "as_of": today.isoformat(),
        "logged_today": any(r["date"] == today and r["weight"] is not None for r in rows),
        "target": target,
        "start_weight": start,
        "planned_change7": -rate if rate else None,
        "kcal": num(plan.get("每天热量 kcal")),
        "maintenance_kcal": num(plan.get("维持热量 kcal")),
        "protein": plan.get("每天蛋白 g", ""),
        "next_review": next_review.isoformat() if next_review else None,
        "review_due": bool(next_review and today >= next_review),
        "days_logged": days,
        "days_to_unlock": max(0, UNLOCK_DAYS - days),
        "unlocked": days >= UNLOCK_DAYS,
        "first_unlock": days >= UNLOCK_DAYS and not (Path(data) / "dashboard.html").exists(),  # 页面只在解锁后生成，没有就是还没开过
        "weeks": [],
    }
    weighed = [r for r in rows if r["weight"] is not None]
    if not weighed:
        return out

    last = weighed[-1]["date"]
    avg = window_mean(rows, last)

    def prior(end):
        # 上一个 7 天窗口没被记录完整盖住时不比：前两周只拿开头几天比，第一周的高点会把降幅算大
        return window_mean(rows, end - timedelta(days=7)) if end - timedelta(days=13) >= weighed[0]["date"] else None

    prev = prior(last)
    current = avg if avg is not None else weighed[-1]["weight"]
    waists = [r for r in rows if r["waist"] is not None]
    plan_now = plan_at(plan, last)
    first_waist = num(plan.get("起始腰围 cm"))
    if first_waist is None and waists:
        first_waist = waists[0]["waist"]

    # 从最新往回，一个 7 天窗口一行，复盘时看「连续几周慢」就靠它
    end = last
    while window_mean(rows, end) is not None:
        before = prior(end)
        waist, waist_before = window_last(rows, end), window_last(rows, end - timedelta(days=7))
        change = round(window_mean(rows, end) - before, 2) if before is not None else None
        out["weeks"].append({
            "end": end.isoformat(),
            "avg": window_mean(rows, end),
            "days_logged": sum(1 for r in weighed if end - timedelta(days=6) <= r["date"] <= end),
            "change": change,
            "pace": pace_of(change, rate),
            "waist": waist,
            "waist_change": round(waist - waist_before, 1) if waist is not None and waist_before is not None else None,
        })
        end -= timedelta(days=7)

    slow_weeks = 0
    for week in out["weeks"]:
        if week["pace"] != "slow":
            break
        slow_weeks += 1

    out.update({
        "last_date": last.isoformat(),
        "last_weight": weighed[-1]["weight"],
        "last_change": round(weighed[-1]["weight"] - weighed[-2]["weight"], 2) if len(weighed) > 1 else None,
        "days_logged_7": out["weeks"][0]["days_logged"] if out["weeks"] else len(weighed),
        "avg7": avg,
        "prev_avg7": prev,
        "change7": round(avg - prev, 2) if avg is not None and prev is not None else None,
        "pace": pace_of(round(avg - prev, 2), rate) if avg is not None and prev is not None else None,
        "slow_weeks": slow_weeks,
        "plan_weight": plan_now,
        "gap_to_plan": round(avg - plan_now, 2) if avg is not None and plan_now is not None else None,
        "change_from_start": round(current - start, 2) if start is not None else None,
        "to_go": round(current - target, 2) if target is not None else None,
        "progress_pct": round(max(0, min(100, (start - current) / (start - target) * 100))) if start and target and start > target else None,
        "waist": waists[-1]["waist"] if waists else None,
        "waist_change_from_start": round(waists[-1]["waist"] - first_waist, 1) if waists and first_waist is not None else None,
        "week_no": ((last - day(plan.get("起始日期"))).days // 7 + 1) if day(plan.get("起始日期")) else None,
    })
    return out


def build_dashboard(data, as_of=None):
    plan, rows = load_plan(data), load_log(data)
    ready = plan and num(plan.get("起始体重 kg")) and num(plan.get("目标体重 kg")) and any(r["weight"] is not None for r in rows)
    payload = {"empty": not ready}
    if not payload["empty"]:
        s = stats(data, as_of or max(r["date"] for r in rows))
        points = []
        for r in rows:
            points.append({
                "d": r["date"].isoformat(),
                "w": r["weight"],
                "a": window_mean(rows, r["date"], min_n=1),  # 曲线从第一天画起，统计数字仍要 3 天
                "p": plan_at(plan, r["date"]),
                "v": r["waist"],
                "n": r["note"],
            })
        payload.update({
            "example": "示例" in plan.get("数据来源", ""),
            "stats": s,
            "points": points,
            "kcal": plan.get("每天热量 kcal", ""),
            "protein": plan.get("每天蛋白 g", ""),
            "eta": plan.get("预计到达", ""),
            "week": plan["_week"],
            "split": plan.get("训练分化", ""),
        })
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    html = read(HERE / "templates" / "dashboard.html").replace("/*__DATA__*/null", blob)
    out = Path(data) / "dashboard.html"
    Path(data).mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out


def notify(title, body):
    if sys.platform == "darwin":
        cmd = ["osascript", "-e", f'display notification "{body}" with title "{title}"']
        extra = {}
    elif sys.platform == "win32":
        # ponytail: 用最老的弹窗，一小时没点自动关，所有 Windows 版本都有。要原生 toast 得加依赖
        cmd = ["powershell", "-NoProfile", "-Command", f"(New-Object -ComObject WScript.Shell).Popup('{body}', 3600, '{title}') | Out-Null"]
        extra = {"creationflags": subprocess.CREATE_NO_WINDOW}
    else:
        cmd = ["notify-send", title, body]
        extra = {}
    subprocess.run(cmd, timeout=3700, check=True, **extra)


def remind(data, force=False):
    s = stats(data)
    if s is None:
        return "还没有 plan.md，不提醒"
    parts = []
    if not s["logged_today"]:
        parts.append("今天还没记体重。称完对你的 Agent 说一句：体重 XX，腰围 YY")
    if s["review_due"]:
        parts.append("今天该做周复盘了，对 Agent 说一句「复盘」")
    if not parts and force:
        parts.append("提醒试一下：能看到这条就说明通知正常")
    if not parts:
        return "今天记过了，不提醒"
    notify("减肥教练", "。".join(parts))
    return "已提醒"


def run_quiet(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def reminder(action, at, data):
    """每天定时跑 remind。系统自带的定时器，不依赖任何 Agent 开着，也不花模型额度。"""
    hh, mm = 0, 0
    if action == "on":
        m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", at or "")
        if not m:
            sys.exit("时间要写成 HH:MM，比如 08:30")
        hh, mm = int(m.group(1)), int(m.group(2))
    script = str(HERE / "coach.py")

    if sys.platform == "darwin":
        domain, plist = f"gui/{os.getuid()}", Path.home() / "Library/LaunchAgents" / f"{MAC_LABEL}.plist"
        if action == "status":
            return "on" if run_quiet(["launchctl", "print", f"{domain}/{MAC_LABEL}"]).returncode == 0 else "off"
        run_quiet(["launchctl", "bootout", f"{domain}/{MAC_LABEL}"])
        if action == "off":
            if plist.exists():
                plist.unlink()
            return "off"
        plist.parent.mkdir(parents=True, exist_ok=True)
        py = shutil.which("python3") or sys.executable  # 定时任务的 PATH 很短，写绝对路径
        plist.write_bytes(plistlib.dumps({
            "Label": MAC_LABEL,
            "ProgramArguments": [py, script, "remind", "--data", str(data)],
            "StartCalendarInterval": {"Hour": hh, "Minute": mm},
        }))
        done = run_quiet(["launchctl", "bootstrap", domain, str(plist)])
        if done.returncode:
            sys.exit(f"装定时任务失败：{done.stderr.strip()}")
        return f"on {hh:02d}:{mm:02d}"

    if sys.platform == "win32":
        if action == "status":
            return "on" if run_quiet(["schtasks", "/Query", "/TN", WIN_TASK]).returncode == 0 else "off"
        if action == "off":
            run_quiet(["schtasks", "/Delete", "/F", "/TN", WIN_TASK])
            return "off"
        pyw = Path(sys.executable).with_name("pythonw.exe")  # pythonw 不闪黑框
        py = str(pyw) if pyw.exists() else sys.executable
        tr = subprocess.list2cmdline([py, script, "remind", "--data", str(data)])
        done = run_quiet(["schtasks", "/Create", "/F", "/SC", "DAILY", "/TN", WIN_TASK, "/ST", f"{hh:02d}:{mm:02d}", "/TR", tr])
        if done.returncode:
            sys.exit(f"装定时任务失败：{done.stderr.strip()}")
        return f"on {hh:02d}:{mm:02d}"

    # ponytail: Linux 只留弹通知的能力，定时靠 Agent 按 SKILL.md 写一条 crontab，有人要再做自动安装
    sys.exit("这个系统不支持自动安装，请用系统的定时工具每天跑 coach.py remind")


def selftest():
    import tempfile

    plan = calc("m", 30, 175, 82, 75, date(2027, 1, 31), 1.375, date(2026, 10, 12))
    assert plan["ok"] and plan["bmr"] == 1769 and plan["maintenance_kcal"] == 2450, plan
    assert plan["kcal"] == 1950 and plan["weekly_loss_kg"] == 0.44 and plan["weeks"] == 16, plan
    assert plan["protein_low"] == 120 and plan["protein_high"] == 150, plan
    dated = calc("m", 24, 175, 86, 78, date(2027, 2, 6), 1.375, date(2026, 11, 6))
    assert dated["eta"] == "2027-02-06" and dated["weeks"] == 13 and dated["deficit_kcal"] == 700, dated
    tight = calc("f", 28, 160, 60, 52, date(2026, 11, 1), 1.2, date(2026, 10, 12))
    assert "date_too_tight" in tight["warnings"] and tight["kcal"] >= 1200, tight
    assert tight["weekly_loss_kg"] <= 0.6, tight
    assert not calc("f", 28, 160, 50, 45, None, 1.2)["ok"], "低于 BMI 18.5 的目标要拒绝"
    assert not calc("m", 30, 175, 70, 75, None, 1.2)["ok"], "目标高于现在不做计划"

    example = HERE / "examples" / "three-weeks"
    s = stats(example, date(2026, 11, 1))
    assert s["days_logged_7"] == 6 and s["avg7"] == 80.65 and s["prev_avg7"] == 81.22, s
    assert s["change7"] == -0.57 and s["pace"] == "on_track" and s["slow_weeks"] == 0, s
    assert s["weeks"][1]["pace"] == "slow" and s["weeks"][1]["change"] == -0.17, s["weeks"]
    assert s["week_no"] == 3 and s["to_go"] == 5.65 and s["progress_pct"] == 19, s
    assert not s["review_due"] and stats(example, date(2026, 11, 8))["review_due"]
    assert plan_at(load_plan(example), date(2026, 10, 15)) == 82.0
    assert s["weeks"][1]["waist"] == 92.6 and s["weeks"][1]["waist_change"] == -0.6, s["weeks"]
    assert s["weeks"][2]["waist"] == 93.2 and s["change_from_start"] == -1.35, s
    assert stats(example, date(2026, 10, 22))["change7"] is None, "前两周上一窗口不完整，不比"
    assert [k for k, _ in load_plan(example)["_week"]] == ["力量训练", "每天步数", "这周重点"], "看板不列饮食建议"

    with tempfile.TemporaryDirectory() as tmp:
        assert "不提醒" in remind(tmp), "没有计划时不该提醒"
        html = build_dashboard(tmp).read_text(encoding="utf-8")
        assert '"empty": true' in html, "没数据时要出空状态页"
        shutil.copy(example / "plan.md", tmp)
        shutil.copy(example / "log.md", tmp)
        with open(Path(tmp) / "log.md", "a", encoding="utf-8") as f:
            f.write("| 2026-11-02 | 79.8 |  | </script><b>x |\n")
        html = build_dashboard(tmp).read_text(encoding="utf-8")
        assert "/*__DATA__*/" not in html and "</script><b>" not in html, "备注里的标签不能破坏页面"

    seven = read(HERE / "examples" / "seven-days.csv").splitlines()  # 表头加 7 天
    last = date(2026, 11, 6)
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copy(example / "plan.md", tmp)
        shutil.copy(HERE / "templates" / "log.md", tmp)
        log_before = read(Path(tmp) / "log.md")
        for wrong in ("2026-11-01 不是数字", "2026-11-01 86\n2026-13-01 86", "2027-01-01 86"):
            try:
                import_log(tmp, wrong, today=last)
                raise AssertionError(f"这批该被拒绝：{wrong}")
            except SystemExit:
                assert read(Path(tmp) / "log.md") == log_before, "有错的一批不能写进一半"
        assert import_log(tmp, "\n".join(seven[:7]), today=last) == 6
        s = stats(tmp, last)
        assert s["days_logged"] == 6 and s["days_to_unlock"] == 1 and not s["unlocked"] and not s["first_unlock"], s
        coach = [sys.executable, str(HERE / "coach.py"), "dashboard", "--data", tmp]
        assert "还差 1 天" in run_quiet(coach).stdout and not (Path(tmp) / "dashboard.html").exists(), "6 天不出页面"
        assert import_log(tmp, seven[7], today=last) == 1
        s = stats(tmp, last)
        assert s["days_logged"] == 7 and s["unlocked"] and s["first_unlock"], s
        run_quiet(coach)
        assert (Path(tmp) / "dashboard.html").exists() and not stats(tmp, last)["first_unlock"], "开过一次就不算第一次"
        import_log(tmp, "2026-11-01 85.2", today=last)
        kept = [r for r in load_log(tmp) if r["date"] == date(2026, 10, 31)][0]
        assert kept["waist"] == 92.0 and load_log(tmp)[1]["weight"] == 85.2 and len(load_log(tmp)) == 7, "覆盖同一天，不丢别的"
        import_log(tmp, "2026-10-31,85.9,,补的备注", today=last)
        assert load_log(tmp)[0]["waist"] == 92.0 and load_log(tmp)[0]["note"] == "补的备注", "这次没报腰围不冲掉"
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copy(example / "plan.md", tmp)
        shutil.copy(HERE / "templates" / "log.md", tmp)
        moved = date(2027, 3, 1)
        assert import_log(tmp, "\n".join(seven), to_today=True, today=moved) == 7
        assert load_log(tmp)[-1]["date"] == moved and stats(tmp, moved)["unlocked"], "演示数据平移到以今天结尾"
    print("selfcheck ok")


def main():
    if sys.stdout:  # pythonw 下没有 stdout
        sys.stdout.reconfigure(encoding="utf-8")
    if sys.argv[1:] == ["--selfcheck"]:
        return selftest()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data", type=Path, default=DEFAULT_DATA, help="数据目录，默认 ~/weight-loss-data")

    sub.add_parser("init", parents=[common])
    c = sub.add_parser("calc")
    c.add_argument("--sex", choices=["m", "f"], required=True)
    c.add_argument("--age", type=int, required=True)
    c.add_argument("--height", type=float, required=True, help="cm")
    c.add_argument("--weight", type=float, required=True, help="kg")
    c.add_argument("--target", type=float, required=True, help="kg")
    c.add_argument("--by", type=date.fromisoformat, help="目标日期 YYYY-MM-DD")
    c.add_argument("--activity", type=float, default=1.375, help="活动系数")
    c.add_argument("--as-of", type=date.fromisoformat, help="按哪天算，默认今天")
    st = sub.add_parser("stats", parents=[common])
    st.add_argument("--as-of", type=date.fromisoformat)
    im = sub.add_parser("import", parents=[common])
    im.add_argument("file", help="CSV 路径，- 表示从标准输入读")
    im.add_argument("--to-today", action="store_true", help="整批平移到以今天结尾，录演示用")
    d = sub.add_parser("dashboard", parents=[common])
    d.add_argument("--open", action="store_true", help="生成后用默认浏览器打开")
    r = sub.add_parser("remind", parents=[common])
    r.add_argument("--force", action="store_true", help="就算今天记过了也弹一条，用来试通知")
    rr = sub.add_parser("reminder", parents=[common])
    rr.add_argument("action", choices=["on", "off", "status"])
    rr.add_argument("time", nargs="?", help="HH:MM")
    a = ap.parse_args()

    if a.cmd == "init":
        a.data.mkdir(parents=True, exist_ok=True)
        for name in ("plan.md", "log.md", "reviews.md", "meals.md"):
            if not (a.data / name).exists():
                shutil.copy(HERE / "templates" / name, a.data / name)
        print(a.data.resolve())
    elif a.cmd == "calc":
        print(json.dumps(calc(a.sex, a.age, a.height, a.weight, a.target, a.by, a.activity, a.as_of), ensure_ascii=False, indent=1))
    elif a.cmd == "stats":
        s = stats(a.data, a.as_of)
        print(json.dumps(s if s else {"error": f"{a.data} 里没有 plan.md，先走第一次用的问答"}, ensure_ascii=False, indent=1))
    elif a.cmd == "import":
        n = import_log(a.data, sys.stdin.read() if a.file == "-" else read(a.file), a.to_today)
        print(json.dumps({"imported": n, **stats(a.data)}, ensure_ascii=False, indent=1))
    elif a.cmd == "dashboard":
        s = stats(a.data)
        if not (s and s["unlocked"]):
            print(f"还没解锁：记满 {UNLOCK_DAYS} 天才有这页，还差 {s['days_to_unlock'] if s else UNLOCK_DAYS} 天")
            return
        out = build_dashboard(a.data)
        print(out.resolve())
        if a.open:
            webbrowser.open(out.resolve().as_uri())
    elif a.cmd == "remind":
        print(remind(a.data, a.force))
    else:
        print(reminder(a.action, a.time, a.data))


if __name__ == "__main__":
    main()
