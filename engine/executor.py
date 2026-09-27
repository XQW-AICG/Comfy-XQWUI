"""executor —— 工作流图执行器。

职责：
  · validate_prompt：结构校验（节点存在 / 必填输入 / 链接解析 / 环检测）
  · 执行：拓扑序求值 + 节点缓存 + OUTPUT_NODE 输出收集 + 进度回调
  · 中断：节点之间检查中断标志
  · 点号键展开：AUTOGROW 输入（ref_images.ref_image_0 → 聚合为 dict 传入）

执行事件通过回调上抛，由 api 层翻译成 Comfy 规范的 WS 消息。
"""
import hashlib
import inspect
import json
import threading
import time
import traceback

from engine.registry import get_node_class, input_spec, combo_options
from engine.types import NodeError


class ValidationError(Exception):
    def __init__(self, error: dict, node_errors: dict):
        super().__init__(error.get("message", "invalid prompt"))
        self.error = error
        self.node_errors = node_errors

    def to_response(self) -> dict:
        return {"error": self.error, "node_errors": self.node_errors}


def _err(etype: str, message: str, details: str = "", **extra) -> dict:
    d = {"type": etype, "message": message, "details": details,
         "extra_info": {}}
    d["extra_info"].update(extra)
    return d


def _is_link(v) -> bool:
    return isinstance(v, (list, tuple)) and len(v) == 2 and \
        isinstance(v[0], str) and isinstance(v[1], int)


def expand_autogrow(graph: dict) -> dict:
    """把 API 图里的点号键按 AUTOGROW 声明聚合。

    例：节点声明 AUTOGROW 输入 "ref_images"，图里出现
    "ref_images.ref_image_0" / "ref_images.ref_image_1" →
    执行时 inputs["ref_images"] = {"ref_image_0": …, "ref_image_1": …}
    """
    out = {}
    for nid, n in (graph or {}).items():
        cls = get_node_class((n or {}).get("class_type", ""))
        spec = input_spec(cls) if cls else {"required": {}, "optional": {}}
        all_inputs = list(spec["required"].items()) + \
            list(spec["optional"].items())
        autogrow_keys = {k for k, v in all_inputs
                         if v == "AUTOGROW" or
                         (isinstance(v, (list, tuple)) and v and
                          v[0] == "AUTOGROW")}
        inputs = {}
        for k, v in (n.get("inputs") or {}).items():
            base, _, rest = k.partition(".")
            if rest and base in autogrow_keys:
                inputs.setdefault(base, {})[rest] = v
            elif base in autogrow_keys:
                inputs.setdefault(base, {}).setdefault("", v)
            else:
                inputs[k] = v
        for k in autogrow_keys:
            inputs.setdefault(k, {})
        g = dict(n)
        g["inputs"] = inputs
        out[nid] = g
    return out


def _graph_deps(graph: dict) -> dict:
    deps = {}
    for nid, n in graph.items():
        deps[nid] = sorted({v[0] for v in (n.get("inputs") or {}).values()
                            if _is_link(v)})
    return deps


