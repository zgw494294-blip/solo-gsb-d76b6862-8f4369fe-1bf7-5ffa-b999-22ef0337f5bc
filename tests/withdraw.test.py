"""撤回末条后端回归测试（FastAPI TestClient + 独立临时数据库）。

覆盖：基本撤回（发出快照回待发队首、保留 ID 与编号、清除发出快照/时间/序号、
版本 +1）；再次确认记新时间；完成态回到演出中；暂停时可撤回且暂停标记不变；
409（版本过期 / 目标不符 / 无已发提示 / 开场前）；同版本与撤回/确认/编辑/
重排/归档/问题记录竞争最多一个成功；其他已发记录、待发相对顺序、问题记录
不变；重启后结果保留；既有档案不变；控制席 403（缺失或失效凭据）。

运行：python3 tests/withdraw.test.py
"""
import os
import sqlite3
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="cue-withdraw-test-")
os.environ["DB_PATH"] = os.path.join(_tmp, "cues.db")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402

failures = 0


def check(cond, msg):
    global failures
    if cond:
        print("  ✓ " + msg)
    else:
        failures += 1
        print("  ✗ " + msg)


CUES3 = [
    {"number": "A1", "content": "灯光渐亮"},
    {"number": "A2", "content": "音乐起"},
    {"number": "A3", "content": "幕落"},
]
CUES4 = CUES3 + [{"number": "A4", "content": "谢幕"}]


def cue_row(cue_id):
    """直接读库核验一条提示的存储字段。"""
    conn = sqlite3.connect(main.DB_PATH)
    try:
        return conn.execute(
            "SELECT status, content, issued_content, issued_at, issued_seq "
            "FROM cues WHERE id = ?",
            (cue_id,),
        ).fetchone()
    finally:
        conn.close()


def fresh_issued(c, confirms=1):
    """重置并开新场（4 条），确认 confirms 条；返回 (version, ids)。"""
    c.post("/api/reset")
    r = c.post("/api/setup", json={"cues": CUES4})
    v = r.json()["version"]
    ids = [p["id"] for p in r.json()["pending"]]
    for _ in range(confirms):
        r = c.post("/api/confirm", json={"version": v})
        v = r.json()["version"]
    return v, ids


