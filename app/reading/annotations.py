import math
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
            "visibility":item.visibility,"text_matched":item.text_matched}
