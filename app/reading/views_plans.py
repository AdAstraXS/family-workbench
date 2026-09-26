from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST
from .models import ReadingPlan, ReadingPlanItem, ReadingPosition
from .permissions import accessible_books
from .views import member_required


class PlanForm(forms.ModelForm):
    class Meta:
        model=ReadingPlan
        fields=["title","target_date"]
        widgets={"target_date":forms.DateInput(attrs={"type":"date"})}


class ManualItemForm(forms.Form):
    title=forms.CharField(label="书名",max_length=250)
    kind=forms.ChoiceField(label="阅读方式",choices=[("paper","纸质书"),("external","外部阅读")])


@member_required
@require_http_methods(["GET","POST"])
def plans(request):
    form=PlanForm(request.POST or None)
    if request.method=="POST" and form.is_valid():
        plan=form.save(commit=False)
        plan.member=request.reader_member
        plan.save()
        return redirect("reading:plan",plan_id=plan.pk)
    return render(request,"reading/plans.html",{"plans":ReadingPlan.objects.filter(member=request.reader_member),"form":form})


@member_required
@require_http_methods(["GET","POST"])
def plan(request,plan_id):
    plan=get_object_or_404(ReadingPlan,pk=plan_id,member=request.reader_member)
    books=accessible_books(request.reader_member)
    if request.method=="POST":
        if request.POST.get("action")=="books":
            ids=request.POST.getlist("books")[:100]
            try:
                selected=list(books.filter(pk__in=ids))
            except (ValueError,ValidationError):
                selected=[]
            if len(selected)!=len(set(ids)):
                messages.error(request,"部分图书不可访问，请刷新后重选。")
            else:
                with transaction.atomic():
                    ReadingPlan.objects.select_for_update().get(pk=plan.pk)
                    for book in selected:
                        ReadingPlanItem.objects.get_or_create(plan=plan,book=book,defaults={"title":book.title,"kind":"online"})
                return redirect("reading:plan",plan_id=plan.pk)
        elif request.POST.get("action")=="settings":
            form=PlanForm(request.POST,instance=plan,prefix="settings")
            if form.is_valid():form.save()
            else:messages.error(request,"请填写计划名称和有效目标日期。")
            return redirect("reading:plan",plan_id=plan.pk)
        else:
            manual=ManualItemForm(request.POST)
            if manual.is_valid():
                ReadingPlanItem.objects.create(plan=plan,**manual.cleaned_data)
                return redirect("reading:plan",plan_id=plan.pk)
            messages.error(request,"请填写纸质书或外部阅读书名。")
    visible_ids=set(books.values_list("pk",flat=True))
    positions={p.book_id:p for p in ReadingPosition.objects.filter(member=request.reader_member,book__in=books)}
    rows=[]
    for item in plan.items.all():
        pos=positions.get(item.book_id)
        hidden=item.book_id is not None and item.book_id not in visible_ids
        rows.append({"item":item,"hidden":hidden,"progress":pos.percent if pos else item.manual_progress,
                     "done":bool(item.completed_at or (pos and pos.completed_at))})
    complete=sum(row["done"] for row in rows if not row["hidden"])
    return render(request,"reading/plan.html",{"plan":plan,"rows":rows,"books":books,"form":PlanForm(instance=plan,prefix="settings"),
                  "manual_form":ManualItemForm(),"complete":complete,"total":len(rows)})


@member_required
@require_POST
def plan_progress(request,item_id):
    item=get_object_or_404(ReadingPlanItem,pk=item_id,plan__member=request.reader_member)
    if item.book_id and not accessible_books(request.reader_member).filter(pk=item.book_id).exists():
        messages.error(request,"该图书已不可访问。")
        return redirect("reading:plan",plan_id=item.plan_id)
    progress=request.POST.get("progress","")
    revision=request.POST.get("revision","")
    if not progress.isdigit() or not 0<=int(progress)<=100 or not revision.isdigit():
        messages.error(request,"进度须在 0 至 100 之间。")
    elif not ReadingPlanItem.objects.filter(pk=item.pk,revision=int(revision)).update(manual_progress=int(progress),
        completed_at=timezone.now() if request.POST.get("done")=="yes" else None,revision=int(revision)+1):
        messages.error(request,"记录已有更新，请刷新后核对。")
    else:
        messages.success(request,"个人阅读记录已保存。")
    return redirect("reading:plan",plan_id=item.plan_id)
