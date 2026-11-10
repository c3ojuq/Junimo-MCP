#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/list 转储与对比（不需要游戏，不需要 JunimoBridge）。

用法：
  python test/tools_list_check.py dump --exe <main.exe> --out <json>
  python test/tools_list_check.py compare --old <old.json> --new <new.json> \
      --expect-count 42 --expect-old-count 41 --expect-added smooth_move

原理：
  - 把 initialize / notifications/initialized / tools/list 三行 JSON-RPC 一次性写进
    stdin 并关闭；进程读到 EOF 自行退出（bin/main.ml:16-32 的循环）。
  - 只解析带 id 的响应，按 id 取 initialize(id=1) 与 tools/list(id=2)。
  - initialize 与 tools/list 都是纯本地逻辑（lib/mcp.ml:10-26、65；
    lib/intents.ml:2969-2978），所以无游戏、无桥接也能跑。

注意：exe 输出是 UTF-8 字节（OCaml 字符串原样 print_string），这里必须显式
encoding="utf-8"；Windows 默认 locale（cp1252）会把中文描述读坏，导致 diff 假阳性。
"""

import argparse
import json
import os
import subprocess
import sys


def _rpc(exe):
    exe = os.path.normpath(os.path.abspath(exe))
    if not os.path.isfile(exe):
        raise SystemExit("找不到可执行文件：%s" % exe)

    reqs = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    payload = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in reqs)

    proc = subprocess.run(
        [exe],
        input=payload,
        capture_output=True,
        timeout=180,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise SystemExit(
            "exe 退出码 %s\n--- stderr ---\n%s"
            % (proc.returncode, (proc.stderr or "")[-2000:])
        )

    msgs = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "id" in obj:
            msgs[obj["id"]] = obj

    if 1 not in msgs or 2 not in msgs:
        raise SystemExit(
            "缺少 initialize / tools/list 响应\n--- stdout ---\n%s\n--- stderr ---\n%s"
            % ((proc.stdout or "")[:2000], (proc.stderr or "")[:2000])
        )
    return msgs[1], msgs[2]


def cmd_dump(args):
    init, tl = _rpc(args.exe)
    result = tl.get("result") or {}
    tools = result.get("tools") or []
    server = (init.get("result") or {}).get("serverInfo") or {}

    doc = {
        "server": server,
        "count": len(tools),
        "tools": sorted(tools, key=lambda t: t.get("name", "")),
    }
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")

    print(
        "ok: %s -> %s（%d 个工具，server=%s %s）"
        % (os.path.basename(args.exe), args.out, len(tools),
           server.get("name"), server.get("version"))
    )
    return 0


def cmd_compare(args):
    with open(args.old, encoding="utf-8") as f:
        old = json.load(f)
    with open(args.new, encoding="utf-8") as f:
        new = json.load(f)

    o = {t["name"]: t for t in old.get("tools", [])}
    n = {t["name"]: t for t in new.get("tools", [])}
    fails = []

    if (new.get("server") or {}).get("name") != "junimo-mcp":
        fails.append("initialize: serverInfo.name 不是 junimo-mcp")
    if new.get("count") != len(n):
        fails.append("新转储自相矛盾：count=%s，实际 %d" % (new.get("count"), len(n)))

    if len(o) != args.expect_old_count:
        fails.append("基线工具数：期望 %d，实际 %d（基线 ref 选错了？）"
                     % (args.expect_old_count, len(o)))
    if len(n) != args.expect_count:
        fails.append("新工具数：期望 %d，实际 %d" % (args.expect_count, len(n)))

    added = sorted(set(n) - set(o))
    removed = sorted(set(o) - set(n))
    if added != sorted(args.expect_added):
        fails.append("新增工具：期望 %s，实际 %s" % (sorted(args.expect_added), added))
    if removed:
        fails.append("被删除的工具：%s" % removed)

    changed = []
    for name in sorted(o):
        if name not in n:
            continue
        for field in ("name", "description", "inputSchema"):
            if o[name].get(field) != n[name].get(field):
                changed.append("%s.%s" % (name, field))
    if changed:
        fails.append("旧工具字段发生变化：%s" % ", ".join(changed))

    print("基线：%d 个工具；新：%d 个工具；新增：%s；删除：%s"
          % (len(o), len(n), added or "无", removed or "无"))
    print("旧工具逐字段对比（name / description / inputSchema）：%s"
          % ("全部一致" if not changed else "%d 处不同" % len(changed)))

    if fails:
        for f in fails:
            print("FAIL - " + f)
        return 1

    print("PASS - %d 个工具，仅新增 %s，旧 %d 个工具零变化"
          % (len(n), ", ".join(args.expect_added), len(o)))
    return 0


def main():
    ap = argparse.ArgumentParser(description="tools/list 转储与对比")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("dump", help="跑一遍 exe，把 tools/list 落成 JSON")
    d.add_argument("--exe", required=True)
    d.add_argument("--out", required=True)
    d.set_defaults(func=cmd_dump)

    c = sub.add_parser("compare", help="对比新旧两份 tools/list")
    c.add_argument("--old", required=True)
    c.add_argument("--new", required=True)
    c.add_argument("--expect-count", type=int, default=42)
    c.add_argument("--expect-old-count", type=int, default=41)
    c.add_argument("--expect-added", nargs="+", default=["smooth_move"])
    c.set_defaults(func=cmd_compare)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
