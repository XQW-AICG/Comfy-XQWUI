"""workflow —— 代码化工作流构建（无模板依赖）。

builder   GraphBuilder DSL：以代码组装 Comfy API 格式工作流图
timeline  时间线规则（帧网格/重叠吸附/时长模型）与 timeline_data v4
h3_video  H3 长视频工作流生成器（每段一图 / 整片一图）
"""
from xqwui.workflow.builder import GraphBuilder, Link, NodeHandle
from xqwui.workflow import timeline, h3_video

__all__ = ["GraphBuilder", "Link", "NodeHandle", "timeline", "h3_video"]
