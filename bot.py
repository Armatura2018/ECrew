import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
import aiosqlite

# === НАСТРОЙКИ ===
BOT_TOKEN = os.getenv("BOT_TOKEN", "8373494520:AAEyyfltaGAxhnRPOkjebO5LL9GS5eG78go") # Вставь свой токен сюда
CREATOR_ID = 8134413995 # Вставь свой ID сюда
DB_PATH = os.getenv("DATABASE_PATH", "data/bot.db")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

# === СОСТОЯНИЯ (FSM) ===
class CreateEvent(StatesGroup):
    choosing_group = State()
    waiting_for_name = State()
    waiting_for_date = State()
    waiting_for_time = State()
    waiting_for_location = State()
    waiting_for_description = State()
    confirming = State()

class LogsCreator(StatesGroup):
    choosing_group = State()
    choosing_action = State()

class CustomMessage(StatesGroup):
    waiting_for_text = State()
    choosing_topic = State()

# === БАЗА ДАННЫХ ===
async def init_db():
    db_file = Path(DB_PATH)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)")
        await db.execute("CREATE TABLE IF NOT EXISTS groups (chat_id INTEGER PRIMARY KEY, name TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS topics (chat_id INTEGER, thread_id INTEGER, name TEXT, PRIMARY KEY(chat_id, thread_id))")
        await db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER, name TEXT, date TEXT, time TEXT, location TEXT, description TEXT, host TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS active_posts (message_id INTEGER PRIMARY KEY, event_id INTEGER, group_id INTEGER)")
        await db.execute("CREATE TABLE IF NOT EXISTS attendees (message_id INTEGER, user_id INTEGER, mention TEXT, PRIMARY KEY(message_id, user_id))")
        await db.execute("CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER, action_text TEXT)")
        await db.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (CREATOR_ID,))
        await db.commit()

async def log_action(group_id: int, action_text: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO logs (group_id, action_text) VALUES (?, ?)", (group_id, action_text))
        await db.commit()

async def get_valid_groups():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT chat_id, name FROM groups") as cursor:
            groups = await cursor.fetchall()
    valid_groups = []
    async with aiosqlite.connect(DB_PATH) as db:
        for gid, name in groups:
            try:
                await bot.get_chat(gid)
                valid_groups.append((gid, name))
            except:
                await db.execute("DELETE FROM groups WHERE chat_id = ?", (gid,))
                await db.commit()
    return valid_groups

async def is_admin(user_id: int, chat_id: int = 0) -> bool:
    if user_id == CREATOR_ID: return True
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,)) as c:
            if await c.fetchone(): return True
    if chat_id != 0:
        try:
            member = await bot.get_chat_member(chat_id, user_id)
            return member.status in ['creator', 'administrator']
        except: pass
    return False

# === КЛАВИАТУРЫ ===
def get_pagination_kb(items: list, page: int, per_page: int, prefix: str):
    b = InlineKeyboardBuilder()
    start = page * per_page
    for i_id, i_name in items[start:start+per_page]:
        b.button(text=str(i_name), callback_data=f"{prefix}_select_{i_id}")
    b.adjust(1)
    total = max(1, (len(items) - 1) // per_page + 1)
    nav = []
    nav.append(InlineKeyboardButton(text="|<|" if page > 0 else " ", callback_data=f"{prefix}_page_{page-1}" if page > 0 else "ignore"))
    nav.append(InlineKeyboardButton(text=f"|{page+1}/{total}|", callback_data="ignore"))
    nav.append(InlineKeyboardButton(text="|>|" if page < total - 1 else " ", callback_data=f"{prefix}_page_{page+1}" if page < total - 1 else "ignore"))
    b.row(*nav)
    return b.as_markup()

def get_cancel_skip_kb(allow_skip: bool = True):
    b = InlineKeyboardBuilder()
    if allow_skip: b.button(text="Пропустить ⏭", callback_data="create_skip")
    b.button(text="Отменить ❌", callback_data="create_cancel")
    b.adjust(1)
    return b.as_markup()

# === ОТСЛЕЖИВАНИЕ ГРУПП ===
@dp.my_chat_member()
async def on_bot_added(event: types.ChatMemberUpdated):
    async with aiosqlite.connect(DB_PATH) as db:
        if event.new_chat_member.status in ["member", "administrator"]:
            await db.execute("INSERT OR REPLACE INTO groups (chat_id, name) VALUES (?, ?)", (event.chat.id, event.chat.title))
        else:
            await db.execute("DELETE FROM groups WHERE chat_id = ?", (event.chat.id,))
        await db.commit()

@dp.message(F.forum_topic_created)
async def track_topic(message: types.Message):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO topics (chat_id, thread_id, name) VALUES (?, ?, ?)", 
                         (message.chat.id, message.message_thread_id, message.forum_topic_created.name))
        await db.commit()

