"""engine —— 轻量级推理引擎。

职责（严格限定）：
  1. 接收前端（应用后端）提交的工作流 JSON（Comfy 后端 API 格式）
  2. 执行推理计算（节点图执行器 + 可插拔 ComputeBackend）
  3. 返回标准化的推理结果（history / ui outputs / 文件落盘）

对外接口完全遵循 ComfyUI 后端 API 规范（基于 v0.37.2 提取）：
  POST /prompt         提交工作流 → {prompt_id, number, node_errors}
  GET  /queue          队列状态 {queue_running, queue_pending}
  POST /queue          {clear} / {delete:[id]}
  POST /interrupt      中断（可带 prompt_id 定向）
  POST /free           释放模型/显存 {unload_models, free_memory}
  GET  /history        历史
  GET  /history/{id}   单条历史
  POST /history        {clear} / {delete:[id]}
  GET  /object_info    节点清单（含 COMBO 选项）
  GET  /system_stats   系统/设备状态
  POST /upload/image   multipart 上传素材 → input 目录
  GET  /view           读取 input/output 文件
  GET  /ws?clientId=   事件推送（status/execution_start/executing/
                       progress/executed/execution_success/error/interrupted
                       + 二进制预览帧：4字节大端事件类型 + JPEG）
"""

# 导入即注册全部内置节点（保证任何入口 import engine 后注册表可用）
from engine import nodes as _nodes  # noqa: F401,E402
