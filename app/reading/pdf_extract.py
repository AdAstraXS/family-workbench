"""Bound PDF parsing in a subprocess: no Django, no network, no PDF scripts."""
import json
import sys


def extract(path, first=0, last=0):
    from pypdf import PdfReader
    reader = PdfReader(path)
    if reader.is_encrypted:
        raise ValueError("暂不支持需要密码的 PDF。")
    count = len(reader.pages)
    if not 1 <= count <= 100000:
        raise ValueError("PDF 页数超出处理范围。")
    if not first:
        return {"page_count": count, "pages": []}
    if not 1 <= first <= last <= count or last-first >= 20:
        raise ValueError("每次最多选择连续 20 页，且页码须有效。")
    pages, total = [], 0
    for number in range(first, last+1):
        text = reader.pages[number-1].extract_text() or ""
        total += len(text)
        if total > 60000:
            raise ValueError("所选页段文字过长，请缩小范围。")
        pages.append({"page":number,"text":text})
    return {"page_count":count,"pages":pages}


if __name__ == "__main__":
    try:
        if sys.platform != "win32":
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (768*1024*1024,768*1024*1024))
            resource.setrlimit(resource.RLIMIT_CPU, (45,45))
        result = extract(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))
        print(json.dumps(result, ensure_ascii=True))
    except Exception:
        print(json.dumps({"error":"PDF 无法在处理限额内解析，或文件需要密码/所选页段无效。"}))
        sys.exit(1)
