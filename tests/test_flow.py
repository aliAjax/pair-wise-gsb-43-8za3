import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import DomainError, ProcurementService, parse_time  # noqa: E402


class ProcurementFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = ProcurementService(Path(self.tmp.name) / "test.db")
        self.vendor1 = self.service.create_vendor("proc1", "procurement", "V-001", "启明科技", "vendor1")
        self.vendor2 = self.service.create_vendor("proc1", "procurement", "V-002", "远山系统", "vendor2")
        criteria = [
            {"name": "报价", "weight": 60, "kind": "cost", "max_value": 1000000},
            {"name": "质量", "weight": 40, "kind": "direct", "max_value": 100},
        ]
        self.tender = self.service.create_tender(
            "proc1", "procurement", "T-001", "数据中心设备", (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat(), criteria
        )
        self.tender = self.service.publish_tender("proc1", "procurement", self.tender["id"], self.tender["version"])

    def tearDown(self):
        self.tmp.cleanup()

    def bid(self, vendor, actor, number, price, quality):
        return self.service.submit_bid(actor, "vendor", self.tender["id"], vendor["id"], {"报价": price, "质量": quality}, price)

    def test_complete_sealed_bid_open_evaluate_and_award_flow(self):
        self.bid(self.vendor1, "vendor1", "B1", 800000, 90)
        self.bid(self.vendor2, "vendor2", "B2", 700000, 80)
        before = self.service.get_tender("vendor1", "vendor", self.tender["id"])
        self.assertEqual("sealed", before["bids"][0]["status"])
        self.assertNotIn("payload", before["bids"][0])
        time.sleep(2.1)
        opened = self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])
        self.assertEqual(2, len(opened["bids"]))
        self.service.evaluate_bid("eval1", "evaluator", opened["bids"][0]["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval2", "evaluator", opened["bids"][0]["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval1", "evaluator", opened["bids"][1]["id"], {"报价": 700000, "质量": 80})
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        self.assertEqual("opened", current["tender"]["status"])
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], current["tender"]["version"])
        self.assertEqual("awarded", award["tender"]["status"])
        self.assertEqual(opened["bids"][0]["id"], award["award"]["winner"]["bid_id"])

    def test_conflict_and_duplicate_evaluation_are_rejected(self):
        bid = self.bid(self.vendor1, "vendor1", "B3", 800000, 90)
        time.sleep(2.1)
        self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])
        self.service.declare_conflict("eval1", "evaluator", self.tender["id"], "eval1", self.vendor1["id"], "曾受雇于供应商")
        with self.assertRaises(DomainError) as ctx:
            self.service.evaluate_bid("eval1", "evaluator", bid["id"], {"报价": 800000, "质量": 90})
        self.assertEqual(403, ctx.exception.status)
        self.service.evaluate_bid("eval2", "evaluator", bid["id"], {"报价": 800000, "质量": 90})
        with self.assertRaises(DomainError) as ctx2:
            self.service.evaluate_bid("eval2", "evaluator", bid["id"], {"报价": 800000, "质量": 90})
        self.assertEqual(409, ctx2.exception.status)

    def test_complaint_reevaluation_award_block_and_permissions(self):
        bid = self.bid(self.vendor1, "vendor1", "B4", 800000, 90)
        time.sleep(2.1)
        self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])
        self.service.evaluate_bid("eval1", "evaluator", bid["id"], {"报价": 800000, "质量": 90})
        complaint = self.service.submit_complaint("vendor1", "vendor", self.tender["id"], "评分标准理解有误")
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        with self.assertRaises(DomainError):
            self.service.award_tender("sup1", "supervisor", self.tender["id"], current["tender"]["version"])
        resolved = self.service.resolve_complaint("sup1", "supervisor", complaint["id"], "accepted", "按新规则重评")
        self.assertEqual("accepted", resolved["status"])
        updated = self.service.get_tender("sup1", "supervisor", self.tender["id"])["tender"]
        self.assertEqual("reevaluation", updated["status"])
        self.assertEqual(2, updated["evaluation_round"])
        with self.assertRaises(DomainError) as ctx:
            self.service.open_bids("vendor1", "vendor", self.tender["id"], updated["version"])
        self.assertEqual(403, ctx.exception.status)


