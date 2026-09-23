#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实时日报推送
============
每次扫描后刷新当日日报文件，并直发老板个人企业微信。
为避免刷屏：日报内容 md5 与上次推送一致时不重复推送（仅数据变化才推）。
用法:
  python3 realtime_report.py
"""
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

# wecom-cli 路径：环境变量 > which 回退 > 本地绝对路径
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
    day_str = datetime.now(BJ_TZ).strftime("%Y-%m-%d")
    text = daily_report.build_report(day_str)

    # 1) 落盘日报文件（实时刷新，随时打开即最新）
    out_path = f"daily_report_{day_str}.md"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"[realtime] {out_path} 已刷新")

    # 2) 内容 md5 去重：无变化不推，防刷屏
    digest = hashlib.md5(text.encode("utf-8")).hexdigest()
    prev = ""
    if os.path.exists(HASH_FILE):
        with open(HASH_FILE, encoding="utf-8") as f:
            prev = f.read().strip()
    if prev == digest:
        print("[realtime] 日报内容无变化，跳过推送")
        return

    # 3) 直发老板个人
    try:
        out = push_cli(text)
        print("[realtime] 已直发个人:", out[:200])
    except Exception as e:  # noqa: BLE001
        print(f"[realtime] 推送失败(文件仍已刷新): {e}")
        # 失败不写 hash，下轮重试推送

    # 4) 记录本次 md5
    with open(HASH_FILE, "w", encoding="utf-8") as f:
        f.write(digest)


if __name__ == "__main__":
    main()
