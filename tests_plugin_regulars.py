"""
Функциональные тесты плагина regulars_detector.py на моках.
Запуск: cd FPC && python3 tests_plugin_regulars.py
"""
import importlib.util
import json
import os
import sys
import time
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, ".")
sys.path.append("plugins")

from FunPayAPI import types as fp_types
from FunPayAPI.common import enums as fp_enums
from FunPayAPI.common.enums import OrderStatuses

spec = importlib.util.spec_from_file_location("plugins.regulars_detector", "plugins/regulars_detector.py")
plugin = importlib.util.module_from_spec(spec)
sys.modules["plugins.regulars_detector"] = plugin
spec.loader.exec_module(plugin)

plugin._refresh_loop = lambda crd: None  # поток тестируем через _full_scan

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("✓ " if cond else "✗ FAIL: ") + name)


def reset_db(**kwargs):
    os.makedirs(plugin.PLUGIN_DIR, exist_ok=True)
    base = json.loads(json.dumps(plugin.DEFAULT_DB))
    base.update(kwargs)
    with open(plugin.DB_PATH, "w", encoding="utf-8") as f:
        json.dump(base, f, ensure_ascii=False)


def mk_order(id_, buyer_id, buyer, price, status=OrderStatuses.CLOSED, days_ago=1.0,
             desc="Аккаунт Steam"):
    date = datetime.now() - __import__("datetime").timedelta(days=days_ago)
    return fp_types.OrderShortcut(id_, desc, price, fp_enums.Currency.RUB, buyer, buyer_id,
                                  7777, status, date, "Аккаунты", None, "",
                                  dont_search_amount=True)


# ───────────────────────────── моки ─────────────────────────────
class MockAccount:
    def __init__(self, pages=None):
        self.is_initiated = True
        self.id = 999
        self.pages = pages if pages is not None else {}   # {start_from: (next_id, orders)}
        self.get_sales_calls = 0

    def get_sales(self, start_from=None, **kw):
        self.get_sales_calls += 1
        if start_from in self.pages:
            return self.pages[start_from]
        return None, [], "ru", {}


class MockBot:
    def __init__(self):
        self.answers, self.edits, self.sent, self.replies = [], [], [], []

    def answer_callback_query(self, cb_id, text=None, show_alert=False):
        self.answers.append((cb_id, text, show_alert))

    def edit_message_text(self, text, chat_id, message_id, parse_mode=None, reply_markup=None):
        self.edits.append((text, reply_markup))
        return SimpleNamespace(id=message_id)

    def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))
        return SimpleNamespace(id=len(self.sent) + 100, chat=SimpleNamespace(id=chat_id))

    def reply_to(self, m, text, parse_mode=None):
        self.replies.append(text)

    def delete_message(self, chat_id, message_id):
        pass


def kb_buttons(kb) -> str:
    if kb is None:
        return ""
    try:
        return " | ".join(b.text for row in kb.keyboard for b in row if b.text)
    except Exception:
        return str(kb)


class MockTelegram:
    def __init__(self):
        self.bot = MockBot()
        self.cbq_handlers, self.msg_handlers, self.user_states = [], [], {}
        self.notification_settings, self.notifications = {}, []

    def cbq_handler(self, handler, func, **kwargs):
        self.cbq_handlers.append((handler, func))

    def msg_handler(self, handler, **kwargs):
        self.msg_handlers.append((handler, kwargs))

    def set_state(self, chat_id, message_id, user_id, state, data=None):
        self.user_states[(chat_id, user_id)] = {"state": state, "mid": message_id, "data": data or {}}

    def get_state(self, chat_id, user_id):
        return self.user_states.get((chat_id, user_id))

    def check_state(self, chat_id, user_id, state):
        return self.user_states.get((chat_id, user_id), {}).get("state") == state

    def clear_state(self, chat_id, user_id, del_msg=False):
        st = self.user_states.pop((chat_id, user_id), None)
        return st.get("mid") if st else None

    def send_notification(self, text, notification_type=None, **kwargs):
        self.notifications.append((text, notification_type))


class MockCardinal:
    def __init__(self, account, telegram):
        self.account = account
        self.telegram = telegram
        self.plugins = {plugin.UUID: SimpleNamespace(enabled=True)}
        self.registered_commands = None

    def add_telegram_commands(self, uuid, commands):
        self.registered_commands = commands


def make_crd(pages=None, with_tg=True):
    acc = MockAccount(pages)
    tg = MockTelegram() if with_tg else None
    return MockCardinal(acc, tg), acc, tg


# ═══════════════════════ ТЕСТ 1: метаданные ═══════════════════════
from uuid import UUID as _UUID
check("1.1 метаданные плагина", all(hasattr(plugin, f) for f in
      ["NAME", "VERSION", "DESCRIPTION", "CREDITS", "SETTINGS_PAGE", "UUID", "BIND_TO_DELETE"]))
