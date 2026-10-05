"""pgmbp: 纯标准库实现的概率图模型近似推断（Loopy Belief Propagation）。

仅依赖 Python 标准库。核心入口：

- FactorGraph / Factor : 因子图建模
- belief_propagation   : 和积置信传播（含阻尼、振荡检测、消息防护）
- variable_elimination : 精确推断（变量消去），用于小规模误差对比
"""

from .factor_graph import Factor, FactorGraph
from .bp import BPConfig, BPResult, belief_propagation
from .exact import variable_elimination, exact_marginals

__all__ = [
    "Factor",
    "FactorGraph",
    "BPConfig",
    "BPResult",
    "belief_propagation",
    "variable_elimination",
    "exact_marginals",
]

__version__ = "0.1.0"