def main_flow():
    with TestClient(main.app) as c:
        print("P1 基本撤回：快照回待发队首，保留 ID 与编号，清除发出信息")
        r = c.post("/api/setup", json={"cues": CUES4})
        s = r.json()
        ids = [p["id"] for p in s["pending"]]
        v = s["version"]
        r = c.post("/api/confirm", json={"version": v})
        v = r.json()["version"]
        r = c.post("/api/confirm", json={"version": v})
        v = r.json()["version"]
        a1_issued = r.json()["issued"][0]
        a2_issued_at = r.json()["issued"][1]["issued_at"]
        # 重排剩余待发为 [A4, A3]，验证撤回后相对顺序不变
        r = c.post("/api/reorder", json={"version": v, "order": [ids[3], ids[2]]})
        v = r.json()["version"]
        check([p["id"] for p in r.json()["pending"]] == [ids[3], ids[2]],
              "重排后待发为 A4、A3")

        r = c.post("/api/withdraw", json={"version": v, "cue_id": ids[1]})
        check(r.status_code == 200, "撤回末条成功")
        s = r.json()
        check(s["version"] == v + 1, "撤回后版本号 +1")
        v = s["version"]
        check(s["status"] == "live", "演出中撤回后仍为演出中")
        check([p["id"] for p in s["pending"]] == [ids[1], ids[3], ids[2]],
              "撤回的提示插到待发队首，其余待发相对顺序不变")
        check(s["pending"][0]["number"] == "A2" and s["pending"][0]["content"] == "音乐起",
              "保留 ID 与编号，待发内容为发出快照")
        check([i["id"] for i in s["issued"]] == [ids[0]], "其他已发记录不变")
        check(s["issued"][0]["issued_at"] == a1_issued["issued_at"],
              "其他已发记录的发出时间不变")
        check(s["current"]["id"] == ids[1], "当前待发指向撤回的提示")
        row = cue_row(ids[1])
        check(row[0] == "pending" and row[2] is None and row[3] is None and row[4] is None,
              "数据库中发出快照、发出时间、发出序号已清除")
        check(row[1] == "音乐起", "数据库中待发内容即发出快照")

        print("P2 再次确认记新时间")
        r = c.post("/api/confirm", json={"version": v})
        check(r.status_code == 200, "撤回后可再次确认")
        s = r.json()
        v = s["version"]
        check([i["id"] for i in s["issued"]] == [ids[0], ids[1]],
              "再次确认后该条回到已发末尾")
        new_at = s["issued"][1]["issued_at"]
        check(new_at is not None and new_at > a2_issued_at, "再次确认记新的发出时间")

        print("P3 完成态撤回：回到演出中")
        r = c.post("/api/confirm", json={"version": v})  # A4
        v = r.json()["version"]
        r = c.post("/api/confirm", json={"version": v})  # A3
        v = r.json()["version"]
        check(r.json()["status"] == "done", "全部发出后为完成态")
        r = c.post("/api/withdraw", json={"version": v, "cue_id": ids[2]})
        check(r.status_code == 200, "完成态可撤回")
        s = r.json()
        v = s["version"]
        check(s["status"] == "live", "完成态撤回后回到演出中")
        check([p["id"] for p in s["pending"]] == [ids[2]], "撤回的提示成为唯一待发")
        check(len(s["issued"]) == 3, "其余已发记录不变")

        print("P4 暂停时可撤回，暂停标记不变")
        r = c.post("/api/pause", json={"version": v, "paused": True})
        v = r.json()["version"]
        r = c.post("/api/withdraw", json={"version": v, "cue_id": ids[3]})
        check(r.status_code == 200, "暂停时可撤回")
        s = r.json()
        v = s["version"]
        check(s["paused"] is True, "撤回后暂停标记不变")
        check([p["id"] for p in s["pending"]] == [ids[3], ids[2]],
              "暂停中撤回仍插到待发队首")
        r = c.post("/api/confirm", json={"version": v})
        check(r.status_code == 409, "暂停期间确认仍被拒绝")
        r = c.post("/api/pause", json={"version": v, "paused": False})
        v = r.json()["version"]

        print("P5 409：版本过期 / 目标不符 / 无已发提示 / 开场前")
        # 当前：已发 [A1, A2]，待发 [A4, A3]，最后已发为 A2
        r = c.post("/api/withdraw", json={"version": v + 99, "cue_id": ids[1]})
        check(r.status_code == 409, "版本过期返回 409")
        check("state" in r.json() and r.json()["state"]["version"] == v,
              "409 附最新状态")
        r = c.post("/api/withdraw", json={"version": v, "cue_id": ids[0]})
        check(r.status_code == 409, "目标不是最后已发返回 409")
        r = c.post("/api/withdraw", json={"version": v, "cue_id": 99999})
        check(r.status_code == 409, "目标不存在返回 409")
        s = c.get("/api/state").json()
        check(s["version"] == v and len(s["issued"]) == 2 and len(s["pending"]) == 2,
              "409 后数据不变")

        r = c.post("/api/reset")
        r = c.post("/api/setup", json={"cues": CUES3})
        v = r.json()["version"]
        new_ids = [p["id"] for p in r.json()["pending"]]
        r = c.post("/api/withdraw", json={"version": v, "cue_id": new_ids[0]})
        check(r.status_code == 409, "无已发提示返回 409")
        r = c.post("/api/reset")
        v = r.json()["version"]
        r = c.post("/api/withdraw", json={"version": v, "cue_id": new_ids[0]})
        check(r.status_code == 409, "开场前返回 409")

        print("P6 同版本竞争：最多一个成功")
        v, ids = fresh_issued(c)
        r1 = c.post("/api/withdraw", json={"version": v, "cue_id": ids[0]})
        r2 = c.post("/api/withdraw", json={"version": v, "cue_id": ids[0]})
        check(sorted([r1.status_code, r2.status_code]) == [200, 409],
              "同版本两个撤回：一个 200 一个 409")

        v, ids = fresh_issued(c)
        r1 = c.post("/api/withdraw", json={"version": v, "cue_id": ids[0]})
        r2 = c.post("/api/confirm", json={"version": v})
        check(sorted([r1.status_code, r2.status_code]) == [200, 409],
              "撤回与确认同版本：最多一个成功")

        v, ids = fresh_issued(c)
        r1 = c.post("/api/edit", json={"version": v, "cue_id": ids[1], "content": "改写"})
        r2 = c.post("/api/withdraw", json={"version": v, "cue_id": ids[0]})
        check(sorted([r1.status_code, r2.status_code]) == [200, 409],
              "撤回与编辑同版本：最多一个成功")

        v, ids = fresh_issued(c)
        r1 = c.post("/api/reorder",
                    json={"version": v, "order": [ids[2], ids[1], ids[3]]})
        r2 = c.post("/api/withdraw", json={"version": v, "cue_id": ids[0]})
        check(sorted([r1.status_code, r2.status_code]) == [200, 409],
              "撤回与重排同版本：最多一个成功")

        v, ids = fresh_issued(c)
        r1 = c.post("/api/archive", json={"version": v, "name": "竞争场"})
        r2 = c.post("/api/withdraw", json={"version": v, "cue_id": ids[0]})
        check(sorted([r1.status_code, r2.status_code]) == [200, 409],
              "撤回与归档同版本：最多一个成功")

        v, ids = fresh_issued(c)
        r1 = c.post("/api/issues", json={"version": v, "cue_id": ids[0], "text": "音乐晚了"})
        r2 = c.post("/api/withdraw", json={"version": v, "cue_id": ids[0]})
        check(sorted([r1.status_code, r2.status_code]) == [200, 409],
              "撤回与问题记录同版本：最多一个成功")

        print("P7 撤回不影响其他已发记录、待发内容与问题记录")
        v, ids = fresh_issued(c, confirms=2)  # 已发 [A1, A2]，待发 [A3, A4]
        r = c.post("/api/issues", json={"version": v, "cue_id": ids[0], "text": "灯光晚了 1 秒"})
        v = r.json()["version"]
        r = c.post("/api/issues", json={"version": v, "cue_id": ids[2], "text": "音响测试"})
        v = r.json()["version"]
        before = c.get("/api/state").json()
        r = c.post("/api/withdraw", json={"version": v, "cue_id": ids[1]})
        check(r.status_code == 200, "撤回成功")
        s = r.json()
        v = s["version"]
        check(s["issues"] == before["issues"], "问题记录快照不变")
        check(s["issued"] == before["issued"][:1], "其他已发记录不变")
        check(s["pending"][1:] == before["pending"], "其余待发内容与顺序不变")

        print("P8 重启后结果保留")
        before = c.get("/api/state").json()
        with TestClient(main.app) as c2:  # 重新进入 lifespan，模拟重启服务
            after = c2.get("/api/state").json()
        check(after == before, "重启后撤回结果保留（状态一致）")

        print("P9 既有档案不受撤回影响")
        r = c.post("/api/archive", json={"version": v, "name": "撤回前存档"})
        check(r.status_code == 200, "归档成功")
        archives_before = c.get("/api/archives").json()
        aid = archives_before["archives"][0]["id"]
        detail_before = c.get(f"/api/archives/{aid}").json()
        r = c.post("/api/setup", json={"cues": CUES3})
        v = r.json()["version"]
        ids = [p["id"] for p in r.json()["pending"]]
        r = c.post("/api/confirm", json={"version": v})
        v = r.json()["version"]
        r = c.post("/api/withdraw", json={"version": v, "cue_id": ids[0]})
        check(r.status_code == 200, "新场撤回成功")
        check(c.get("/api/archives").json() == archives_before, "撤回后档案列表不变")
        check(c.get(f"/api/archives/{aid}").json() == detail_before,
              "撤回后档案详情不变")


