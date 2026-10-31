import unittest,itertools
from fractions import Fraction as F
from oabm.routing.physical_generation_math import *

class RealizationTests(unittest.TestCase):
    def test_all_cardinal_turns(self):
        axes=[tuple(s if i==k else 0 for i in range(3)) for k in range(3) for s in (-1,1)]
        for a,b in itertools.product(axes,repeat=2):
            if dot(a,b):continue
            for r in (1,300000000,10000000000):
                corner=(10000000001,-20000000003,30000000007)
                start=sub(corner,scale(a,r+1));end=add(corner,scale(b,r+2))
                v=realize((start,corner,end),r,a,b,1,2)
                self.assertEqual(v.straight_ticks,3)
                line,arc,last=v.pieces
                self.assertEqual(line.end,arc.start);self.assertEqual(arc.end,last.start)
                center=add(arc.start,scale(b,r))
                self.assertEqual(center,sub(arc.end,scale(a,r)))
                self.assertEqual(cross(arc.normal,sub(arc.start,center)),scale(a,r*arc.turn))
                self.assertEqual(cross(arc.normal,sub(arc.end,center)),scale(b,r*arc.turn))
                self.assertEqual(dot(sub(arc.start,center),sub(arc.start,center)),r*r)
                self.assertEqual(v.objective(7),(F(10),F(r,2)))

    def test_tiny_interbend(self):
        v=realize(((0,0,0),(10,0,0),(10,7,0),(20,7,0)),3,(1,0,0),(1,0,0),between_min=1)
        self.assertEqual(v.pieces[2].start,(10,3,0));self.assertEqual(v.pieces[2].end,(10,4,0))
        self.assertEqual(v.straight_ticks,15)
        with self.assertRaises(ValueError):realize(((0,0,0),(10,0,0),(10,6,0),(20,6,0)),3,(1,0,0),(1,0,0))

    def test_single_straight_uses_max_not_sum(self):
        self.assertEqual(realize(((0,0,0),(10,0,0)),3,(1,0,0),(1,0,0),8,9).straight_ticks,10)

    def test_invalid_paths(self):
        for points in [((0,0,0),(0,0,0)),((0,0,0),(1,1,0)),((0,0,0),(10,0,0),(20,0,0)),((0,0,0),(10,0,0),(1,0,0)),((0,0,0),(3,0,0),(3,10,0))]:
            with self.assertRaises(ValueError):realize(points,3,(1,0,0),(0,1,0))

    def test_pi_and_objectives(self):
        # Independent classical rational enclosure and nesting, not float pi.
        low,high=pi_bounds(32)
        self.assertGreater(low,F(103993,33102));self.assertLess(high,F(104348,33215))
        old=pi_bounds(1)
        for n in (2,4,8,16,32):
            new=pi_bounds(n);self.assertGreaterEqual(new[0],old[0]);self.assertLessEqual(new[1],old[1]);old=new
        self.assertEqual(compare_objectives((F(1),F(2)),(F(1),F(2))),0)
        self.assertEqual(compare_objectives((F(0),F(1)),(F(3),F(0))),1)
        self.assertEqual(compare_objectives((F(0),F(-1)),(F(-3),F(0))),-1)
        midpoint=sum(pi_bounds(1))/2
        self.assertIsNone(compare_objectives((F(0),F(1)),(midpoint,F(0)),max_terms=1))
        self.assertIsNotNone(compare_objectives((F(0),F(1)),(midpoint,F(0)),max_terms=32))

if __name__=='__main__':unittest.main()
