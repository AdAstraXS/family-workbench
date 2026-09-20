"""投研模块请求测试专用 URLConf。

直接复制 config.urls 的 urlpatterns 列表（M1-D 起生产路由已含
/research/，不再追加，避免重复注册 namespace）；
base.html 原有链接仍可 reverse，同时不导入生产 settings。
"""
from .urls import urlpatterns as base_urlpatterns

urlpatterns = list(base_urlpatterns)
