import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from django.core.exceptions import ValidationError
from .importer import parse_xml, local_name
from .storage import storage


def pdf_text(file, first=0, last=0):
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("pdf_extract.py")),
                                 storage().path(file.original_path), str(first), str(last)],
                                capture_output=True, timeout=55, check=False)
        data = json.loads(result.stdout)
        if result.returncode or data.get("error"):
            raise ValueError
        return data
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise ValidationError("PDF 暂无法解析。请检查密码、文件完整性、页码或缩小范围后重试。")


def chapter_text(file, index):
    if type(index) is not int or not 0 <= index < len(file.sections):
        raise ValidationError("请选择有效章节。")
    path = file.sections[index]["href"]
    with zipfile.ZipFile(storage().path(file.normalized_path)) as archive:
        root = parse_xml(archive.read(path))
    body = next((e for e in root.iter() if local_name(e.tag)=="body"), None)
    title = next(("".join(e.itertext()).strip() for e in root.iter() if local_name(e.tag) in {"h1","h2","title"}), "")
    return {"href":path,"index":index,"title":title or f"第 {index+1} 节", "text":"\n".join(body.itertext()).strip() if body is not None else ""}


def compact_text(value):
    return re.sub(r"\s+", "", value)
