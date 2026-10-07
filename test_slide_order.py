"""slide_order 库的自测（python3 -m unittest -v）。仅依赖标准库。"""

import random
import unittest

from slide_order import (
    CYCLE,
    DANGLING_REF,
    DUPLICATE_REF,
    MULTIPLE_HEADS,
    SELF_REF,
    Slide,
    derive_order,
)


def chain(items):
    """items 为 (编号, 名称) 列表，按给出的顺序连成 next 链，末尾无后继。"""
    slides = []
    for i, (sid, name) in enumerate(items):
        nxt = items[i + 1][1] if i + 1 < len(items) else None
        slides.append(Slide(sid, name, nxt))
    return slides


class TestSingleSlide(unittest.TestCase):
    def test_single_slide_no_next(self):
        result = derive_order([Slide(1, "slide1.xml")])
        self.assertEqual(result.order, ["slide1.xml"])
        self.assertEqual(result.numeric_order, ["slide1.xml"])
        self.assertEqual(result.anomalies, [])
        self.assertEqual(result.mismatches, [])

    def test_single_slide_self_ref_is_reported(self):
        result = derive_order([Slide(1, "slide1.xml", "slide1.xml")])
        self.assertEqual(result.order, ["slide1.xml"])
        kinds = {a.kind for a in result.anomalies}
        self.assertEqual(kinds, {SELF_REF})
        self.assertTrue(any(a.suggestion for a in result.anomalies))


class TestOrderMatchingNumbering(unittest.TestCase):
    def test_chain_in_numeric_order(self):
        deck = chain([(1, "slide1.xml"), (2, "slide2.xml"), (3, "slide3.xml")])
        result = derive_order(deck)
        self.assertEqual(result.order, result.numeric_order)
        self.assertEqual(result.mismatches, [])
        self.assertEqual(result.anomalies, [])


class TestRelationWinsOverNumbering(unittest.TestCase):
    def test_shuffled_chain_relation_order(self):
        # 编号 1..5，关系链: slide3 -> slide1 -> slide5 -> slide2 -> slide4
        deck = chain([
            (3, "slide3.xml"), (1, "slide1.xml"), (5, "slide5.xml"),
            (2, "slide2.xml"), (4, "slide4.xml"),
        ])
        result = derive_order(deck)
        self.assertEqual(
            result.order,
            ["slide3.xml", "slide1.xml", "slide5.xml", "slide2.xml", "slide4.xml"],
        )
        self.assertEqual(result.numeric_order, [f"slide{i}.xml" for i in range(1, 6)])
        self.assertEqual(len(result.mismatches), 5)  # 每页位都不同
        # 第一页位：编号期望 slide1，关系推导为 slide3，原因指向关系边
        first = result.mismatches[0]
        self.assertEqual((first.position, first.numeric, first.derived),
                         (1, "slide1.xml", "slide3.xml"))
        self.assertIn("slide3.xml", first.reason)
        self.assertNotIn("slide1.xml 是", first.reason)

    def test_every_slide_appears_exactly_once(self):
        rng = random.Random(7)
        perm = list(range(1, 101))
        rng.shuffle(perm)
        deck = chain([(i, f"slide{i}.xml") for i in perm])
        result = derive_order(deck)
        self.assertEqual(sorted(result.order), sorted(result.numeric_order))
        self.assertEqual(len(result.order), 100)


class TestCycle(unittest.TestCase):
    def test_simple_cycle_lists_all_nodes(self):
        deck = [
            Slide(1, "slide1.xml", "slide2.xml"),
            Slide(2, "slide2.xml", "slide3.xml"),
            Slide(3, "slide3.xml", "slide1.xml"),
        ]
        result = derive_order(deck)
        cycle_anomalies = [a for a in result.anomalies if a.kind == CYCLE]
        self.assertEqual(len(cycle_anomalies), 1)
        self.assertEqual(set(cycle_anomalies[0].slides),
                         {"slide1.xml", "slide2.xml", "slide3.xml"})
        # 环上全部节点仍出现且只出现一次，函数必须正常返回而非崩溃
        self.assertEqual(sorted(result.order),
                         ["slide1.xml", "slide2.xml", "slide3.xml"])
        self.assertTrue(cycle_anomalies[0].suggestion)

    def test_tail_into_cycle_is_fully_covered(self):
        deck = [
            Slide(10, "slide10.xml", "slide11.xml"),
            Slide(11, "slide11.xml", "slide12.xml"),
            Slide(12, "slide12.xml", "slide11.xml"),
        ]
        result = derive_order(deck)
        cycle = [a for a in result.anomalies if a.kind == CYCLE][0]
        self.assertEqual(set(cycle.slides), {"slide11.xml", "slide12.xml"})
        self.assertEqual(result.order[0], "slide10.xml")
        self.assertEqual(sorted(result.order),
                         ["slide10.xml", "slide11.xml", "slide12.xml"])


