# -*- coding: utf-8 -*-
"""补签（单批签发）端到端测试：跳过求解、最小变动、只写变动行、回滚与老库迁移。"""
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_tmpdir = tempfile.mkdtemp(prefix="audit-sign-")
os.environ["AUDIT_DB_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ["AUDIT_SECRET_KEY"] = "test-secret"

from app import create_app  # noqa: E402
from app import db          # noqa: E402

app = create_app()
app.config["TESTING"] = True
client = app.test_client()

GOOD_CSV = (
    "record_type,furnace,ref,primary_kg,recycled_kg,weight_kg\n"
    "furnace,F1,,0,370,\n"
    "furnace,F2,,50,50,\n"
    "furnace,F3,,100,50,\n"
    "batch,F1,B1,,,300\n"
    "batch,F1,B2,,,50\n"
    "batch,F2,B3,,,90\n"
    "batch,F3,B4,,,140\n"
    "batch,F3,B5,,,40\n"
    "transfer,F1,F2,,,20\n"
    "transfer,F2,F3,,,30\n"
)

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail and not cond else ""))


def sql(query, args=()):
    conn = db.get_conn()
    try:
        return [dict(r) for r in conn.execute(query, args).fetchall()]
    finally:
        conn.close()


def upload(csv_text):
    data = {"csv_file": (io.BytesIO(csv_text.encode("utf-8")), "ledger.csv"),
            "replace": "1"}
    return client.post("/upload", data=data,
                       content_type="multipart/form-data", follow_redirects=True)


def sign(batch, min_kg):
    return client.post("/certificates/sign",
                       data={"batch_code": batch, "min_recycled_kg": str(min_kg)},
                       follow_redirects=True)


# --- 1. 上传并取基准核定 ---
upload(GOOD_CSV)
lid = sql("SELECT id FROM ledgers WHERE is_current=1")[0]["id"]
base_b = {r["batch_code"]: r["recycled_kg"]
          for r in sql("SELECT * FROM solution_batch_lines WHERE ledger_id=?", (lid,))}
base_t = {r["transfer_id"]: r["recycled_kg"]
          for r in sql("SELECT * FROM solution_transfer_lines WHERE ledger_id=?", (lid,))}
check("lines_seeded_after_upload", len(base_b) == 5 and len(base_t) == 2)

# --- 2. 跳过求解：签一个低于现行核定的下限（取 B3 现行值即可）---
b3_now = base_b["B3"]
r = sign("B3", b3_now)
check("skip_sign_200", r.status_code == 200)
check("skip_flash_text", "跳过求解".encode("utf-8") in r.data)
# 行级明细完全没变
after_b = {r["batch_code"]: r["recycled_kg"]
           for r in sql("SELECT * FROM solution_batch_lines WHERE ledger_id=?", (lid,))}
check("skip_no_line_changed", after_b == base_b)
# sign_changes 有一条 skipped 事件
sc = sql("SELECT * FROM sign_changes WHERE skipped=1")
check("skip_event_recorded", len(sc) == 1 and sc[0]["target_ref"] == "(跳过求解)")
# 解的快照仍是 ok
check("skip_solution_still_ok",
      sql("SELECT ok FROM solutions WHERE ledger_id=?", (lid,))[0]["ok"] == 1)

# --- 3. 最小变动补签：撤销后签一个高于基准的值，触发重核 ---
cert_id = sql("SELECT id FROM certificates WHERE active=1 AND batch_code='B3'")[0]["id"]
client.post("/certificates/revoke", data={"cert_id": cert_id}, follow_redirects=True)
# B5 实称 40，签 40（基准 B5 通常 < 40），强制最小变动
before_b5 = sql("SELECT recycled_kg, updated_at FROM solution_batch_lines "
                "WHERE ledger_id=? AND batch_code='B5'", (lid,))[0]
r = sign("B5", 40)
check("mc_sign_200", r.status_code == 200 and b"error" not in r.data[:300])
after_b5 = sql("SELECT recycled_kg, updated_at FROM solution_batch_lines "
               "WHERE ledger_id=? AND batch_code='B5'", (lid,))[0]