def validate_prompt(prompt_id: str, graph: dict) -> tuple:
    """校验工作流。返回 (ok, error, outputs_list, node_errors)。"""
    node_errors = {}
    if not isinstance(graph, dict) or not graph:
        return False, _err("invalid_prompt", "prompt 不能为空"), [], node_errors

    for nid, n in graph.items():
        if not isinstance(n, dict) or not n.get("class_type"):
            node_errors[nid] = {"errors": [
                _err("invalid_node", "节点 %s 缺少 class_type" % nid)]}
            continue
        if get_node_class(n["class_type"]) is None:
            node_errors[nid] = {"errors": [
                _err("node_not_found", "未注册的节点类型: %s" % n["class_type"])]}
    if node_errors:
        return False, _err("invalid_prompt", "工作流校验失败"), [], node_errors

    for nid, n in graph.items():
        cls = get_node_class(n["class_type"])
        spec = input_spec(cls)
        declared = dict(spec["required"])
        declared.update(spec["optional"])
        errs = []
        inputs = n.get("inputs") or {}
        for k, v in inputs.items():
            if not _is_link(v):
                continue
            up = graph.get(v[0])
            if up is None:
                errs.append(_err(
                    "bad_link", "输入 %s 引用了不存在的节点 %s" % (k, v[0])))
                continue
            upcls = get_node_class(up.get("class_type"))
            if upcls is None:
                continue
            rt = list(getattr(upcls, "RETURN_TYPES", ()))
            if v[1] >= max(1, len(rt)):
                errs.append(_err(
                    "bad_link", "输入 %s 引用槽位 %d 越界（%s 共 %d 个输出）"
                    % (k, v[1], up.get("class_type"), len(rt))))
        for k, decl in declared.items():
            if k in inputs or k in spec["optional"]:
                continue
            errs.append(_err("missing_input", "缺少必填输入: %s" % k))
        for k, decl in declared.items():
            if k in inputs or not isinstance(decl, (list, tuple)):
                continue
            opts = combo_options(decl)
            if opts is None:
                continue
            v = inputs.get(k)
            if isinstance(v, str) and v not in opts:
                errs.append(_err(
                    "bad_combo_value", "输入 %s 取值 %r 不在合法选项中" % (k, v)))
        if errs:
            node_errors[nid] = {"errors": errs}

    if node_errors:
        return False, _err("invalid_prompt", "工作流校验失败"), [], node_errors

    # 环检测（DFS）
    deps = _graph_deps(graph)
    state: dict = {}

    def visit(nid) -> bool:
        st = state.get(nid)
        if st == "done":
            return True
        if st == "visiting":
            return False
        state[nid] = "visiting"
        for d in deps.get(nid, []):
            if not visit(d):
                return False
        state[nid] = "done"
        return True

    for nid in graph:
        if not visit(nid):
            return False, _err("cycle", "工作流存在循环依赖"), [], \
                {"$": {"errors": [_err("cycle", "循环依赖")]}}

    outputs = [nid for nid in graph
               if getattr(get_node_class(graph[nid]["class_type"]),
                          "OUTPUT_NODE", False)]
    return True, None, outputs, {}


class ExecContext:
    """传给节点的执行上下文：进度上报 + 中断检查。"""

    def __init__(self, prompt_id, node_id, client_id, report, check_interrupt):
        self.prompt_id = prompt_id
        self.node_id = node_id
        self.client_id = client_id
        self.report = report
        self.check_interrupt = check_interrupt


