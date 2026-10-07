import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import DomainError, ProcurementService  # noqa: E402

CRITERIA = [
    {"name": "报价", "weight": 60, "kind": "cost", "max_value": 1000000},
    {"name": "质量", "weight": 40, "kind": "direct", "max_value": 100},
]


class FakeClock:
    def __init__(self, dt):
        self.dt = dt

    def __call__(self):
        return self.dt

    def advance(self, seconds):
        self.dt += timedelta(seconds=seconds)


class ReverseAuctionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "auction.db"
        self.service = ProcurementService(self.db_path, auto_sweep=False)
        self.v1 = self.service.create_vendor("proc1", "procurement", "V-001", "启明科技", "vendor1")
        self.v2 = self.service.create_vendor("proc1", "procurement", "V-002", "远山系统", "vendor2")
        self.v3 = self.service.create_vendor("proc1", "procurement", "V-003", "北岳集成", "vendor3")
        self.deadline = (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()

    def tearDown(self):
        self.service.shutdown()
        self.tmp.cleanup()

    def prepare_tender(self, p1=(800000, 90), p2=(700000, 80), p3=(600000, 70)):
        tender = self.service.create_tender("proc1", "procurement", "T-A1", "数据中心设备", self.deadline, CRITERIA)
        tender = self.service.publish_tender("proc1", "procurement", tender["id"], tender["version"])
        b1 = self.service.submit_bid("vendor1", "vendor", tender["id"], self.v1["id"], {"报价": p1[0], "质量": p1[1]}, p1[0])
        b2 = self.service.submit_bid("vendor2", "vendor", tender["id"], self.v2["id"], {"报价": p2[0], "质量": p2[1]}, p2[0])
        b3 = self.service.submit_bid("vendor3", "vendor", tender["id"], self.v3["id"], {"报价": p3[0], "质量": p3[1]}, p3[0])
        time.sleep(2.1)
        opened = self.service.open_bids("proc1", "procurement", tender["id"], tender["version"])
        # v3 的评审人存在利益冲突记录 → 竞价准入时应被排除
        self.service.declare_conflict("eval3", "evaluator", tender["id"], "eval3", self.v3["id"], "曾任职于该供应商")
        for bid, values in [(b1, {"报价": p1[0], "质量": p1[1]}), (b2, {"报价": p2[0], "质量": p2[1]}),
                            (b3, {"报价": p3[0], "质量": p3[1]})]:
            self.service.evaluate_bid("eval1", "evaluator", bid["id"], values)
        return tender, b1, b2, b3

    def start(self, tender, minutes=60):
        clock = FakeClock(datetime.now(timezone.utc))
        self.service.clock = clock
        auction = self.service.start_auction("proc1", "procurement", tender["id"],
                                             duration_minutes=minutes,
                                             extension_window_seconds=120, extension_seconds=120)
        return clock, auction

    def refresh(self, *bids):
        out = []
        with self.service.connect() as conn:
            for bid in bids:
                out.append(dict(conn.execute("SELECT * FROM bids WHERE id=?", (bid["id"],)).fetchone()))
        return out[0] if len(out) == 1 else out

    def quote(self, bid_id, price, version, token, actor):
        return self.service.submit_auction_quote(actor, "vendor", 1, bid_id, price, version, token)

    def test_admission_only_opened_non_disqualified_conflict_free(self):
        tender, b1, b2, b3 = self.prepare_tender()
        # 未开标不能启动
        draft = self.service.create_tender(
            "proc1", "procurement", "T-A2", "交换机", (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), CRITERIA)
        with self.assertRaises(DomainError) as ctx:
            self.service.start_auction("proc1", "procurement", draft["id"])
        self.assertEqual(409, ctx.exception.status)
        clock, auction = self.start(tender)
        b1, b2, b3 = self.refresh(b1, b2, b3)
        self.assertEqual("active", auction["status"])
        entrants = {(e["bid_id"], e["vendor_id"]) for e in auction["entries"] if e.get("bid_id")}
        self.assertEqual({(b1["id"], self.v1["id"]), (b2["id"], self.v2["id"])}, entrants)
        self.assertEqual(700000, auction["initial_lowest_price"])
        self.assertEqual(b2["id"], auction["current_lead_bid_id"])
        # 名册冻结：不能再废标、再评分
        with self.assertRaises(DomainError):
            self.service.disqualify_bid("proc1", "procurement", b1["id"], "竞价后试图废标", b1["version"])
        with self.assertRaises(DomainError):
            self.service.evaluate_bid("eval1", "evaluator", b1["id"], {"报价": 800000, "质量": 91})
        # 重复启动被拒绝
        with self.assertRaises(DomainError):
            self.service.start_auction("proc1", "procurement", tender["id"])

    def test_quote_must_beat_lowest_and_match_bid_version(self):
        tender, b1, b2, b3 = self.prepare_tender()
        clock, auction = self.start(tender)
        b1, b2, b3 = self.refresh(b1, b2, b3)
        # 名册外供应商
        with self.assertRaises(DomainError) as ctx:
            self.quote(b3["id"], 500000, b3["version"], "tok-x", "vendor3")
        self.assertEqual(403, ctx.exception.status)
        # 非降价：输入保留，回传当前最低价
        with self.assertRaises(DomainError) as ctx:
            self.quote(b1["id"], 700000, b1["version"], "tok-eq", "vendor1")
        self.assertEqual(409, ctx.exception.status)
        self.assertEqual(700000, ctx.exception.details["current_lowest_price"])
        self.assertEqual(700000, ctx.exception.details["entered_price"])
        # 投标版本不匹配
        with self.assertRaises(DomainError) as ctx:
            self.quote(b1["id"], 690000, b1["version"] + 5, "tok-ver", "vendor1")
        self.assertEqual(409, ctx.exception.status)
        self.assertEqual(700000, ctx.exception.details["current_lowest_price"])
        # 有效报价
        ok = self.quote(b1["id"], 690000, b1["version"], "tok-1", "vendor1")
        self.assertTrue(ok["accepted"])
        self.assertEqual(690000, ok["auction"]["current_lowest_price"])
        self.assertEqual(1, ok["auction"]["ranking_epoch"])
        # 开标后版本为 2，成交一次后投标版本推进到 3
        self.assertEqual(3, self._bid_version(b1["id"]))
        # 窗口外报价不顺延
        self.assertEqual(0, ok["auction"]["extension_count"])
        # 重复令牌：幂等重放，不产生新序号、不加时
        replay = self.quote(b1["id"], 690000, b1["version"], "tok-1", "vendor1")
        self.assertTrue(replay["replayed"])
        self.assertEqual(1, replay["auction"]["quote_seq"])
        self.assertEqual(0, replay["auction"]["extension_count"])
        # 另一供应商用自己的版本继续降价
        ok2 = self.quote(b2["id"], 680000, b2["version"], "tok-2", "vendor2")
        self.assertEqual(680000, ok2["auction"]["current_lowest_price"])
        # vendor1 若还拿着成交前的旧版本（2）会收到冲突，详情回传当前最低价并保留输入
        with self.assertRaises(DomainError) as ctx:
            self.quote(b1["id"], 670000, 2, "tok-stale", "vendor1")
        self.assertEqual(409, ctx.exception.status)
        self.assertEqual(680000, ctx.exception.details["current_lowest_price"])
        self.quote(b1["id"], 670000, 3, "tok-3", "vendor1")

    def _bid_version(self, bid_id):
        with self.service.connect() as conn:
            return conn.execute("SELECT version FROM bids WHERE id=?", (bid_id,)).fetchone()["version"]

    def test_last_two_minutes_extends_once_per_accepted_quote(self):
        tender, b1, b2, b3 = self.prepare_tender()
        clock, auction = self.start(tender, minutes=60)
        b1, b2, b3 = self.refresh(b1, b2, b3)
        original_ends = auction["ends_at"]
        self.quote(b1["id"], 690000, b1["version"], "q1", "vendor1")
        # 进入最后两分钟：有效降价顺延两分钟
        clock.advance(60 * 60 - 60)
        ok = self.quote(b2["id"], 680000, b2["version"], "q2", "vendor2")
        self.assertTrue(ok["quote"]["extended"])
        extended_ends = ok["auction"]["ends_at"]
        self.assertGreater(extended_ends, original_ends)
        self.assertEqual(1, ok["auction"]["extension_count"])
        # 重复提交（同令牌）不能再次加时
        replay = self.quote(b2["id"], 680000, b2["version"], "q2", "vendor2")
        self.assertTrue(replay["replayed"])
        self.assertEqual(extended_ends, replay["auction"]["ends_at"])
        self.assertEqual(1, replay["auction"]["extension_count"])
        # 新的最后两分钟里再次有效降价，可继续顺延
        clock.advance(120)
        ok3 = self.quote(b1["id"], 650000, 3, "q3", "vendor1")
        self.assertTrue(ok3["quote"]["extended"])
        self.assertEqual(2, ok3["auction"]["extension_count"])
        # 到时后提交：自动收盘并拒绝
        clock.advance(200)
        with self.assertRaises(DomainError) as ctx:
            self.quote(b2["id"], 640000, 3, "q4-late", "vendor2")
        self.assertEqual(409, ctx.exception.status)
        closed = self.service.get_auction("proc1", "procurement", 1)
        self.assertEqual("closed", closed["status"])
        self.assertEqual(650000, closed["close_snapshot"]["final_lowest_price"])
        self.assertTrue(closed["close_snapshot"]["recovered"])
        self.assertEqual(closed["snapshot_hash"], closed["close_snapshot"] and self._hash(closed["close_snapshot"]))

    @staticmethod
    def _hash(snapshot):
        import hashlib
        import json
        raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def test_ranking_recalculated_on_fresh_prices_and_award_gate(self):
        # 初始：vendor1 质量高居首；竞价后 vendor2 大幅降价反超
        tender, b1, b2, b3 = self.prepare_tender(p1=(800000, 90), p2=(700000, 80))
        clock, auction = self.start(tender, minutes=60)
        b1, b2, b3 = self.refresh(b1, b2, b3)
        self.quote(b1["id"], 690000, b1["version"], "r1", "vendor1")
        self.quote(b2["id"], 500000, b2["version"], "r2", "vendor2")
        # 未结束不能授标
        current = self.service.get_tender("sup1", "supervisor", tender["id"])["tender"]
        with self.assertRaises(DomainError) as ctx:
            self.service.award_tender("sup1", "supervisor", tender["id"], current["version"])
        self.assertEqual(409, ctx.exception.status)
        # 收盘冻结
        closed = self.service.close_auction("proc1", "procurement", 1)
        snapshot = closed["close_snapshot"]
        ranking = snapshot["ranking"]
        self.assertEqual(b2["id"], ranking[0]["bid_id"])
        self.assertEqual(1, ranking[0]["rank"])
        self.assertEqual(500000, ranking[0]["price"])
        # 92 = 100*0.6 + 80*0.4：评审轮次 b1 价格分被上限封顶为 100、以 96 分居首，
        # 竞价按最低价基准重算后 b2 满分价分、92 分反超，证明旧排名作废重算
        self.assertEqual(92.0, ranking[0]["score"])
        self.assertEqual(b1["id"], ranking[1]["bid_id"])
        # 结束后才能授标，且按冻结快照定标
        current = self.service.get_tender("sup1", "supervisor", tender["id"])["tender"]
        award = self.service.award_tender("sup1", "supervisor", tender["id"], current["version"])
        self.assertEqual("awarded", award["tender"]["status"])
        self.assertEqual(b2["id"], award["award"]["winner"]["bid_id"])
        self.assertEqual("reverse_auction", award["award"]["source"])
        self.assertEqual(auction["id"], award["award"]["auction_id"])

    def test_tampered_snapshot_blocks_award(self):
        tender, b1, b2, b3 = self.prepare_tender()
        clock, auction = self.start(tender, minutes=60)
        b1, b2, b3 = self.refresh(b1, b2, b3)
        self.quote(b1["id"], 690000, b1["version"], "s1", "vendor1")
        self.service.close_auction("proc1", "procurement", 1)
        with self.service.connect() as conn:
            row = conn.execute("SELECT close_snapshot FROM auctions WHERE id=1").fetchone()
            tampered = row["close_snapshot"].replace('"final_lowest_price": 690000.0', '"final_lowest_price": 1.0')
            conn.execute("UPDATE auctions SET close_snapshot=? WHERE id=1", (tampered,))
        current = self.service.get_tender("sup1", "supervisor", tender["id"])["tender"]
        with self.assertRaises(DomainError) as ctx:
            self.service.award_tender("sup1", "supervisor", tender["id"], current["version"])
        self.assertEqual(409, ctx.exception.status)

    def test_recovery_restores_timer_and_events_after_crash(self):
        tender, b1, b2, b3 = self.prepare_tender()
        clock, auction = self.start(tender, minutes=60)
        b1, b2, b3 = self.refresh(b1, b2, b3)
        ends_at = __import__("app").parse_time(auction["ends_at"])
        self.quote(b1["id"], 690000, b1["version"], "c1", "vendor1")
        # 模拟服务崩溃后重启：新实例读到活动竞价，计时沿用落库的 ends_at，事件仍在
        restart_clock = FakeClock(ends_at - timedelta(seconds=300))
        revived = ProcurementService(self.db_path, clock=restart_clock, auto_sweep=False)
        view = revived.get_auction("proc1", "procurement", 1)
        self.assertEqual("active", view["status"])
        self.assertEqual(auction["ends_at"], view["ends_at"])
        types = [e["type"] for e in view["events"]]
        self.assertEqual(["auction.started", "quote.accepted"], types)
        # 重启后仍可在结束前（窗口外）报价，不顺延
        revived.submit_auction_quote("vendor2", "vendor", 1, b2["id"], 680000, b2["version"], "c2")
        # 越过结束时间：显式恢复应补收盘，事件与快照完整
        restart_clock.advance(310)
        info = revived.recover()
        self.assertIn(1, info["closed_on_recovery"])
        closed = revived.get_auction("proc1", "procurement", 1)
        self.assertEqual("closed", closed["status"])
        self.assertTrue(closed["close_snapshot"]["recovered"])
        self.assertEqual("auction.closed", closed["events"][-1]["type"])
        # 再次“启动新进程”不会重复收盘，快照保持一致
        revived2 = ProcurementService(self.db_path, clock=restart_clock, auto_sweep=False)
        view = revived2.get_auction("proc1", "procurement", 1)
        self.assertEqual("closed", view["status"])
        self.assertEqual(closed["snapshot_hash"], view["snapshot_hash"])
        self.assertEqual([], revived2.recover()["closed_on_recovery"])
        revived.shutdown()
        revived2.shutdown()

    def test_concurrent_windows_loser_keeps_input_and_sees_lowest(self):
        tender, b1, b2, b3 = self.prepare_tender()
        clock, auction = self.start(tender, minutes=60)
        b1, b2, b3 = self.refresh(b1, b2, b3)
        results = []
        barrier = threading.Barrier(2)

        def fire(token, price):
            barrier.wait()
            try:
                results.append(("ok", self.quote(b1["id"], price, b1["version"], token, "vendor1")))
            except DomainError as exc:
                results.append(("err", exc))

        t_a = threading.Thread(target=fire, args=("win-a", 690000))
        t_b = threading.Thread(target=fire, args=("win-b", 695000))
        t_a.start(); t_b.start(); t_a.join(); t_b.join()
        oks = [r for r in results if r[0] == "ok"]
        errs = [r for r in results if r[0] == "err"]
        self.assertEqual(1, len(oks))
        self.assertEqual(1, len(errs))
        loser = errs[0][1]
        winner_price = oks[0][1]["quote"]["price"]
        loser_price = next(p for tok, p in [("win-a", 690000), ("win-b", 695000)] if p != winner_price)
        self.assertEqual(409, loser.status)
        # 后到者保留输入价格，并看到刚刚产生的当前最低价
        self.assertEqual(loser.details["entered_price"], loser_price)
        self.assertEqual(loser.details["current_lowest_price"], winner_price)
        # 后到者基于新版本立即改价即可成交
        retry = self.quote(b1["id"], winner_price - 5000, 3, "win-b-retry", "vendor1")
        self.assertTrue(retry["accepted"])
        self.assertEqual(winner_price - 5000, retry["auction"]["current_lowest_price"])

    def test_vendor_sees_lowest_but_not_other_roster_identity(self):
        tender, b1, b2, b3 = self.prepare_tender()
        clock, auction = self.start(tender)
        b1, b2, b3 = self.refresh(b1, b2, b3)
        self.quote(b1["id"], 690000, b1["version"], "v1q", "vendor1")
        view = self.service.get_auction("vendor2", "vendor", 1)
        self.assertEqual(690000, view["current_lowest_price"])
        own = [e for e in view["entries"] if e.get("is_mine")]
        others = [e for e in view["entries"] if not e.get("is_mine")]
        self.assertEqual(b2["id"], own[0]["bid_id"])
        self.assertNotIn("bid_id", others[0])
        # 管理员能看到完整名册
        full = self.service.get_auction("proc1", "procurement", 1)
        self.assertTrue(all("bid_id" in e for e in full["entries"]))


if __name__ == "__main__":
    unittest.main()