class TestDanglingReference(unittest.TestCase):
    def test_dangling_ref_ends_chain(self):
        deck = [
            Slide(1, "slide1.xml", "slide2.xml"),
            Slide(2, "slide2.xml", "slide404.xml"),
            Slide(3, "slide3.xml"),
        ]
        result = derive_order(deck)
        # 悬空边使 slide3 成为第二个起始页，两种分类都会被报告
        self.assertEqual({a.kind for a in result.anomalies},
                         {DANGLING_REF, MULTIPLE_HEADS})
        dangling = [a for a in result.anomalies if a.kind == DANGLING_REF][0]
        self.assertEqual(dangling.slides, ("slide2.xml",))
        self.assertIn("slide404.xml", dangling.detail)
        self.assertTrue(dangling.suggestion)
        # 悬空边按链结束处理；slide3 是另一个起始页，两页都不丢
        self.assertEqual(sorted(result.order),
                         ["slide1.xml", "slide2.xml", "slide3.xml"])


class TestDuplicateReference(unittest.TestCase):
    def test_two_predecessors_point_to_same_slide(self):
        deck = [
            Slide(1, "slide1.xml", "slide3.xml"),
            Slide(2, "slide2.xml", "slide3.xml"),
            Slide(3, "slide3.xml"),
        ]
        result = derive_order(deck)
        dup = [a for a in result.anomalies if a.kind == DUPLICATE_REF]
        self.assertEqual(len(dup), 1)
        self.assertEqual(dup[0].slides[0], "slide3.xml")
        self.assertEqual(set(dup[0].slides[1:]),
                         {"slide1.xml", "slide2.xml"})
        self.assertTrue(dup[0].suggestion)
        # 所有页恰好出现一次，不重复不丢失
        self.assertEqual(sorted(result.order),
                         ["slide1.xml", "slide2.xml", "slide3.xml"])

    def test_multiple_heads_reported(self):
        deck = [
            Slide(1, "slide1.xml", "slide2.xml"),
            Slide(3, "slide3.xml", "slide4.xml"),
            Slide(2, "slide2.xml"),
            Slide(4, "slide4.xml"),
        ]
        result = derive_order(deck)
        kinds = {a.kind for a in result.anomalies}
        self.assertIn(MULTIPLE_HEADS, kinds)
        self.assertEqual(sorted(result.order),
                         [f"slide{i}.xml" for i in range(1, 5)])


class TestMixedAnomalies(unittest.TestCase):
    def test_dangling_duplicate_self_cycle_together(self):
        deck = [
            Slide(1, "slide1.xml", "slide3.xml"),
            Slide(2, "slide2.xml", "slide1.xml"),
            Slide(3, "slide3.xml", "slide99.xml"),
            Slide(4, "slide4.xml", "slide4.xml"),
            Slide(5, "slide5.xml", "slide6.xml"),
            Slide(6, "slide6.xml", "slide7.xml"),
            Slide(7, "slide7.xml", "slide5.xml"),
            Slide(8, "slide8.xml", "slide6.xml"),
        ]
        result = derive_order(deck)
        kinds = {a.kind: a for a in result.anomalies}
        self.assertIn(DANGLING_REF, kinds)
        self.assertIn(DUPLICATE_REF, kinds)
        self.assertIn(SELF_REF, kinds)
        self.assertIn(CYCLE, kinds)
        self.assertEqual(set(kinds[CYCLE].slides),
                         {"slide5.xml", "slide6.xml", "slide7.xml"})
        # 8 页全部出现且仅一次
        self.assertEqual(sorted(result.order),
                         [f"slide{i}.xml" for i in range(1, 9)])
        for a in result.anomalies:
            self.assertTrue(a.suggestion.strip())


class TestFiveHundredSlides(unittest.TestCase):
    def test_500_slides_bounds(self):
        rng = random.Random(42)
        perm = list(range(1, 501))
        rng.shuffle(perm)
        deck = []
        for k, sid in enumerate(perm):
            nxt = f"slide{perm[k + 1]}.xml" if k + 1 < len(perm) else None
            deck.append(Slide(sid, f"slide{sid}.xml", nxt))
        result = derive_order(deck)
        self.assertEqual(len(result.order), 500)
        self.assertEqual(len(set(result.order)), 500)
        self.assertEqual(result.order, [f"slide{i}.xml" for i in perm])
        self.assertLess(result.stats["elapsed_seconds"], 1.0)
        self.assertEqual(result.stats["max_recursion_depth"], 1)
        self.assertLessEqual(result.stats["walk_steps"], 500)


class TestCycleWith500NodesDoesNotHang(unittest.TestCase):
    def test_500_node_cycle_returns_without_crash(self):
        deck = [
            Slide(i, f"slide{i}.xml",
                  f"slide{i + 1}.xml" if i < 500 else "slide1.xml")
            for i in range(1, 501)
        ]
        result = derive_order(deck)
        cycle = [a for a in result.anomalies if a.kind == CYCLE]
        self.assertEqual(len(cycle), 1)
        self.assertEqual(len(cycle[0].slides), 500)
        self.assertEqual(len(result.order), 500)


if __name__ == "__main__":
    unittest.main(verbosity=2)