check("1.2 UUID валиден", str(_UUID(plugin.UUID, version=4)) == plugin.UUID)
check("1.3 BIND_TO_NEW_ORDER привязан", plugin.BIND_TO_NEW_ORDER == [plugin.new_order_handler])

# ═══════════════════════ ТЕСТ 2: полное сканирование истории ═══════════════════════
pages = {
    None: ("PAGE2", [mk_order("A1", 501, "vasya", 100.0, days_ago=10),
                     mk_order("A2", 501, "vasya", 200.0, days_ago=5),
                     mk_order("A3", 502, "petya", 50.0, days_ago=7),
                     mk_order("A4", 503, "unpaid_guy", 10.0, status=OrderStatuses.UNPAID)]),
    "PAGE2": (None, [mk_order("A5", 501, "vasya", 300.0, days_ago=1),
                     mk_order("A6", 504, "masha", 700.0, status=OrderStatuses.PAID, days_ago=2)]),
}
crd, acc, tg = make_crd(pages)
reset_db()
scanned = plugin._full_scan(crd)
db = plugin._load_db()
check("2.1 скан: обработаны обе страницы", scanned == 5 and acc.get_sales_calls == 2)
check("2.2 UNPAID-заказ не учитывается", "503" not in db["buyers"])
check("2.3 покупки и суммы агрегированы",
      db["buyers"]["501"]["count"] == 3 and db["buyers"]["501"]["spent"] == 600.0
      and db["buyers"]["504"]["count"] == 1)
check("2.4 PAID-заказ учтен (не только CLOSED)", db["buyers"]["504"]["spent"] == 700.0)
check("2.5 имя — из последнего заказа, last_ts обновлен",
      db["buyers"]["501"]["name"] == "vasya" and db["buyers"]["501"]["last_ts"] > 0)
check("2.6 last_scan установлен", db["last_scan"] > time.time() - 60)

# повторный скан не дублирует (перезаписывает базу)
scanned = plugin._full_scan(crd)
db = plugin._load_db()
check("2.7 повторный скан — те же данные (без удвоения)", db["buyers"]["501"]["count"] == 3)

# ═══════════════════════ ТЕСТ 3: обработчик нового заказа ═══════════════════════
crd, acc, tg = make_crd(with_tg=True)
reset_db(threshold=2, buyers={"501": {"name": "vasya", "count": 2, "spent": 300.0, "last_ts": 0}})

e = SimpleNamespace(order=mk_order("NEW1", 501, "vasya", 150.0, status=OrderStatuses.PAID, days_ago=0))
plugin.new_order_handler(crd, e)
db = plugin._load_db()
check("3.1 инкремент: count=3, spent=450", db["buyers"]["501"]["count"] == 3
      and db["buyers"]["501"]["spent"] == 450.0)
check("3.2 уведомление 👑 «3-й раз»", any("👑" in t and "3-й раз" in t for t, _ in tg.notifications))
check("3.3 в уведомлении сумма заказа и всего потрачено",
      any("150" in t and "450" in t for t, _ in tg.notifications))

# первый раз (ниже порога) — без уведомления
tg.notifications.clear()
e = SimpleNamespace(order=mk_order("NEW2", 600, "novichok", 90.0, status=OrderStatuses.PAID, days_ago=0))
plugin.new_order_handler(crd, e)
db = plugin._load_db()
check("3.4 первая покупка: в базе, но без 👑", db["buyers"]["600"]["count"] == 1
      and tg.notifications == [])

# вторая покупка — ровно на пороге
e = SimpleNamespace(order=mk_order("NEW3", 600, "novichok", 90.0, status=OrderStatuses.PAID, days_ago=0))
plugin.new_order_handler(crd, e)
check("3.5 вторая покупка (порог=2) — 👑 пришло", any("👑" in t and "2-й раз" in t
      for t, _ in tg.notifications))

# выключенный плагин
crd.plugins[plugin.UUID].enabled = False
tg.notifications.clear()
before = plugin._load_db()["buyers"]["600"]["count"]
plugin.new_order_handler(crd, SimpleNamespace(order=mk_order("NEW4", 600, "novichok", 10.0,
                                                             status=OrderStatuses.PAID, days_ago=0)))
check("3.6 выключенный плагин не обновляет базу и не уведомляет",
      plugin._load_db()["buyers"]["600"]["count"] == before and tg.notifications == [])
crd.plugins[plugin.UUID].enabled = True

# событие без order — не падаем
plugin.new_order_handler(crd, SimpleNamespace())
check("3.7 событие без заказа не ломает плагин", True)

