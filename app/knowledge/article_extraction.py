"""Bounded, local Mozilla Readability; no whole-page fallback or external AI."""
import json
import os
from pathlib import Path
import subprocess

from django.conf import settings

from .web_fetch import WebCaptureError

EXTRACTION_VERSION = "readability-0.6.0-v1"
MAX_INPUT_BYTES = 20 * 1024 * 1024


def runtime_command():
    return [getattr(settings, "KNOWLEDGE_READABILITY_NODE", "node"), "--max-old-space-size=256",
            str(Path(__file__).resolve().parent.parent / "article_extractor" / "extract.cjs")]


def runtime_environment():
    return {key: value for key, value in os.environ.items() if key in {"PATH", "Path", "SYSTEMROOT", "SystemRoot", "NODE_PATH", "LD_LIBRARY_PATH"}}


def runtime_ready():
    try:
        result = subprocess.run(runtime_command() + ["--check"], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=5, check=False, env=runtime_environment())
        return result.returncode == 0 and result.stdout.decode().strip() == EXTRACTION_VERSION
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return False


def extract_article(snapshot):
    payload = json.dumps({key: snapshot.get(key, "") for key in ("url", "html", "rawHtml")}, ensure_ascii=False).encode()
    if len(payload) > MAX_INPUT_BYTES:
        raise WebCaptureError("网页过大，无法可靠识别正文。请改用正文文件导入。")
    # Only the interpreter executable is configurable; never accept arguments from a URL.
    try:
        result = subprocess.run(runtime_command(), input=payload, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=15, check=False, env=runtime_environment())
    except FileNotFoundError as exc:
        raise WebCaptureError("正文提取运行环境尚未安装，请管理员构建新版网页服务镜像后重试。") from exc
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise WebCaptureError("正文识别超时或不可用，未将整页作为正文保存。请稍后重试或导入正文文件。") from exc
    try:
        parsed = json.loads(result.stdout)
        article = parsed.get("article")
        if result.returncode or not parsed.get("ok") or not isinstance(article, dict) or not isinstance(article.get("html"), str):
            raise ValueError
        if article.get("version") != EXTRACTION_VERSION or len(article["html"].encode()) > MAX_INPUT_BYTES:
            raise ValueError
        return article
    except (ValueError, TypeError, AttributeError) as exc:
        raise WebCaptureError("正文识别待确认：未识别到可靠的完整文章，未保存整页广告或推荐内容。请核对原网页，或上传仅含正文的文件。") from exc


def snapshot_body(snapshot):
    """Old archives keep their original converter; only new captures are extracted."""
    article = snapshot.get("article")
    if isinstance(article, dict) and article.get("version") == EXTRACTION_VERSION:
        return article["html"]
    return snapshot["html"]
