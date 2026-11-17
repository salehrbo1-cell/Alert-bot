#!/usr/bin/env python3
"""Run exactly the V3 definitions on a full 12-month window, separately
from the May-Nov 2026 6.5-month comparison."""
import sys
import study_sweep_poi_ifvg_cisd_v3 as v3
v3.b.START=v3.b.ts('2025-11-17T09:00:00Z')
v3.b.SPLIT=v3.b.ts('2026-05-17T09:00:00Z')
v3.b.END=v3.b.ts('2026-11-17T09:00:00Z')
if __name__=='__main__':
    v3.run(sys.argv[1],sys.argv[2])
