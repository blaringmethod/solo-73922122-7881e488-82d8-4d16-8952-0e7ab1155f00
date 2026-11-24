# -*- coding: utf-8 -*-
"""最小变动求解器（补签）单元测试。

覆盖：
  1. 目标函数等于改变量绝对值之和，且严格最优（与朴素枚举对照）；
  2. 基准已达下限的输入仍可行（web 层会跳过求解，求解器本身也应支持）；
  3. 下界高于基准时，基准批被强制上调，费用正确；
  4. 转料改变量计入目标；
  5. 无解时返回原因且不抛异常；
  6. 基准不守恒/缺基准时抛 SolveError；
  7. 解满足全部约束（守恒、实称上限、签单下界、整数性）。
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.solver import (  # noqa: E402
    CertRequirement, FurnaceBatchLink, FurnaceInput, SolveError, TransferInput,
    solve_allocation, solve_min_change,
)

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail and not cond else ""))


def verify_constraints(res, furnaces, links, transfers, reqs):
    req_map = {r.batch: r.min_recycled_kg for r in reqs}
    for bl in links:
        v = res.batch_recycled[bl.batch]
        assert isinstance(v, int)
        assert 0 <= v <= bl.weight_kg, (bl.batch, v)
        if bl.batch in req_map:
            assert v >= req_map[bl.batch]
    for t in transfers:
        v = res.transfer_recycled[t.tid]
        assert isinstance(v, int) and 0 <= v <= t.weight_kg
    for f in furnaces:
        row = res.furnace_balance[f.code]
        assert row["in_total"] == row["out_total"], (f.code, row)


def total_abs(res, base_b, base_t):
    d = sum(abs(res.batch_recycled[b] - base_b[b]) for b in base_b)
    d += sum(abs(res.transfer_recycled[t] - base_t[t]) for t in base_t)
    return d


# ---------------------------------------------------------------- 场景 1
# 两炉一转料：F1 再生 100、B1 上限 100；F2 无再生、B2 上限 60；转料 F1->F2 上限 60。
# 物料平衡：F1 进 100 = B1(40)+转料(60)；F2 进 60 = B2(60)。
furnaces = [FurnaceInput("F1", 0, 100), FurnaceInput("F2", 60, 0)]
links = [FurnaceBatchLink("F1", "B1", 40), FurnaceBatchLink("F2", "B2", 60)]
transfers = [TransferInput(1, "F1", "F2", 60)]

# 基准：无签单时整体最大化的一版解
base_res = solve_allocation(furnaces, links, transfers, [])
check("base_feasible", base_res.ok)
base_b = dict(base_res.batch_recycled)
base_t = dict(base_res.transfer_recycled)
check("base_is_max", sum(base_b.values()) == 100, str(base_b))

# 给 B2 签 50：基准 B2 通常 < 50，需要最小变动
res = solve_min_change(furnaces, links, transfers,
                       [CertRequirement("B2", 50)], base_b, base_t)
check("mc_feasible", res.ok, res.reason)
verify_constraints(res, furnaces, links, transfers, [CertRequirement("B2", 50)])
check("mc_b2_meets_lb", res.batch_recycled["B2"] >= 50)
check("mc_total_change_matches", res.total_change == total_abs(res, base_b, base_t),
      f"{res.total_change} vs {total_abs(res, base_b, base_t)}")

# 朴素枚举全部整数可行流，找真实最小改变量，验证最优性
best = None
best_x = None
for x1 in range(0, 41):           # B1
    for x2 in range(0, 61):       # B2
        for y in range(0, 61):    # 转料再生料
            # F1 守恒：x1 + y = 100；F2 守恒：x2 = y
            if x1 + y != 100 or x2 != y:
                continue
            if x2 < 50:
                continue
            dist = (abs(x1 - base_b["B1"]) + abs(x2 - base_b["B2"])
                    + abs(y - base_t[1]))
            if best is None or dist < best:
                best, best_x = dist, (x1, x2, y)
check("mc_optimal_vs_enumeration", res.total_change == best,
      f"solver={res.total_change}, enum_best={best}, enum_point={best_x}")

# ---------------------------------------------------------------- 场景 2
# 基准已满足新下限：求解器返回 0 变动解（web 层在此情形直接跳过求解）
res2 = solve_min_change(furnaces, links, transfers,
                        [CertRequirement("B2", res.batch_recycled["B2"])],
                        base_b, base_t)
check("mc_zero_change_when_base_satisfies",
      res2.ok and res2.total_change == 0 and res2.batch_recycled == base_b
      and res2.transfer_recycled == base_t)

# ---------------------------------------------------------------- 场景 3
# 下界高于基准：B2 直接要到上限 60
res3 = solve_min_change(furnaces, links, transfers,
                        [CertRequirement("B2", 60)], base_b, base_t)
check("mc_force_up", res3.ok and res3.batch_recycled["B2"] == 60)
check("mc_force_up_change",
      res3.total_change == total_abs(res3, base_b, base_t))
# 此时 F1 守恒要求转料=60、B1=40，检查联动调整
check("mc_force_up_linkage",
      res3.transfer_recycled[1] == 60 and res3.batch_recycled["B1"] == 40,
      str((res3.batch_recycled, res3.transfer_recycled)))

# ---------------------------------------------------------------- 场景 4
# 三炉链式转料，验证转料改变量也计入目标且解最优
f3 = [
    FurnaceInput("F1", 0, 100),
    FurnaceInput("F2", 0, 0),
    FurnaceInput("F3", 0, 0),
]
l3 = [
    FurnaceBatchLink("F1", "B1", 20),
    FurnaceBatchLink("F2", "B2", 40),
    FurnaceBatchLink("F3", "B3", 80),
]
t3 = [TransferInput(1, "F1", "F2", 80), TransferInput(2, "F2", "F3", 80)]
b3res = solve_allocation(f3, l3, t3, [])
bb3 = dict(b3res.batch_recycled)
bt3 = dict(b3res.transfer_recycled)
# F2 自身无外购再生料，最多只能把 F1 来料中的 40 转给 F3（B2 上限 40 占用其余），
# 故 B3 的可达上限是 40；取 30 验证链式最小变动。
res4 = solve_min_change(f3, l3, t3, [CertRequirement("B3", 30)], bb3, bt3)
check("mc_chain_feasible", res4.ok, res4.reason)
verify_constraints(res4, f3, l3, t3, [CertRequirement("B3", 30)])
check("mc_chain_meets_lb", res4.batch_recycled["B3"] >= 30)
check("mc_chain_change_accounted",
      res4.total_change == total_abs(res4, bb3, bt3))
# F3 无外购再生料：其出货再生料必须全部来自转料，且每炉守恒严格成立
check("mc_chain_f3_from_transfer",
      res4.batch_recycled["B3"] == res4.transfer_recycled[2])
check("mc_chain_f3_conservation",
      res4.furnace_balance["F3"]["in_total"]
      == res4.furnace_balance["F3"]["out_total"])

# ---------------------------------------------------------------- 场景 5
# 无解：B2 要 61（超实称 60，由 precheck 拦截）
res5 = solve_min_change(furnaces, links, transfers,
                        [CertRequirement("B2", 61)], base_b, base_t)
check("mc_infeasible_returns_reason", not res5.ok and bool(res5.reason))

# 网络级可达性：F2 来料 80 可在 B2 与转 F3 间调配，B3 最高可达 80（=其实称）。
# B3=61 仍可行（F2 把 B2 压到 19、转 F3 61），且必须守恒。
res5b = solve_min_change(f3, l3, t3, [CertRequirement("B3", 61)], bb3, bt3)
check("mc_network_feasible_61",
      res5b.ok and res5b.batch_recycled["B3"] >= 61
      and res5b.furnace_balance["F3"]["in_total"]
      == res5b.furnace_balance["F3"]["out_total"],
      str(res5b.furnace_balance))
# 可达边界 80 可行且守恒
res5b_ok = solve_min_change(f3, l3, t3, [CertRequirement("B3", 80)], bb3, bt3)
check("mc_feasible_boundary_80",
      res5b_ok.ok and res5b_ok.batch_recycled["B3"] == 80
      and res5b_ok.furnace_balance["F3"]["in_total"]
      == res5b_ok.furnace_balance["F3"]["out_total"],
      str(res5b_ok.batch_recycled))
res5c = solve_min_change(f3, l3, t3, [CertRequirement("B3", 81)], bb3, bt3)
check("mc_infeasible_over_weight", not res5c.ok and "B3" in (res5c.reason or ""))

# ---------------------------------------------------------------- 场景 6
# 基准异常：缺行 / 不守恒
try:
    solve_min_change(furnaces, links, transfers,
                     [CertRequirement("B2", 50)],
                     {"B1": base_b["B1"]}, base_t)
    check("mc_missing_baseline_raises", False)
except SolveError:
    check("mc_missing_baseline_raises", True)

bad_base_b = dict(base_b)
bad_base_b["B1"] += 5  # 破坏 F1 守恒
try:
    solve_min_change(furnaces, links, transfers,
                     [CertRequirement("B2", 50)], bad_base_b, base_t)
    check("mc_unbalanced_baseline_raises", False)
except SolveError:
    check("mc_unbalanced_baseline_raises", True)

# ---------------------------------------------------------------- 场景 7
# 多批签单同时存在时的最小变动（场景1再加 B1 下限）
res7 = solve_min_change(
    furnaces, links, transfers,
    [CertRequirement("B1", 30), CertRequirement("B2", 50)], base_b, base_t,
)
check("mc_multi_cert_feasible", res7.ok, res7.reason)
verify_constraints(res7, furnaces, links, transfers,
                   [CertRequirement("B1", 30), CertRequirement("B2", 50)])
check("mc_multi_cert_optimal",
      res7.total_change == total_abs(res7, base_b, base_t))

# ---------------------------------------------------------------- 场景 8
# 大整数：费用与流量均为整数，不溢出、结果正确
fb = [FurnaceInput("F1", 0, 123456)]
lb = [FurnaceBatchLink("F1", "B1", 100000), FurnaceBatchLink("F1", "B2", 23456)]
b0 = solve_allocation(fb, lb, [], [])
res8 = solve_min_change(fb, lb, [], [CertRequirement("B1", 99999)],
                        dict(b0.batch_recycled), {})
check("mc_large_ints", res8.ok and isinstance(res8.total_change, int)
      and res8.batch_recycled["B1"] >= 99999)

failed = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