# ═══════════════════════ ТЕСТ 4: Telegram UI ═══════════════════════
crd, acc, tg = make_crd(with_tg=True)
plugin.init(crd)
bot = tg.bot
reset_db(threshold=2, last_scan=time.time(),
         buyers={"501": {"name": "vasya", "count": 5, "spent": 900.0, "last_ts": time.time()},
                 "502": {"name": "petya", "count": 2, "spent": 1500.0, "last_ts": time.time()},
                 "503": {"name": "masha", "count": 1, "spent": 100.0, "last_ts": time.time()}})
check("4.1 команда /regulars зарегистрирована",
      any(kw.get("commands") == ["regulars"] for _, kw in tg.msg_handlers))


def dispatch_cb(data, chat_id=111, msg_id=555, user_id=222):
    c = SimpleNamespace(data=data, id="cb1",
                        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), id=msg_id),
                        from_user=SimpleNamespace(id=user_id))
    for handler, func in tg.cbq_handlers:
        try:
            matched = func(c)
        except Exception:
            matched = False
        if matched:
            handler(c)
            return handler.__name__
    return None


def dispatch_msg(text, chat_id=111, user_id=222):
    m = SimpleNamespace(chat=SimpleNamespace(id=chat_id), from_user=SimpleNamespace(id=user_id),
                        text=text)
    for h, kw in tg.msg_handlers:
        f = kw.get("func")
        try:
            if f and f(m):
                h(m)
                return h.__name__
        except Exception:
            pass
    return None


def last_text():
    if bot.edits:
        return bot.edits[-1][0]
    return bot.sent[-1][1] if bot.sent else ""


def last_kb():
    return kb_buttons(bot.edits[-1][1]) if bot.edits else ""


name = dispatch_cb(f"47:{plugin.UUID}:0")
check("4.2 страница настроек открывает меню", name == "open_home" and "Детектор постоянников" in last_text())
check("4.3 статистика: 3 покупателя, 2 постоянника",
      "Покупателей в базе: <b>3</b>" in last_text() and "Постоянных" in last_text()
      and "<b>2</b>" in last_text())

name = dispatch_cb(f"{plugin.CB_TOP}:0")
btns = last_kb()
check("4.4 топ: vasya (5 покупок) первый", name == "open_top"
      and btns.index("vasya") < btns.index("petya"))
check("4.5 у постоянников 👑, у разовых 👤", "👑 vasya" in btns and "👤 masha" in btns)
check("4.6 сумма в кнопке топa", "900" in btns)

# порог
dispatch_cb(plugin.CB_THR)
st = tg.user_states.get((111, 222), {})
check("4.7 кнопка порога ставит состояние", st.get("state") == plugin.ST_THR)
dispatch_msg("5")
check("4.8 порог сохранен", plugin._load_db()["threshold"] == 5)
dispatch_cb(plugin.CB_THR)
dispatch_msg("1")
check("4.9 порог зажимается (1 → 2)", plugin._load_db()["threshold"] == 2)
dispatch_cb(plugin.CB_THR)
dispatch_msg("xyz")
check("4.10 нечисловой ввод отклоняется", plugin._load_db()["threshold"] == 2
      and "❌" in bot.replies[-1])

# автообновление
dispatch_cb(plugin.CB_RH)
dispatch_msg("12")
check("4.11 период автообновления сохранен", plugin._load_db()["refresh_hours"] == 12)

# ручное обновление базы (поток)
crd.account.pages = {None: (None, [mk_order("B1", 700, "olya", 55.0, days_ago=0)])}
name = dispatch_cb(plugin.CB_REFRESH)
time.sleep(0.5)
db = plugin._load_db()
check("4.12 «Обновить базу» пересканировало историю",
      name == "refresh_now" and db["buyers"].get("700", {}).get("count") == 1)
check("4.13 уведомление о завершении скана", any("База постоянников обновлена" in t
      for t, _ in tg.notifications))
dispatch_cb(plugin.CB_NOOP)
check("4.14 noop отвечает", True)

# ═══════════════════════ ТЕСТ 5: нормализация базы ═══════════════════════
os.makedirs(plugin.PLUGIN_DIR, exist_ok=True)
with open(plugin.DB_PATH, "w", encoding="utf-8") as f:
    json.dump({"buyers": "broken", "last_scan": "abc", "threshold": 999, "refresh_hours": 0}, f)
db = plugin._load_db()
check("5.1 битые значения нормализуются", db["buyers"] == {} and db["last_scan"] == 0.0
      and db["threshold"] == 100 and db["refresh_hours"] == 1)
with open(plugin.DB_PATH, "w") as f:
    f.write("{broken")
db = plugin._load_db()
check("5.2 битый JSON → дефолты", db["buyers"] == {} and db["threshold"] == 2)
reset_db()

print(f"\n{'=' * 60}\nПройдено: {len(PASS)}  Провалено: {len(FAIL)}")
if FAIL:
    print("Проваленные тесты:")
    for f_ in FAIL:
        print("  ✗", f_)
    sys.exit(1)
print("ВСЕ ТЕСТЫ ПРОЙДЕНЫ ✅")
