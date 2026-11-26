"""考題集：把主人真的傳過的訊息丟給正在用的 bot，照固定標準打分數。

    py -3 tools/exam.py run                 全部考一次（每題幾分鐘，10 題約 30–60 分鐘）
    py -3 tools/exam.py run --only vague    只考一題
    py -3 tools/exam.py list                看有哪些題目

每題用 `hermes -p <profile> chat -q … --source tool` 問（source=tool 不會出現在主人的對話清單），
多輪的題目用 --resume 接著問。考完把 bot 改到的 opportunities.json、preferences.json 放回考前的樣子
（考試中被改過的版本另存成 *.exam-<時間>.json，不會弄丟）。成績和每題回覆寫在
<Hermes>/shared/travel-workflow/exam-results/，不進 repo（回覆可能有私人資料）。

及格標準（grade）：
- 有回覆、沒出錯、在時限內答完
- 手機排版檢查（phone-check.py）沒有問題
- 沒寫主人沒說過的預算
- packages：一則回覆三個不同地方的方案（不夠三個要寫「只有…符合」），每個方案都是 format-plan.py 組出來的
  （方案標題和總計那行原樣出現在 format-plan.py 的輸出裡），有指定地點就要是那裡；
  有說預算時，最便宜的超出預算要講「超出」
- ask_dates：日期和天數對不上，先講清楚再問，不能直接出方案
- one：「你幫我挑一個」只給一個方案的完整明細，總計那行是 format-plan.py 算的
- cheaper：嫌貴時，給比上一則最便宜的還便宜的方案；找不到就直說「沒找到」並寫出價格
"""
import argparse
import datetime as dt
import html
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
QUESTIONS = REPO / "exam" / "questions.json"
TIME_LIMIT_S = 900
# recommended.json: format-plan.py remembers what it recommended, so an exam would make later replies avoid those places.
KEPT_FILES = ("opportunities.json", "preferences.json", "recommended.json")
FORMAT_PLAN = "format-plan.py"
HOT_SPRING = re.compile(r"溫泉|湯屋|泡湯|湯$|湯｜|湯\s")
OVER_BUDGET = re.compile(r"超出|超過|超支")
# 2026-11-26: 「沒有比玫瑰園更便宜的雙人湯屋」 is a not-found too.
NOT_FOUND = re.compile(r"沒(?:有)?找到|找不到|沒有更便宜|沒有比[^，。！？；\n]{0,20}更便宜|沒查到")  # one clause only
ASKED_DATES = re.compile(r"對不上|不一致|矛盾|其實是|三天兩夜|兩晚|[23]\s*晚")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sys.path.insert(0, str(REPO / "scripts"))
phone_check = load("phone_check", REPO / "scripts" / "phone-check.py")
travel_money = phone_check.travel_money


def hermes_root():
    return Path(os.environ.get("HERMES_ROOT") or Path(os.environ.get("LOCALAPPDATA", "")) / "hermes")


# ---- 題目 -------------------------------------------------------------------------------------

def weekend(today):
    """The coming weekend's Friday, Saturday and Sunday: next week's from Saturday on, so {週五} is
    never already past (Antigravity's review of 67f8e37)."""
    saturday = today + dt.timedelta(days=(5 - today.weekday()) % 7 or 7)
    return saturday - dt.timedelta(days=1), saturday, saturday + dt.timedelta(days=1)


def fill_dates(text, today):
    """Turn {週五} {週六} {週日} {下週六} into M/D dates, so a question never goes stale."""
    friday, saturday, sunday = weekend(today)
    dates = {"週五": friday, "週六": saturday, "週日": sunday, "下週六": saturday + dt.timedelta(days=7)}
    return re.sub(r"\{(週五|週六|週日|下週六)\}", lambda m: f"{dates[m.group(1)].month}/{dates[m.group(1)].day}", text)


def questions(path=QUESTIONS):
    return json.loads(Path(path).read_text(encoding="utf-8"))["questions"]


# ---- 打分數 ------------------------------------------------------------------------------------

def package_totals(block):
    """The NT$ amounts on a package's 總計 line."""
    for line in block:
        if line.startswith("總計"):
            return [int(re.sub(r"\D", "", amount)) for amount in travel_money.amounts(line)]
    return []


PHONE_ONLY = "缺 電話（或 電話：未取得）"


