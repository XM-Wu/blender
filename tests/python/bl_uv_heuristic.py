# SPDX-License-Identifier: GPL-2.0-or-later
"""Background heuristic regression, including constraints from the existing suite."""
import sys
import time
import re
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import bpy
import bl_uv_texel_pack as base

specs = [dict(size=size, uv=(2+i*.1, 2), uv_size=(size[0]*.1,size[1]*.1))
         for i,size in enumerate(((3,1),(2,2),(1,4),(1,1),(2,1),(1,2),(3,2),(2,3)))]
ob,ranges=base.make_islands(specs)
assert base.pack(target='FIXED',udim_tile=1001,rotation='CARDINAL',use_stacking=False)=={'FINISHED'}
baseline=sum(abs(base.area_px(i)) for i in base.island_uvs(ob,ranges))
ob,ranges=base.make_islands(specs)
started=time.monotonic()
assert base.pack(target='FIXED',udim_tile=1001,rotation='CARDINAL',use_stacking=False,heuristic_search=True,
                 search_time=.3,no_improvement_time=0)=={'FINISHED'}
assert time.monotonic()-started < 10
islands=base.island_uvs(ob,ranges)
base.TexelPackTest().assert_layout(islands)
assert sum(abs(base.area_px(i)) for i in islands) >= baseline-1e-3
report=bpy.context.scene['uv_texel_pack_report']
assert int(re.search(r'search (\d+) attempts',report)[1])>0,report
print('SEARCH_RESULT',report,flush=True)
ob,ranges=base.make_islands(specs)
assert base.pack(target='FIXED',udim_tile=1001,rotation='CARDINAL',use_stacking=False,heuristic_search=True,
                 search_time=0,no_improvement_time=.1)=={'FINISHED'}
base.TexelPackTest().assert_layout(base.island_uvs(ob,ranges))
before=base.all_uvs(ob)
try:
    base.pack(heuristic_search=True,search_time=0,no_improvement_time=0)
    raise AssertionError('Unbounded background search accepted')
except RuntimeError:
    pass
assert base.all_uvs(ob)==before

original_pack=base.pack
def searched_pack(**kw):
    kw.update(heuristic_search=True,search_time=.03,no_improvement_time=0)
    return original_pack(**kw)
base.pack=searched_pack
result=unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromTestCase(base.TexelPackTest))
assert result.wasSuccessful()
print('PASS: bounded search, no-improvement stop, non-regression and all packing constraints')