def control_seat():
    """全新库：控制席启用后，撤回须携带当前凭据。"""
    main.DB_PATH = os.path.join(_tmp, "cues2.db")
    with TestClient(main.app) as c:
        print("P10 控制席：缺失或旧凭据 403，数据不变")
        v, ids = fresh_issued(c)
        r = c.post("/api/control/enable", json={"version": v})
        check(r.status_code == 200, "启用控制席")
        tok = r.json()["control_token"]
        v = r.json()["version"]

        r = c.post("/api/withdraw", json={"version": v, "cue_id": ids[0]})
        check(r.status_code == 403, "无凭据撤回返回 403")
        check("state" in r.json(), "403 附最新状态")
        r = c.post("/api/withdraw", json={"version": v, "cue_id": ids[0]},
                   headers={"X-Control-Token": "wrong"})
        check(r.status_code == 403, "错误凭据撤回返回 403")
        s = c.get("/api/state").json()
        check(s["version"] == v and len(s["issued"]) == 1, "403 后数据不变")

        r = c.post("/api/withdraw", json={"version": v, "cue_id": ids[0]},
                   headers={"X-Control-Token": tok})
        check(r.status_code == 200, "正确凭据可撤回")
        v = r.json()["version"]

        r = c.post("/api/confirm", json={"version": v}, headers={"X-Control-Token": tok})
        v = r.json()["version"]  # 重新发出，回到已发 1 条
        r = c.post("/api/control/takeover", json={"version": v})
        tok2 = r.json()["control_token"]
        v = r.json()["version"]
        r = c.post("/api/withdraw", json={"version": v, "cue_id": ids[0]},
                   headers={"X-Control-Token": tok})
        check(r.status_code == 403, "被接管后旧凭据撤回返回 403")
        r = c.post("/api/withdraw", json={"version": v, "cue_id": ids[0]},
                   headers={"X-Control-Token": tok2})
        check(r.status_code == 200, "新凭据可撤回")


main_flow()
control_seat()

print("\n全部通过" if failures == 0 else f"\n失败 {failures} 处断言")
sys.exit(0 if failures == 0 else 1)