def hide_unconfirmed(folder):
    try:
        return json.loads((Path(folder) / "preferences.json").read_text(encoding="utf-8-sig")).get(
            "show_unconfirmed") is False
    except (OSError, ValueError, AttributeError):
        return False


def grade(question, said, reply, tool_outputs, elapsed=0, error="", earlier=(), hide_unconfirmed=False):
    """Why this answer fails the question (Traditional Chinese, for the owner), or [] when it passes.

    `said` is what the owner typed (the question's turns), `tool_outputs` the text of every
    format-plan.py result in the conversation, `earlier` the replies before the last one,
    `hide_unconfirmed` preferences.json's show_unconfirmed being false."""
    if error:
        return [f"出錯：{error[:120]}"]
    if not reply.strip():
        return ["沒有回覆"]
    problems = []
    if elapsed > question.get("time_limit", TIME_LIMIT_S):
        problems.append(f"太慢：{round(elapsed / 60)} 分鐘")
    report = phone_check.check(reply)
    # 「未確認就不要傳給我」: the plugin drops 「電話：未取得」, so a package with no phone line is what the owner chose.
    # The same for 「照片：未取得」 (missing_photo).
    kinds = sorted({p["kind"] for p in report["problems"] if p["kind"] != "unsaid_budget"
                    and not (hide_unconfirmed and (p["kind"] == "missing_photo" or p["kind"] == "incomplete_package"
                                                   and p.get("text", "").endswith(PHONE_ONLY)))})
    if kinds:
        problems.append("手機排版檢查沒過：" + "、".join(kinds))
    for written, _ in travel_money.unsaid_budgets(reply, said):
        problems.append(f"寫了主人沒說過的預算：{written}")
    blocks = phone_check.package_blocks(reply)
    if question["expect"] == "ask_dates":
        if blocks:
            problems.append("日期對不上卻直接出方案")
        if not ASKED_DATES.search(reply):
            problems.append("沒講日期和天數對不上")
        return problems
    made = "\n".join(tool_outputs)
    if question["expect"] == "one":  # 「你幫我挑一個」: one full plan, not three to choose from
        if len(blocks) > 1:
            problems.append(f"要它挑一個，卻給了 {len(blocks)} 個方案")
        totals = [line for line in reply.splitlines() if line.startswith("總計")]
        if len(blocks) <= 1 and len(totals) > 1:  # two plans pasted together (Antigravity's review)
            problems.append(f"要它挑一個，卻給了 {len(totals)} 個方案")
        if not totals:
            problems.append("沒有總價")
        elif not all(line in made for line in totals):
            problems.append("總價不是 format-plan.py 算的")
        return problems
    if question["expect"] == "cheaper":
        # 「太貴，有沒有更便宜的」: cheaper packages, or an honest 「沒找到」 that gives the price (the role's rule).
        before = [min(t) for r in earlier for t in map(package_totals, phone_check.package_blocks(r)) if t]
        if not blocks:
            if NOT_FOUND.search(reply) and travel_money.PRICE.search(reply):
                return problems
            return problems + ["沒有更便宜的方案，也沒說沒找到"]
        totals = [min(t) for t in map(package_totals, blocks) if t]
        if before and totals and min(totals) >= min(before):
            problems.append(f"沒有比原本最便宜的 {travel_money.money(min(before))} 更便宜")
    if not blocks:
        return problems + ["沒有給 ①②③ 方案"]
    if len(blocks) < phone_check.PACKAGES and not phone_check.FEW_FIT.search(reply):
        problems.append(f"只有 {len(blocks)} 個方案，也沒說為什麼")
    # 2026-11-26: three packages were three rooms of one 龍山溫泉旅館.
    names = [re.sub(r"\s+", "", phone_check.PACKAGE.sub("", block[0]).split("｜")[0]) for block in blocks]
    for i, name in enumerate(names):
        twin = next((other for other in names[:i] if other and name and (other in name or name in other)), None)
        if twin:
            problems.append(f"「{twin}」重複：三個方案要三個不同的地方")
    # 2026-11-26: 「為甚麼只有溫泉明明還有那麼多地方可以玩」. A question that names no kind of trip gets mixed kinds.
    if question.get("mixed") and len(blocks) >= 2 and all(HOT_SPRING.search(block[0]) for block in blocks):
        problems.append("沒說玩什麼，三個方案卻全是溫泉")
    for block in blocks:
        if block[0] not in made or not any(line.startswith("總計") and line in made for line in block):
            problems.append(f"{block[0][:12]} 不是 format-plan.py 組的")
    if question.get("place") and question["place"] not in reply:
        problems.append(f"方案不在{question['place']}")
    budget = question.get("budget")
    totals = [min(t) for t in map(package_totals, blocks) if t]
    if budget and totals and min(totals) > budget and not OVER_BUDGET.search(reply):
        problems.append(f"全部超出預算 {travel_money.money(budget)} 卻沒說")
    return problems


