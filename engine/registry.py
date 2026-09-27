"""registry —— 节点注册表。

节点以装饰器注册，声明方式与 ComfyUI 一致：
  @node("UNETLoader", category="load")
  class UNETLoader:
      INPUT_TYPES = classmethod → {"required": {...}, "optional": {...}}
      RETURN_TYPES = ("MODEL",)
      RETURN_NAMES = ("model",)        # 可选
      OUTPUT_NODE = False              # 可选，True 则输出进 history.outputs
      CATEGORY = "load"
      FUNCTION = "run"
      def run(self, **inputs): ...

组合输入类型：
  COMBO：值 = (选项列表,) 或 (选项列表, {...元数据})
  AUTOGROW：值 = "AUTOGROW" —— API 图里的点号键（a.b_0）会聚合为 dict 传入
"""
import inspect

REGISTRY: dict = {}          # name -> class
COMBO_CACHE: dict = {}       # name -> {input: options}（object_info 用）


class NodeDefError(Exception):
    pass


def node(name: str, category: str = "general"):
    def deco(cls):
        if name in REGISTRY:
            raise NodeDefError("节点重复注册: %s" % name)
        if not hasattr(cls, "INPUT_TYPES") or not hasattr(cls, "FUNCTION"):
            raise NodeDefError(
                "节点 %s 缺少 INPUT_TYPES / FUNCTION" % name)
        cls.CATEGORY = category
        REGISTRY[name] = cls
        return cls
    return deco


def get_node_class(name: str):
    return REGISTRY.get(name)


def input_spec(cls) -> dict:
    """展开 INPUT_TYPES（支持 classmethod/静态/实例方法）。"""
    fn = cls.INPUT_TYPES
    try:
        spec = fn()
    except TypeError:
        spec = fn(cls)
    req = spec.get("required") or {}
    opt = spec.get("optional") or {}
    return {"required": req, "optional": opt}


def combo_options(value):
    """COMBO 输入的选项列表；非 COMBO 返回 None。"""
    if not isinstance(value, (list, tuple)) or not value:
        return None
    first = value[0]
    if isinstance(first, list):
        return first
    if isinstance(first, (str, int, float)):
        return list(value[0:]) if len(value) > 1 and not isinstance(
            value[1], dict) else list(value[0:])
    return None


def object_info() -> dict:
    """生成 Comfy 规范的 /object_info 结构。"""
    out = {}
    for name, cls in REGISTRY.items():
        spec = input_spec(cls)
        entry = {
            "input": spec,
            "output": list(getattr(cls, "RETURN_TYPES", ())),
            "output_name": list(getattr(cls, "RETURN_NAMES",
                                        getattr(cls, "RETURN_TYPES", ()))),
            "name": name,
            "display_name": name,
            "description": (inspect.getdoc(cls) or "").strip(),
            "category": getattr(cls, "CATEGORY", "general"),
            "output_node": bool(getattr(cls, "OUTPUT_NODE", False)),
            "python_module": "engine",
        }
        out[name] = entry
    return out
