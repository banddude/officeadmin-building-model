"""Exact search-internal realization. Not a canonical model or certificate.
All coordinates and lengths here are integer nanometres. The eventual adapter
must emit canonical PhysicalRoute objects and invoke the live verifier.
"""
from dataclasses import dataclass
from fractions import Fraction as F
from functools import lru_cache


def add(a, b): return tuple(x+y for x,y in zip(a,b))
def sub(a, b): return tuple(x-y for x,y in zip(a,b))
def scale(a, n): return tuple(x*n for x in a)
def dot(a, b): return sum(x*y for x,y in zip(a,b))
def cross(a,b): return (a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0])
def direction(a,b):
    d=sub(b,a)
    if sum(x!=0 for x in d)!=1: raise ValueError('noncardinal or zero edge')
    return tuple((x>0)-(x<0) for x in d)

@dataclass(frozen=True)
class Piece:
    kind: str
    start: tuple
    end: tuple
    normal: tuple | None = None
    turn: int = 0

@dataclass(frozen=True)
class Realization:
    pieces: tuple
    straight_ticks: int
    bends: int
    radius_ticks: int

    def objective(self, bend_penalty_ticks):
        # Actual physical length plus penalty, in nanometres: a + b*pi.
        return (F(self.straight_ticks + self.bends*bend_penalty_ticks),
                F(self.bends*self.radius_ticks, 2))


def realize(vertices, radius, start_direction, final_direction,
            start_min=0, end_min=0, between_min=0):
    """Trim cardinal control edges, inserting exact tangent quarter circles.

    No collision check or obstacle pruning takes place on the control polygon.
    Nonmaximal lines, reversals and zero-length residual lines are refused.
    """
    vertices=tuple(vertices)
    if not 2<=len(vertices)<=513: raise ValueError('vertex count outside profile')
    if any(type(p) is not tuple or len(p)!=3 or any(type(v) is not int for v in p) for p in vertices):
        raise ValueError('integer triples required')
    if any(type(v) is not int for v in (radius,start_min,end_min,between_min)) or radius<=0 or min(start_min,end_min,between_min)<0:
        raise ValueError('invalid lengths')
    dirs=tuple(direction(a,b) for a,b in zip(vertices,vertices[1:]))
    if dirs[0]!=start_direction or dirs[-1]!=final_direction:
        raise ValueError('terminal tangent mismatch')
    if any(dot(a,b)!=0 for a,b in zip(dirs,dirs[1:])):
        raise ValueError('only perpendicular turns; coalesce straight edges')
    pieces=[]; straight=0
    for i,d in enumerate(dirs):
        a=add(vertices[i],scale(d,radius)) if i else vertices[i]
        b=sub(vertices[i+1],scale(d,radius)) if i<len(dirs)-1 else vertices[i+1]
        length=dot(sub(b,a),d)
        minimum=max(start_min,end_min) if len(dirs)==1 else (start_min if i==0 else end_min if i==len(dirs)-1 else between_min)
        if length<=0 or length<minimum: raise ValueError('insufficient straight span')
        pieces.append(Piece('line',a,b)); straight+=length
        if i<len(dirs)-1:
            out=dirs[i+1]; end=add(vertices[i+1],scale(out,radius))
            n=cross(d,out); sign=next(x for x in n if x)
            pieces.append(Piece('quarter_arc',b,end,scale(n,sign),sign))
    return Realization(tuple(pieces),straight,len(dirs)-1,radius)

@lru_cache(maxsize=16)
def pi_bounds(terms):
    """Machin identity with exact alternating-series outward bounds."""
    if type(terms) is not int or not 1<=terms<=1024: raise ValueError('term budget')
    def atan_bounds(q):
        s=sum((F((-1)**k,(2*k+1)*q**(2*k+1)) for k in range(terms)),F(0))
        t=s+F((-1)**terms,(2*terms+1)*q**(2*terms+1))
        return min(s,t),max(s,t)
    a,b=atan_bounds(5);c,d=atan_bounds(239)
    return 16*a-4*d,16*b-4*c

def compare_objectives(left,right,max_terms=64):
    """Return -1/0/1, or None if the declared precision cannot separate.

    Equality is exact coefficient equality, never overlapping float intervals.
    """
    if type(max_terms) is not int or not 1<=max_terms<=1024: raise ValueError('term budget')
    a=left[0]-right[0];b=left[1]-right[1]
    if not b: return (a>0)-(a<0)
    n=1
    while True:
        lo,hi=pi_bounds(n)
        lo,hi=(a+b*lo,a+b*hi) if b>0 else (a+b*hi,a+b*lo)
        if lo>0:return 1
        if hi<0:return -1
        if n==max_terms:return None
        n=min(n*2,max_terms)