# ---- 問 bot ------------------------------------------------------------------------------------

def answer_folder(root):
    """Where the bot answers from: its Hermes home, as on Telegram. 2026-11-26: started inside this
    checkout, the bot read tests/test_format_plan.py to see how to get past the checks."""
    folder = root / "profiles"
    return folder if folder.is_dir() else root


def ask(profile, text, session_id=None, timeout=TIME_LIMIT_S + 300):
    """One turn through the Hermes CLI: (reply, session id, error, format-plan.py outputs)."""
    command = ["hermes", "-p", profile, "chat", "-q", text, "--source", "tool", "--format", "stream-json"]
    if session_id:
        command += ["--resume", session_id]
    try:
        run = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=timeout, cwd=answer_folder(hermes_root()))
    except subprocess.TimeoutExpired:
        return "", session_id, f"超過 {timeout // 60} 分鐘沒答完", []
    return parse_stream(run.stdout, run.stderr, session_id)


def parse_stream(stdout, stderr="", session_id=None):
    """The reply, session id, error and format-plan.py outputs from `--format stream-json` output."""
    result, plans, outputs = None, set(), []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "tool_use" and FORMAT_PLAN in json.dumps(event.get("input"), ensure_ascii=False):
            plans.add(event.get("tool_call_id"))
        elif event.get("type") == "tool_result" and event.get("tool_call_id") in plans:
            outputs.append(result_text(event.get("output")))
        elif event.get("type") == "result":
            result = event
    if result is None:
        return "", session_id, (stderr.strip() or stdout.strip() or "沒有結果")[-200:], outputs
    error = result.get("error") or ("" if not result.get("exit_code") else f"exit {result['exit_code']}")
    return result.get("text") or "", result.get("session_id") or session_id, error or "", outputs


def result_text(output):
    """A tool result's text: the "output" of a JSON result, else the result itself."""
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except ValueError:
            return output.replace("\r\n", "\n")
    if isinstance(output, dict):
        output = output.get("output", "")
    return str(output or "").replace("\r\n", "\n")


# ---- gateway 有沒有載入最新的外掛 -------------------------------------------------------------------
# 2026-11-26: the first exam answer lost the totals of packages ② and ③. The fixed plugin was on disk,
# but the gateway had started before it was copied there and still ran the old one (which knew only one
# package). A plugin newer than the gateway process means its changes are not live yet.

def gateway_started():
    """When the running Hermes gateway process started (UTC), or None when that cannot be read."""
    try:
        status = subprocess.run(["hermes", "gateway", "status"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=60).stdout
        pid = re.search(r"PID:\s*(\d+)", status)
        if not pid:
            return None
        started = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  f"(Get-Process -Id {pid.group(1)}).StartTime.ToUniversalTime().ToString('o')"],
                                 capture_output=True, text=True, timeout=60).stdout.strip()
        return dt.datetime.fromisoformat(started[:26].rstrip("Z")).replace(tzinfo=dt.timezone.utc)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def plugin_files(root):
    return sorted(root.glob("profiles/*/plugins/travel-reply-check/*.py"))


def newer_than(paths, started):
    """The files changed after `started`: their changes are not in the running gateway."""
    if started is None:
        return []
    return [p for p in paths
            if dt.datetime.fromtimestamp(p.stat().st_mtime, dt.timezone.utc) > started]


# ---- 考前考後的紀錄 ------------------------------------------------------------------------------

def snapshot(folder):
    """Each kept file's bytes, or None for one that is not there yet."""
    return {name: (folder / name).read_bytes() if (folder / name).exists() else None for name in KEPT_FILES}


