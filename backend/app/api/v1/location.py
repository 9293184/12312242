"""基于访问者公网 IP 的地理定位接口。

前端天气/日出日落组件优先使用浏览器 Geolocation API,但该 API 要求安全上下文
(HTTPS 或 localhost)。通过明文 HTTP 部署时(如 http://<服务器 IP>/),浏览器
定位不可用,且浏览器直连国外 IP 定位 API 常因网络原因超时。

本接口在服务端读取反代传入的真实客户端 IP(X-Forwarded-For / X-Real-IP),
再查询免费 IP 地理库,作为前端的可靠降级路径。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import urllib.error
import urllib.request

from fastapi import APIRouter, Request

router = APIRouter(prefix="/location", tags=["location"])

logger = logging.getLogger(__name__)


def _client_ip(request: Request) -> str:
    """提取真实客户端 IP。

    nginx 需配置:
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    """
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    real_ip = request.headers.get("x-real-ip", "").strip()
    if real_ip:
        return real_ip
    return request.client.host if request.client else ""


def _is_public_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved)


def _http_get_json(url: str, timeout: float = 4.0):
    req = urllib.request.Request(url, headers={"User-Agent": "PaperPilot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (URL 为固定白名单服务)
        return json.loads(resp.read().decode("utf-8"))


@router.get("/geo")
def geo_by_ip(request: Request) -> dict:
    """按访问者公网 IP 返回坐标与城市。

    成功: {source: "ip", lat, lng, city, ip}
    无法定位(内网 IP / 上游失败): {source: "none", ...},由前端继续降级。
    """
    ip = _client_ip(request)
    if not ip or not _is_public_ip(ip):
        return {"source": "none", "ip": ip or "", "lat": None, "lng": None, "city": ""}

    # 供应商 1: ip-api.com(免费、无需 key、支持中文,约 45 次/分钟限流)
    try:
        data = _http_get_json(
            f"http://ip-api.com/json/{ip}?fields=status,message,lat,lon,city,regionName&lang=zh-CN",
            timeout=4.0,
        )
        if data.get("status") == "success" and data.get("lat") is not None and data.get("lon") is not None:
            return {
                "source": "ip",
                "ip": ip,
                "lat": data["lat"],
                "lng": data["lon"],
                "city": data.get("city") or data.get("regionName") or "",
            }
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        logger.info("ip-api.com 定位失败: %s", exc)

    # 供应商 2: ipapi.co(HTTPS)
    try:
        data = _http_get_json(f"https://ipapi.co/{ip}/json/", timeout=4.0)
        if isinstance(data.get("latitude"), (int, float)) and isinstance(data.get("longitude"), (int, float)):
            return {
                "source": "ip",
                "ip": ip,
                "lat": data["latitude"],
                "lng": data["longitude"],
                "city": data.get("city") or data.get("region") or "",
            }
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        logger.info("ipapi.co 定位失败: %s", exc)

    return {"source": "none", "ip": ip, "lat": None, "lng": None, "city": ""}
