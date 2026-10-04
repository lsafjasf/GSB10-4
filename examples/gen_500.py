"""生成 500 页、编号与放映顺序故意不一致的关系文件。"""
import json, random
random.seed(42)
n = 500
order = list(range(1, n + 1))
random.shuffle(order)
relations = {str(order[i]): [order[i + 1]] for i in range(n - 1)}
relations[str(order[-1])] = []
spec = {"start": order[0], "slides": list(range(1, n + 1)), "relations": relations}
with open("deck500.json", "w", encoding="utf-8") as fh:
    json.dump(spec, fh, ensure_ascii=False)
print("deck500.json written, start =", order[0])
