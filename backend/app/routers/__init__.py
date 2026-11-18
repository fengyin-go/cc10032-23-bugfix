"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import lineage_replay, resource

# 固定路径路由（/api/lineage/...、/api/replay/...）必须排在通用资源路由前面，
# 否则会被 /api/{module} 的动态路径抢先匹配。
ROUTERS = [lineage_replay, resource]
