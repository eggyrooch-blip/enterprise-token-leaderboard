#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性存量清理:把某个合成身份(litellm-key:<alias>)的 usage/people 行并入真人邮箱。

背景(2026-06-13 排障):带审批单号后缀的 LiteLLM key 别名(如
`zhangyiqi-202606030074`)在归属修复前会被合成成 `litellm-key:zhangyiqi-202606030074`
假身份,污染个人榜。采集端已修复(litellm_collector 新增去尾号兜底),但生产库里
已落库的幽灵行需手工并回 `zhangyiqi@keep.com`。

用法:
    python cleanup_synthetic_keys.py <synthetic_email> <target_email> [--db PATH]

幂等:重复执行无副作用(第二次 synthetic 行已清零,只打印 0 计数)。一次只处理一个
synthetic_email;若有多个幽灵身份(如 litellm-key:zhangyiqi 与
litellm-key:zhangyiqi-202606030074),对每个分别跑一次即可。

合并语义与 usage 表主键一致:usage 主键为
(email, period_type, period, source, client, provider, model)。除 email 外的键元组
相同的 synthetic 行,数值列(input/output/cache_read/cache_write/reasoning/total/
cost/messages)累加进 target 同键行;target 无同键行的,直接把该 synthetic 行改挂到
target(保留其 dept 等非数值列)。处理后删除残留 synthetic usage 行与 people 行。
"""
from __future__ import print_function

import argparse
import os
import sqlite3
import sys

# 与 dev_collector 的库路径解析保持一致(同一个 DEV_DB 环境变量、同一默认值)。
DEFAULT_DB = os.environ.get("DEV_DB", "/tmp/tok.db")

# usage 表主键里除 email 外的部分 —— 合并的归并键。
_KEY_COLS = ("period_type", "period", "source", "client", "provider", "model")
# 累加的数值列。
_NUM_COLS = ("input", "output", "cache_read", "cache_write",
             "reasoning", "total", "cost", "messages")


def _counts(conn, synthetic, target):
    """返回 (synthetic_usage, target_usage, synthetic_people) 三个计数,用于前后对照。"""
    synthetic_usage = conn.execute(
        "SELECT COUNT(*) FROM usage WHERE email=?", (synthetic,)).fetchone()[0]
    target_usage = conn.execute(
        "SELECT COUNT(*) FROM usage WHERE email=?", (target,)).fetchone()[0]
    synthetic_people = conn.execute(
        "SELECT COUNT(*) FROM people WHERE email=?", (synthetic,)).fetchone()[0]
    return synthetic_usage, target_usage, synthetic_people


def _print_counts(label, counts):
    print("%s synthetic_usage=%d target_usage=%d synthetic_people=%d"
          % (label, counts[0], counts[1], counts[2]))


def merge_synthetic(conn, synthetic, target):
    """把 synthetic 的 usage/people 行并入 target。单事务,幂等。"""
    key_match = " AND ".join("%s=?" % c for c in _KEY_COLS)
    add_set = ", ".join("%s = %s + ?" % (c, c) for c in _NUM_COLS)

    synthetic_rows = conn.execute(
        "SELECT %s, %s FROM usage WHERE email=?"
        % (", ".join(_KEY_COLS), ", ".join(_NUM_COLS)),
        (synthetic,),
    ).fetchall()

    for row in synthetic_rows:
        key_vals = row[:len(_KEY_COLS)]
        num_vals = row[len(_KEY_COLS):]
        target_exists = conn.execute(
            "SELECT 1 FROM usage WHERE email=? AND %s" % key_match,
            (target,) + tuple(key_vals),
        ).fetchone()
        if target_exists:
            # target 已有同键行 → 数值列累加进 target,随后删掉这条 synthetic 行。
            conn.execute(
                "UPDATE usage SET %s WHERE email=? AND %s" % (add_set, key_match),
                tuple(num_vals) + (target,) + tuple(key_vals),
            )
            conn.execute(
                "DELETE FROM usage WHERE email=? AND %s" % key_match,
                (synthetic,) + tuple(key_vals),
            )
        else:
            # target 无同键行 → 直接把这条 synthetic 行改挂到 target(保留非数值列)。
            conn.execute(
                "UPDATE usage SET email=? WHERE email=? AND %s" % key_match,
                (target, synthetic) + tuple(key_vals),
            )

    # 删除残留(理论上已无)与 people 幽灵行。
    conn.execute("DELETE FROM usage WHERE email=?", (synthetic,))
    conn.execute("DELETE FROM people WHERE email=?", (synthetic,))


def run(db_path, synthetic, target):
    conn = sqlite3.connect(db_path)
    try:
        before = _counts(conn, synthetic, target)
        _print_counts("BEFORE", before)
        with conn:  # 单事务:全部成功才提交。
            merge_synthetic(conn, synthetic, target)
        after = _counts(conn, synthetic, target)
        _print_counts("AFTER", after)
    finally:
        conn.close()


def main(argv):
    parser = argparse.ArgumentParser(
        description="合并合成 LiteLLM key 身份到真人邮箱(usage+people),幂等。")
    parser.add_argument("synthetic_email", help="要清理的合成身份,如 litellm-key:zhangyiqi-202606030074")
    parser.add_argument("target_email", help="并入的真人规范邮箱,如 zhangyiqi@keep.com")
    parser.add_argument("--db", default=DEFAULT_DB,
                        help="sqlite 库路径(默认取 $DEV_DB,与 dev_collector 一致)")
    args = parser.parse_args(argv)
    run(args.db, args.synthetic_email, args.target_email)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