class ReverseAuctionFlowTest(unittest.TestCase):
    """开标评审后的限时反向竞价：低于最低价、版本校验、顺延、幂等、排名重算、恢复与授标门禁。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"
        self.service = ProcurementService(self.db)
        self.vendor1 = self.service.create_vendor("proc1", "procurement", "V-101", "启明科技", "vendor1")
        self.vendor2 = self.service.create_vendor("proc1", "procurement", "V-102", "远山系统", "vendor2")
        self.vendor3 = self.service.create_vendor("proc1", "procurement", "V-103", "蓝海集成", "vendor3")
        criteria = [
            {"name": "报价", "weight": 60, "kind": "cost", "max_value": 1000000},
            {"name": "质量", "weight": 40, "kind": "direct", "max_value": 100},
        ]
        tender = self.service.create_tender(
            "proc1", "procurement", "T-AUC", "机房改造",
            (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat(), criteria,
        )
        tender = self.service.publish_tender("proc1", "procurement", tender["id"], tender["version"])
        self.tender_id = tender["id"]
        self.service.submit_bid("vendor1", "vendor", self.tender_id, self.vendor1["id"], {"报价": 900000, "质量": 90}, 900000)
        self.service.submit_bid("vendor2", "vendor", self.tender_id, self.vendor2["id"], {"报价": 800000, "质量": 85}, 800000)
        self.service.submit_bid("vendor3", "vendor", self.tender_id, self.vendor3["id"], {"报价": 700000, "质量": 80}, 700000)
        time.sleep(2.1)
        self.service.open_bids("proc1", "procurement", self.tender_id, tender["version"])
        for bid_id, price, quality in self._bid_specs():
            self.service.evaluate_bid("eval1", "evaluator", bid_id, {"报价": price, "质量": quality})

    def tearDown(self):
        self.tmp.cleanup()

    def _bids(self):
        return {b["vendor_id"]: b for b in self.service.get_tender("proc1", "procurement", self.tender_id)["bids"]}

    def _bid_specs(self):
        quality = {self.vendor1["id"]: 90, self.vendor2["id"]: 85, self.vendor3["id"]: 80}
        return [(b["id"], b["price"], quality[vendor_id]) for vendor_id, b in self._bids().items()]

    def _tender_version(self):
        return self.service.get_tender("proc1", "procurement", self.tender_id)["tender"]["version"]

    def _start(self, duration=300):
        return self.service.start_auction("proc1", "procurement", self.tender_id, self._tender_version(), duration_seconds=duration)

    def test_offer_undercut_version_check_and_ranking_recompute(self):
        started = self._start()
        auction = started["auction"]
        bids = self._bids()
        bid1, bid3 = bids[self.vendor1["id"]], bids[self.vendor3["id"]]
        self.assertEqual("active", auction["status"])
        self.assertEqual(700000, auction["current_lowest"])
        self.assertEqual(1, auction["ranking_version"])
        self.assertEqual(bid3["id"], auction["ranking"][0]["bid_id"])
        self.assertEqual(92.0, auction["ranking"][0]["score"])
        # 不低于当前最低价 → 409 并带回当前最低价
        with self.assertRaises(DomainError) as ctx:
            self.service.submit_offer("vendor1", "vendor", self.tender_id, bid1["id"], 750000, bid1["version"], "k-high")
        self.assertEqual(409, ctx.exception.status)
        self.assertEqual(700000, ctx.exception.details["current_lowest"])
        # 投标版本不一致 → 409 并带回当前最低价
        with self.assertRaises(DomainError) as ctx2:
            self.service.submit_offer("vendor1", "vendor", self.tender_id, bid1["id"], 650000, bid1["version"] + 9, "k-stale")
        self.assertEqual(409, ctx2.exception.status)
        self.assertEqual(700000, ctx2.exception.details["current_lowest"])
        # 有效降价：最低价更新，旧排名失效重算
        result = self.service.submit_offer("vendor1", "vendor", self.tender_id, bid1["id"], 650000, bid1["version"], "k-1")
        auction = result["auction"]
        self.assertFalse(result["replay"])
        self.assertEqual(650000, auction["current_lowest"])
        self.assertEqual(2, auction["ranking_version"])
        self.assertEqual(bid1["id"], auction["ranking"][0]["bid_id"])
        self.assertEqual(96.0, auction["ranking"][0]["score"])
        scores = {item["bid_id"]: item["score"] for item in auction["ranking"]}
        self.assertEqual(87.71, scores[bid3["id"]])
        self.assertEqual(82.75, scores[bids[self.vendor2["id"]]["id"]])
        event_types = [e["event_type"] for e in result["events"]]
        self.assertIn("auction.offer_accepted", event_types)
        self.assertEqual(2, event_types.count("auction.ranking_recomputed"))

    def test_last_two_minutes_extension_and_duplicate_replay(self):
        started = self._start(duration=120)
        ends0 = parse_time(started["auction"]["ends_at"])
        bid1 = self._bids()[self.vendor1["id"]]
        # 最后两分钟内的有效降价 → 结束时间顺延两分钟
        first = self.service.submit_offer("vendor1", "vendor", self.tender_id, bid1["id"], 650000, bid1["version"], "ext-1")
        ends1 = parse_time(first["auction"]["ends_at"])
        self.assertEqual(ends0 + timedelta(seconds=120), ends1)
        self.assertEqual(1, first["auction"]["extensions_count"])
        self.assertEqual(1, first["offer"]["extended"])
        # 重复提交同一幂等键 → 返回首次结果，不再加时
        bid1_stale = bid1  # 版本已过期，但幂等重放优先于版本校验
        replay = self.service.submit_offer("vendor1", "vendor", self.tender_id, bid1_stale["id"], 650000, bid1_stale["version"], "ext-1")
        self.assertTrue(replay["replay"])
        self.assertEqual(first["offer"]["id"], replay["offer"]["id"])
        self.assertEqual(ends1, parse_time(replay["auction"]["ends_at"]))
        self.assertEqual(1, replay["auction"]["extensions_count"])
        self.assertEqual(1, len(replay["offers"]))
        # 窗口外不降价的重复报价（新幂等键、价格不再低于最低价）→ 409 不加时
        with self.assertRaises(DomainError):
            self.service.submit_offer("vendor1", "vendor", self.tender_id, bid1_stale["id"], 660000, bid1_stale["version"], "ext-2")

    def test_no_extension_outside_last_two_minutes(self):
        started = self._start(duration=300)
        ends0 = parse_time(started["auction"]["ends_at"])
        bid1 = self._bids()[self.vendor1["id"]]
        result = self.service.submit_offer("vendor1", "vendor", self.tender_id, bid1["id"], 650000, bid1["version"], "noext-1")
        self.assertEqual(ends0, parse_time(result["auction"]["ends_at"]))
        self.assertEqual(0, result["auction"]["extensions_count"])
        self.assertEqual(0, result["offer"]["extended"])

    def test_concurrent_offers_loser_keeps_input_and_sees_lowest(self):
        self._start()
        bid1 = self._bids()[self.vendor1["id"]]
        barrier = threading.Barrier(2)
        results = []

        def offer(price, key):
            barrier.wait()
            try:
                res = self.service.submit_offer("vendor1", "vendor", self.tender_id, bid1["id"], price, bid1["version"], key)
                results.append(("ok", res))
            except DomainError as exc:
                results.append(("err", exc))

        threads = [threading.Thread(target=offer, args=(650000, "c-1")),
                   threading.Thread(target=offer, args=(640000, "c-2"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        oks = [r for kind, r in results if kind == "ok"]
        errs = [r for kind, r in results if kind == "err"]
        self.assertEqual(1, len(oks))
        self.assertEqual(1, len(errs))
        err = errs[0]
        self.assertEqual(409, err.status)
        # 后到者看到当前最低价（即先到者的成交价），输入未被系统保存
        self.assertEqual(oks[0]["offer"]["price"], err.details["current_lowest"])
        view = self.service.get_auction("proc1", "procurement", self.tender_id)
        self.assertEqual(1, len(view["offers"]))
        self.assertEqual(oks[0]["offer"]["price"], view["auction"]["current_lowest"])

    def test_only_opened_qualified_conflictfree_vendors_can_enter(self):
        # 未开标项目不能发起竞价
        draft = self.service.create_tender(
            "proc1", "procurement", "T-DRAFT", "未开标项目",
            (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            [{"name": "报价", "weight": 100, "kind": "cost", "max_value": 1000000}],
        )
        with self.assertRaises(DomainError) as ctx:
            self.service.start_auction("proc1", "procurement", draft["id"], draft["version"], duration_seconds=300)
        self.assertEqual(409, ctx.exception.status)
        with self.assertRaises(DomainError) as ctx_role:
            self.service.start_auction("vendor1", "vendor", self.tender_id, self._tender_version(), duration_seconds=300)
        self.assertEqual(403, ctx_role.exception.status)
        # 废标与利益冲突的供应商被排除在竞价之外
        bids = self._bids()
        bid2, bid3 = bids[self.vendor2["id"]], bids[self.vendor3["id"]]
        self.service.disqualify_bid("proc1", "procurement", bid2["id"], "虚假材料", bid2["version"])
        self.service.declare_conflict("eval9", "evaluator", self.tender_id, "eval9", self.vendor3["id"], "亲属任职")
        started = self._start()
        self.assertEqual(900000, started["auction"]["current_lowest"])
        self.assertEqual([bids[self.vendor1["id"]]["id"]], [i["bid_id"] for i in started["auction"]["ranking"]])
        # 已废标供应商不能报价
        with self.assertRaises(DomainError) as ctx2:
            self.service.submit_offer("vendor2", "vendor", self.tender_id, bid2["id"], 850000, bid2["version"], "e-dq")
        self.assertEqual(409, ctx2.exception.status)
        # 有利益冲突的供应商不能报价
        with self.assertRaises(DomainError) as ctx3:
            self.service.submit_offer("vendor3", "vendor", self.tender_id, bid3["id"], 850000, bid3["version"], "e-co")
        self.assertEqual(403, ctx3.exception.status)
        # 不能替别人的投标报价
        bid1 = bids[self.vendor1["id"]]
        with self.assertRaises(DomainError) as ctx4:
            self.service.submit_offer("vendor2", "vendor", self.tender_id, bid1["id"], 850000, bid1["version"], "e-other")
        self.assertEqual(403, ctx4.exception.status)
        # 符合条件的供应商可以正常降价进入
        ok = self.service.submit_offer("vendor1", "vendor", self.tender_id, bid1["id"], 850000, bid1["version"], "e-ok")
        self.assertEqual(850000, ok["auction"]["current_lowest"])

    def test_award_blocked_until_auction_finished(self):
        self._start(duration=300)
        with self.assertRaises(DomainError) as ctx:
            self.service.award_tender("sup1", "supervisor", self.tender_id, self._tender_version())
        self.assertEqual(409, ctx.exception.status)
        self.assertIn("ends_at", ctx.exception.details)

    def test_award_after_finish_uses_frozen_snapshot(self):
        self._start(duration=2)
        time.sleep(2.1)
        view = self.service.get_auction("sup1", "supervisor", self.tender_id)
        self.assertEqual("finished", view["auction"]["status"])
        snapshot = view["auction"]["final_snapshot"]
        bid3 = self._bids()[self.vendor3["id"]]
        self.assertEqual(bid3["id"], snapshot["winner"]["bid_id"])
        self.assertEqual(700000, snapshot["final_lowest"])
        # 结束后报价被拒绝
        with self.assertRaises(DomainError) as ctx:
            self.service.submit_offer("vendor1", "vendor", self.tender_id, self._bids()[self.vendor1["id"]]["id"],
                                      600000, 1, "after-end")
        self.assertEqual(409, ctx.exception.status)
        # 授标按冻结快照成交
        award = self.service.award_tender("sup1", "supervisor", self.tender_id, self._tender_version())
        self.assertEqual("awarded", award["tender"]["status"])
        self.assertEqual(bid3["id"], award["award"]["winner"]["bid_id"])
        self.assertEqual(view["auction"]["id"], award["award"]["auction_id"])

    def test_crash_recovery_finalizes_expired_auction_and_restores_events(self):
        self._start(duration=300)
        bid1 = self._bids()[self.vendor1["id"]]
        self.service.submit_offer("vendor1", "vendor", self.tender_id, bid1["id"], 650000, bid1["version"], "rec-1")
        # 模拟服务崩溃期间竞价到期：把结束时间拨到过去，再重启服务
        past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(timespec="seconds")
        with sqlite3.connect(self.db) as raw:
            raw.execute("UPDATE auctions SET ends_at=?", (past,))
        restarted = ProcurementService(self.db)
        view = restarted.get_auction("sup1", "supervisor", self.tender_id)
        self.assertEqual("finished", view["auction"]["status"])
        snapshot = view["auction"]["final_snapshot"]
        self.assertEqual(650000, snapshot["final_lowest"])
        self.assertEqual(bid1["id"], snapshot["winner"]["bid_id"])
        self.assertEqual("recovered", snapshot["finish_reason"])
        event_types = [e["event_type"] for e in view["events"]]
        self.assertIn("auction.started", event_types)
        self.assertIn("auction.offer_accepted", event_types)
        self.assertIn("auction.finished", event_types)
        with self.assertRaises(DomainError) as ctx:
            restarted.submit_offer("vendor1", "vendor", self.tender_id, bid1["id"], 600000, 1, "rec-2")
        self.assertEqual(409, ctx.exception.status)

    def test_restart_restores_running_auction_timer(self):
        self._start(duration=300)
        restarted = ProcurementService(self.db)
        view = restarted.get_auction("proc1", "procurement", self.tender_id)
        self.assertEqual("active", view["auction"]["status"])
        self.assertGreater(view["auction"]["remaining_seconds"], 0)
        self.assertLessEqual(view["auction"]["remaining_seconds"], 300)
        event_types = [e["event_type"] for e in view["events"]]
        self.assertIn("auction.recovered", event_types)


if __name__ == "__main__":
    unittest.main()