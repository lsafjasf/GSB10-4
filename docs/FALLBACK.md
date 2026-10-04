# 回退链与目录约定说明

## 目录约定

主题是一个目录，结构如下（仅标准库实现，坏文件判定为签名级）：

```
<search_path>/
  <theme>/
    index.theme                 # 必需，[Icon Theme] 节，可含 Inherits=父主题,...
    <size>x<size>/              # 必须小写，如 16x16、48x48；48X48 不合规
      <context>/                # 标准 context：apps/actions/places/mimetypes/...
        <name>.<ext>            # ext：png / svg / xpm
    scalable/<context>/<name>.svg
    <name>.<ext>                # L5 unthemed 根目录图标
  <parenttheme>/...             # Inherits 指向的兄弟主题
  hicolor/...                   # 隐式最终兜底主题
```

## 回退链顺序（命中依据）

对一次查询 `(name, size, context)`，严格按下列顺序探测，第一个**有效**文件即命中：

| 阶段 | 级 | 探测路径 | 规则 |
|---|---|---|---|
| main | L1 | `<theme>/<size>x<size>/<context>/<name>.<ext>` | 精确尺寸 |
| main | L2 | `<theme>/<s>x<s>/<context>/...`，`s > size` | 更大尺寸，**从近到远**（升序），逐个目录探测 |
| main | L3 | `<theme>/<s>x<s>/<context>/...`，`s < size` | 更小尺寸，**从近到远**（降序），逐个目录探测 |
| main | L4 | `<theme>/scalable/<context>/<name>.svg` | 只认 `.svg` |
| main | L5 | `<theme>/<name>.<ext>` | unthemed 根目录 |
| inherited | L1–L5 | 按 `index.theme` 中 `Inherits=` 的**声明顺序**，每个父主题重复 L1–L5 | 缺失的主题目录记 `skip` 后继续；有环（含自继承）靠 visited 集终止 |
| hicolor | L1–L5 | 搜索路径下名为 `hicolor` 的兄弟主题 | 只在最后探测一次，即使已在 Inherits 中也不重复 |

每次命中都返回 `Hit(stage, level, level_name, size_dir, theme, path)`，明确说出**哪一阶段、哪一级、哪个主题**提供；未命中时 `Resolution.chain` 按序保留每一级的 `hit/miss/corrupt/skip` 记录，可完整复盘。

## 确定性规则

- **目录大小写**：主题目录、尺寸目录（`NxN`）、context 目录一律**大小写敏感精确匹配**。`32x32/Apps` 不满足 context `apps`；`48X48` 不是尺寸目录（校验报 error）。
- **文件名**：图标主名大小写敏感；**扩展名大小写不敏感**（`a.PNG` 可匹配 `a.png` 的查询）。
- **同目录多候选排序**（键依次比较）：
  1. 扩展名优先级：`png < svg < xpm`（固定尺寸目录优先位图）；
  2. 扩展名全小写优先（`both.png` 胜过 `both.PNG`）；
  3. 文件名字典序。
  L4 scalable 目录只匹配 `.svg`（放 `.png` 永不命中，校验给 warning）。
- **多分辨率**：L1 精确 → L2 更大（就近）→ L3 更小（就近），每个尺寸目录单独探测，所以“只有低分辨率”时 L3 会继续向更小目录找直到命中（见对拍用例 `L3-only-low-res-needs-second-probe`）。

## 坏文件 vs 缺文件（分开报告）

- **坏文件**：文件存在但签名校验失败（PNG 魔数/IHDR、SVG 需含 `<svg`、XPM 需含 `XPM`、空文件）。坏文件**永远不算命中**：记入 `Resolution.corrupt`（含所在级），链继续往下走。
- **缺文件**：整链走完没有任何有效文件，`status="missing"`。
  - `missing_clean`：链上连坏文件都没有（纯缺）；
  - `missing_corrupt_only`：只找到过坏文件。两者在 `validate_icons` 的汇总中分桶。
- 主题级校验 `validate_theme` 单独产出 `report.corrupt` 列表，与 convention problem 分开。

## 缓存与目录遍历计数

`DirCache` 对每个目录只做一次 `os.scandir`（计为一次目录遍历）并缓存条目，5000 项共享一个缓存时总遍历次数只等于实际涉及的目录数；每查询新建缓存则是无缓存对照。