# === УПРАВЛЕНИЕ АДМИНАМИ (ТОЛЬКО СОЗДАТЕЛЬ) ===
@dp.message(Command("add_admin"), F.chat.type == "private")
async def add_admin_cmd(message: types.Message):
    if message.from_user.id != CREATOR_ID: return # Тихо игнорируем всех, кроме создателя
    
    args = message.text.split()
    if len(args) != 2 or not args[1].isdigit():
        return await message.answer("Использование: /add_admin <ID_пользователя>")
        
    new_admin_id = int(args[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (new_admin_id,))
        await db.commit()
        
    await message.answer(f"✅ Пользователь {new_admin_id} назначен администратором бота.")

# === ПАНЕЛЬ ЛОГОВ (ТОЛЬКО СОЗДАТЕЛЬ) ===
@dp.message(Command("logs"), F.chat.type == "private")
async def logs_cmd(message: types.Message, state: FSMContext):
    if message.from_user.id != CREATOR_ID: return
    groups = await get_valid_groups()
    if not groups: return await message.answer("Нет доступных групп.")
    await message.answer("📋 Группы (Логи):", reply_markup=get_pagination_kb(groups, 0, 5, "logsg"))
    await state.set_state(LogsCreator.choosing_group)

@dp.callback_query(F.data.startswith("logsg_select_"), LogsCreator.choosing_group)
async def log_group_sel(call: CallbackQuery, state: FSMContext):
    gid = int(call.data.split("_")[2])
    await state.update_data(target_group_id=gid)
    b = InlineKeyboardBuilder()
    b.button(text="Посмотреть логи", callback_data="logs_view")
    b.button(text="Отправить сообщение", callback_data="logs_send_msg")
    b.adjust(1)
    await call.message.edit_text(f"Управление группой ID: {gid}", reply_markup=b.as_markup())
    await state.set_state(LogsCreator.choosing_action)

@dp.callback_query(F.data == "logs_view", LogsCreator.choosing_action)
async def view_logs(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT action_text FROM logs WHERE group_id = ?", (data['target_group_id'],)) as c:
            logs = [row[0] for row in await c.fetchall()]
    await call.message.edit_text("📜 Логи:\n\n" + "\n".join(logs) if logs else "Логи пусты.")
    await state.clear()

@dp.callback_query(F.data == "logs_send_msg", LogsCreator.choosing_action)
async def logs_msg(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("Введите текст:")
    await state.set_state(CustomMessage.waiting_for_text)

# === ПАНЕЛЬ АДМИНОВ (/start, /create) ===
@dp.message(CommandStart(), F.chat.type == "private")
async def start_cmd(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id): return # Тихо игнорируем
    groups = await get_valid_groups()
    admin_groups = [g for g in groups if await is_admin(message.from_user.id, g[0])]
    if not admin_groups: return
    await message.answer("Выберите группу:", reply_markup=get_pagination_kb(admin_groups, 0, 5, "group"))
    await state.set_state(CreateEvent.choosing_group)

@dp.callback_query(F.data.startswith("group_select_"), CreateEvent.choosing_group)
async def admin_grp_sel(call: CallbackQuery, state: FSMContext):
    gid = int(call.data.split("_")[2])
    await state.update_data(selected_group=gid)
    b = InlineKeyboardBuilder()
    b.button(text="📝 Создать мероприятие", callback_data="admin_create_event")
    b.button(text="✉️ Отправить сообщение", callback_data="admin_send_msg")
    b.adjust(1)
    await call.message.edit_text("Выберите действие:", reply_markup=b.as_markup())

@dp.callback_query(F.data == "admin_send_msg", CreateEvent.choosing_group)
async def admin_msg(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(target_group_id=data['selected_group'])
    await call.message.edit_text("Введите сообщение:")
    await state.set_state(CustomMessage.waiting_for_text)

# === ОТПРАВКА СООБЩЕНИЙ В ГРУППЫ С ТОПИКАМИ ===
@dp.message(CustomMessage.waiting_for_text)
async def msg_received(message: types.Message, state: FSMContext):
    await state.update_data(msg_text=message.text)
    gid = (await state.get_data())['target_group_id']
    try:
        chat = await bot.get_chat(gid)
        if chat.is_forum:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT thread_id, name FROM topics WHERE chat_id = ?", (gid,)) as c:
                    topics = await c.fetchall()
            b = InlineKeyboardBuilder()
            b.button(text="Общий чат", callback_data="sc_0")
            for tid, tname in topics: b.button(text=tname, callback_data=f"sc_{tid}")
            b.adjust(1)
            await message.answer("Выберите топик:", reply_markup=b.as_markup())
            await state.set_state(CustomMessage.choosing_topic)
        else:
            await bot.send_message(gid, message.text)
            await message.answer("✅ Отправлено!")
            await state.clear()
    except: await state.clear()

@dp.callback_query(F.data.startswith("sc_"), CustomMessage.choosing_topic)
async def topic_sel(call: CallbackQuery, state: FSMContext):
    tid = int(call.data.split("_")[1])
    data = await state.get_data()
    await bot.send_message(data['target_group_id'], data['msg_text'], message_thread_id=tid if tid != 0 else None)
    await call.message.edit_text("✅ Отправлено!")
    await state.clear()

# === СОЗДАНИЕ МЕРОПРИЯТИЯ ПОШАГОВО ===
@dp.callback_query(F.data == "admin_create_event", CreateEvent.choosing_group)
@dp.message(Command("create"), F.chat.type == "private")
async def start_creation(update: types.Message | CallbackQuery, state: FSMContext):
    user_id = update.from_user.id
    if not await is_admin(user_id): return
    
    current_state = await state.get_state()
    # Если вызвали через /create, но группу еще не выбрали
    if isinstance(update, types.Message) and current_state != CreateEvent.choosing_group.state:
        groups = await get_valid_groups()
        admin_groups = [g for g in groups if await is_admin(user_id, g[0])]
        if not admin_groups: return
        await update.answer("Сначала выберите группу:", reply_markup=get_pagination_kb(admin_groups, 0, 5, "group"))
        await state.set_state(CreateEvent.choosing_group)
        return

    await state.set_state(CreateEvent.waiting_for_name)
    text = "Введите название мероприятия:"
    if isinstance(update, CallbackQuery):
        await update.message.edit_text(text, reply_markup=get_cancel_skip_kb(False))
    else:
        await update.answer(text, reply_markup=get_cancel_skip_kb(False))

@dp.callback_query(F.data == "create_cancel", StateFilter(CreateEvent))
async def cancel_ev(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Отменено. Введите /start")

@dp.callback_query(F.data == "create_skip", StateFilter(CreateEvent))
async def skip_step(call: CallbackQuery, state: FSMContext):
    # Проверяем права именно того, кто нажал на кнопку
    if not await is_admin(call.from_user.id): 
        return await call.answer()
        
    await call.answer() # Убирает "часики" (анимацию загрузки) с кнопки
    # Передаем управление дальше, явно указывая пользователя
    await process_step(call.message, state, is_skip=True, user=call.from_user)


@dp.message(StateFilter(CreateEvent.waiting_for_name, CreateEvent.waiting_for_date, 
                        CreateEvent.waiting_for_time, CreateEvent.waiting_for_location, 
                        CreateEvent.waiting_for_description))
async def process_step(message: types.Message, state: FSMContext, is_skip=False, user: types.User = None):
    # Определяем, кого проверять: если нажата кнопка — берем user, иначе — автора сообщения
    user_to_check = user if is_skip else message.from_user
    
    if not await is_admin(user_to_check.id): 
        return
        
    st = await state.get_state()
    val = " " if is_skip else message.text

    if st == CreateEvent.waiting_for_name.state:
        await state.update_data(name=val)
        await state.set_state(CreateEvent.waiting_for_date)
        await message.answer("Введите дату:", reply_markup=get_cancel_skip_kb())
        
    elif st == CreateEvent.waiting_for_date.state:
        await state.update_data(date=val)
        await state.set_state(CreateEvent.waiting_for_time)
        await message.answer("Введите время:", reply_markup=get_cancel_skip_kb())
        
    elif st == CreateEvent.waiting_for_time.state:
        await state.update_data(time=val)
        await state.set_state(CreateEvent.waiting_for_location)
        await message.answer("Место проведения:", reply_markup=get_cancel_skip_kb())
        
    elif st == CreateEvent.waiting_for_location.state:
        await state.update_data(location=val)
        await state.set_state(CreateEvent.waiting_for_description)
        await message.answer("Описание:", reply_markup=get_cancel_skip_kb())
        
    elif st == CreateEvent.waiting_for_description.state:
        await state.update_data(description=val)
        data = await state.get_data()
        
        host = f"@{user_to_check.username}" if user_to_check.username else user_to_check.first_name
        await state.update_data(host=host)
        
        # Используем тройные кавычки для железобетонных отступов
        preview = f"""**{data['name']}**

Хост: {host}

Дата: {data.get('date', ' ')}

Время: {data.get('time', ' ')}

Место: {data.get('location', ' ')}

Описание: {val}"""
                   
        b = InlineKeyboardBuilder()
        b.button(text="Подтвердить ✅", callback_data="confirm_event")
        b.button(text="Удалить ❌", callback_data="create_cancel")
        
        await message.answer(f"Предпросмотр:\n\n{preview}", reply_markup=b.as_markup(), parse_mode="Markdown")
        await state.set_state(CreateEvent.confirming)

@dp.callback_query(F.data == "confirm_event", CreateEvent.confirming)
async def confirm_ev(call: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO events (group_id, name, date, time, location, description, host) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (d['selected_group'], d['name'], d.get('date',' '), d.get('time',' '), d.get('location',' '), d.get('description',' '), d['host']))
        await db.commit()
    await log_action(d['selected_group'], f"✅ Создано мероприятие: {d['name']}") # Тихо в лог
    await call.message.edit_text("✅ Мероприятие успешно создано! Вызовите /events в группе, чтобы опубликовать его.")
    await state.clear()

# === ПУБЛИКАЦИЯ В ГРУППЕ (/events) ===
@dp.message(Command("events"), F.chat.type.in_(["group", "supergroup"]))
async def grp_events(message: types.Message):
    await message.delete()
    if not await is_admin(message.from_user.id, message.chat.id): return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, name FROM events WHERE group_id = ?", (message.chat.id,)) as c:
            evs = await c.fetchall()
    if not evs: return
    await message.answer("Выберите мероприятие для публикации:", reply_markup=get_pagination_kb(evs, 0, 5, "post_event"))

@dp.callback_query(F.data.startswith("post_event_select_"))
async def choose_ev_topic(call: CallbackQuery):
    if not await is_admin(call.from_user.id, call.message.chat.id): return await call.answer("Нет прав", show_alert=True)
    eid = int(call.data.split("_")[3])
    if call.message.chat.is_forum:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT thread_id, name FROM topics WHERE chat_id = ?", (call.message.chat.id,)) as c:
                topics = await c.fetchall()
        b = InlineKeyboardBuilder()
        b.button(text="Общая", callback_data=f"sev_{eid}_0")
        for tid, tname in topics: b.button(text=tname, callback_data=f"sev_{eid}_{tid}")
        b.adjust(1)
        await call.message.edit_text("Выберите топик:", reply_markup=b.as_markup())
    else:
        await send_ev(call, eid, None)

@dp.callback_query(F.data.startswith("sev_"))
async def proc_sev(call: CallbackQuery):
    if not await is_admin(call.from_user.id, call.message.chat.id): return await call.answer("Нет прав", show_alert=True)
    pts = call.data.split("_")
    await send_ev(call, int(pts[1]), int(pts[2]) if pts[2] != "0" else None)

async def send_ev(call: CallbackQuery, eid: int, tid: Optional[int]):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT name, date, time, location, description, host FROM events WHERE id = ?", (eid,)) as c:
            row = await c.fetchone()
            
    if not row: 
        return
        
    text = f"""**{row[0]}**

Хост: {row[5]}

Дата: {row[1]}

Время: {row[2]}

Место: {row[3]}

Описание: {row[4]}

Нажмите ✅ чтобы записаться."""
            
    b = InlineKeyboardBuilder().button(text="✅", callback_data=f"att_{eid}")
    await call.message.delete()
    
    msg = await bot.send_message(call.message.chat.id, text, reply_markup=b.as_markup(), parse_mode="Markdown", message_thread_id=tid)
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO active_posts (message_id, event_id, group_id) VALUES (?, ?, ?)", (msg.message_id, eid, call.message.chat.id))
        await db.commit()

@dp.callback_query(F.data.startswith("att_"))
async def attend(call: CallbackQuery):
    msg_id = call.message.message_id
    uid = call.from_user.id
    mention = f"@{call.from_user.username}" if call.from_user.username else call.from_user.first_name
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM active_posts WHERE message_id = ?", (msg_id,)) as c:
            if not await c.fetchone(): return await call.answer("Пост не активен.", show_alert=True)
        try:
            await db.execute("INSERT INTO attendees (message_id, user_id, mention) VALUES (?, ?, ?)", (msg_id, uid, mention))
            await db.commit()
            await call.answer("Вы успешно записались!")
        except:
            await call.answer("Вы уже записаны!", show_alert=True)

# === ЗАВЕРШЕНИЕ (/finish) ===
@dp.message(Command("finish"), F.chat.type.in_(["group", "supergroup"]))
async def fin_cmd(message: types.Message):
    await message.delete()
    if not await is_admin(message.from_user.id, message.chat.id): return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT a.message_id, e.name FROM active_posts a JOIN events e ON a.event_id = e.id WHERE a.group_id = ?", (message.chat.id,)) as c:
            posts = await c.fetchall()
    if not posts: return
    await message.answer("Завершить набор:", reply_markup=get_pagination_kb(posts, 0, 5, "fin"))

@dp.callback_query(F.data.startswith("fin_select_"))
async def fin_sel(call: CallbackQuery):
    if not await is_admin(call.from_user.id, call.message.chat.id): return await call.answer("Нет прав", show_alert=True)
    msg_id = int(call.data.split("_")[2])
    if call.message.chat.is_forum:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT thread_id, name FROM topics WHERE chat_id = ?", (call.message.chat.id,)) as c:
                topics = await c.fetchall()
        b = InlineKeyboardBuilder()
        b.button(text="Общая", callback_data=f"sfin_{msg_id}_0")
        for tid, tname in topics: b.button(text=tname, callback_data=f"sfin_{msg_id}_{tid}")
        b.adjust(1)
        await call.message.edit_text("Куда отправить итоги?", reply_markup=b.as_markup())
    else:
        await send_fin(call, msg_id, None)

@dp.callback_query(F.data.startswith("sfin_"))
async def proc_sfin(call: CallbackQuery):
    if not await is_admin(call.from_user.id, call.message.chat.id): return await call.answer("Нет прав", show_alert=True)
    pts = call.data.split("_")
    await send_fin(call, int(pts[1]), int(pts[2]) if pts[2] != "0" else None)

async def send_fin(call: CallbackQuery, msg_id: int, tid: Optional[int]):
    gid = call.message.chat.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT e.name FROM active_posts a JOIN events e ON a.event_id = e.id WHERE a.message_id = ?", (msg_id,)) as c:
            row = await c.fetchone()
            if not row: return await call.answer("Пост не найден.")
            ev_name = row[0]
        async with db.execute("SELECT mention FROM attendees WHERE message_id = ?", (msg_id,)) as c:
            att = [r[0] for r in await c.fetchall()]
        await db.execute("DELETE FROM active_posts WHERE message_id = ?", (msg_id,))
        await db.commit()
        
    await call.message.delete()
    try: await bot.delete_message(gid, msg_id)
    except: pass
    
    txt = f"Сбор на **{ev_name}** завершен!\n\n**Участники:**\n" + ("\n".join(att) if att else "Никто не записался 😢")
    await bot.send_message(gid, txt, parse_mode="Markdown", message_thread_id=tid)
    await log_action(gid, f"🏁 Завершен сбор: {ev_name} (Участников: {len(att)})") # Тихо в лог
    await call.answer()

@dp.callback_query(F.data == "ignore")
async def ignore_cb(call: CallbackQuery): await call.answer()

# === ЗАПУСК ===
async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":

    asyncio.run(main())

if __name__ == "__main__":

    asyncio.run(main())
