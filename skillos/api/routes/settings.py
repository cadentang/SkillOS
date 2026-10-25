"""设备侧开关（V2-T1 / N-11.8/9）：`GET|PUT /api/settings`。

开关是**设备本地域**（采集开不开、上报开不开、L2 会话路径）—— 存在 daemon 侧的
`app_settings` 表。云端只代理，不落库：多设备各有各的开关，云端合成一份等于制造假象。

PUT 必须带**显式值**（T13 精神）：缺 `items` 直接拒，不做"没传就写默认"。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from skillos.api.deps import get_current_user_id
from skillos.api.routes._daemon_proxy import call_daemon

router = APIRouter(prefix="/settings", tags=["settings"])


class SettingsPutBody(BaseModel):
    items: dict[str, object] = Field(
        ..., description="要写入的开关（{key: value}，值即最终值，不合并猜测）",
    )


@router.get("")
async def get_settings(request: Request,
                       user_id: str = Depends(get_current_user_id)) -> dict:
    """全部已登记开关的当前值（含默认值，客户端不必自己维护默认表）。"""
    return await call_daemon(request, user_id, "settings_get", {}, timeout=15.0)


@router.put("")
async def put_settings(body: SettingsPutBody, request: Request,
                       user_id: str = Depends(get_current_user_id)) -> dict:
    """批量写开关：先全量校验键名再写，不允许半批生效。

    `telemetry.runtime_paths` 是 map（runtime → 目录列表），传 `{}` 表示清空手动路径，
    回到自动探测（N-11.9③）。
    """
    return await call_daemon(
        request, user_id, "settings_put", {"items": body.items}, timeout=15.0,
    )
