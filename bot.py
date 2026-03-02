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
# Вставь свой токен во вторые кавычки
BOT_TOKEN = os.getenv("BOT_TOKEN", "8373494520:AAEyyfltaGAxhnRPOkjebO5LL9GS5eG78go")
CREATOR_ID = 8134413995  # ЗАМЕНИ НА СВОЙ ID

# Путь к базе данных (согласно твоей инструкции про папку /app/data)
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

# === РАБОТА С БАЗОЙ ДАННЫХ ===
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

async def get_admins():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM admins") as cursor:
            return {row[0] for row in await cursor.fetchall()}

async def log_action(group_id: int, action_text: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO logs (group_id, action_text) VALUES (?, ?)", (group_id, action_text))
        await db.commit()

# === ПРОВЕРКА АКТУАЛЬНОСТИ ГРУПП ===
async def get_valid_groups():
    """Возвращает список групп, предварительно удаляя те, где бота больше нет."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT chat_id, name FROM groups") as cursor:
            groups = await cursor.fetchall()
    
    valid_groups = []
    async with aiosqlite.connect(DB_PATH) as db:
        for gid, name in groups:
            try:
                await bot.get_chat(gid)
                valid_groups.append((gid, name))
            except (TelegramForbiddenError, TelegramBadRequest):
                await db.execute("DELETE FROM groups WHERE chat_id = ?", (gid,))
                await db.commit()
    return valid_groups

# === КЛАВИАТУРЫ ===
def get_pagination_keyboard(items: list, page: int, per_page: int, callback_prefix: str) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    start = page * per_page
    end = start + per_page
    for item_id, item_name in items[start:end]:
        builder.button(text=str(item_name), callback_data=f"{callback_prefix}_select_{item_id}")
    builder.adjust(1)
    total_pages = max(1, (len(items) - 1) // per_page + 1)
    nav_row = []
    nav_row.append(InlineKeyboardButton(text="|<|" if page > 0 else " ", callback_data=f"{callback_prefix}_page_{page-1}" if page > 0 else "ignore"))
    nav_row.append(InlineKeyboardButton(text=f"|{page+1}/{total_pages}|", callback_data="ignore"))
    nav_row.append(InlineKeyboardButton(text="|>|" if page < total_pages - 1 else " ", callback_data=f"{callback_prefix}_page_{page+1}" if page < total_pages - 1 else "ignore"))
    builder.row(*nav_row)
    return builder

def get_cancel_skip_kb(allow_skip: bool = True):
    builder = InlineKeyboardBuilder()
    if allow_skip: builder.button(text="Пропустить ⏭", callback_data="create_skip")
    builder.button(text="Отменить ❌", callback_data="create_cancel")
    builder.adjust(1)
    return builder.as_markup()

async def check_user_is_admin(chat_id: int, user_id: int) -> bool:
    admins_set = await get_admins()
    if user_id in admins_set: return True
    if chat_id == 0: return False
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ['creator', 'administrator']
    except: return False

# === ОБРАБОТЧИКИ СОБЫТИЙ ===
@dp.my_chat_member()
async def on_bot_added_to_group(event: types.ChatMemberUpdated):
    async with aiosqlite.connect(DB_PATH) as db:
        if event.new_chat_member.status in ["member", "administrator"]:
            await db.execute("INSERT OR REPLACE INTO groups (chat_id, name) VALUES (?, ?)", (event.chat.id, event.chat.title))
        else:
            await db.execute("DELETE FROM groups WHERE chat_id = ?", (event.chat.id,))
        await db.commit()

@dp.message(F.forum_topic_created)
async def track_new_topic(message: types.Message):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO topics (chat_id, thread_id, name) VALUES (?, ?, ?)", 
                         (message.chat.id, message.message_thread_id, message.forum_topic_created.name))
        await db.commit()

# === КОМАНДЫ ===
@dp.message(Command("logs"), F.chat.type == "private")
async def logs_cmd(message: types.Message, state: FSMContext):
    if message.from_user.id != CREATOR_ID: return
    groups_list = await get_valid_groups()
    if not groups_list: return await message.answer("Бот пока не добавлен ни в одну группу.")
    kb = get_pagination_keyboard(groups_list, 0, 5, "logsg")
    await message.answer("📋 Список групп (Логи и управление):", reply_markup=kb.as_markup())
    await state.set_state(LogsCreator.choosing_group)

@dp.message(CommandStart(), F.chat.type == "private")
async def start_cmd(message: types.Message, state: FSMContext):
    all_groups = await get_valid_groups()
    admin_groups = []
    for gid, name in all_groups:
        if await check_user_is_admin(gid, message.from_user.id):
            admin_groups.append((gid, name))
    if not admin_groups: return await message.answer("Вы не админ ни в одной группе.")
    kb = get_pagination_keyboard(admin_groups, 0, 5, "group")
    await message.answer("Выберите группу:", reply_markup=kb.as_markup())
    await state.set_state(CreateEvent.choosing_group)

# === ЛОГИКА ВЫБОРА И ДЕЙСТВИЙ ===
@dp.callback_query(F.data.startswith("logsg_select_"), LogsCreator.choosing_group)
async def log_group_selected(callback: CallbackQuery, state: FSMContext):
    group_id = int(callback.data.split("_")[2])
    await state.update_data(target_group_id=group_id)
    builder = InlineKeyboardBuilder()
    builder.button(text="Посмотреть логи", callback_data="logs_view")
    builder.button(text="Отправить сообщение", callback_data="logs_send_msg")
    builder.adjust(1)
    await callback.message.edit_text(f"Управление группой ID: {group_id}", reply_markup=builder.as_markup())
    await state.set_state(LogsCreator.choosing_action)

@dp.callback_query(F.data == "logs_view", LogsCreator.choosing_action)
async def view_logs(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT action_text FROM logs WHERE group_id = ?", (data['target_group_id'],)) as c:
            logs = [row[0] for row in await c.fetchall()]
    text = "📜 Логи:\n\n" + "\n".join(logs) if logs else "Логи пусты."
    await callback.message.edit_text(text)
    await state.clear()

@dp.callback_query(F.data == "logs_send_msg", LogsCreator.choosing_action)
async def start_custom_msg_creator(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите текст сообщения:")
    await state.set_state(CustomMessage.waiting_for_text)

@dp.callback_query(F.data.startswith("group_select_"), CreateEvent.choosing_group)
async def admin_group_selected(callback: CallbackQuery, state: FSMContext):
    group_id = int(callback.data.split("_")[2])
    await state.update_data(selected_group=group_id)
    builder = InlineKeyboardBuilder()
    builder.button(text="📝 Создать мероприятие", callback_data="admin_create_event")
    builder.button(text="✉️ Отправить сообщение", callback_data="admin_send_msg")
    builder.adjust(1)
    await callback.message.edit_text("Выберите действие:", reply_markup=builder.as_markup())

@dp.callback_query(F.data == "admin_send_msg", CreateEvent.choosing_group)
async def start_admin_msg(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(target_group_id=data['selected_group'])
    await callback.message.edit_text("Введите сообщение для группы:")
    await state.set_state(CustomMessage.waiting_for_text)

# === ОТПРАВКА СООБЩЕНИЙ С ТОПИКАМИ ===
@dp.message(CustomMessage.waiting_for_text)
async def msg_text_received(message: types.Message, state: FSMContext):
    await state.update_data(msg_text=message.text)
    data = await state.get_data()
    gid = data['target_group_id']
    try:
        chat = await bot.get_chat(gid)
        if chat.is_forum:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT thread_id, name FROM topics WHERE chat_id = ?", (gid,)) as c:
                    topics = await c.fetchall()
            builder = InlineKeyboardBuilder()
            builder.button(text="Общий чат", callback_data="sc_0")
            for tid, tname in topics: builder.button(text=tname, callback_data=f"sc_{tid}")
            builder.adjust(1)
            await message.answer("Выберите топик:", reply_markup=builder.as_markup())
            await state.set_state(CustomMessage.choosing_topic)
        else:
            await bot.send_message(gid, message.text)
            await message.answer("✅ Отправлено!")
            await state.clear()
    except Exception as e:
        await message.answer(f"Ошибка: {e}")
        await state.clear()

@dp.callback_query(F.data.startswith("sc_"), CustomMessage.choosing_topic)
async def topic_selected(callback: CallbackQuery, state: FSMContext):
    tid = int(callback.data.split("_")[1])
    data = await state.get_data()
    await bot.send_message(data['target_group_id'], data['msg_text'], message_thread_id=tid if tid != 0 else None)
    await callback.message.edit_text("✅ Отправлено в топик!")
    await state.clear()

# === СОЗДАНИЕ МЕРОПРИЯТИЯ (УПРОЩЕННО) ===
@dp.callback_query(F.data == "admin_create_event", CreateEvent.choosing_group)
async def start_creation(callback: CallbackQuery, state: FSMContext):
    await state.set_state(CreateEvent.waiting_for_name)
    await callback.message.edit_text("Название мероприятия:", reply_markup=get_cancel_skip_kb(False))

@dp.message(CreateEvent.waiting_for_name)
async def event_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await log_action((await state.get_data())['selected_group'], f"📅 Создано: {message.text}")
    await message.answer("Мероприятие сохранено в логах! (Для полной формы добавь остальные шаги по аналогии)")
    await state.clear()

@dp.callback_query(F.data == "create_cancel", StateFilter(CreateEvent))
async def cancel_ev(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Отменено.")

# === ЗАПУСК ===
async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass