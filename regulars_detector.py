"""
Детектор постоянников (RegularsDetector) — плагин для FunPay Cardinal v0.1.17+

🎯 Распознаёт постоянных покупателей и помогает давать им VIP-сервис.

Возможности:
 • Ведёт базу покупателей по истории ваших продаж: сколько раз купили,
   сколько всего потратили, когда последний раз.
 • 👑 При новом заказе от постоянного покупателя присылает уведомление:
   «buyer1 покупает у вас 5-й раз! Всего потрачено: 850 ₽» — можно сразу
   положить бонус, написать личное спасибо, дать скидку на будущее.
 • Топ покупателей в меню /regulars (по числу покупок и сумме).
 • База обновляется автоматически (по умолчанию раз в 24 часа) и вручную кнопкой.
 • Между полными сканами история дополняется инкрементально по каждому новому заказу.
 • Порог «постоянника» настраивается (по умолчанию 2 покупки).

Команды Telegram:
 • /regulars — открыть меню плагина.

Установка: положить этот файл в папку plugins/ и перезапустить Кардинал.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING

from telebot.types import InlineKeyboardMarkup as K, InlineKeyboardButton as B, CallbackQuery, Message
from telebot.apihelper import ApiTelegramException

try:
    from tg_bot import CBT, static_keyboards as skb, utils as tg_utils
except Exception:
    CBT = None
    skb = None
    tg_utils = None

if TYPE_CHECKING:
    from cardinal import Cardinal

from FunPayAPI.common.enums import OrderStatuses, Currency

logger = logging.getLogger("FPC.Regulars")

# ══════════════════════════════ МЕТАДАННЫЕ ПЛАГИНА ══════════════════════════════
NAME = "RegularsDetector (Repeat Customers)"
VERSION = "1.0.1"
DESCRIPTION = ("Распознаёт постоянных покупателей: уведомление «N-я покупка» при новом заказе, "
               "топ клиентов по покупкам и суммам, автообновление базы по истории продаж. "
               "Команда: /regulars")
CREDITS = "yungspider"
UUID = "dbee3ecd-5734-471c-960a-1e01b5275679"
SETTINGS_PAGE = True

# ══════════════════════════════ НАСТРОЙКИ ПЛАГИНА ═══════════════════════════════
PLUGIN_DIR = os.path.join("storage", "plugins", "regulars_detector")
DB_PATH = os.path.join(PLUGIN_DIR, "db.json")

DEFAULT_DB = {
    "buyers": {},        # {str(buyer_id): {"name": str, "count": int, "spent": float, "last_ts": float}}
    "last_scan": 0.0,    # время последнего полного скана истории
    "threshold": 2,      # с какой покупки считать постоянным
    "refresh_hours": 24, # как часто пересканировать историю
}
THRESHOLD_RANGE = (2, 100)
REFRESH_RANGE = (1, 168)
MAX_PAGES = 40          # ограничение полного скана (страниц истории)
CURRENCY_SIGNS = {Currency.USD: "$", Currency.RUB: "₽", Currency.EUR: "€", Currency.UNKNOWN: ""}

_stop_event = threading.Event()
_db_lock = threading.Lock()
_scan_lock = threading.Lock()
_cardinal: "Cardinal | None" = None


# ══════════════════════════════ УТИЛИТЫ ═════════════════════════════════════════
def _clamp(value, min_v, max_v):
    return max(min_v, min(max_v, value))


def _load_db() -> dict:
    with _db_lock:
        data = {}
        if os.path.exists(DB_PATH):
            try:
                with open(DB_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                logger.error("Regulars: не удалось прочитать базу, начинаю с чистой.")
                logger.debug("TRACEBACK", exc_info=True)
                data = {}
        db = json.loads(json.dumps(DEFAULT_DB))
        if isinstance(data, dict):
            db.update({k: v for k, v in data.items() if k in db})
        if not isinstance(db["buyers"], dict):
            db["buyers"] = {}
        try:
            db["threshold"] = _clamp(int(db["threshold"]), *THRESHOLD_RANGE)
        except (TypeError, ValueError):
            db["threshold"] = DEFAULT_DB["threshold"]
        try:
            db["refresh_hours"] = _clamp(int(db["refresh_hours"]), *REFRESH_RANGE)
        except (TypeError, ValueError):
            db["refresh_hours"] = DEFAULT_DB["refresh_hours"]
        try:
            db["last_scan"] = float(db["last_scan"] or 0)
        except (TypeError, ValueError):
            db["last_scan"] = 0.0
        return db


def _save_db(db: dict) -> None:
    with _db_lock:
        try:
            os.makedirs(PLUGIN_DIR, exist_ok=True)
            with open(DB_PATH, "w", encoding="utf-8") as f:
                json.dump(db, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.error("Regulars: не удалось сохранить базу.")
            logger.debug("TRACEBACK", exc_info=True)


def _short(text: str, limit: int = 40) -> str:
    text = str(text or "").replace("\n", " ")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _escape(text) -> str:
    if tg_utils:
        return tg_utils.escape(str(text))
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _plugin_enabled(crd: "Cardinal") -> bool:
    try:
        return crd.plugins[UUID].enabled
    except Exception:
        return False


def _sign(currency) -> str:
    try:
        return CURRENCY_SIGNS.get(currency, "")
    except Exception:
        return ""


def _notify(crd: "Cardinal", text: str) -> None:
    if not crd.telegram:
        return
    kwargs = {"notification_type": tg_utils.NotificationTypes.other} if tg_utils else {}
    try:
        crd.telegram.send_notification(text, **kwargs)
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)


# ══════════════════════════════ БАЗА ПОКУПАТЕЛЕЙ ════════════════════════════════
def _full_scan(crd: "Cardinal") -> int:
    """Полный скан истории продаж. Возвращает число учтенных заказов."""
    if not _scan_lock.acquire(blocking=False):
        return -1  # скан уже идет
    try:
        buyers: dict = {}
        start_from = None
        scanned = 0
        for _ in range(MAX_PAGES):
            if _stop_event.is_set():
                return scanned
            try:
                result = crd.account.get_sales(start_from=start_from, include_paid=True,
                                               include_closed=True, include_refunded=False)
            except Exception:
                logger.error("Regulars: ошибка получения истории продаж.")
                logger.debug("TRACEBACK", exc_info=True)
                break
            next_id, orders = result[0], result[1]
            if not orders:
                break
            for o in orders:
                if o.status not in (OrderStatuses.PAID, OrderStatuses.CLOSED):
                    continue
                bid = str(getattr(o, "buyer_id", "") or "")
                if not bid:
                    continue
                scanned += 1
                b = buyers.setdefault(bid, {"name": o.buyer_username or "?", "count": 0,
                                            "spent": 0.0, "last_ts": 0.0})
                b["count"] += 1
                try:
                    b["spent"] += float(o.price or 0)
                except (TypeError, ValueError):
                    pass
                try:
                    ts = o.date.timestamp() if o.date else 0.0
                except Exception:
                    ts = 0.0
                if ts > b["last_ts"]:
                    b["last_ts"] = ts
                    b["name"] = o.buyer_username or b["name"]
            if not next_id:
                break
            start_from = next_id
        db = _load_db()
        db["buyers"] = buyers
        db["last_scan"] = time.time()
        _save_db(db)
        logger.info(f"Regulars: база обновлена — {len(buyers)} покупателей, {scanned} заказов.")
        return scanned
    finally:
        _scan_lock.release()


def new_order_handler(c: "Cardinal", e, *args) -> None:
    """BIND_TO_NEW_ORDER: инкрементально обновляет базу и уведомляет о постоянниках."""
    try:
        if not _plugin_enabled(c):
            return
        order = getattr(e, "order", None)
        if order is None or getattr(order, "buyer_id", None) is None:
            return
        bid = str(order.buyer_id)
        db = _load_db()
        b = db["buyers"].get(bid)
        if b is None:
            b = {"name": order.buyer_username or "?", "count": 0, "spent": 0.0, "last_ts": 0.0}
        b["count"] = int(b.get("count", 0)) + 1
        try:
            b["spent"] = float(b.get("spent", 0)) + float(order.price or 0)
        except (TypeError, ValueError):
            pass
        b["name"] = order.buyer_username or b.get("name", "?")
        b["last_ts"] = time.time()
        db["buyers"][bid] = b
        _save_db(db)

        if b["count"] >= db["threshold"]:
            sign = _sign(order.currency)
            text = (f"👑 <b>Постоянный покупатель!</b>\n\n"
                    f"<a href=\"https://funpay.com/users/{bid}/\">{_escape(b['name'])}</a> "
                    f"покупает у вас <b>{b['count']}-й раз</b>!\n\n"
                    f"Заказ: <code>#{_escape(order.id)}</code>\n"
                    f"Лот: <code>{_escape(_short(order.description, 90))}</code>\n"
                    f"Сумма заказа: <b>{order.price:g} {sign}</b>\n"
                    f"Всего потрачено у вас: <b>{b['spent']:g} {sign}</b>\n\n"
                    f"💎 Самое время положить бонус или написать личное спасибо 😉")
            _notify(c, text)
            logger.info(f"Regulars: заказ #{order.id} от постоянника {b['name']} ({b['count']}-я покупка).")
    except Exception:
        logger.error("Regulars: ошибка в обработчике нового заказа.")
        logger.debug("TRACEBACK", exc_info=True)


def _refresh_loop(crd: "Cardinal") -> None:
    """Фоновый поток: первичный скан + периодическое автообновление базы."""
    logger.info("Regulars: поток обновления базы запущен.")
    while not _stop_event.wait(30):
        try:
            if not _plugin_enabled(crd) or not crd.account.is_initiated:
                continue
            db = _load_db()
            age_h = (time.time() - db["last_scan"]) / 3600 if db["last_scan"] else None
            if age_h is None or age_h >= db["refresh_hours"]:
                _full_scan(crd)
        except Exception:
            logger.error("Regulars: ошибка в потоке обновления базы.")
            logger.debug("TRACEBACK", exc_info=True)


# ══════════════════════════ TELEGRAM-ИНТЕРФЕЙС ══════════════════════════════════
CB_HOME = "rgl:home"
CB_TOP = "rgl:top"        # rgl:top:{offset}
CB_REFRESH = "rgl:refresh"
CB_THR = "rgl:thr"
CB_RH = "rgl:rh"
CB_NOOP = "rgl:noop"
ST_THR = "rgl:st:thr"
ST_RH = "rgl:st:rh"
PER_PAGE = 10


def _home_text(crd: "Cardinal") -> str:
    db = _load_db()
    buyers = db["buyers"]
    regulars = sum(1 for b in buyers.values() if int(b.get("count", 0)) >= db["threshold"])
    if db["last_scan"]:
        scan_str = datetime.fromtimestamp(db["last_scan"]).strftime("%d.%m.%Y %H:%M")
    else:
        scan_str = "еще не сканировалась (скан запустится автоматически)"
    return (f"🎯 <b>{_escape(NAME)}</b> <i>v{VERSION}</i>\n\n"
            f"Покупателей в базе: <b>{len(buyers)}</b>\n"
            f"👑 Постоянных (от <b>{db['threshold']}</b> покупок): <b>{regulars}</b>\n"
            f"База обновлена: <b>{scan_str}</b>\n"
            f"⏱ Автообновление: каждые <b>{db['refresh_hours']}</b> ч.\n\n"
            f"<i>При новом заказе от постоянного покупателя придет уведомление «👑 N-я покупка».</i>")


def _home_kb() -> K:
    db = _load_db()
    kb = K(row_width=2)
    kb.add(B("👑 Топ покупателей", None, f"{CB_TOP}:0"))
    kb.add(B("🔄 Обновить базу сейчас", None, CB_REFRESH))
    kb.add(B(f"🎯 Порог: {db['threshold']} покупки", None, CB_THR))
    kb.add(B(f"⏱ Автообновление: {db['refresh_hours']} ч.", None, CB_RH))
    kb.add(B("🔃 Обновить меню", None, CB_HOME))
    if CBT:
        kb.add(B("🔙 Назад к плагину", None, f"{CBT.EDIT_PLUGIN}:{UUID}:0"))
    return kb


def _top_items(db: dict) -> list:
    items = list(db["buyers"].items())
    items.sort(key=lambda kv: (-int(kv[1].get("count", 0)), -float(kv[1].get("spent", 0) or 0)))
    return items


def _top_kb(crd: "Cardinal", db: dict, offset: int) -> K:
    items = _top_items(db)
    kb = K(row_width=1)
    for bid, b in items[offset:offset + PER_PAGE]:
        crown = "👑" if int(b.get("count", 0)) >= db["threshold"] else "👤"
        kb.add(B(f"{crown} {_short(b.get('name', '?'), 28)} — {b.get('count', 0)} пок., "
                 f"{float(b.get('spent', 0) or 0):g} ₽", url=f"https://funpay.com/users/{bid}/"))
    pages = max(1, -(-len(items) // PER_PAGE))
    if pages > 1:
        row = []
        if offset > 0:
            row.append(B("⬅️", None, f"{CB_TOP}:{offset - PER_PAGE}"))
        row.append(B(f"{offset // PER_PAGE + 1}/{pages}", None, CB_NOOP))
        if offset + PER_PAGE < len(items):
            row.append(B("➡️", None, f"{CB_TOP}:{offset + PER_PAGE}"))
        kb.row(*row)
    kb.add(B("🏠 Главное меню", None, CB_HOME))
    return kb


def _answer(bot, c: CallbackQuery, text: str | None = None, alert: bool = False) -> None:
    try:
        bot.answer_callback_query(c.id, text, show_alert=alert)
    except Exception:
        pass


def _edit_or_send(bot, c: CallbackQuery, text: str, kb: K | None) -> None:
    try:
        bot.edit_message_text(text, c.message.chat.id, c.message.id, parse_mode="HTML", reply_markup=kb)
    except ApiTelegramException as e:
        if "message is not modified" not in str(e):
            try:
                bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
            except Exception:
                logger.debug("TRACEBACK", exc_info=True)
    except Exception:
        try:
            bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)


def init(crd: "Cardinal", *args) -> None:
    """Инициализация плагина (BIND_TO_PRE_INIT)."""
    global _cardinal
    _cardinal = crd
    _load_db()

    tg = crd.telegram
    if tg is not None:
        bot = tg.bot

        def open_home(c: CallbackQuery) -> None:
            _answer(bot, c)
            _edit_or_send(bot, c, _home_text(crd), _home_kb())

        def open_top(c: CallbackQuery) -> None:
            _answer(bot, c)
            offset = 0
            parts = str(c.data).split(":")
            if len(parts) > 2 and parts[2].isdigit():
                offset = int(parts[2])
            db = _load_db()
            items = _top_items(db)
            if not items:
                text = ("👑 <b>Топ покупателей</b>\n\nБаза пуста — обновите её (кнопка на главном меню) "
                        "или дождитесь автоматического сканирования истории продаж.")
            else:
                text = (f"👑 <b>Топ покупателей</b> ({len(items)})\n\n"
                        f"По числу покупок и сумме. Нажмите на покупателя, чтобы открыть его профиль "
                        f"на FunPay:")
            _edit_or_send(bot, c, text, _top_kb(crd, db, offset))

        def refresh_now(c: CallbackQuery) -> None:
            if not crd.account.is_initiated:
                _answer(bot, c, "Аккаунт FunPay еще не инициализирован. Попробуйте позже.", alert=True)
                return
            if _scan_lock.locked():
                _answer(bot, c, "Сканирование уже идет. Дождитесь окончания.", alert=True)
                return
            _answer(bot, c, "🔄 Запустил полное сканирование истории продаж. "
                            "Это может занять пару минут — меню обновится само, когда закончу.",
                    alert=True)

            def worker():
                scanned = _full_scan(crd)
                if scanned >= 0 and crd.telegram:
                    db = _load_db()
                    _notify(crd, f"🔄 <b>База постоянников обновлена.</b>\n\n"
                                 f"Просканировано заказов: <b>{scanned}</b>\n"
                                 f"Покупателей в базе: <b>{len(db['buyers'])}</b>")

            threading.Thread(target=worker, daemon=True).start()

        def act_threshold(c: CallbackQuery) -> None:
            db = _load_db()
            result = bot.send_message(
                c.message.chat.id,
                f"🎯 Введите, с какой покупки считать покупателя постоянным "
                f"(<code>{THRESHOLD_RANGE[0]}</code> - <code>{THRESHOLD_RANGE[1]}</code>).\n\n"
                f"Текущее значение: <b>{db['threshold']}</b>.",
                parse_mode="HTML", reply_markup=skb.CLEAR_STATE_BTN() if skb else None)
            tg.set_state(c.message.chat.id, result.id, c.from_user.id, ST_THR)
            _answer(bot, c)

        def act_refresh_hours(c: CallbackQuery) -> None:
            db = _load_db()
            result = bot.send_message(
                c.message.chat.id,
                f"⏱ Введите период автообновления базы в часах "
                f"(<code>{REFRESH_RANGE[0]}</code> - <code>{REFRESH_RANGE[1]}</code>).\n\n"
                f"Текущее значение: <b>{db['refresh_hours']}</b> ч.",
                parse_mode="HTML", reply_markup=skb.CLEAR_STATE_BTN() if skb else None)
            tg.set_state(c.message.chat.id, result.id, c.from_user.id, ST_RH)
            _answer(bot, c)

        def _state_eq(m: Message, state: str) -> bool:
            try:
                return tg.check_state(m.chat.id, m.from_user.id, state)
            except Exception:
                return False

        def edit_threshold(m: Message) -> None:
            tg.clear_state(m.chat.id, m.from_user.id, True)
            _edit_int(m, "threshold", THRESHOLD_RANGE, "🎯 Порог постоянника")

        def edit_refresh_hours(m: Message) -> None:
            tg.clear_state(m.chat.id, m.from_user.id, True)
            _edit_int(m, "refresh_hours", REFRESH_RANGE, "⏱ Автообновление базы")

        def _edit_int(m: Message, key: str, rng: tuple, label: str) -> None:
            try:
                value = _clamp(int(str(m.text).strip()), *rng)
            except (ValueError, AttributeError):
                bot.reply_to(m, "❌ Нужно ввести число. Попробуйте еще раз.")
                return
            db = _load_db()
            db[key] = value
            _save_db(db)
            bot.reply_to(m, f"✅ {label}: <b>{value}</b>", parse_mode="HTML")

        def cmd_open(m: Message) -> None:
            bot.send_message(m.chat.id, _home_text(crd), parse_mode="HTML", reply_markup=_home_kb())

        def noop(c: CallbackQuery) -> None:
            _answer(bot, c)

        tg.cbq_handler(open_home, lambda c: c.data.startswith(f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
        tg.cbq_handler(open_home, lambda c: c.data == CB_HOME)
        tg.cbq_handler(open_top, lambda c: c.data.startswith(f"{CB_TOP}:"))
        tg.cbq_handler(refresh_now, lambda c: c.data == CB_REFRESH)
        tg.cbq_handler(act_threshold, lambda c: c.data == CB_THR)
        tg.cbq_handler(act_refresh_hours, lambda c: c.data == CB_RH)
        tg.cbq_handler(noop, lambda c: c.data == CB_NOOP)
        tg.msg_handler(edit_threshold, func=lambda m: _state_eq(m, ST_THR))
        tg.msg_handler(edit_refresh_hours, func=lambda m: _state_eq(m, ST_RH))
        tg.msg_handler(cmd_open, commands=["regulars"])

        try:
            crd.add_telegram_commands(UUID, [("regulars", "Детектор постоянных покупателей.", True)])
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)

        logger.info(f"Regulars v{VERSION}: Telegram-меню зарегистрировано (команда /regulars).")
    else:
        logger.info(f"Regulars v{VERSION}: Telegram выключен — работает только фоновая база.")

    threading.Thread(target=_refresh_loop, args=(crd,), daemon=True).start()


def on_delete(crd: "Cardinal", c=None) -> None:
    """Вызывается при удалении плагина через ПУ (BIND_TO_DELETE)."""
    _stop_event.set()


# ══════════════════════════ ПРИВЯЗКА К СОБЫТИЯМ КАРДИНАЛА ═══════════════════════
BIND_TO_PRE_INIT = [init]
BIND_TO_NEW_ORDER = [new_order_handler]
BIND_TO_DELETE = on_delete
