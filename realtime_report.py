#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实时日报推送（完整骨架版）
============================
每次扫描后：
  1. 生成完整日报（10 节骨架）落盘 daily_report_<date>.md
  2. 生成精简推送版，内容 md5 有变化才直发老板个人（防刷屏）
用法:
  python3 realtime_report.py [--base-dir .]
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import daily_report

BJ_TZ = timezone(timedelta(hours=8))
HASH_FILE = ".daily_report_hash"

WECOM_CLI = (os.environ.get("WECOM_CLI")
             or shutil.which("wecom-cli")
             or "/home/marvis/.npm-global/bin/wecom-cli")
WECOM_USERID = "won22_bgAAVzd-4_LKT6VxA_Sf8lNoIw"  # Wind


def push_cli(text):
    """通过 wecom-cli 直发授权真人 Wind。"""
    cmd = [WECOM_CLI, "message", "send",
           "--chat-id", WECOM_USERID,
           "--msg-type", "text",
           "--text", json.dumps({"content": text}, ensure_ascii=False)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"wecom-cli 发送失败: {proc.stderr[:300]}")
    return proc.stdout


def main():
    parser = argparse.ArgumentParser(description="实时日报：刷新文件+有变化直发个人")
    parser.add_argument("--base-dir", default=".", help="daily_trades/.fvg_pushed 所在目录")
    args = parser.parse_args()

    day_str = datetime.now(BJ_TZ).strftime("%Y-%m-%d")

    # 1) 完整版落盘（10 节骨架，随时打开即最新）
    full = daily_report.build_report(day_str, base_dir=args.base_dir, compact=False)
    out_path = f"daily_report_{day_str}.md"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(full)
    print(f"[realtime] {out_path} 已刷新（完整版）")

    # 2) 精简推送版 + md5 去重
    push = daily_report.build_report(day_str, base_dir=args.base_dir, compact=True)
    if len(push.encode("utf-8")) > 2048:
        print(f"[realtime] 警告：推送版 {len(push.encode('utf-8'))}B 超企微上限，截断处理")
        push = push[:1000] + "\n...(截断)"
    digest = hashlib.md5(push.encode("utf-8")).hexdigest()
    prev = ""
    if os.path.exists(HASH_FILE):
        with open(HASH_FILE, encoding="utf-8") as f:
            prev = f.read().strip()
    if prev == digest:
        print("[realtime] 日报内容无变化，跳过推送")
        return

    # 3) 直发老板个人
    try:
        out = push_cli(push)
        print("[realtime] 已直发个人:", out[:200])
    except Exception as e:  # noqa: BLE001
        print(f"[realtime] 推送失败(文件仍已刷新): {e}")
        # 失败不写 hash，下轮重试推送

    # 4) 记录本次 md5
    with open(HASH_FILE, "w", encoding="utf-8") as f:
        f.write(digest)


if __name__ == "__main__":
    main()