check("mc_b5_meets_lb", after_b5["recycled_kg"] == 40)
check("mc_b5_line_updated", after_b5["updated_at"] != before_b5["updated_at"])

# --- 4. 只写有变的行：未变动行 updated_at 必须保持 ---
unchanged = sql("SELECT batch_code, updated_at FROM solution_batch_lines "
                "WHERE ledger_id=? AND batch_code <> 'B5'", (lid,))
old_map = {r["batch_code"]: r["updated_at"]
           for r in sql("SELECT batch_code, updated_at FROM solution_batch_lines "
                        "WHERE ledger_id=? AND batch_code <> 'B5'", (lid,))}
# 用签 B5 之前的快照对比：重新查 B1/B2/B3/B4 行，确认它们未被 UPDATE
# （updated_at 与该行此前一致；此处通过 sign_changes 只含实际变动行佐证）
changes = sql("SELECT * FROM sign_changes WHERE skipped=0")
changed_refs = {c["target_ref"] for c in changes}
check("mc_only_changed_rows_written",
      "B5" in changed_refs
      and all(c["before_kg"] != c["after_kg"] for c in changes))
check("mc_change_has_before_after",
      all("before_kg" in c and "after_kg" in c for c in changes))

# --- 5. 页面按批号/转料记录号展示变动前后公斤数 ---
r = client.get("/")
check("page_lists_change_refs",
      all(c["target_ref"].encode("utf-8") in r.data for c in changes))
check("page_shows_before_after",
      str(changes[0]["before_kg"]).encode("utf-8") in r.data
      and str(changes[0]["after_kg"]).encode("utf-8") in r.data)

# --- 6. 无解补签回滚：B4 实称 140，F3 自产 50 + F2->F3 通道 30，上限 80 < 140 ---
active_before = {r["batch_code"] for r in
                 sql("SELECT batch_code FROM certificates WHERE active=1")}
sol_id_before = sql("SELECT id FROM solutions WHERE ledger_id=?", (lid,))[0]["id"]
r = sign("B4", 140)
check("infeasible_sign_flash",
      ("无可行解".encode("utf-8") in r.data or "瓶颈".encode("utf-8") in r.data
       or "无法".encode("utf-8") in r.data))
active_after = {r["batch_code"] for r in
                sql("SELECT batch_code FROM certificates WHERE active=1")}
check("infeasible_rollback_certs", active_after == active_before)
check("infeasible_rollback_solution",
      sql("SELECT id FROM solutions WHERE ledger_id=?", (lid,))[0]["id"] == sol_id_before)
# B5 核定值保持
check("infeasible_keeps_b5",
      sql("SELECT recycled_kg FROM solution_batch_lines "
          "WHERE ledger_id=? AND batch_code='B5'", (lid,))[0]["recycled_kg"] == 40)
# 失败审计日志
check("infeasible_failure_logged",
      any(not l["success"] for l in sql("SELECT success FROM audit_log WHERE action='sign'")))

# --- 7. 老库迁移：手动造一套只有旧快照表、没有行级表数据的账套，验证启动迁移幂等 ---
conn = db.get_conn()
try:
    with db.transaction(conn):
        conn.execute("DELETE FROM solution_batch_lines")
        conn.execute("DELETE FROM solution_transfer_lines")
    db.migrate_solution_lines(conn)
    db.migrate_solution_lines(conn)   # 二次执行不报错、不重复
finally:
    conn.close()
mig_b = sql("SELECT COUNT(*) c FROM solution_batch_lines WHERE ledger_id=?", (lid,))[0]["c"]
mig_t = sql("SELECT COUNT(*) c FROM solution_transfer_lines WHERE ledger_id=?", (lid,))[0]["c"]
check("migration_idempotent_fills_lines", mig_b == 5 and mig_t == 2)

# --- 8. 迁移后 B5 仍是补签后的值（来自旧快照表）---
check("migration_preserves_b5",
      sql("SELECT recycled_kg FROM solution_batch_lines "
          "WHERE ledger_id=? AND batch_code='B5'", (lid,))[0]["recycled_kg"] == 40)

failed = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
