"""Fresh processes catch cycles masked by full-suite collection order."""
import os
from pathlib import Path
import subprocess
import sys
import pytest


@pytest.mark.parametrize('code',[
    'from oabm.quantities import extract_quantities; from oabm.routing import route_checked_assembly',
    'from oabm.routing.checked_assembly import CheckedAssemblyRoute; from oabm.quantities import extract_quantities',
    'from oabm.routing import *; from oabm.qa import revision_diff',
])
def test_public_modules_import_without_collection_order_dependency(code):
    root=Path(__file__).parents[1]
    result=subprocess.run([sys.executable,'-c',code],cwd=root,
        env={**os.environ,'PYTHONPATH':str(root/'src')},capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
