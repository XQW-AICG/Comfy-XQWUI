"""shared —— 引擎与应用后端共用的零第三方依赖基础设施。

模块：
  util    通用工具（原子写、路径安全、文件名净化）
  ws      RFC6455 WebSocket（服务端 Hub + 客户端连接）
  httpd   ThreadingHTTPServer 基座（路由、JSON、multipart、Range、静态）
"""