def restore(folder, before, stamp):
    """Put back what the exam changed; keep the changed copy beside it. Returns the names put back."""
    restored = []
    for name, content in before.items():
        path = folder / name
        if path.exists() and path.read_bytes() != content:
            kept = path.with_name(f"{path.stem}.exam-{stamp}{path.suffix}")
            if content is None:  # the exam made it: move it aside, nothing is deleted
                path.replace(kept)
            else:
                shutil.copy2(path, kept)
                path.write_bytes(content)
            restored.append(name)
    return restored


# ---- 成績單 ------------------------------------------------------------------------------------

def report_html(results, stamp):
    passed = sum(not r["problems"] for r in results)
    rows = []
    for r in results:
        mark = "✅" if not r["problems"] else "❌"
        why = "".join(f"<li>{html.escape(p)}</li>" for p in r["problems"]) or "<li>及格</li>"
        rows.append(f"<section><h2>{mark} {html.escape(r['id'])}（{round(r['elapsed'] / 60, 1)} 分）</h2>"
                    f"<p class=q>{html.escape(r['said'][-1])}</p><ul>{why}</ul>"
                    f"<pre>{html.escape(r['reply'])}</pre></section>")
    return (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            f"<title>考題集成績</title><style>body{{font:15px system-ui;margin:12px;max-width:420px}}"
            f"pre{{white-space:pre-wrap;background:#eef4ff;padding:8px;border-radius:10px}}"
            f".q{{background:#dcf8c6;padding:6px;border-radius:10px}}h2{{font-size:16px}}</style>"
            f"<h1>考題集 {passed}/{len(results)} 及格</h1><p>{stamp}</p>{''.join(rows)}")


def run(args):
    root = hermes_root()
    folder = root / "shared" / "travel-workflow"
    stale = newer_than(plugin_files(root), gateway_started())
    if stale:
        print("外掛改過但 gateway 還在用舊的，考了也不準。先跑 `hermes gateway restart` 再考：\n"
              + "\n".join(f"  {p}" for p in stale))
        return 2
    every = questions(args.questions)
    unknown = sorted(set(args.only or ()) - {q["id"] for q in every})
    if unknown:  # a typo would otherwise grade nothing and report 0/0 as a pass
        print("沒有這些題目：" + "、".join(unknown) + "\n有的題目：" + "、".join(q["id"] for q in every))
        return 2
    picked = [q for q in every if not args.only or q["id"] in args.only]
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    before = snapshot(folder)
    results = []
    try:
        for question in picked:
            said = [fill_dates(turn, dt.date.today()) for turn in question["turns"]]
            start, session_id, reply, error, made, replies = time.time(), None, "", "", [], []
            for turn in said:
                reply, session_id, error, outputs = ask(question["profile"], turn, session_id)
                made += outputs
                replies.append(reply)
                if error:
                    break
            elapsed = time.time() - start
            problems = grade(question, said, reply, made, elapsed, error, earlier=replies[:-1],
                             hide_unconfirmed=hide_unconfirmed(folder))
            results.append({"id": question["id"], "said": said, "reply": reply, "session_id": session_id,
                            "elapsed": elapsed, "problems": problems})
            print(f"{'✅' if not problems else '❌'} {question['id']}（{round(elapsed / 60, 1)} 分）"
                  + "".join(f"\n   - {p}" for p in problems), flush=True)
    finally:
        restored = restore(folder, before, stamp)
    out = folder / "exam-results"
    out.mkdir(exist_ok=True)
    (out / f"{stamp}.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / f"{stamp}.html").write_text(report_html(results, stamp), encoding="utf-8")
    passed = sum(not r["problems"] for r in results)
    print(f"\n成績：{passed}/{len(results)} 及格")
    if restored:
        print("已把考試中被改的檔案放回考前：" + "、".join(restored) + f"（改過的版本存成 *.exam-{stamp}.json）")
    print(f"成績單：{out / (stamp + '.html')}")
    return 0 if passed == len(results) else 1


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    go = sub.add_parser("run", help="考一次")
    go.add_argument("--only", nargs="+", help="只考這幾題（題目 id）")
    go.add_argument("--questions", default=QUESTIONS, help=argparse.SUPPRESS)
    sub.add_parser("list", help="列出題目")
    args = parser.parse_args()
    if args.command == "list":
        for q in questions():
            print(f"{q['id']:<18} {q['profile']:<16} {fill_dates(q['turns'][-1], dt.date.today())}")
        return 0
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
