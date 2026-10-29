import os
from pathlib import Path
import re

def test_no_stash_residue():
    src_dir = Path("src")
    
    pattern = re.compile(r'_last_spatial_results|_last_inventory_results')
    
    found = []
    for root, _, files in os.walk(src_dir):
        for file in files:
            if file.endswith('.py'):
                path = Path(root) / file
                with open(path, 'r', encoding='utf-8') as f:
                    content = f.read()
                    if pattern.search(content):
                        found.append(str(path))
                        
    assert not found, f"Found stash residue in {found}"
