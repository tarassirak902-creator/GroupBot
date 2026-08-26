from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from groupbot.advertising_models import AdvertisingDeal
from groupbot.routers import advertising_requests as requests_module
from groupbot.routers import advertising_sales_nav as sales_module


def _is_mutual(deal: AdvertisingDeal) -> bool:
    return bool((deal.agreed_terms_json or {}).get("mutual_op"))


def install_mutual_ui_patches() -> None:
    original_kind = requests_module._kind_text
    original_keyboard = requests_module._deal_keyboard
    original_sales_kind = sales_module._kind_text

    def request_kind(deal: AdvertisingDeal) -> str:
        if _is_mutual(deal):
            return "🤝 Взаимное ОП"
        return original_kind(deal)

    def sales_kind(deal: AdvertisingDeal) -> str:
        if _is_mutual(deal):
            return "🤝"
        return original_sales_kind(deal)

    def deal_keyboard(deal: AdvertisingDeal, viewer_id: int) -> InlineKeyboardMarkup:
        if not _is_mutual(deal):
            return original_keyboard(deal, viewer_id)
        rows: list[list[InlineKeyboardButton]] = []
        other_id = deal.buyer_user_id if viewer_id == deal.seller_user_id else deal.seller_user_id
        rows.append([InlineKeyboardButton(text="💬 Связаться", url=f"tg://user?id={other_id}")])
        if viewer_id == deal.seller_user_id and deal.status == "pending":
            rows.append([
                InlineKeyboardButton(text="✅ Принять", callback_data=f"ads:mutual:accept:{deal.id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"ads:mutual:reject:{deal.id}"),
            ])
        if viewer_id == deal.buyer_user_id and deal.status == "pending" and deal.started_at is None:
            rows.append([InlineKeyboardButton(text="❌ Отменить заявку", callback_data=f"ads:deal:cancel_ask:{deal.id}")])
        if viewer_id == deal.seller_user_id:
            rows.append([InlineKeyboardButton(text="◀️ Мои продажи", callback_data="ads:my_sales")])
        else:
            rows.append([InlineKeyboardButton(text="📋 Мои покупки", callback_data="ads:my_buys")])
        rows.append([InlineKeyboardButton(text="◀️ Реклама", callback_data="ads:home")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    requests_module._kind_text = request_kind
    requests_module._deal_keyboard = deal_keyboard
    sales_module._kind_text = sales_kind
