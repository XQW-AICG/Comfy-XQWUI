"""api —— REST 路由体系（按域拆分）。

tasks        任务提交 / 查询 / 暂停恢复取消重试删除 / 工作流预览
system       系统状态 / 健康 / 设置 / 模型清单 / 引擎控制
assets       素材与成品（存储桶读写）+ /media 媒体服务
presets      方案预设 / 自动存档
projects     项目管理（多项目分镜数据隔离）
storage_api  素材库 / 成品库本地路径配置

register_all(app) 统一挂载到 shared.httpd.App。
"""
from xqwui.api import (tasks, system, assets, presets, projects,
                       storage_api)

MODULES = (tasks, system, assets, presets, projects, storage_api)


def register_all(app):
    for m in MODULES:
        m.register(app)
