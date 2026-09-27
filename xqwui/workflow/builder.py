"""builder —— GraphBuilder DSL：以代码组装 Comfy API 格式工作流图。

所有工作流均由本模块动态生成，不读取任何外部 JSON 模板文件。
节点间连接以 Link 对象表达，序列化（to_api_format）时转成
Comfy 规范的 [node_id, slot] 二元组；Autogrow 组继续沿用
点号键名（如 "groups.group_0"），由引擎执行期拆回嵌套 dict。

用法：
    b = GraphBuilder()
    unet = b.add("UNETLoader", {"unet_name": "x.safetensors"})
    b.add("Foo", {"model": unet.out(0)})
    graph = b.to_api_format()      # → POST /prompt 的 prompt 字段
"""
from engine.registry import get_node_class, input_spec  # 复用引擎节点注册表


class Link:
    """一个输出槽引用：node_id 节点的第 slot 个输出。"""
    __slots__ = ("node_id", "slot")

    def __init__(self, node_id, slot=0):
        self.node_id, self.slot = str(node_id), int(slot)

    def to_list(self):
        return [self.node_id, self.slot]


class NodeHandle:
    """已创建节点的句柄：.out(slot) 取输出链接，.id 为节点编号。"""

    def __init__(self, builder, node_id):
        self._b, self.id = builder, str(node_id)

    def out(self, slot=0) -> Link:
        return Link(self.id, slot)

    def set(self, **inputs):
        """补充 / 覆盖输入（构建后微调用）。"""
        self._b._nodes[self.id]["inputs"].update(inputs)
        return self


class GraphBuilder:
    """工作流图构建器。

    add() 新建节点并返回句柄；inputs 的值可以是标量、Link、
    或嵌套含 Link 的 dict/list（Autogrow 点号键与组值均支持）。
    """

    def __init__(self):
        self._nodes = {}            # id -> {"class_type": str, "inputs": dict}
        self._n = 0

    # ------------------------------------------------------------ 构建
    def add(self, class_type: str, inputs: dict | None = None,
            node_id: str | None = None) -> NodeHandle:
        nid = str(node_id) if node_id is not None else self._next_id()
        if nid in self._nodes:
            raise ValueError("节点编号冲突: %s" % nid)
        self._nodes[nid] = {"class_type": class_type,
                            "inputs": dict(inputs or {})}
        return NodeHandle(self, nid)

    def get(self, node_id) -> NodeHandle:
        if str(node_id) not in self._nodes:
            raise KeyError("节点不存在: %s" % node_id)
        return NodeHandle(self, node_id)

    def has(self, node_id) -> bool:
        return str(node_id) in self._nodes

    def remove(self, node_id):
        """删除节点，并清掉指向它的顶层链接。"""
        self._nodes.pop(str(node_id), None)
        for nd in self._nodes.values():
            for k in list(nd["inputs"]):
                v = nd["inputs"][k]
                if isinstance(v, Link) and v.node_id == str(node_id):
                    del nd["inputs"][k]

    def _next_id(self) -> str:
        self._n += 1
        while str(self._n) in self._nodes:
            self._n += 1
        return str(self._n)

    # ------------------------------------------------------------ 校验
    def validate(self) -> list:
        """结构校验（构建期快检）：空图 / 链接指向存在性 / 依赖成环。

        返回错误描述列表（空列表 = 通过）。输入取值与类型匹配由
        引擎 /prompt 的 validate_prompt 做权威校验（含 combo 选项）。
        """
        errs = []
        if not self._nodes:
            return ["空工作流"]
        for nid, nd in self._nodes.items():
            if not nd.get("class_type"):
                errs.append("节点 %s 缺少 class_type" % nid)
                continue
            for k, v in nd["inputs"].items():
                for link in _walk_links(v):
                    if link.node_id not in self._nodes:
                        errs.append("%s.%s → 不存在的节点 %s"
                                    % (nid, k, link.node_id))
        if not errs and _has_cycle(self._nodes):
            errs.append("工作流存在循环依赖")
        return errs

    # ------------------------------------------------------------ 导出
    def to_api_format(self) -> dict:
        """序列化为 Comfy /prompt 规范的 prompt 字段（校验不过则抛错）。"""
        errs = self.validate()
        if errs:
            raise ValueError("; ".join(errs))
        return {nid: {"class_type": nd["class_type"],
                      "inputs": {k: _ser(v)
                                 for k, v in nd["inputs"].items()}}
                for nid, nd in self._nodes.items()}

    def spec_of(self, class_type: str) -> dict:
        """查询引擎注册表中某节点的输入规格（required/optional）。"""
        return input_spec(get_node_class(class_type))


# ---------------------------------------------------------------- 内部
def _ser(v):
    """递归序列化：Link → [id, slot]，其余原样。"""
    if isinstance(v, Link):
        return v.to_list()
    if isinstance(v, dict):
        return {k: _ser(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_ser(x) for x in v]
    return v


def _walk_links(v):
    if isinstance(v, Link):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from _walk_links(x)
    elif isinstance(v, (list, tuple)):
        for x in v:
            yield from _walk_links(x)


def _has_cycle(nodes) -> bool:
    """Kahn 拓扑排序判环（依赖数 = 指向的节点数）。"""
    deps = {nid: {lk.node_id for lk in _walk_links(nd["inputs"])}
            for nid, nd in nodes.items()}
    indeg = {nid: len(d) for nid, d in deps.items()}
    queue = [n for n, d in indeg.items() if d == 0]
    seen = 0
    while queue:
        n = queue.pop()
        seen += 1
        for m, d in deps.items():
            if n in d:
                d.discard(n)
                indeg[m] = len(d)
                if indeg[m] == 0:
                    queue.append(m)
    return seen != len(nodes)
