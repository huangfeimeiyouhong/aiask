#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置加载 —— 从 .env 读取部署配置；缺失时使用合理默认值。

可配置项：
  APP_SECRET        应用签名密钥（必填，用于会话 cookie 签名）；未设置时自动生成并落盘 data/.app_secret
  PORT              监听端口，默认 8011
  HOST              监听地址，默认 0.0.0.0（容器内/服务器部署用）
  HUNYUAN_SECRET_ID 腾讯云 SecretId（不设则 LLM 走本地 Mock）
  HUNYUAN_SECRET_KEY 腾讯云 SecretKey
  HUNYUAN_MODEL     模型名，默认 hunyuan-turbo
  MAAS_API_KEY      腾讯 MaaS tokenhub 等 OpenAI 兼容网关的 API Key（设了即用真实模型）
  MAAS_BASE_URL     网关地址，默认 https://tokenhub.tencentmaas.com/v1
  MAAS_MODEL        单模型名（未配 MAAS_MODELS 时回退用），默认 hy3
  MAAS_MODELS       候选模型列表（逗号分隔），按顺序尝试；某模型额度/限流/不可用时
                    自动切到下一个模型，全部失败才报错。未配则回退 MAAS_MODEL/内置默认列表。
  HCG_BASE_URL      后厨管家接口基地址（可频繁切换：改 .env 该行或环境变量后重启）。
                    默认测试环境 http://hcgj-test-merchant.zou-yun.com/；
                    切回生产改回 https://wms.houchuguanjia.com/
  SESSION_SECURE    是否给 cookie 加 Secure 标志（走 HTTPS 反向代理时设 1）
  MOCK_LLM          设 1 强制走本地 MockLLM（无密钥演示）
  MAX_RECORDS       单次查询（单月）允许拉取的最大记录数；超过则触发「按月切片」或
                    「友好提示」保护，避免大区间直接全量拉取导致超时/内存不足。
                    默认 200000（服务器充足内存）；沙箱/低配环境可调小（如 8000）。
"""

import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")


def _load_dotenv(path=None):
    """极简 .env 解析（KEY=VALUE，支持 # 注释，忽略引号包裹）。"""
    p = path or os.path.join(BASE_DIR, ".env")
    if not os.path.exists(p):
        return
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            v = v.strip("\"'")
            os.environ.setdefault(k, v)


_load_dotenv()


def _resolve_secret():
    s = os.environ.get("APP_SECRET")
    if s:
        return s
    # 未配置则生成并落盘，避免每次重启会话失效
    os.makedirs(DATA_DIR, exist_ok=True)
    sp = os.path.join(DATA_DIR, ".app_secret")
    if os.path.exists(sp):
        with open(sp, "r", encoding="utf-8") as f:
            return f.read().strip()
    import secrets
    val = secrets.token_hex(32)
    with open(sp, "w", encoding="utf-8") as f:
        f.write(val)
    try:
        os.chmod(sp, 0o600)
    except Exception:
        pass
    return val


SETTINGS = {
    "APP_SECRET": _resolve_secret(),
    "PORT": int(os.environ.get("PORT", "8011")),
    "HOST": os.environ.get("HOST", "0.0.0.0"),
    "HUNYUAN_SECRET_ID": os.environ.get("HUNYUAN_SECRET_ID", ""),
    "HUNYUAN_SECRET_KEY": os.environ.get("HUNYUAN_SECRET_KEY", ""),
    "HUNYUAN_MODEL": os.environ.get("HUNYUAN_MODEL", "hunyuan-turbo"),
    "MAAS_API_KEY": os.environ.get("MAAS_API_KEY", ""),
    "MAAS_BASE_URL": os.environ.get("MAAS_BASE_URL", "https://tokenhub.tencentmaas.com/v1"),
    "MAAS_MODEL": os.environ.get("MAAS_MODEL", "hy3"),
    "MAAS_MODELS": os.environ.get("MAAS_MODELS", ""),
    "HCG_BASE_URL": os.environ.get("HCG_BASE_URL", "http://hcgj-test-merchant.zou-yun.com/"),
    "SESSION_SECURE": os.environ.get("SESSION_SECURE", "0") == "1",
    "MOCK_LLM": os.environ.get("MOCK_LLM", "0") == "1",
    "MAX_RECORDS": int(os.environ.get("MAX_RECORDS", "200000")),
    "SNAPSHOT_MAX_BYTES": int(os.environ.get("SNAPSHOT_MAX_BYTES", str(15 * 1024 * 1024))),
    "DATA_DIR": DATA_DIR,
}

# 同步到环境变量，便于 hunyuan.get_llm() 读取
os.environ.setdefault("HUNYUAN_SECRET_ID", SETTINGS["HUNYUAN_SECRET_ID"])
os.environ.setdefault("HUNYUAN_SECRET_KEY", SETTINGS["HUNYUAN_SECRET_KEY"])
os.environ.setdefault("HUNYUAN_MODEL", SETTINGS["HUNYUAN_MODEL"])
os.environ.setdefault("MAAS_API_KEY", SETTINGS["MAAS_API_KEY"])
os.environ.setdefault("MAAS_BASE_URL", SETTINGS["MAAS_BASE_URL"])
os.environ.setdefault("MAAS_MODEL", SETTINGS["MAAS_MODEL"])
if SETTINGS["MOCK_LLM"]:
    os.environ["MOCK_LLM"] = "1"

# 暴露为模块级变量，供 semantic_tools 直接 import
MAX_RECORDS = SETTINGS["MAX_RECORDS"]
SNAPSHOT_MAX_BYTES = SETTINGS["SNAPSHOT_MAX_BYTES"]

if __name__ == "__main__":
    for k, v in SETTINGS.items():
        print(f"{k} = {v if 'SECRET' not in k and 'KEY' not in k else '***'}")
    sys.exit(0)