class GraphExecutor:
    """图执行器 + 队列工作线程（GPU 任务串行）。"""

    def __init__(self, queue_man, on_event=None):
        """on_event(event, data)：执行事件回调，api 层负责翻译成 WS 消息。"""
        self.queue = queue_man
        self.on_event = on_event or (lambda e, d: None)
        self._cache: dict = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._worker, name="engine-executor", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    # ---- 内部

    def _emit(self, event, data):
        try:
            self.on_event(event, data)
        except Exception:
            traceback.print_exc()

    def _worker(self):
        while not self._stop.is_set():
            item = self.queue.pop_next(timeout=0.5)
            if item is None:
                continue
            self._run_prompt(*item)

    def _run_prompt(self, number, prompt_id, prompt, extra_data,
                    outputs_to_execute):
        self.queue.clear_interrupt()
        client_id = (extra_data or {}).get("client_id")
        self._emit("execution_start", {"prompt_id": prompt_id})
        t0 = time.time()
        outputs_ui: dict = {}
        executed: list = []
        try:
            graph = expand_autogrow(prompt)
            deps = _graph_deps(graph)
            order: list = []
            state: dict = {}
            for nid in graph:
                self._topo(nid, deps, state, order)
            cached = []
            values: dict = {}
            for nid in order:
                if self.queue.should_interrupt(prompt_id):
                    raise InterruptedError()
                n = graph[nid]
                key = self._cache_key(nid, n)
                if key in self._cache:
                    values[nid] = self._cache[key]
                    cached.append(nid)
                    continue
                self._emit("executing", {"node": nid, "prompt_id": prompt_id,
                                         "display_node": nid})
                vals = self._exec_node(prompt_id, nid, n, values, client_id)
                values[nid] = vals
                self._cache[key] = vals
                executed.append(nid)
                cls = get_node_class(n["class_type"])
                if getattr(cls, "OUTPUT_NODE", False):
                    ui = vals[-1] if vals and isinstance(vals[-1], dict) \
                        and "ui" in vals[-1] else {"output": list(vals)}
                    outputs_ui[nid] = ui
                    self._emit("executed", {"node": nid, "output": ui,
                                            "prompt_id": prompt_id})
            if cached:
                self._emit("execution_cached", {
                    "prompt_id": prompt_id, "nodes": cached})
            self.queue.put_history(prompt_id, prompt, outputs_ui, {
                "status_str": "success", "completed": True, "messages": []},
                extra_data)
            self._emit("execution_success", {"prompt_id": prompt_id})
        except InterruptedError:
            self.queue.put_history(prompt_id, prompt, outputs_ui, {
                "status_str": "error", "completed": False,
                "messages": [{"type": "execution_interrupted"}]}, extra_data)
            self._emit("execution_interrupted",
                       {"prompt_id": prompt_id, "executed": executed})
        except NodeError as e:
            mes = {"prompt_id": prompt_id, "node_id": e.node,
                   "exception_message": e.message,
                   "exception_type": "NodeError",
                   "traceback": traceback.format_exc()}
            self.queue.put_history(prompt_id, prompt, outputs_ui, {
                "status_str": "error", "completed": False,
                "messages": [mes]}, extra_data)
            self._emit("execution_error", mes)
        except Exception as e:
            mes = {"prompt_id": prompt_id, "node_id": None,
                   "exception_message": str(e),
                   "exception_type": e.__class__.__name__,
                   "traceback": traceback.format_exc()}
            self.queue.put_history(prompt_id, prompt, outputs_ui, {
                "status_str": "error", "completed": False,
                "messages": [mes]}, extra_data)
            self._emit("execution_error", mes)
        finally:
            self.queue.finish_running()
            self._emit("status", {"elapsed": round(time.time() - t0, 2),
                                  "prompt_id": prompt_id})

    def _topo(self, nid, deps, state, order):
        st = state.get(nid)
        if st == "done":
            return
        if st == "visiting":
            raise NodeError(nid, "工作流存在循环依赖")
        state[nid] = "visiting"
        for d in deps.get(nid, []):
            self._topo(d, deps, state, order)
        state[nid] = "done"
        order.append(nid)

    def _cache_key(self, nid, n) -> str:
        """节点缓存键：class_type + 输入字面量（对象只取 id 供会话内缓存）。"""
        deps = sorted("%s@%s" % (v[0], v[1])
                      for v in (n.get("inputs") or {}).values() if _is_link(v))
        plain = {}
        for k, v in (n.get("inputs") or {}).items():
            if _is_link(v):
                plain[k] = "%s[%d]" % (v[0], v[1])
            elif isinstance(v, (str, int, float, bool)) or v is None:
                plain[k] = v
            else:
                plain[k] = id(v)
        payload = json.dumps(
            {"c": n.get("class_type"), "i": plain, "d": deps},
            ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(payload.encode()).hexdigest()

    def _exec_node(self, prompt_id, nid, n, values, client_id):
        cls = get_node_class(n["class_type"])
        kwargs = {}
        for k, v in (n.get("inputs") or {}).items():
            if _is_link(v):
                up_vals = values.get(v[0])
                if up_vals is None:
                    raise NodeError(nid, "依赖节点 %s 尚无输出" % v[0])
                try:
                    kwargs[k] = up_vals[v[1]]
                except IndexError:
                    raise NodeError(nid, "依赖槽位越界: %s ← %s[%d]"
                                    % (k, v[0], v[1]))
            else:
                kwargs[k] = v

        ctx = ExecContext(
            prompt_id=prompt_id, node_id=nid, client_id=client_id,
            report=lambda cur, total, preview=None: self._report(
                prompt_id, nid, client_id, cur, total, preview),
            check_interrupt=lambda: self.queue.should_interrupt(prompt_id))
        inst = cls()
        fn = getattr(inst, cls.FUNCTION)
        if "_ctx" in inspect.signature(fn).parameters:
            return fn(_ctx=ctx, **kwargs)
        return fn(**kwargs)

    def _report(self, prompt_id, nid, client_id, cur, total, preview=None):
        self._emit("progress", {"prompt_id": prompt_id, "node": nid,
                                "value": cur, "max": total})
        if preview:
            # 二进制预览帧：事件类型 8（PREVIEW_IMAGE），载荷 = 格式字节 + JPEG
            self.on_event("_binary", {"event": 8, "data": preview,
                                      "sid": client_id})
