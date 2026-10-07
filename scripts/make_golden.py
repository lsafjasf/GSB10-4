"""生成/刷新对拍黄金数据 golden/hog_report.json（确定性）。"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fairloop import analyze
from fairloop.scenarios import scenario_one_hog

OUT = os.path.join(os.path.dirname(__file__), "..", "golden", "hog_report.json")

if __name__ == "__main__":
    report = analyze(scenario_one_hog()).to_dict()
    with open(OUT, "w") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print(f"wrote {OUT} ({report['total_runs']} runs)")
