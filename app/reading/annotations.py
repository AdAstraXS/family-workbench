import math
import re
from django.core.exceptions import ValidationError
from django.db.models import Q
from .models import Annotation, Book
from .permissions import accessible_books
from .services import validate_location
from .text import chapter_text, compact_text, pdf_text


def accessible_annotations(member, book=None):
    qs = Annotation.objects.filter(book__in=accessible_books(member)).filter(Q(author=member)|Q(visibility=Book.FAMILY))
    if book is not None:
        qs = qs.filter(book=book)
    return qs.select_related("author", "book", "book__file")


def _cfi_bounds(cfi):
    """Compare locations produced by Foliate's range CFI serializer."""
    if not isinstance(cfi, str) or not cfi.startswith("epubcfi("):
        return None
    parts = cfi[8:-1].split(",")
    if len(parts) not in {1, 3}:
        return None
    base = parts[0].split("!")[-1]
    def position(path):
        path = re.sub(r"\[[^]]*]", "", path)
        steps = re.findall(r"/(\d+)(?::(\d+))?", path)
        return tuple((int(index), int(offset) if offset else -1) for index, offset in steps)
    if len(parts) == 1:
        point = position(base)
        return (point, point) if point else None
    start, end = position(base + parts[1]), position(base + parts[2])
    return (start, end) if start and end and start <= end else None


def same_passage(left, right):
    if "page" in left or "page" in right:
        if left.get("page") != right.get("page"):
            return False
        for a in left.get("rects", []):
            for b in right.get("rects", []):
                width = min(a[0]+a[2], b[0]+b[2])-max(a[0], b[0])
                height = min(a[1]+a[3], b[1]+b[3])-max(a[1], b[1])
                if width > min(a[2], b[2])*.1 and height > min(a[3], b[3])*.1:
                    return True
        return False
    if left.get("section") != right.get("section"):
        return False
    if left.get("cfi") == right.get("cfi"):
        return True
    a, b = _cfi_bounds(left.get("cfi")), _cfi_bounds(right.get("cfi"))
    return bool(a and b and a[0] < b[1] and b[0] < a[1])


def validate_annotation(book, data):
    file = book.file
    quote = data.get("quote", "")
    if not isinstance(quote, str) or not quote.strip() or len(quote) > 4000:
        raise ValidationError("请选择 1 至 4000 字的原文。")
    note = data.get("note", "")
    if not isinstance(note, str) or len(note) > 10000:
        raise ValidationError("批注最多 10000 字。")
    visibility = data.get("visibility", Book.PRIVATE)
    if visibility not in {Book.PRIVATE, Book.FAMILY}:
        raise ValidationError("批注分享范围不正确。")
    anchor = data.get("anchor")
    if not isinstance(anchor, dict):
        raise ValidationError("划线位置无效。")
    validate_location(file, {**data,"location":anchor,"revision":0,"progress":0})
    if file.format == "pdf":
        page, rectangles = anchor.get("page"), anchor.get("rects")
        if file.page_count and page > file.page_count:
            raise ValidationError("PDF 页码超出范围。")
        if not isinstance(rectangles,list) or not 1 <= len(rectangles) <= 100:
            raise ValidationError("请重新选择页内文字。")
        for rect in rectangles:
            if not isinstance(rect,list) or len(rect)!=4 or any(type(v) not in {int,float} or not math.isfinite(v) or not 0<=v<=1 for v in rect):
                raise ValidationError("划线区域无效。")
            if rect[0]+rect[2]>1.001 or rect[1]+rect[3]>1.001 or rect[2]<=0 or rect[3]<=0:
                raise ValidationError("划线区域超出页面。")
        # Text existence is checked independently; geometry is restored by the reader.
        text = pdf_text(file, page, page)["pages"][0]["text"]
        clean_anchor = {"page":page,"rects":rectangles}
    else:
        chapter = chapter_text(file,anchor.get("section"))
        text = chapter["text"]
        clean_anchor = {"cfi":anchor["cfi"],"section":chapter["index"],"href":chapter["href"]}
    return {"quote":quote.strip(),"note":note.strip(),"visibility":visibility,"anchor":clean_anchor,
            "file_hash":file.sha256,"normalizer_version":file.normalizer_version,
            "text_matched": bool(compact_text(quote)) and compact_text(quote) in compact_text(text)}


def annotation_payload(item, member):
    return {"id":str(item.pk),"quote":item.quote,"note":item.note,"anchor":item.anchor,
            "author":item.author.display_name,"owned":item.author_id==member.pk,"revision":item.revision,
            "visibility":item.visibility,"highlight_visible":item.highlight_visible,
            "has_comments":item.comments.exists(),"text_matched":item.text_matched}
