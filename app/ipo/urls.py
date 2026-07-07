from django.urls import path

from . import views


app_name = "ipo"

urlpatterns = [
    path("", views.index, name="index"),
    path("listings/", views.listing_list, name="listing_list"),
    path("listings/expected-margin/", views.expected_margin_data, name="expected_margin_data"),
    path("subscriptions/", views.subscription_trade_list, name="subscription_trade_list"),
    path("subscriptions/create/", views.subscription_trade_create, name="subscription_trade_create"),
    path("subscriptions/<int:pk>/", views.subscription_trade_detail, name="subscription_trade_detail"),
    path("subscriptions/<int:pk>/edit/", views.subscription_trade_edit, name="subscription_trade_edit"),
    path("subscriptions/<int:pk>/allotment/", views.subscription_trade_allotment, name="subscription_trade_allotment"),
    path("subscriptions/<int:pk>/sale/", views.subscription_trade_sale, name="subscription_trade_sale"),
    path("subscriptions/<int:pk>/sale/<int:transaction_id>/", views.subscription_trade_sale, name="subscription_trade_sale_edit"),
    path("subscriptions/<int:pk>/sale/<int:transaction_id>/cancel/", views.subscription_trade_sale_cancel, name="subscription_trade_sale_cancel"),
    path("subscriptions/<int:pk>/delete/", views.subscription_trade_delete, name="subscription_trade_delete"),
    path("allotments/", views.allotment_index, name="allotment_index"),
    path("strategies/", views.strategy_index, name="strategy_index"),
    path("reviews/", views.review_index, name="review_index"),
    path("listings/recognize-image/", views.recognize_listing_image, name="recognize_listing_image"),
    path("listings/create/", views.listing_create, name="listing_create"),
    path("listings/<int:pk>/", views.listing_detail, name="listing_detail"),
    path("listings/<int:pk>/edit/", views.listing_edit, name="listing_edit"),
]
